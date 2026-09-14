"""MCP Server：stdio、只读、fail-closed。

- 传输仅 stdio（mcp.server.stdio）；不启动 TCP 监听、不注册 HTTP/SSE/Streamable
  HTTP 路由；本模块与 deadlatch 业务代码均不发起网络请求（MCP SDK 的
  HTTP 栈仅为传递依赖，业务源码不 import）。
- 五个工具：check_order / get_account_status / get_policy /
  kill_switch_status / recent_decisions。tools/list 只暴露这五个；
  无 resources/prompts/roots 文件读取面。
- policy / portfolio / audit / kill-switch path 均为服务器进程启动配置
  （--policy / --portfolio / --audit-path / --kill-switch-path），不能成为任何
  工具入参；policy 变更会在下一次工具调用时自动重载，独立 kill-switch 文件
  每次调用无条件读取；MCP 无任何写入或解除开关的工具。
- stdout 只承载 MCP 协议帧；诊断只写 stderr（禁止 print/traceback 污染 stdout）。
- 统一错误出口：工具错误一律 isError=true + fail_closed，input 错误带
  input_error=true + exit_code=4，内部异常 exit_code=5，portfolio/审计
  不可用 exit_code=3；error content 不含 traceback、真实绝对路径、Token、
  Cookie/API key 或输入全文。
- check_order 调用既有 Guard 库 API（不复制规则/投影/退出码/审计逻辑），
  每次调用从服务器配置路径读取/校验当前快照；exit 3 是正常风控结果，
  exit 4/5 转工具错误（该次 Guard 审计记录保留）。

入口：
    deadlatch-mcp --policy policy.yaml --portfolio portfolio.json \
        [--audit-path audit.jsonl] [--kill-switch-path kill-switch]
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsRequest,
    ListToolsResult,
    TextContent,
    Tool,
)

from ._resources import schema_dict
from ._timeutil import parse_rfc3339, to_epoch_seconds
from ._validation import InputValidationError, _validator
from .audit import AuditError, read_audit_records
from .engine import GuardEngine
from .exposure import business_number, position_exposure
from .guard import MAX_PORTFOLIO_FILE_BYTES, Guard, check_file_size, load_policy_file, resolve_audit_path
from .model import Order, Policy, Portfolio
from .rules.registry import RULE_IDS, is_rule_enabled, standard_rule_registry

_ORDER_SCHEMA = schema_dict("order")  # package Schema（wheel 安装后可用）
_KILL_SWITCH_MODES = ("off", "reduce_only", "full")
_KILL_SWITCH_RANK = {mode: rank for rank, mode in enumerate(_KILL_SWITCH_MODES)}
_MAX_KILL_SWITCH_FILE_BYTES = 64


class MCPServerError(Exception):
    """MCP 层统一错误（message 已脱敏：不含路径/Token/traceback）。"""

    def __init__(self, message: str, exit_code: int, input_error: bool = False):
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.input_error = input_error


# ---------------------------------------------------------------- 入参 Schema

def _order_input_schema() -> dict:
    """check_order 入参：严格等价 order.schema.json（additionalProperties=false）。"""
    order = {k: v for k, v in _ORDER_SCHEMA.items() if k not in ("$schema", "$id", "title", "description")}
    return {
        "type": "object",
        "properties": {
            "order": {
                **order,
                "description": (
                    "The order to evaluate. Single-leg only in v0.1. "
                    "Portfolio snapshot is NEVER accepted here — it is read from the "
                    "local configured path only (security model constraint)."
                ),
            }
        },
        "required": ["order"],
        "additionalProperties": False,
    }


_NO_ARG_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}

_RECENT_SCHEMA = {
    "type": "object",
    "properties": {
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20,
                  "description": "Optional; defaults to 20 when omitted."},
        "since": {"type": "string", "format": "date-time",
                  "description": "RFC3339 with explicit timezone. Return only records evaluated at or after this time (optional)."},
    },
    "additionalProperties": False,
}

_CHECK_ORDER_DESCRIPTION = """\
Check a single order against the full pre-trade risk policy and return a structured decision.

THIS IS A MANDATORY CONSTRAINT, NOT ADVICE:
- A decision of "BLOCK" means the order MUST NOT be submitted. Do not submit the order, do not retry with the same parameters, do not bypass or ignore this result.
- A decision of "WARN" means the order may proceed only if your execution policy explicitly allows warnings; otherwise treat it as a hard stop.
- A decision of "PASS" authorizes only this exact order as given; any change to the order requires a new check.
- If the tool call itself fails or returns no decision, treat it as BLOCK (fail-closed). Never assume PASS on an error.
- This tool never places an order. It only evaluates.

Honest boundary: this guard cannot force an agent that never calls it to call it, and it cannot stop an agent that ignores a BLOCK from submitting elsewhere. Whether you call this tool and honor its result is your decision."""

_GET_ACCOUNT_DESCRIPTION = """\
Return the current account snapshot summary: snapshot freshness, equity, cash, daily PnL, drawdown, and gross exposure utilization. Read-only; never modifies anything. Use this before deciding whether the account is in a safe state to trade. If the snapshot is stale (older than the configured freshness limit), the response marks stale: true and any order check would BLOCK."""

_GET_POLICY_DESCRIPTION = """\
Return the currently effective risk policy (mode, base_currency, kill_switch state, acknowledged_disabled, and all enabled limits) as read-only data. The acknowledged_disabled list and the set of enabled rules are part of the returned policy so callers can see exactly which rules are inactive. This is informational only: there is no tool to change the policy or to disarm the kill switch. Local policy changes are validated and reloaded automatically on the next tool call; an invalid reload fails closed."""

_KILL_SWITCH_DESCRIPTION = """\
Return the global kill switch state (mode: off/full/reduce_only) plus engagement time when not off. READ-ONLY: this tool cannot change the kill switch, and no other tool can either. In full mode, every check_order call returns BLOCK for every order, including closing orders. In reduce_only mode, only orders inferred as closing (evidence.order_direction = close) are allowed; opening orders are BLOCKed. Verify this before each trading session."""

_RECENT_DESCRIPTION = """\
Return the N most recent decision records from the local audit log (oldest-first within the returned window). Read-only. Each record includes decision, exit_code, rule hits, and input hash for traceability. Use this to review what the guard has been doing; it cannot alter any record."""


# ---------------------------------------------------------------- Server

class MCPGuardServer:
    """stdio MCP Server。policy 变更自动重载；portfolio/switch 每次调用读取。"""

    def __init__(self, policy_path: str, portfolio_path: str, audit_path: str | None = None,
                 kill_switch_path: str | None = None):
        self._policy_path = Path(policy_path)
        self._portfolio_path = Path(portfolio_path)
        self._audit_path = resolve_audit_path(audit_path)
        self._kill_switch_path = Path(kill_switch_path) if kill_switch_path else None
        self._policy_signature: tuple[int, int, int, int] | None = None
        self._base_policy: Policy | None = None
        self._effective_kill_switch: str | None = None
        # 启动配置 fail-fast：policy 加载/校验失败 → 抛 InputValidationError（main → stderr + exit 4）
        self._reload_effective_policy(force=True)
        self._server = Server("deadlatch")
        self._register()

    def _current_policy_signature(self) -> tuple[int, int, int, int]:
        try:
            stat = self._policy_path.stat()
        except OSError as exc:
            raise InputValidationError(["policy 文件不存在或不可读取"]) from exc
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def _load_policy_stable(self) -> tuple[Policy, tuple[int, int, int, int]]:
        """Load one stable policy snapshot; concurrent rewrites fail closed."""
        for _ in range(2):
            before = self._current_policy_signature()
            policy = load_policy_file(str(self._policy_path))
            after = self._current_policy_signature()
            if before == after:
                return policy, after
        raise InputValidationError(["policy 在读取期间持续变化，拒绝载入"])

    def _read_kill_switch_mode(self) -> str | None:
        """Read the optional local switch every call; never cache its contents."""
        if self._kill_switch_path is None:
            return None
        try:
            stat = self._kill_switch_path.stat()
            if stat.st_size > _MAX_KILL_SWITCH_FILE_BYTES:
                raise InputValidationError(["kill switch 文件超过大小上限"])
            mode = self._kill_switch_path.read_text(encoding="utf-8").strip()
        except InputValidationError:
            raise
        except OSError as exc:
            raise InputValidationError(["kill switch 文件不存在或不可读取"]) from exc
        if mode not in _KILL_SWITCH_MODES:
            raise InputValidationError(["kill switch 文件必须仅包含 off/full/reduce_only"])
        return mode

    @staticmethod
    def _effective_policy(base: Policy, switch_mode: str | None) -> Policy:
        data = base.to_dict()
        if switch_mode is not None:
            # 独立文件只能收紧 policy，不能意外解除 policy 自身已开启的紧急状态。
            data["kill_switch"] = max(
                (base.kill_switch, switch_mode), key=lambda mode: _KILL_SWITCH_RANK[mode]
            )
        return Policy.from_dict(data)

    def _reload_effective_policy(self, *, force: bool = False) -> None:
        """Atomically refresh policy/guard; any invalid live state blocks the call."""
        signature = self._current_policy_signature()
        base_changed = force or self._base_policy is None or signature != self._policy_signature
        base = self._base_policy
        if base_changed:
            base, signature = self._load_policy_stable()
        assert base is not None
        switch_mode = self._read_kill_switch_mode()  # unconditional when configured
        effective = self._effective_policy(base, switch_mode)
        if base_changed or force or effective.kill_switch != self._effective_kill_switch:
            guard = Guard(
                GuardEngine(effective, rules=standard_rule_registry()),
                audit_path=str(self._audit_path),
            )
            # Commit the new objects only after every read and validation succeeded.
            self._base_policy = base
            self._policy_signature = signature
            self._policy = effective
            self._guard = guard
            self._effective_kill_switch = effective.kill_switch

    def _refresh_policy_for_call(self) -> None:
        try:
            self._reload_effective_policy()
        except InputValidationError as exc:
            raise MCPServerError(
                "policy 或 kill switch 状态无效（input_error，fail-closed）",
                4,
                input_error=True,
            ) from exc

    # ---- 工具注册 ----

    def _register(self) -> None:
        # mcp SDK 2.x：低层 Server 用 add_request_handler 注册方法
        # （params_type 用 *Params 模型——runner 以 JSON-RPC params dict 直接 model_validate）
        self._server.add_request_handler("tools/list", ListToolsRequest, self._list_tools)
        self._server.add_request_handler("tools/call", CallToolRequestParams, self._call_tool)

    async def _list_tools(self, ctx, params: ListToolsRequest) -> ListToolsResult:
        return ListToolsResult(tools=[
            Tool(name="check_order", description=_CHECK_ORDER_DESCRIPTION,
                 inputSchema=_order_input_schema()),
            Tool(name="get_account_status", description=_GET_ACCOUNT_DESCRIPTION,
                 inputSchema=_NO_ARG_SCHEMA),
            Tool(name="get_policy", description=_GET_POLICY_DESCRIPTION,
                 inputSchema=_NO_ARG_SCHEMA),
            Tool(name="kill_switch_status", description=_KILL_SWITCH_DESCRIPTION,
                 inputSchema=_NO_ARG_SCHEMA),
            Tool(name="recent_decisions", description=_RECENT_DESCRIPTION,
                 inputSchema=_RECENT_SCHEMA),
        ])

    async def _call_tool(self, ctx, params: CallToolRequestParams) -> CallToolResult:
        name = params.name
        arguments = params.arguments or {}
        try:
            self._refresh_policy_for_call()
            if name == "check_order":
                return await self._check_order(arguments)
            if name == "get_account_status":
                return await self._get_account_status(arguments)
            if name == "get_policy":
                return await self._get_policy(arguments)
            if name == "kill_switch_status":
                return await self._kill_switch_status(arguments)
            if name == "recent_decisions":
                return await self._recent_decisions(arguments)
            # 未知工具名不回显（恶意工具名不得成为泄漏通道）
            raise MCPServerError("未知工具（fail-closed）", 5)
        except MCPServerError as exc:
            return self._error_result(exc.message, exc.exit_code, exc.input_error)
        except Exception as exc:  # MCP 层内部异常 → fail-closed（禁止空结果 / PASS）
            return self._error_result(
                f"MCP 内部异常（{type(exc).__name__}），fail-closed 按 BLOCK 处理", 5
            )

    # ---- 统一错误出口 ----

    @staticmethod
    def _error_result(message: str, exit_code: int, input_error: bool = False) -> CallToolResult:
        payload = {"error": message, "fail_closed": True, "exit_code": exit_code}
        if input_error:
            payload["input_error"] = True
        return CallToolResult(
            isError=True, content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]
        )

    @staticmethod
    def _ok(data: dict) -> CallToolResult:
        return CallToolResult(
            isError=False, content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=2))]
        )

    # 错误消息安全化——回显只允许"通用类别 + 脱敏字段路径"，绝不回显实际值/未知字段内容
    _ERROR_CATEGORY = {
        "required": "缺少必填字段",
        "type": "类型非法",
        "enum": "枚举值非法",
        "const": "值非法",
        "pattern": "格式非法",
        "minimum": "数值低于下限",
        "maximum": "数值高于上限",
        "exclusiveMinimum": "数值不满足下限",
        "exclusiveMaximum": "数值不满足上限",
        "minLength": "长度不足",
        "maxLength": "长度超限",
        "additionalProperties": "未知字段",
        "items": "数组元素非法",
        "format": "格式非法",
    }

    @staticmethod
    def _safe_path(path: str) -> str:
        """字段路径脱敏：恶意属性名（Token/路径/Cookie 形态）不得成为泄漏通道。"""
        from .audit import sanitize_text

        return sanitize_text(path, [])

    def _validate_args(self, arguments: dict, schema: dict) -> None:
        """工具入参校验：Schema 失败 → input_error（exit 4），同时挡住注入字段。

        错误消息只含"安全顶层字段名 + 通用类别"，不回显
        ValidationError message（其中可能含调用方注入的 Token/Cookie/路径等
        实际值）；未知顶层字段名一律 <redacted>（恶意属性名不得成为泄漏通道）。
        """
        errors = sorted(Draft202012Validator(schema).iter_errors(arguments), key=lambda e: list(e.path))
        if errors:
            first = errors[0]
            parts = [str(p) for p in first.path]
            top = parts[0] if parts else "$"
            allowed = set(schema.get("properties", {}).keys())
            path_display = top if top in allowed else "<redacted>"
            category = self._ERROR_CATEGORY.get(first.validator, "参数校验失败")
            raise MCPServerError(
                f"工具参数非法: 字段 {path_display}，{category}（input_error，fail-closed）",
                4, input_error=True,
            )

    # ---- portfolio 快照（服务器配置路径，每次调用读取/校验）----

    def _load_portfolio(self) -> Portfolio:
        """读取并完整校验 portfolio 快照（任何 Schema/必填/类型/约束
        非法 → fail-closed；不得用 policy 默认值替代缺失快照字段）。"""
        path = self._portfolio_path
        try:
            check_file_size(path, MAX_PORTFOLIO_FILE_BYTES, "portfolio")  # size limit：超限拒绝
            data = json.loads(path.read_text(encoding="utf-8"))
        except InputValidationError as exc:
            raise MCPServerError(f"portfolio 快照非法（{str(exc)}，fail-closed）", 3) from exc
        except FileNotFoundError:
            raise MCPServerError("portfolio 快照文件不存在（fail-closed）", 3)
        except (OSError, json.JSONDecodeError) as exc:
            raise MCPServerError(f"portfolio 快照不可读/损坏（{type(exc).__name__}，fail-closed）", 3)
        if not isinstance(data, dict):
            raise MCPServerError("portfolio 顶层必须是对象（fail-closed）", 3)
        pf = Portfolio.from_dict(data)
        # 完整 Schema 校验：缺 schema_version/base_currency/day_start_equity/peak_equity
        # 等必填字段、类型/约束非法 → fail-closed（业务数据缺失由本层/R12 语义处理）
        for err in _validator("portfolio").iter_errors(pf.to_dict()):
            path_ = "/".join(str(p) for p in err.path) or "$"
            raise MCPServerError(
                f"portfolio 快照非法: 字段 {self._safe_path(path_)}（fail-closed）", 3
            )
        return pf

    # ---- 工具 1：check_order ----

    async def _check_order(self, arguments: dict) -> CallToolResult:
        self._validate_args(arguments, _order_input_schema())
        order_data = arguments["order"]
        try:
            pf = self._load_portfolio()
            result = self._guard.check(Order.from_dict(order_data), pf)
        except MCPServerError:
            raise
        except InputValidationError as exc:
            # policy 层输入错误（纵深防御，正常路径已被 MCP 参数校验拦截）；
            # 消息含实际值 → 脱敏后转通用类别
            raise MCPServerError(
                f"订单输入非法（{self._safe_path(str(exc.details[0]) if exc.details else 'unknown')}，input_error，fail-closed）",
                4, input_error=True,
            ) from exc
        except Exception as exc:
            raise MCPServerError(f"引擎内部异常（{type(exc).__name__}，fail-closed，按 BLOCK 处理）", 5) from exc
        if result.exit_code in (4, 5):
            # exit 4/5 按契约转工具错误；该次 Guard 审计记录已写入
            kind = "输入错误" if result.exit_code == 4 else "内部异常"
            raise MCPServerError(f"check_order 裁决为 {kind}（exit {result.exit_code}，fail-closed）", result.exit_code,
                                 input_error=(result.exit_code == 4))
        return self._ok(result.to_dict())  # 完整 result.schema.json 实例（含 evidence.inactive_rules）

    # ---- 工具 2：get_account_status ----

    async def _get_account_status(self, arguments: dict) -> CallToolResult:
        self._validate_args(arguments, _NO_ARG_SCHEMA)
        pf = self._load_portfolio()
        data = pf.data
        now = datetime.now(timezone.utc)

        equity = business_number(data.get("equity"))
        cash = business_number(data.get("cash"))
        daily_pnl = business_number(data.get("daily_pnl"))
        drawdown = business_number(data.get("drawdown_ratio"))
        for name, value in (("equity", equity), ("cash", cash), ("daily_pnl", daily_pnl),
                            ("drawdown_ratio", drawdown)):
            if value is None:
                raise MCPServerError(f"portfolio.{name} 缺失/非法，无法安全计算（fail-closed）", 3)

        # 新鲜度（与 R11 同口径）：未来快照 → fail-closed（不得报告 stale=false）
        snap = parse_rfc3339(data.get("snapshot_at", ""))
        max_age = self._policy.limits.get("max_snapshot_age_seconds")
        if snap is None or max_age is None:
            raise MCPServerError("portfolio.snapshot_at 缺失/不可解析，无法判定新鲜度（fail-closed）", 3)
        age = to_epoch_seconds(now) - to_epoch_seconds(snap)
        if age < 0:
            # 与 R11 一致——未来快照按 fail-closed 处理，不得报告 stale=false
            raise MCPServerError("portfolio.snapshot_at 在未来（数据可疑，fail-closed）", 3)
        stale = age > max_age

        # 敞口（Decimal 计算；任何持仓不可用 → fail-closed）
        positions = data.get("positions")
        if not isinstance(positions, list):
            raise MCPServerError("portfolio.positions 缺失/非数组，无法计算敞口（fail-closed）", 3)
        from decimal import Decimal

        total_gross = Decimal("0")
        for i, pos in enumerate(positions):
            if not isinstance(pos, dict):
                raise MCPServerError(f"portfolio.positions[{i}] 非对象，无法计算敞口（fail-closed）", 3)
            exp = position_exposure(pos)
            if exp is None:
                raise MCPServerError(f"portfolio.positions[{i}] 敞口数据不可用（fail-closed）", 3)
            total_gross += exp
        if equity <= 0:
            raise MCPServerError("账户权益非正，无法安全计算敞口利用率（fail-closed）", 3)
        utilization = total_gross / equity

        inactive = [rid for rid in RULE_IDS if not is_rule_enabled(rid, self._policy)]
        # base_currency 为必填字段（_load_portfolio 已保证），不回调 policy 兜底；
        # USD-only：与 policy 不一致（如未知 ISO 代码 EUR）→ fail-closed
        base_currency = data.get("base_currency")
        assert isinstance(base_currency, str) and base_currency  # schema 已校验
        if base_currency != self._policy.base_currency:
            raise MCPServerError("portfolio.base_currency 与 policy 不一致（USD-only，fail-closed）", 3)
        return self._ok({
            "freshness": {
                "snapshot_at": snap.isoformat(),
                "age_seconds": age,
                "max_age_seconds": max_age,
                "stale": stale,
            },
            "account": {
                "equity": float(equity),
                "cash": float(cash),
                "daily_pnl": float(daily_pnl),
                "drawdown_ratio": float(drawdown),
                "base_currency": base_currency,
            },
            "exposure": {
                "total_gross": float(total_gross),
                "utilization_ratio": float(utilization),
            },
            "policy": {"inactive_rule_count": len(inactive), "inactive_rules": inactive},
            "evaluated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })

    # ---- 工具 3：get_policy ----

    async def _get_policy(self, arguments: dict) -> CallToolResult:
        self._validate_args(arguments, _NO_ARG_SCHEMA)
        # 返回合法 policy.schema.json 实例（mcp-contract §3），不附加字段；
        # 禁用规则可见性由契约字段 acknowledged_disabled 与 limits 键本身承载
        # （inactive_rules/inactive_rule_count 由 check_order 与 get_account_status 提供）
        return self._ok(dict(self._policy.to_dict()))  # 只读投影；无 set/update/reload/write 能力

    # ---- 工具 4：kill_switch_status ----

    async def _kill_switch_status(self, arguments: dict) -> CallToolResult:
        self._validate_args(arguments, _NO_ARG_SCHEMA)
        mode = self._policy.kill_switch
        if mode not in ("off", "full", "reduce_only"):
            # 状态不可确认 → fail-closed（调用方必须按 full 语义处理；不得误报 off）
            raise MCPServerError(f"kill switch 状态不可确认（{mode!r}，fail-closed，按 full 处理）", 4)
        engaged_at = None
        try:
            records = read_audit_records(self._audit_path)
            for rec in reversed(records):  # 文件顺序近似时间序，取最近命中
                if any(h.get("rule_id") == "kill_switch" for h in rec.get("rule_hits", [])):
                    engaged_at = rec.get("evaluated_at")
                    break
        except AuditError:
            engaged_at = None  # 审计不可用只影响 engaged_at 追溯，mode 以 policy 为准
        return self._ok({
            "mode": mode,
            "engaged_at": engaged_at,
            "policy_version": self._policy.version,
            "evaluated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })

    # ---- 工具 5：recent_decisions ----

    async def _recent_decisions(self, arguments: dict) -> CallToolResult:
        self._validate_args(arguments, _RECENT_SCHEMA)
        limit = arguments.get("limit", 20)
        since_raw = arguments.get("since")
        since = None
        if since_raw is not None:
            since = parse_rfc3339(since_raw)
            if since is None:
                raise MCPServerError("since 必须是带显式时区的 RFC3339 时间（input_error，fail-closed）", 4,
                                     input_error=True)
        try:
            records = read_audit_records(self._audit_path)  # 已含 Schema 校验；malformed → AuditError
        except AuditError as exc:
            raise MCPServerError(f"审计日志不可用（{str(exc)}，fail-closed）", 3) from exc

        # 按时间点（解析为 aware datetime）比较，不使用原始字符串；
        # 相同时间点用 record_id 作稳定次序；解析失败 → fail-closed，不静默跳过
        def _dt(rec) -> datetime:
            dt = parse_rfc3339(rec.get("evaluated_at", ""))
            if dt is None:
                raise MCPServerError("审计记录 evaluated_at 不可解析（fail-closed）", 3)
            return dt

        window = []
        for rec in records:
            dt = _dt(rec)
            if since is not None and dt < since:
                continue
            window.append((dt, rec.get("record_id", ""), rec))
        window.sort(key=lambda t: (t[0], t[1]), reverse=True)  # 最近 N 条（时间点 + record_id 稳定）
        recent = window[:limit]
        recent.sort(key=lambda t: (t[0], t[1]))  # 返回窗口内 oldest-first（同样稳定）
        return self._ok({
            "count": len(recent),
            "records": [rec for _, _, rec in recent],
            "evaluated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })

    # ---- stdio 运行 ----

    async def run(self) -> None:
        async with stdio_server() as (read_stream, write_stream):
            await self._server.run(
                read_stream, write_stream, self._server.create_initialization_options()
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deadlatch-mcp",
        description="Local-first pre-trade risk evaluation MCP server (stdio, read-only, fail-closed, advisory-only).",
    )
    parser.add_argument("--policy", required=True, help="policy.yaml/.yml/.json（启动配置，非工具参数）")
    parser.add_argument("--portfolio", required=True, help="portfolio.json 本地快照路径（启动配置，非工具参数）")
    parser.add_argument("--audit-path", default=None, help="审计 JSONL 路径覆盖（缺省 $DEADLATCH_AUDIT_PATH 或 ~/.deadlatch/audit.jsonl）")
    parser.add_argument(
        "--kill-switch-path",
        default=None,
        help="可选独立 kill switch 文件；仅含 off/full/reduce_only，每次工具调用重读（启动配置，非工具参数）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        server = MCPGuardServer(
            args.policy, args.portfolio, args.audit_path, args.kill_switch_path
        )
    except InputValidationError as exc:
        for d in exc.details:
            print(f"error: {d}", file=sys.stderr)  # 诊断只写 stderr（stdout 只承载 MCP 帧）
        return 4
    except Exception as exc:  # 启动失败 fail-closed（不启动服务器）
        print(f"internal error: {type(exc).__name__}", file=sys.stderr)
        return 5
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
