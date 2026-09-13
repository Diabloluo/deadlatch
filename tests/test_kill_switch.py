"""R1 kill_switch 三态测试（含 reduce_only 方向推断与快照可信门）。"""

from datetime import timedelta

from deadlatch.rules.kill_switch import KillSwitchRule
from tests.conftest import NOW, make_engine, option_order, portfolio, stock_order

POS_LONG_100 = {
    "symbol": "AAA",
    "instrument_type": "stock",
    "side": "long",
    "quantity": 100,
    "market_value": 19000.0,
    "currency": "USD",
}

POS_SHORT_OPTION_10 = {
    "symbol": "AAA 260918P00190000",
    "instrument_type": "option",
    "side": "short",
    "quantity": 10,
    "market_value": 2500.0,
    "currency": "USD",
    "option": {
        "underlying": "AAA", "expiry": "2026-09-18", "strike": 190.0,
        "right": "put", "multiplier": 100,
    },
}


def _stale_pf(**overrides):
    """陈旧快照（snapshot_at = NOW − 3600s，超过 300s 新鲜窗口）。"""
    return portfolio(
        snapshot_at=(NOW - timedelta(seconds=3600)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **overrides,
    )


def test_off_passes(policy_off, order_buy):
    r = make_engine(policy_off, [KillSwitchRule()]).check(order_buy, portfolio())
    assert r.decision == "PASS"
    assert r.exit_code == 0


def test_full_blocks_open(policy_full, order_buy):
    r = make_engine(policy_full, [KillSwitchRule()]).check(order_buy, portfolio())
    assert r.decision == "BLOCK"
    assert r.exit_code == 3
    assert any(v["rule_id"] == "kill_switch" for v in r.violations)


def test_full_blocks_close_option(policy_full):
    close = option_order(side="buy_to_close", option={
        "underlying": "AAA", "expiry": "2026-09-18", "strike": 190.0,
        "right": "put", "multiplier": 100,
    })
    r = make_engine(policy_full, [KillSwitchRule()]).check(
        close, portfolio(positions=[POS_SHORT_OPTION_10])
    )
    assert r.decision == "BLOCK"  # full 下平仓也拦截
    assert r.exit_code == 3


def test_reduce_only_blocks_option_open(policy_reduce_only, order_put_sell_open):
    r = make_engine(policy_reduce_only, [KillSwitchRule()]).check(
        order_put_sell_open, portfolio()
    )
    assert r.decision == "BLOCK"
    assert r.exit_code == 3
    assert any(
        e["name"] == "order_direction" and e["value"] == "open"
        for e in r.evidence["rule_evidence"]["kill_switch"]
    )


def test_reduce_only_blocks_ghost_option_close_claim(policy_reduce_only):
    """A $1.8m option order cannot bypass R1 by claiming sell_to_close."""
    ghost = option_order(
        side="sell_to_close",
        quantity=100,
        price=180.0,
        option={
            "underlying": "ZZZ", "expiry": "2026-09-18", "strike": 180.0,
            "right": "call", "multiplier": 100,
        },
        symbol="ZZZ 260918C00180000",
    )
    result = make_engine(policy_reduce_only, [KillSwitchRule()]).check(
        ghost, portfolio(positions=[])
    )
    assert (result.decision, result.exit_code) == ("BLOCK", 3)
    assert any(v["rule_id"] == "kill_switch" for v in result.violations)
    assert any(
        e["name"] == "order_direction" and e["value"] == "open"
        for e in result.evidence["rule_evidence"]["kill_switch"]
    )


def test_reduce_only_allows_option_close(policy_reduce_only):
    close = option_order(side="buy_to_close", option={
        "underlying": "AAA", "expiry": "2026-09-18", "strike": 190.0,
        "right": "put", "multiplier": 100,
    })
    r = make_engine(policy_reduce_only, [KillSwitchRule()]).check(
        close, portfolio(positions=[POS_SHORT_OPTION_10])
    )
    assert r.decision == "PASS"  # R1 放行（其余规则继续执行是引擎默认行为）
    assert r.exit_code == 0


def test_reduce_only_blocks_stock_open_inference(policy_reduce_only):
    # 无持仓 sell → 推断 open → BLOCK
    r = make_engine(policy_reduce_only, [KillSwitchRule()]).check(
        stock_order(side="sell", quantity=20), portfolio()
    )
    assert r.decision == "BLOCK"
    assert r.exit_code == 3


def test_reduce_only_allows_stock_close_inference(policy_reduce_only):
    # 多头 100，卖出 20 → 推断 close → R1 放行
    pf = portfolio(positions=[POS_LONG_100])
    r = make_engine(policy_reduce_only, [KillSwitchRule()]).check(
        stock_order(side="sell", quantity=20), pf
    )
    assert r.decision == "PASS"
    assert r.exit_code == 0


def test_reduce_only_stock_qty_over_position_is_open(policy_reduce_only):
    # 多头 100，卖出 200 → 超量 → 整单按 open（更严格）→ BLOCK
    pf = portfolio(positions=[POS_LONG_100])
    r = make_engine(policy_reduce_only, [KillSwitchRule()]).check(
        stock_order(side="sell", quantity=200), pf
    )
    assert r.decision == "BLOCK"
    assert r.exit_code == 3


def test_shadow_mode_kill_switch_still_enforced(policy_shadow_full, order_buy):
    r = make_engine(policy_shadow_full, [KillSwitchRule()]).check(order_buy, portfolio())
    assert r.shadow_mode is True
    assert r.decision == "BLOCK"  # shadow 位开启时 kill switch 仍生效
    assert r.exit_code == 3


# ---- -B §2.4：reduce_only 不得凭陈旧/不可解析快照放行 ----

def test_reduce_only_stale_snapshot_blocks_stock_close(policy_reduce_only):
    # 多头 100 卖 50 本可推断 close，但快照陈旧 → 方向 open → BLOCK
    r = make_engine(policy_reduce_only, [KillSwitchRule()]).check(
        stock_order(side="sell", quantity=50), _stale_pf(positions=[POS_LONG_100])
    )
    assert r.decision == "BLOCK"
    assert r.exit_code == 3
    assert any(v["rule_id"] == "kill_switch" for v in r.violations)
    assert any(
        e["name"] == "order_direction" and e["value"] == "open"
        for e in r.evidence["rule_evidence"]["kill_switch"]
    )


def test_reduce_only_unparseable_snapshot_blocks(policy_reduce_only):
    # "2026-02-30T..." 符合 schema pattern 但非真实日期 → R12 exit 3，方向 open → R1 亦 BLOCK
    r = make_engine(policy_reduce_only, [KillSwitchRule()]).check(
        stock_order(side="sell", quantity=50),
        portfolio(snapshot_at="2026-02-30T10:00:00Z", positions=[POS_LONG_100]),
    )
    assert r.exit_code == 3  # 时间不可解析 → 方向 open → R1 BLOCK（R12 亦 BLOCK）
    assert any(v["rule_id"] == "kill_switch" for v in r.violations)


def test_reduce_only_stale_snapshot_blocks_option_close(policy_reduce_only):
    # 期权 buy_to_close 同样受快照可信门约束：陈旧快照 → 不放行
    close = option_order(side="buy_to_close", option={
        "underlying": "AAA", "expiry": "2026-09-18", "strike": 190.0,
        "right": "put", "multiplier": 100,
    })
    r = make_engine(policy_reduce_only, [KillSwitchRule()]).check(close, _stale_pf())
    assert r.exit_code == 3
    assert any(v["rule_id"] == "kill_switch" for v in r.violations)
