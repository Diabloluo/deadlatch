""" MCP Server 测试：真实 stdio 客户端集成（subprocess）+ 进程内异常注入。

- 协议级：官方 MCP Python 客户端经真实 subprocess stdio 完成 initialize /
  tools/list / tools/call（函数直调不算协议验收，因此关键矩阵全部走 stdio）；
- 进程内：异常注入矩阵（monkeypatch 需要进程内）验证统一错误出口；
- 所有 server 配置（policy/portfolio/audit）均为 tmp_path 虚构数据，审计写入
  不触碰用户真实目录。
"""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from deadlatch.mcp_server import MCPGuardServer, MCPServerError

REPO = Path(__file__).resolve().parents[1]

_FULL_LIMITS = (
    "  max_order_quantity: 500\n"
    "  max_order_value: 5000.0\n"
    "  max_symbol_exposure_ratio: 0.10\n"
    "  max_total_exposure_ratio: 0.60\n"
    "  min_cash: 0.0\n"
    "  max_options_margin_ratio: 0.35\n"
    "  max_daily_loss_ratio: 0.03\n"
    "  max_drawdown_ratio: 0.15\n"
    "  max_order_age_seconds: 300\n"
    "  max_snapshot_age_seconds: 300\n"
)


def _ts(offset: timedelta) -> str:
    return (datetime.now(timezone.utc) + offset).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_policy(tmp_path: Path, *, name: str = "policy.yaml", mode="enforce",
                  kill_switch="off", version="1.0.0",
                  acknowledged: list[str] | None = None, limits: str = _FULL_LIMITS) -> Path:
    p = tmp_path / name
    ack = acknowledged if acknowledged is not None else []
    p.write_text(
        "schema_version: 2\n"
        f"version: '{version}'\n"
        f"mode: {mode}\n"
        "base_currency: USD\n"
        f'kill_switch: "{kill_switch}"\n'
        f"acknowledged_disabled: {json.dumps(ack)}\n"
        f"limits:\n{limits}",
        encoding="utf-8",
    )
    return p


def _write_portfolio(tmp_path: Path, *, name: str = "portfolio.json",
                     snapshot_ts: str | None = None, **overrides) -> Path:
    p = tmp_path / name
    data = {
        "schema_version": 3, "equity": 123456.78, "cash": 30000.0,
        "day_start_equity": 125000.0, "peak_equity": 128000.0,
        "daily_pnl": -1200.5, "drawdown_ratio": 0.0355,
        "snapshot_at": snapshot_ts or _ts(timedelta(seconds=-60)),
        "base_currency": "USD", "positions": [],
    }
    data.update(overrides)
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _audit_record(rid: str, evaluated_at: str, decision: str, exit_code: int,
                  shadow_verdict=None, hits=None) -> str:
    rec = {
        "schema_version": 1, "record_id": rid, "evaluated_at": evaluated_at,
        "input_hash": "0" * 64, "decision": decision, "shadow_mode": shadow_verdict is not None,
        "shadow_verdict": shadow_verdict, "exit_code": exit_code,
        "policy_version": "1.0.0", "rule_hits": hits or [],
    }
    return json.dumps(rec, sort_keys=True, separators=(",", ":"))


def _write_audit(tmp_path: Path, lines: list[str]) -> Path:
    p = tmp_path / "audit.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _params(policy: Path, portfolio: Path, audit: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "deadlatch.mcp_server", "--policy", str(policy),
              "--portfolio", str(portfolio), "--audit-path", str(audit)],
    )


def _order(**overrides) -> dict:
    d = {
        "schema_version": 2, "symbol": "AAA", "instrument_type": "stock",
        "side": "buy", "quantity": 20, "price": 190.0, "order_type": "limit",
        "currency": "USD", "created_at": _ts(timedelta(seconds=-1)),
    }
    d.update(overrides)
    return d


async def _call(session: ClientSession, name: str, args: dict):
    r = await session.call_tool(name, args)
    text = r.content[0].text if r.content else ""
    return r.is_error, (json.loads(text) if text else None)


async def _connect(params):
    ctx = stdio_client(params)
    read, write = await ctx.__aenter__()
    session = ClientSession(read, write)
    await session.__aenter__()
    await session.initialize()
    return ctx, session


# ---------------------------------------------------------------- 会话夹具

@pytest.fixture(scope="module")
def std_env(tmp_path_factory):
    """标准服务器环境（enforce/off、新鲜快照、预写 3 条审计）——模块级复用。"""
    d = tmp_path_factory.mktemp("mcp-std")
    policy = _write_policy(d)
    portfolio = _write_portfolio(d)
    audit = _write_audit(d, [
        _audit_record("rec00000001", _ts(timedelta(seconds=-300)), "PASS", 0),
        _audit_record("rec00000002", _ts(timedelta(seconds=-200)), "BLOCK", 3,
                      hits=[{"rule_id": "max_order_value", "severity": "BLOCK", "detail": "单笔订单金额超限"}]),
        _audit_record("rec00000003", _ts(timedelta(seconds=-100)), "WARN", 2,
                      hits=[{"rule_id": "max_order_quantity", "severity": "WARN", "detail": "数量接近上限"}]),
    ])
    return {"params": _params(policy, portfolio, audit), "dir": d,
            "policy": policy, "portfolio": portfolio, "audit": audit}


# ---------------------------------------------------------------- 1/2：初始化、工具清单、schema

def test_initialize_and_tools_exact_five(std_env):
    async def _run():
        ctx, session = await _connect(std_env["params"])
        try:
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            assert names == ["check_order", "get_account_status", "get_policy",
                             "kill_switch_status", "recent_decisions"]
        finally:
            await session.__aexit__(None, None, None)
            await ctx.__aexit__(None, None, None)
    asyncio.run(_run())
    # 无 resources/prompts/文件读取面：服务器只注册 tools/list 与 tools/call 两个 handler
    srv = MCPGuardServer(str(std_env["policy"]), str(std_env["portfolio"]),
                         str(std_env["audit"]))
    for method in ("resources/list", "resources/read", "prompts/list", "prompts/get",
                   "roots/list", "logging/setLevel"):
        assert srv._server.get_request_handler(method) is None, method


def test_all_input_schemas_additional_properties_false(std_env):
    async def _run():
        ctx, session = await _connect(std_env["params"])
        try:
            tools = {t.name: t for t in (await session.list_tools()).tools}
            for name, tool in tools.items():
                assert tool.input_schema.get("additionalProperties") is False, name
            order_schema = tools["check_order"].input_schema["properties"]["order"]
            assert order_schema.get("additionalProperties") is False
            assert list(tools["check_order"].input_schema["properties"]) == ["order"]
            assert tools["check_order"].input_schema["required"] == ["order"]
            assert list(tools["recent_decisions"].input_schema["properties"]) == ["limit", "since"]
            for name in ("get_account_status", "get_policy", "kill_switch_status"):
                assert tools[name].input_schema["properties"] == {}
        finally:
            await session.__aexit__(None, None, None)
            await ctx.__aexit__(None, None, None)
    asyncio.run(_run())


# ---------------------------------------------------------------- 3：check_order 决策矩阵

def test_check_order_pass_full_result(std_env):
    async def _run():
        ctx, session = await _connect(std_env["params"])
        try:
            err, r = await _call(session, "check_order", {"order": _order()})
            assert err is False
            assert r["decision"] == "PASS" and r["exit_code"] == 0
            assert r["shadow_mode"] is False and r["shadow_verdict"] is None
            assert "inactive_rules" in r["evidence"]  # 完整 result.schema 实例
            assert r["evidence"]["input_hash"]
        finally:
            await session.__aexit__(None, None, None)
            await ctx.__aexit__(None, None, None)
    asyncio.run(_run())


def test_check_order_plain_block_is_normal_result(std_env):
    async def _run():
        ctx, session = await _connect(std_env["params"])
        try:
            err, r = await _call(session, "check_order", {"order": _order(quantity=1000)})
            assert err is False  # exit 3 是正常风控结果，不是工具错误
            assert r["decision"] == "BLOCK" and r["exit_code"] == 3
            assert any(v["rule_id"] == "max_order_quantity" for v in r["violations"])
        finally:
            await session.__aexit__(None, None, None)
            await ctx.__aexit__(None, None, None)
    asyncio.run(_run())


def test_check_order_shadow_projection(tmp_path):
    policy = _write_policy(tmp_path, mode="shadow")
    portfolio = _write_portfolio(tmp_path)
    audit = _write_audit(tmp_path, [])
    async def _run():
        ctx, session = await _connect(_params(policy, portfolio, audit))
        try:
            err, r = await _call(session, "check_order", {"order": _order(quantity=1000)})
            assert err is False
            assert r["decision"] == "PASS" and r["exit_code"] == 0  # 投影
            assert r["shadow_mode"] is True and r["shadow_verdict"] == "BLOCK"
            assert r["violations"]  # 内部命中保留
        finally:
            await session.__aexit__(None, None, None)
            await ctx.__aexit__(None, None, None)
    asyncio.run(_run())


def test_check_order_kill_full_and_reduce_only(tmp_path):
    full_pol = _write_policy(tmp_path, name="full.yaml", kill_switch="full")
    red_pol = _write_policy(tmp_path, name="reduce.yaml", kill_switch="reduce_only")
    portfolio = _write_portfolio(tmp_path, positions=[
        {"symbol": "AAA", "instrument_type": "stock", "side": "long",
         "quantity": 100, "market_value": 19000.0, "currency": "USD"}
    ])
    audit = _write_audit(tmp_path, [])
    async def _run():
        ctx, session = await _connect(_params(full_pol, portfolio, audit))
        try:
            for order in (_order(), _order(side="sell", quantity=50)):  # open 与 close 都 BLOCK
                err, r = await _call(session, "check_order", {"order": order})
                assert err is False and r["decision"] == "BLOCK" and r["exit_code"] == 3
        finally:
            await session.__aexit__(None, None, None)
            await ctx.__aexit__(None, None, None)

        ctx2, session2 = await _connect(_params(red_pol, portfolio, audit))
        try:
            err, r = await _call(session2, "check_order", {"order": _order()})
            assert r["decision"] == "BLOCK" and r["exit_code"] == 3  # open → BLOCK
            err, r = await _call(session2, "check_order", {"order": _order(side="sell", quantity=50)})
            assert r["decision"] == "PASS" and r["exit_code"] == 0  # close → 放行
        finally:
            await session2.__aexit__(None, None, None)
            await ctx2.__aexit__(None, None, None)
    asyncio.run(_run())


def test_check_order_exit4_tool_error_keeps_audit(tmp_path):
    policy = _write_policy(tmp_path)
    portfolio = _write_portfolio(tmp_path)
    audit = _write_audit(tmp_path, [])
    async def _run():
        ctx, session = await _connect(_params(policy, portfolio, audit))
        try:
            bad = _order(currency="HKD")
            err, r = await _call(session, "check_order", {"order": bad})
            assert err is True
            assert r["input_error"] is True and r["fail_closed"] is True and r["exit_code"] == 4
            # 该次 Guard 审计记录保留（exit 4 也写审计）
            lines = audit.read_text(encoding="utf-8").splitlines()
            assert any(json.loads(l)["exit_code"] == 4 for l in lines if l.strip())
        finally:
            await session.__aexit__(None, None, None)
            await ctx.__aexit__(None, None, None)
    asyncio.run(_run())


# ---------------------------------------------------------------- 4：参数注入拒绝

def test_injection_parameters_rejected_and_files_unchanged(std_env):
    policy_before = std_env["policy"].read_bytes()
    portfolio_before = std_env["portfolio"].read_bytes()
    audit_before = std_env["audit"].read_bytes()
    order = _order()
    injections = [
        {"order": order, "portfolio": {"equity": 1}},
        {"order": order, "policy": {"limits": {}}},
        {"order": order, "kill_switch": "off"},
        {"order": order, "limits": {"max_order_quantity": 999999}},
        {"order": order, "audit_path": "/tmp/evil.jsonl"},
        {"order": order, "path": "/etc/passwd"},
        {"order": {**order, "portfolio": {}}},  # order 内部注入
    ]
    async def _run():
        ctx, session = await _connect(std_env["params"])
        try:
            for args in injections:
                err, r = await _call(session, "check_order", args)
                assert err is True, args
                assert r["input_error"] is True and r["fail_closed"] is True
                assert r["exit_code"] == 4
            # recent_decisions 未知字段拒绝
            err, r = await _call(session, "recent_decisions", {"limit": 5, "path": "/x"})
            assert err is True and r["input_error"] is True
        finally:
            await session.__aexit__(None, None, None)
            await ctx.__aexit__(None, None, None)
    asyncio.run(_run())
    # 本地文件不变
    assert std_env["policy"].read_bytes() == policy_before
    assert std_env["portfolio"].read_bytes() == portfolio_before
    assert std_env["audit"].read_bytes() == audit_before


# ---------------------------------------------------------------- 5/6：kill full 100 次 + 无切换

def test_kill_full_100_retries_all_block(tmp_path):
    policy = _write_policy(tmp_path, kill_switch="full")
    portfolio = _write_portfolio(tmp_path, positions=[
        {"symbol": "AAA", "instrument_type": "stock", "side": "long",
         "quantity": 100, "market_value": 19000.0, "currency": "USD"}
    ])
    audit = _write_audit(tmp_path, [])
    policy_before = policy.read_bytes()
    async def _run():
        ctx, session = await _connect(_params(policy, portfolio, audit))
        try:
            for i in range(100):
                # 同参 + 参数扰动（symbol/quantity/side 变化），含平仓订单
                order = _order(quantity=20 + (i % 7), symbol=["AAA", "BBB", "CCC"][i % 3])
                if i % 2:
                    order = _order(side="sell", quantity=50, symbol="AAA")  # 平仓订单
                err, r = await _call(session, "check_order", {"order": order})
                assert err is False, i
                assert r["decision"] == "BLOCK" and r["exit_code"] == 3, (i, r)
        finally:
            await session.__aexit__(None, None, None)
            await ctx.__aexit__(None, None, None)
    asyncio.run(_run())
    assert policy.read_bytes() == policy_before  # policy 文件字节级不变（无任何切换路径）


# ---------------------------------------------------------------- 7：异常注入（进程内）

def test_handler_args_parse_exception_fail_closed(tmp_path, monkeypatch):
    policy = _write_policy(tmp_path)
    portfolio = _write_portfolio(tmp_path)
    audit = _write_audit(tmp_path, [])
    srv = MCPGuardServer(str(policy), str(portfolio), str(audit))

    def boom_validate(*a, **k):
        raise RuntimeError("args boom")

    monkeypatch.setattr(srv, "_validate_args", boom_validate)
    r = asyncio.run(srv._call_tool(None, type("P", (), {"name": "get_policy", "arguments": {}})()))
    body = json.loads(r.content[0].text)
    assert r.is_error and body["fail_closed"] is True and body["exit_code"] == 5
    assert "args boom" not in json.dumps(body)  # 异常原文不进 error content


def test_handler_portfolio_loader_exception_fail_closed(tmp_path, monkeypatch):
    policy = _write_policy(tmp_path)
    portfolio = _write_portfolio(tmp_path)
    audit = _write_audit(tmp_path, [])
    srv = MCPGuardServer(str(policy), str(portfolio), str(audit))

    def boom_portfolio(*a, **k):
        raise MCPServerError("portfolio 不可用（fail-closed）", 3)

    monkeypatch.setattr(srv, "_load_portfolio", boom_portfolio)
    r = asyncio.run(srv._call_tool(None, type("Q", (), {"name": "check_order",
                                                        "arguments": {"order": _order()}})()))
    body = json.loads(r.content[0].text)
    assert r.is_error and body["fail_closed"] is True and body["exit_code"] == 3
    assert "decision" not in body  # 不伪造安全状态


def test_handler_guard_call_exception_fail_closed(tmp_path, monkeypatch):
    policy = _write_policy(tmp_path)
    portfolio = _write_portfolio(tmp_path)
    audit = _write_audit(tmp_path, [])
    srv = MCPGuardServer(str(policy), str(portfolio), str(audit))

    def boom_check(*a, **k):
        raise RuntimeError("engine boom")

    monkeypatch.setattr(srv._guard, "check", boom_check)
    r = asyncio.run(srv._call_tool(None, type("Q", (), {"name": "check_order",
                                                        "arguments": {"order": _order()}})()))
    body = json.loads(r.content[0].text)
    assert r.is_error and body["fail_closed"] is True and body["exit_code"] == 5
    assert "engine boom" not in json.dumps(body)  # 异常原文不进 error content


# ---------------------------------------------------------------- 8：get_account_status

def test_get_account_status_fresh_and_stale(tmp_path):
    pol = _write_policy(tmp_path)
    fresh = _write_portfolio(tmp_path, name="fresh.json")
    stale = _write_portfolio(tmp_path, name="stale.json", snapshot_ts=_ts(timedelta(hours=-2)))
    audit = _write_audit(tmp_path, [])
    async def _run():
        ctx, s = await _connect(_params(pol, fresh, audit))
        try:
            err, r = await _call(s, "get_account_status", {})
            assert err is False
            assert r["freshness"]["stale"] is False
            assert r["freshness"]["max_age_seconds"] == 300
            assert r["account"]["equity"] == 123456.78
            assert r["exposure"]["total_gross"] == 0.0
            assert r["policy"]["inactive_rule_count"] == 0
        finally:
            await s.__aexit__(None, None, None)
            await ctx.__aexit__(None, None, None)
        ctx2, s2 = await _connect(_params(pol, stale, audit))
        try:
            err, r = await _call(s2, "get_account_status", {})
            assert err is False and r["freshness"]["stale"] is True  # 有效但陈旧
        finally:
            await s2.__aexit__(None, None, None)
            await ctx2.__aexit__(None, None, None)
    asyncio.run(_run())


def test_get_account_status_missing_corrupt_and_nonpositive_equity(tmp_path):
    pol = _write_policy(tmp_path)
    audit = _write_audit(tmp_path, [])
    missing = tmp_path / "missing.json"
    corrupt = _write_portfolio(tmp_path)
    corrupt.write_text("{not-json", encoding="utf-8")
    neg = _write_portfolio(tmp_path, equity=-5000.0)
    async def _run():
        # 缺失
        ctx, s = await _connect(_params(pol, missing, audit))
        try:
            err, r = await _call(s, "get_account_status", {})
            assert err is True and r["fail_closed"] is True
            assert "decision" not in r  # 不伪造安全状态
        finally:
            await s.__aexit__(None, None, None)
            await ctx.__aexit__(None, None, None)
        # 损坏
        ctx, s = await _connect(_params(pol, corrupt, audit))
        try:
            err, r = await _call(s, "get_account_status", {})
            assert err is True and r["fail_closed"] is True
        finally:
            await s.__aexit__(None, None, None)
            await ctx.__aexit__(None, None, None)
        # 非正权益 → 无法安全计算利用率 → fail-closed
        ctx, s = await _connect(_params(pol, neg, audit))
        try:
            err, r = await _call(s, "get_account_status", {})
            assert err is True and r["fail_closed"] is True
        finally:
            await s.__aexit__(None, None, None)
            await ctx.__aexit__(None, None, None)
    asyncio.run(_run())


def test_get_account_status_inactive_rules_visible(tmp_path):
    pol = _write_policy(tmp_path, acknowledged=[
        "max_order_quantity", "max_order_value", "max_symbol_exposure",
        "max_total_exposure", "cash_margin_check",
    ], limits=(
        "  max_daily_loss_ratio: 0.03\n"
        "  max_drawdown_ratio: 0.15\n"
        "  max_order_age_seconds: 300\n"
        "  max_snapshot_age_seconds: 300\n"
    ))
    portfolio = _write_portfolio(tmp_path)
    audit = _write_audit(tmp_path, [])
    async def _run():
        ctx, s = await _connect(_params(pol, portfolio, audit))
        try:
            err, r = await _call(s, "get_account_status", {})
            assert err is False
            assert r["policy"]["inactive_rule_count"] == 5
            assert set(r["policy"]["inactive_rules"]) == {
                "max_order_quantity", "max_order_value", "max_symbol_exposure",
                "max_total_exposure", "cash_margin_check",
            }
        finally:
            await s.__aexit__(None, None, None)
            await ctx.__aexit__(None, None, None)
    asyncio.run(_run())


# ---------------------------------------------------------------- 9：get_policy

def test_get_policy_readonly_projection_with_inactive(std_env):
    async def _run():
        ctx, s = await _connect(std_env["params"])
        try:
            err, r = await _call(s, "get_policy", {})
            assert err is False
            # FIX-004-1：返回值必须通过 policy.schema.json（不附加 inactive_* 字段）
            Draft202012Validator(
                json.loads((REPO / "schemas" / "policy.schema.json").read_text())
            ).validate(r)
            assert r["schema_version"] == 2 and r["mode"] == "enforce"
            assert r["base_currency"] == "USD"
            assert r["acknowledged_disabled"] == []  # 契约字段保留
            assert r["limits"]["max_order_quantity"] == 500
            assert "inactive_rules" not in r and "inactive_rule_count" not in r  # 不附加字段
            assert "path" not in json.dumps(r)  # 不返回 policy 文件路径
            assert not any(k in r for k in ("set", "update", "reload", "write"))
        finally:
            await s.__aexit__(None, None, None)
            await ctx.__aexit__(None, None, None)
    asyncio.run(_run())


def test_get_policy_shows_acknowledged_disabled(tmp_path):
    pol = _write_policy(tmp_path, acknowledged=["max_order_quantity"],
                        limits=_FULL_LIMITS.replace("  max_order_quantity: 500\n", ""))
    portfolio = _write_portfolio(tmp_path)
    audit = _write_audit(tmp_path, [])
    async def _run():
        ctx, s = await _connect(_params(pol, portfolio, audit))
        try:
            err, r = await _call(s, "get_policy", {})
            assert err is False
            Draft202012Validator(
                json.loads((REPO / "schemas" / "policy.schema.json").read_text())
            ).validate(r)
            assert r["acknowledged_disabled"] == ["max_order_quantity"]  # 禁用可见性契约字段
            assert "max_order_quantity" not in r["limits"]  # 键缺失即禁用
        finally:
            await s.__aexit__(None, None, None)
            await ctx.__aexit__(None, None, None)
    asyncio.run(_run())


def test_mcp_server_startup_policy_error_exit4(tmp_path, capsys):
    # 启动配置失败（policy 缺失/非法）→ 进程退出码 4（fail-fast，不启动服务器）
    portfolio = _write_portfolio(tmp_path)
    r = subprocess.run(
        [sys.executable, "-m", "deadlatch.mcp_server",
         "--policy", str(tmp_path / "nope.yaml"), "--portfolio", str(portfolio)],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 4
    assert "error:" in r.stderr
    assert "Traceback" not in r.stdout and "Traceback" not in r.stderr


# ---------------------------------------------------------------- 10：kill_switch_status

def test_kill_switch_status_three_states(tmp_path):
    audit = _write_audit(tmp_path, [])
    async def _run():
        for ks in ("off", "full", "reduce_only"):
            pol = _write_policy(tmp_path, kill_switch=ks)
            portfolio = _write_portfolio(tmp_path)
            ctx, s = await _connect(_params(pol, portfolio, audit))
            try:
                err, r = await _call(s, "kill_switch_status", {})
                assert err is False and r["mode"] == ks
                assert r["policy_version"] == "1.0.0"
                assert r["evaluated_at"]
            finally:
                await s.__aexit__(None, None, None)
                await ctx.__aexit__(None, None, None)
    asyncio.run(_run())


def test_kill_switch_status_unknown_mode_fail_closed(tmp_path):
    # 进程内构造非法 mode（policy schema 拦不到的场景防御）→ fail-closed，不误报 off
    pol = _write_policy(tmp_path)
    portfolio = _write_portfolio(tmp_path)
    audit = _write_audit(tmp_path, [])
    srv = MCPGuardServer(str(pol), str(portfolio), str(audit))
    srv._policy.data["kill_switch"] = "enable"  # 模拟被篡改的运行时状态
    r = asyncio.run(srv._call_tool(None, type("P", (), {"name": "kill_switch_status", "arguments": {}})()))
    body = json.loads(r.content[0].text)
    assert r.is_error and body["fail_closed"] is True
    assert body["exit_code"] == 4
    assert "off" not in body.get("mode", "")  # 不得误报 off


# ---------------------------------------------------------------- 11：recent_decisions

def test_recent_decisions_limit_sorting_and_since(std_env):
    async def _run():
        ctx, s = await _connect(std_env["params"])
        try:
            # 默认 limit=20 → 全部 3 条 + check 追加的 1 条 = 4 条
            err, r = await _call(s, "recent_decisions", {})
            assert err is False and r["count"] >= 4
            ts = [rec["evaluated_at"] for rec in r["records"]]
            assert ts == sorted(ts)  # 窗口内 oldest-first
            # since 过滤
            since = _ts(timedelta(seconds=-250))
            err, r = await _call(s, "recent_decisions", {"since": since})
            assert all(rec["evaluated_at"] >= since for rec in r["records"])
            # limit=1 → 最近 1 条（oldest-first 窗口内仍 1 条）
            err, r = await _call(s, "recent_decisions", {"limit": 1})
            assert r["count"] == 1
            # limit=100 边界
            err, r = await _call(s, "recent_decisions", {"limit": 100})
            assert err is False
            # limit 越界 → input_error
            err, r = await _call(s, "recent_decisions", {"limit": 0})
            assert err is True and r["input_error"] is True
            err, r = await _call(s, "recent_decisions", {"limit": 101})
            assert err is True and r["input_error"] is True
            # 非法 since → input_error
            err, r = await _call(s, "recent_decisions", {"since": "not-a-date"})
            assert err is True and r["input_error"] is True
        finally:
            await s.__aexit__(None, None, None)
            await ctx.__aexit__(None, None, None)
    asyncio.run(_run())


def test_recent_decisions_records_are_redacted(tmp_path):
    # 经真实 check_order 注入假 Token 形态 symbol → 审计脱敏落盘 → recent_decisions 零明文
    audit = _write_audit(tmp_path, [])
    pol = _write_policy(tmp_path)
    portfolio = _write_portfolio(tmp_path)
    async def _run():
        ctx, s = await _connect(_params(pol, portfolio, audit))
        try:
            fake = "sk-FAKEKEY1234567890"
            err, r = await _call(s, "check_order", {"order": _order(symbol=fake, quantity=1000)})
            assert err is False and r["exit_code"] == 3  # R5 BLOCK（detail 含 symbol）
            err, r = await _call(s, "recent_decisions", {})
            assert err is False and r["count"] >= 1
            raw = json.dumps(r)
            assert fake not in raw  # 脱敏：Token 形态零明文
            assert "<redacted>" in raw
        finally:
            await s.__aexit__(None, None, None)
            await ctx.__aexit__(None, None, None)
    asyncio.run(_run())


def test_recent_decisions_corrupt_log_fail_closed(tmp_path):
    audit = _write_audit(tmp_path, ["not-json-line\n"])
    pol = _write_policy(tmp_path)
    portfolio = _write_portfolio(tmp_path)
    async def _run():
        ctx, s = await _connect(_params(pol, portfolio, audit))
        try:
            err, r = await _call(s, "recent_decisions", {})
            assert err is True and r["fail_closed"] is True  # 不返回空成功掩盖异常
            assert r["exit_code"] == 3
        finally:
            await s.__aexit__(None, None, None)
            await ctx.__aexit__(None, None, None)
    asyncio.run(_run())


# ---------------------------------------------------------------- 12/13：stdout/stderr、进程与端口

def test_stdout_only_protocol_frames_and_clean_stderr(tmp_path):
    pol = _write_policy(tmp_path)
    portfolio = _write_portfolio(tmp_path)
    audit = _write_audit(tmp_path, [])
    proc = subprocess.Popen(
        [sys.executable, "-m", "deadlatch.mcp_server",
         "--policy", str(pol), "--portfolio", str(portfolio), "--audit-path", str(audit)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        # 初始化握手：写 JSON-RPC initialize 帧，读回——stdout 只应有 JSON 帧
        init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "t", "version": "0"}}}) + "\n"
        proc.stdin.write(init.encode())
        proc.stdin.flush()
        line = proc.stdout.readline()
        frame = json.loads(line)  # 非 JSON → 抛错（stdout 被污染）
        assert frame.get("id") == 1
        time.sleep(0.3)  # 给诊断流留时间——若实现误用 print() 会出现在 stdout
    finally:
        proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=10)
    err = proc.stderr.read().decode()
    assert "Traceback" not in err  # 无 traceback 泄漏
    assert proc.poll() is not None  # 退出后无残留进程


def test_no_listen_port_and_no_residual_process(tmp_path):
    pol = _write_policy(tmp_path)
    portfolio = _write_portfolio(tmp_path)
    audit = _write_audit(tmp_path, [])
    proc = subprocess.Popen(
        [sys.executable, "-m", "deadlatch.mcp_server",
         "--policy", str(pol), "--portfolio", str(portfolio), "--audit-path", str(audit)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        time.sleep(0.5)
        # 不开放监听端口（stdio 无网络面）
        out = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-a", "-p", str(proc.pid)],
                             capture_output=True, text=True, timeout=15)
        assert out.stdout.strip() == "", f"发现监听端口: {out.stdout}"
    finally:
        proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=10)
    assert proc.poll() is not None  # 无残留子进程


# ---------------------------------------------------------------- 配置示例存在性

def test_mcp_config_examples_exist():
    base = REPO / "examples" / "mcp"
    assert (base / "claude_desktop_config.json").exists()
    assert (base / "cursor-mcp.json").exists()
    assert (base / "README.md").exists()
    for f in ("claude_desktop_config.json", "cursor-mcp.json"):
        text = (base / f).read_text(encoding="utf-8")
        assert "/Users/" not in text and "nini" not in text  # 虚构占位路径


# ================================================================  原单修补回归

def _portfolio_doc(**overrides) -> dict:
    d = {
        "schema_version": 3, "equity": 123456.78, "cash": 30000.0,
        "day_start_equity": 125000.0, "peak_equity": 128000.0,
        "daily_pnl": -1200.5, "drawdown_ratio": 0.0355,
        "snapshot_at": _ts(timedelta(seconds=-60)),
        "base_currency": "USD", "positions": [],
    }
    d.update(overrides)
    return d


def _write_portfolio_doc(tmp_path, doc: dict, name: str = "portfolio.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def test_fix1_get_policy_passes_policy_schema_via_stdio(std_env):
    async def _run():
        ctx, s = await _connect(std_env["params"])
        try:
            err, r = await _call(s, "get_policy", {})
            assert err is False
            Draft202012Validator(
                json.loads((REPO / "schemas" / "policy.schema.json").read_text())
            ).validate(r)
        finally:
            await s.__aexit__(None, None, None)
            await ctx.__aexit__(None, None, None)
    asyncio.run(_run())


def test_fix2_snapshot_future_and_missing_fields_fail_closed(tmp_path):
    pol = _write_policy(tmp_path)
    audit = _write_audit(tmp_path, [])
    cases = {
        "future.json": _portfolio_doc(snapshot_at=_ts(timedelta(hours=1))),        # 未来快照
        "no_base.json": {k: v for k, v in _portfolio_doc().items() if k != "base_currency"},
        "no_version.json": {k: v for k, v in _portfolio_doc().items() if k != "schema_version"},
        "no_day_start.json": {k: v for k, v in _portfolio_doc().items() if k != "day_start_equity"},
        "no_peak.json": {k: v for k, v in _portfolio_doc().items() if k != "peak_equity"},
        "bad_currency.json": _portfolio_doc(base_currency="usd"),                  # pattern 非法
        "bad_equity.json": _portfolio_doc(equity="not-a-number"),                  # 类型非法
        "bad_positions.json": _portfolio_doc(positions={"symbol": "AAA"}),         # 非数组
    }
    async def _run():
        for name, doc in cases.items():
            pf = _write_portfolio_doc(tmp_path, doc, name=name)
            ctx, s = await _connect(_params(pol, pf, audit))
            try:
                err, r = await _call(s, "get_account_status", {})
                assert err is True, name  # FIX-004-2：未来/缺字段/非法 → 零安全成功
                assert r["fail_closed"] is True, name
                assert "decision" not in r, name  # 不伪造安全状态
                assert "mode" not in r, name
            finally:
                await s.__aexit__(None, None, None)
                await ctx.__aexit__(None, None, None)
    asyncio.run(_run())


def test_fix3_param_errors_never_echo_sensitive_values(std_env):
    secrets = ["sk-FAKESECRET1234567890", "FAKECOOKIE123456789", "/tmp/evil/path"]
    async def _run():
        ctx, s = await _connect(std_env["params"])
        try:
            for secret in secrets:
                # side 枚举非法（值含假 Token）→ input_error，值零明文
                err, r = await _call(s, "check_order", {"order": _order(side=secret)})
                assert err is True and r["input_error"] is True
                raw = json.dumps(r)
                assert secret not in raw, secret
                # symbol 超长（maxLength 64）→ input_error，值零明文
                err, r = await _call(s, "check_order", {"order": _order(symbol=secret + "x" * 60)})
                assert err is True and r["input_error"] is True
                assert secret not in json.dumps(r)
                # 未知字段名含假 Token（additionalProperties 路径回显检查）
                err, r = await _call(s, "check_order", {secret: 1})
                assert err is True and r["input_error"] is True
                assert secret not in json.dumps(r)
                # 未知工具名含假 Token
                err, r = await _call(s, secret, {})
                assert err is True and r["fail_closed"] is True
                assert secret not in json.dumps(r)
                # recent_decisions 未知字段含假 Token
                err, r = await _call(s, "recent_decisions", {secret: 1})
                assert err is True and r["input_error"] is True
                assert secret not in json.dumps(r)
        finally:
            await s.__aexit__(None, None, None)
            await ctx.__aexit__(None, None, None)
    asyncio.run(_run())


def test_fix4_recent_decisions_timezone_aware_ordering(tmp_path):
    # A: +08:00 → 02:00Z；B: 03:00Z。oldest-first 应为 A、B（字符串排序会给出 B、A）
    pol = _write_policy(tmp_path)
    portfolio = _write_portfolio(tmp_path)
    audit = _write_audit(tmp_path, [
        _audit_record("rec-a-00001", "2026-08-29T10:00:00+08:00", "PASS", 0),  # 02:00Z
        _audit_record("rec-b-00001", "2026-08-29T03:00:00Z", "BLOCK", 3),      # 03:00Z
        _audit_record("rec-c-00001", "2026-08-29T05:30:00+08:00", "WARN", 2),  # 21:30Z 前一日
    ])
    async def _run():
        ctx, s = await _connect(_params(pol, portfolio, audit))
        try:
            # 全部 3 条：oldest-first = C（21:30Z 前一日）→ A（02:00Z）→ B（03:00Z）
            err, r = await _call(s, "recent_decisions", {})
            assert err is False and r["count"] == 3
            assert [rec["record_id"] for rec in r["records"]] == [
                "rec-c-00001", "rec-a-00001", "rec-b-00001",
            ]
            # since=02:30Z → 只剩 B（03:00Z）
            err, r = await _call(s, "recent_decisions", {"since": "2026-08-29T02:30:00Z"})
            assert [rec["record_id"] for rec in r["records"]] == ["rec-b-00001"]
            # limit=2 → 最近两条 = B、A（时间点降序取 2，再升序）
            err, r = await _call(s, "recent_decisions", {"limit": 2})
            assert [rec["record_id"] for rec in r["records"]] == ["rec-a-00001", "rec-b-00001"]
        finally:
            await s.__aexit__(None, None, None)
            await ctx.__aexit__(None, None, None)
    asyncio.run(_run())


def test_fix4_same_timestamp_stable_order_by_record_id(tmp_path):
    # 相同时间点：record_id 作稳定次序（oldest-first 升序：id 字典序）
    pol = _write_policy(tmp_path)
    portfolio = _write_portfolio(tmp_path)
    same_ts = _ts(timedelta(seconds=-60))
    audit = _write_audit(tmp_path, [
        _audit_record("rec-z-00001", same_ts, "PASS", 0),
        _audit_record("rec-a-00001", same_ts, "BLOCK", 3),
    ])
    async def _run():
        ctx, s = await _connect(_params(pol, portfolio, audit))
        try:
            err, r = await _call(s, "recent_decisions", {})
            assert err is False
            assert [rec["record_id"] for rec in r["records"]] == ["rec-a-00001", "rec-z-00001"]
        finally:
            await s.__aexit__(None, None, None)
            await ctx.__aexit__(None, None, None)
    asyncio.run(_run())
