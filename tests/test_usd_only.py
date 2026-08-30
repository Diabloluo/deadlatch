""" §八 USD-only 显式拒绝：三层（库 API / CLI / MCP）币种不一致回归。"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import NOW, fresh_order, fresh_portfolio, full_policy, make_standard
from tests.test_mcp_server import _order, _params, _write_audit, _write_policy, _write_portfolio
from deadlatch.mcp_server import MCPGuardServer

REPO = Path(__file__).resolve().parents[1]

POS_USD = {"symbol": "AAA", "instrument_type": "stock", "side": "long",
           "quantity": 100, "market_value": 19000.0, "currency": "USD"}


# ---------------- 库 API ----------------

def test_lib_order_currency_variants_exit4():
    pol = full_policy()
    for bad_currency in ("usd", "USD ", " EUR", "EUR", "Usd"):
        order = fresh_order(currency=bad_currency)
        r = make_standard(pol).check(order, fresh_portfolio(), now=NOW)
        assert r.exit_code == 4, bad_currency  # 大小写/空白/未知 ISO → exit 4


def test_lib_portfolio_base_currency_variants_exit4():
    pol = full_policy()
    for bad_currency in ("usd", "EUR", " USD"):
        pf = fresh_portfolio(base_currency=bad_currency)
        r = make_standard(pol).check(fresh_order(), pf, now=NOW)
        assert r.exit_code == 4, bad_currency


def test_lib_positions_currency_mismatch_exit4():
    pol = full_policy()
    for bad_currency in ("EUR", "hkd", "GBP"):
        pf = fresh_portfolio(positions=[{**POS_USD, "currency": bad_currency}])
        r = make_standard(pol).check(fresh_order(), pf, now=NOW)
        assert r.exit_code == 4, bad_currency


def test_lib_three_way_consistency_matrix():
    pol = full_policy()
    order_usd = fresh_order()          # order=USD
    pf_all_usd = fresh_portfolio(positions=[])          # portfolio=USD, pos 空 → PASS
    r = make_standard(pol).check(order_usd, pf_all_usd, now=NOW)
    assert r.exit_code == 0
    # order mismatch 而 portfolio/positions 一致 → exit 4
    r = make_standard(pol).check(fresh_order(currency="EUR"), pf_all_usd, now=NOW)
    assert r.exit_code == 4
    # portfolio mismatch 而 order/positions 一致 → exit 4
    r = make_standard(pol).check(order_usd, fresh_portfolio(base_currency="EUR",
                                                            positions=[POS_USD]), now=NOW)
    assert r.exit_code == 4
    # positions mismatch 而 order/portfolio 一致 → exit 4
    r = make_standard(pol).check(order_usd, fresh_portfolio(positions=[{**POS_USD, "currency": "EUR"}]),
                                 now=NOW)
    assert r.exit_code == 4
    # 缺 order.currency（required）→ exit 4，不得默认 USD
    order_no_cur = fresh_order()
    del order_no_cur.data["currency"]
    r = make_standard(pol).check(order_no_cur, pf_all_usd, now=NOW)
    assert r.exit_code == 4


# ---------------- CLI ----------------

def _run_cli(*args, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "deadlatch.cli", *args],
        capture_output=True, text=True, cwd=str(cwd),
        env={"PYTHONPATH": str(REPO / "src")}, timeout=60,
    )


def test_cli_currency_mismatch_exit4(tmp_path):
    policy = _write_policy(tmp_path)
    portfolio = _write_portfolio(tmp_path)
    order_bad = tmp_path / "order_bad.json"
    order_bad.write_text(json.dumps(_order(currency="EUR")), encoding="utf-8")
    r = _run_cli("check", "--policy", str(policy), "--order", str(order_bad),
                 "--portfolio", str(portfolio), cwd=tmp_path)
    assert r.returncode == 4
    assert "非风控拦截" in r.stdout or "输入/配置错误" in r.stdout


# ---------------- MCP ----------------

def _mcp_call(srv, name, args):
    """进程内调用 MCP handler（协议级已由 test_mcp_server 覆盖）。"""
    params = type("P", (), {"name": name, "arguments": args})()
    r = asyncio.run(srv._call_tool(None, params))
    return r.is_error, json.loads(r.content[0].text)


def test_mcp_check_order_currency_mismatch_fail_closed(tmp_path):
    policy = _write_policy(tmp_path)
    portfolio = _write_portfolio(tmp_path)
    audit = _write_audit(tmp_path, [])
    srv = MCPGuardServer(str(policy), str(portfolio), str(audit))
    err, r = _mcp_call(srv, "check_order", {"order": _order(currency="EUR")})
    assert err is True and r["input_error"] is True and r["exit_code"] == 4


def test_mcp_get_account_status_currency_variant_fail_closed(tmp_path):
    policy = _write_policy(tmp_path)
    audit = _write_audit(tmp_path, [])
    for bad in ("usd", "EUR", "USD "):
        pf = _write_portfolio(tmp_path, base_currency=bad)
        srv = MCPGuardServer(str(policy), str(pf), str(audit))
        err, r = _mcp_call(srv, "get_account_status", {})
        assert err is True and r["fail_closed"] is True, bad


def test_mcp_positions_currency_mismatch_via_stdio(tmp_path):
    # 真实 stdio：positions[].currency=EUR → check_order fail-closed（exit 4 语义）
    policy = _write_policy(tmp_path)
    portfolio = _write_portfolio(tmp_path, positions=[{**POS_USD, "currency": "EUR"}])
    audit = _write_audit(tmp_path, [])
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
    from tests.test_mcp_server import _connect, _call

    async def _run():
        ctx, s = await _connect(_params(policy, portfolio, audit))
        try:
            err, r = await _call(s, "check_order", {"order": _order()})
            assert err is True and r["exit_code"] == 4
            assert r["fail_closed"] is True
        finally:
            await s.__aexit__(None, None, None)
            await ctx.__aexit__(None, None, None)
    asyncio.run(_run())


def test_core_path_no_exchange_rate_network():
    # 核心路径无汇率表/网络/货币换算：相关模块不 import 网络库
    import inspect

    import deadlatch.engine
    import deadlatch.guard
    import deadlatch.exposure

    for mod in (deadlatch.engine, deadlatch.guard, deadlatch.exposure):
        src = inspect.getsource(mod)
        assert "http" not in src.lower() and "exchange_rate" not in src.lower()
