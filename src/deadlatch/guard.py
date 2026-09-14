"""Guard 门面（M-3 库 API 主接口）+ policy 文件加载 + 审计记录。

Guard.from_policy(path) 装载完整标准规则集（12 条），调用方无法漏装规则；
底层规则注入能力保留但仅限测试/扩展（engine 构造校验不可绕过）。

审计：
- 每次 check 均尝试原子追加一条 AuditRecord（含 PASS/WARN/BLOCK、shadow
  内部裁决、exit 4/5）；审计路径可显式覆盖（from_policy(audit_path=...) /
  CLI --audit-path），未传时用环境变量 DEADLATCH_AUDIT_PATH，再退回
  文档化默认路径 ~/.deadlatch/audit.jsonl；
- 无"关闭审计"开关；
- 审计失败：仍返回本次规则裁决，但严重度只升不降——PASS/0 → WARN/2；
  WARN/2 不变；BLOCK/3、4、5 与 kill switch 保持原 decision/exit，仅附
  rule_id=audit_write_failed 的显式 warning 与安全 evidence（异常类型/阶段，
  不含路径、Token 或凭据）；shadow 投影 PASS 但审计失败 → WARN/2，
  shadow_verdict 保留内部裁决。
"""

import json
import os
from pathlib import Path

from ._validation import InputValidationError
from .audit import DEFAULT_AUDIT_PATH, append_audit, build_audit_record
from .engine import GuardEngine, validate_inputs_policy
from .model import Order, Policy, Portfolio, Result
from .rules.registry import standard_rule_registry

# 文件入口大小上限（命名常量；超限在完整载入/解析前拒绝）
MAX_POLICY_FILE_BYTES = 2 * 1024 * 1024      # 2 MiB
MAX_ORDER_FILE_BYTES = 2 * 1024 * 1024       # 2 MiB
MAX_PORTFOLIO_FILE_BYTES = 2 * 1024 * 1024   # 2 MiB


def check_file_size(path, limit: int, label: str) -> None:
    """载入前大小检查：超限抛 InputValidationError（exit 4 语义），拒绝完整读取。"""
    try:
        size = Path(path).stat().st_size
    except OSError:
        return  # 文件不存在/不可 stat 由后续读取路径处理
    if size > limit:
        raise InputValidationError(
            [f"{label} 超过大小上限 {limit} 字节（实际 {size} 字节），拒绝载入"]
        )


def resolve_audit_path(explicit: str | None = None) -> Path:
    """审计路径解析顺序：显式参数 > 环境变量 > 文档化默认路径。"""
    if explicit:
        return Path(explicit)
    env = os.environ.get("DEADLATCH_AUDIT_PATH")
    if env:
        return Path(env)
    return DEFAULT_AUDIT_PATH


class Guard:
    """用户主接口。"""

    def __init__(self, engine: GuardEngine, audit_path: str | None = None):
        self._engine = engine
        self._audit_path = resolve_audit_path(audit_path)

    @classmethod
    def from_policy(cls, path, audit_path: str | None = None) -> "Guard":
        """从 policy 文件（.yaml/.yml/.json）构建 Guard，装载完整 12 条规则集。

        audit_path 可选覆盖审计 JSONL 路径（缺省：DEADLATCH_AUDIT_PATH
        环境变量或 ~/.deadlatch/audit.jsonl）；无关闭审计开关。
        """
        policy = load_policy_file(path)
        engine = GuardEngine(policy, rules=standard_rule_registry())
        return cls(engine, audit_path=audit_path)

    def check(self, order: Order, portfolio: Portfolio, **kwargs) -> "Result":
        result = self._engine.check(order, portfolio, **kwargs)
        return self._record_audit(order, portfolio, result)

    @property
    def policy(self) -> Policy:
        return self._engine.policy

    @property
    def audit_path(self) -> Path:
        return self._audit_path

    # ---- 审计 ----

    def _record_audit(self, order, portfolio, result: Result) -> Result:
        try:
            record = build_audit_record(order, portfolio, self.policy, result)
            append_audit(self._audit_path, record)
        except Exception as exc:  # 审计失败：可见降级，不逃逸、不降低原裁决
            return self._degrade_for_audit_failure(result, exc)
        return result

    def _degrade_for_audit_failure(self, result: Result, exc: Exception) -> Result:
        """写失败严重度矩阵（只升不降）。审计 evidence 仅记录安全异常类型/阶段。"""
        warn = {
            "rule_id": "audit_write_failed",
            "severity": "WARN",
            "detail": f"审计写入失败（{type(exc).__name__}），本次裁决已保留",
        }
        result.warnings.append(warn)
        result.evidence.setdefault("rule_evidence", {}).setdefault("audit_write_failed", []).extend(
            [
                {"name": "exception_type", "value": type(exc).__name__},
                {"name": "stage", "value": "audit"},
            ]
        )
        if result.exit_code == 0:  # PASS（含 shadow 投影 PASS）→ WARN/2；shadow_verdict 保留
            result.decision = "WARN"
            result.exit_code = 2
        # WARN/2 与 BLOCK/3、4、5：decision/exit 保持不变（kill switch 不得因此变 WARN/PASS）
        return result


def load_policy_file(path) -> Policy:
    """读取并校验 policy 文件。失败抛 InputValidationError（exit 4 语义，非引擎异常）。"""
    p = Path(path)
    if not p.exists():
        raise InputValidationError([f"policy 文件不存在: {path}"])
    check_file_size(p, MAX_POLICY_FILE_BYTES, "policy")  # size limit
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise InputValidationError([f"policy 文件无法读取: {path} ({exc})"]) from exc
    try:
        if p.suffix.lower() in (".yaml", ".yml"):
            import yaml

            data = yaml.safe_load(text)
        else:
            data = json.loads(text)
    except Exception as exc:
        raise InputValidationError(
            [f"policy 文件解析失败: {path} ({type(exc).__name__})"]
        ) from exc
    if not isinstance(data, dict):
        raise InputValidationError([f"policy 顶层必须是对象: {path}"])
    # YAML 1.1 陷阱防御：off/on/yes/no 会被解析为布尔，导致 kill_switch 类型非法
    if not isinstance(data.get("kill_switch"), str):
        raise InputValidationError(
            [
                "policy.kill_switch 必须是字符串（'off'/'full'/'reduce_only'）。"
                "YAML 1.1 会把裸 off 解析为布尔 False，请写引号形式：kill_switch: \"off\""
            ]
        )
    policy = Policy.from_dict(data)
    validate_inputs_policy(policy)  # Schema/版本/acknowledged_disabled/阈值合法性 → exit 4
    return policy
