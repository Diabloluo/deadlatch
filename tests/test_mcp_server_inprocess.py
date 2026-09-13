"""mcp_server.py 进程内覆盖测试（协议级行为已由 test_mcp_server.py 经真实
subprocess stdio 验证；本文件直调 handler 使主进程 coverage 覆盖服务器代码）。

不重复协议验收——这里只为补足覆盖率与验证边界分支。
"""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from deadlatch.mcp_server import (
    MCPGuardServer,
    MCPServerError,
    _order_input_schema,
    main,
)
from tests.test_mcp_server import _audit_record, _order, _ts, _write_audit, _write_policy, _write_portfolio


def _params(name: str, arguments: dict):
    return type("P", (), {"name": name, "arguments": arguments})()


def _call(srv, name: str, arguments: dict):
    r = asyncio.run(srv._call_tool(None, _params(name, arguments)))
    return r.is_error, json.loads(r.content[0].text)


def _make(tmp_path, **pol_kw):
    policy = _write_policy(tmp_path, **pol_kw)
    portfolio = _write_portfolio(tmp_path)
    audit = _write_audit(tmp_path, [])
    return MCPGuardServer(str(policy), str(portfolio), str(audit))


def test_inprocess_check_order_matrix(tmp_path):
    srv = _make(tmp_path)
    # PASS
    err, r = _call(srv, "check_order", {"order": _order()})
    assert err is False and r["exit_code"] == 0 and r["evidence"]["inactive_rules"] == []
    # BLOCK
    err, r = _call(srv, "check_order", {"order": _order(quantity=1000)})
    assert err is False and r["exit_code"] == 3
    # exit 4 → 工具错误（input_error）
    err, r = _call(srv, "check_order", {"order": _order(currency="HKD")})
    assert err is True and r["input_error"] is True and r["exit_code"] == 4
    # 参数缺 order
    err, r = _call(srv, "check_order", {})
    assert err is True and r["input_error"] is True


def test_inprocess_validate_args_rejects_extra(tmp_path):
    srv = _make(tmp_path)
    err, r = _call(srv, "check_order", {"order": _order(), "hack": 1})
    assert err is True and r["input_error"] is True
    err, r = _call(srv, "get_account_status", {"hack": 1})
    assert err is True and r["input_error"] is True


def test_inprocess_load_portfolio_failures(tmp_path):
    policy = _write_policy(tmp_path)
    audit = _write_audit(tmp_path, [])
    # 文件缺失
    srv = MCPGuardServer(str(policy), str(tmp_path / "nope.json"), str(audit))
    err, r = _call(srv, "get_account_status", {})
    assert err is True and r["fail_closed"] is True and r["exit_code"] == 3
    # 损坏
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    srv = MCPGuardServer(str(policy), str(bad), str(audit))
    err, r = _call(srv, "get_account_status", {})
    assert err is True and r["fail_closed"] is True
    # 结构非法（额外字段 → additionalProperties）
    extra = tmp_path / "extra.json"
    extra.write_text(json.dumps({"schema_version": 3, "hack": 1}), encoding="utf-8")
    srv = MCPGuardServer(str(policy), str(extra), str(audit))
    err, r = _call(srv, "get_account_status", {})
    assert err is True and r["fail_closed"] is True


def test_inprocess_account_status_branches(tmp_path):
    srv = _make(tmp_path)
    err, r = _call(srv, "get_account_status", {})
    assert err is False and r["freshness"]["stale"] is False
    # 缺 equity → fail-closed
    policy = _write_policy(tmp_path)
    audit = _write_audit(tmp_path, [])
    pf = _write_portfolio(tmp_path)
    data = json.loads(pf.read_text())
    del data["equity"]
    pf.write_text(json.dumps(data))
    srv = MCPGuardServer(str(policy), str(pf), str(audit))
    err, r = _call(srv, "get_account_status", {})
    assert err is True and r["fail_closed"] is True
    # 快照时间不可解析 → fail-closed
    pf2 = _write_portfolio(tmp_path, snapshot_ts="2026-02-30T10:00:00Z")
    srv = MCPGuardServer(str(policy), str(pf2), str(audit))
    err, r = _call(srv, "get_account_status", {})
    assert err is True and r["fail_closed"] is True
    # positions 元素非法 → fail-closed
    pf3 = _write_portfolio(tmp_path, positions=[123])
    srv = MCPGuardServer(str(policy), str(pf3), str(audit))
    err, r = _call(srv, "get_account_status", {})
    assert err is True and r["fail_closed"] is True


def test_inprocess_kill_switch_engaged_at_from_audit(tmp_path):
    policy = _write_policy(tmp_path, kill_switch="full")
    portfolio = _write_portfolio(tmp_path)
    hit_ts = _ts(timedelta(seconds=-50))
    audit = _write_audit(tmp_path, [
        _audit_record("rec00000001", _ts(timedelta(seconds=-100)), "PASS", 0),
        _audit_record("rec00000002", hit_ts, "BLOCK", 3,
                      hits=[{"rule_id": "kill_switch", "severity": "BLOCK", "detail": "full"}]),
    ])
    srv = MCPGuardServer(str(policy), str(portfolio), str(audit))
    err, r = _call(srv, "kill_switch_status", {})
    assert err is False and r["mode"] == "full"
    assert r["engaged_at"] == hit_ts  # 从审计追溯最近命中


def test_policy_change_reloads_on_next_call_and_invalid_change_fails_closed(tmp_path):
    policy = _write_policy(tmp_path, kill_switch="off")
    portfolio = _write_portfolio(tmp_path)
    audit = _write_audit(tmp_path, [])
    srv = MCPGuardServer(str(policy), str(portfolio), str(audit))

    err, result = _call(srv, "get_policy", {})
    assert err is False and result["kill_switch"] == "off"

    _write_policy(tmp_path, kill_switch="full", version="1.0.1")
    err, result = _call(srv, "check_order", {"order": _order()})
    assert err is False and (result["decision"], result["exit_code"]) == ("BLOCK", 3)
    err, result = _call(srv, "kill_switch_status", {})
    assert err is False and result["mode"] == "full" and result["policy_version"] == "1.0.1"

    policy.write_text("kill_switch: enable\n", encoding="utf-8")
    err, result = _call(srv, "get_policy", {})
    assert err is True and result["input_error"] is True and result["exit_code"] == 4
    assert "full" not in json.dumps(result)

    _write_policy(tmp_path, kill_switch="reduce_only", version="1.0.2")
    err, result = _call(srv, "get_policy", {})
    assert err is False and result["kill_switch"] == "reduce_only"


def test_independent_kill_switch_is_uncached_and_cannot_weaken_policy(tmp_path):
    policy = _write_policy(tmp_path, kill_switch="off")
    portfolio = _write_portfolio(tmp_path)
    audit = _write_audit(tmp_path, [])
    switch = tmp_path / "kill-switch"
    switch.write_text("full", encoding="utf-8")
    before = switch.stat()
    srv = MCPGuardServer(str(policy), str(portfolio), str(audit), str(switch))

    err, result = _call(srv, "kill_switch_status", {})
    assert err is False and result["mode"] == "full"

    # Keep the same byte length and restore mtime: a stat-cache implementation
    # would miss this, but the independent switch must be read every call.
    switch.write_text("off ", encoding="utf-8")
    os.utime(switch, ns=(before.st_atime_ns, before.st_mtime_ns))
    err, result = _call(srv, "kill_switch_status", {})
    assert err is False and result["mode"] == "off"

    _write_policy(tmp_path, kill_switch="full")
    err, result = _call(srv, "kill_switch_status", {})
    assert err is False and result["mode"] == "full"  # switch=off cannot disarm policy=full

    switch.write_text("unknown", encoding="utf-8")
    err, result = _call(srv, "recent_decisions", {})
    assert err is True and result["input_error"] is True and result["exit_code"] == 4


def test_inprocess_recent_decisions_branches(tmp_path):
    policy = _write_policy(tmp_path)
    portfolio = _write_portfolio(tmp_path)
    audit = _write_audit(tmp_path, [
        _audit_record("rec00000001", _ts(timedelta(seconds=-100)), "BLOCK", 3),
        _audit_record("rec00000002", _ts(timedelta(seconds=-50)), "PASS", 0),
    ])
    srv = MCPGuardServer(str(policy), str(portfolio), str(audit))
    err, r = _call(srv, "recent_decisions", {})
    assert err is False and r["count"] == 2
    err, r = _call(srv, "recent_decisions", {"limit": 1})
    assert r["count"] == 1
    err, r = _call(srv, "recent_decisions", {"since": _ts(timedelta(seconds=-60))})
    assert r["count"] == 1
    # 非法 since → input_error
    err, r = _call(srv, "recent_decisions", {"since": "garbage"})
    assert err is True and r["input_error"] is True
    # 未知工具
    err, r = _call(srv, "no_such_tool", {})
    assert err is True and r["fail_closed"] is True and r["exit_code"] == 5


def test_inprocess_main_startup_errors(tmp_path, capsys):
    portfolio = _write_portfolio(tmp_path)
    # policy 缺失 → exit 4（stderr 诊断）
    assert main(["--policy", str(tmp_path / "nope.yaml"), "--portfolio", str(portfolio)]) == 4
    assert "error:" in capsys.readouterr().err
    # policy 非法 → exit 4
    bad_pol = tmp_path / "bad.yaml"
    bad_pol.write_text("kill_switch: enable\n", encoding="utf-8")
    assert main(["--policy", str(bad_pol), "--portfolio", str(portfolio)]) == 4
    # 缺参数 → argparse SystemExit 2
    with pytest.raises(SystemExit) as ei:
        main([])
    assert ei.value.code == 2


def test_inprocess_order_input_schema_structure():
    schema = _order_input_schema()
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["order"]
    order = schema["properties"]["order"]
    assert order["additionalProperties"] is False
    assert "schema_version" in order["properties"]  # 严格等价 order.schema.json
    assert "created_at" in order["properties"]
    assert order["required"] and "currency" in order["required"]
