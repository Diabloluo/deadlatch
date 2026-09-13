""" §2.1 shadow 投影矩阵测试（引擎层，不经 Guard 审计）。

矩阵逐条对照：enforce 原样；shadow 普通 PASS/WARN/BLOCK → 对外 PASS/0 +
shadow_verdict；shadow × kill switch / exit 4 / exit 5 保持真实 BLOCK。
"""

from tests.conftest import (
    NOW,
    fresh_order,
    fresh_portfolio,
    make_engine,
    option_order,
    portfolio,
    stock_order,
)
from deadlatch import Policy
from deadlatch.rules.kill_switch import KillSwitchRule
from deadlatch.rules.stubs import BoomRule, ViolationRule, WarnRule

POS_LONG_100 = {
    "symbol": "AAA",
    "instrument_type": "stock",
    "side": "long",
    "quantity": 100,
    "market_value": 19000.0,
    "currency": "USD",
}


def _shadow_policy(**overrides) -> Policy:
    from tests.conftest import _policy

    return Policy.from_dict(_policy(mode="shadow", **overrides))


# ---------------- enforce：完全回归（ 行为不变，shadow_verdict=None） ----------------

def test_enforce_pass_unchanged(policy_off, order_buy):
    r = make_engine(policy_off, []).check(order_buy, portfolio(), now=NOW)
    assert (r.decision, r.exit_code) == ("PASS", 0)
    assert r.shadow_mode is False
    assert r.shadow_verdict is None


def test_enforce_warn_unchanged(policy_off, order_buy):
    r = make_engine(policy_off, [WarnRule()]).check(order_buy, portfolio(), now=NOW)
    assert (r.decision, r.exit_code) == ("WARN", 2)
    assert r.shadow_verdict is None


def test_enforce_block_unchanged(policy_off, order_buy):
    r = make_engine(policy_off, [ViolationRule()]).check(order_buy, portfolio(), now=NOW)
    assert (r.decision, r.exit_code) == ("BLOCK", 3)
    assert r.shadow_verdict is None


def test_enforce_exit4_unchanged(policy_off, order_buy):
    r = make_engine(policy_off, []).check(stock_order(currency="HKD"), portfolio(), now=NOW)
    assert (r.decision, r.exit_code) == ("BLOCK", 4)
    assert r.shadow_verdict is None


def test_enforce_exit5_unchanged(policy_off, order_buy):
    r = make_engine(policy_off, [BoomRule()]).check(order_buy, portfolio(), now=NOW)
    assert (r.decision, r.exit_code) == ("BLOCK", 5)
    assert r.shadow_verdict is None


# ---------------- shadow：普通 PASS/WARN/BLOCK 投影 ----------------

def test_shadow_plain_pass_projected(policy_off, order_buy):
    r = make_engine(_shadow_policy(), []).check(order_buy, portfolio(), now=NOW)
    assert (r.decision, r.exit_code) == ("PASS", 0)
    assert r.shadow_mode is True
    assert r.shadow_verdict == "PASS"


def test_shadow_plain_warn_projected(policy_off, order_buy):
    r = make_engine(_shadow_policy(), [WarnRule()]).check(order_buy, portfolio(), now=NOW)
    assert (r.decision, r.exit_code) == ("PASS", 0)  # 对外 PASS
    assert r.shadow_verdict == "WARN"  # 内部裁决保留
    assert any(w["rule_id"] == "stub_warn" for w in r.warnings)  # 命中未丢失


def test_shadow_plain_block_projected(policy_off, order_buy):
    r = make_engine(_shadow_policy(), [ViolationRule()]).check(order_buy, portfolio(), now=NOW)
    assert (r.decision, r.exit_code) == ("PASS", 0)  # 对外 PASS
    assert r.shadow_verdict == "BLOCK"
    assert any(v["rule_id"] == "stub_violation" for v in r.violations)  # 内部命中保留


# ---------------- shadow × kill switch（唯一例外，不得借 shadow 绕过） ----------------

def test_shadow_kill_full_blocks(policy_off, order_buy):
    r = make_engine(_shadow_policy(kill_switch="full"), [KillSwitchRule()]).check(
        order_buy, portfolio(), now=NOW
    )
    assert (r.decision, r.exit_code) == ("BLOCK", 3)
    assert r.shadow_verdict == "BLOCK"


def test_shadow_kill_full_blocks_close_too(policy_off):
    close = stock_order(side="sell", quantity=50)
    r = make_engine(_shadow_policy(kill_switch="full"), [KillSwitchRule()]).check(
        close, portfolio(positions=[POS_LONG_100]), now=NOW
    )
    assert (r.decision, r.exit_code) == ("BLOCK", 3)  # full 含平仓仍拦截
    assert r.shadow_verdict == "BLOCK"


def test_shadow_kill_reduce_only_open_blocks(policy_off, order_buy):
    # 无持仓 buy → 推断 open → shadow 下仍 BLOCK
    r = make_engine(_shadow_policy(kill_switch="reduce_only"), [KillSwitchRule()]).check(
        order_buy, portfolio(), now=NOW
    )
    assert (r.decision, r.exit_code) == ("BLOCK", 3)
    assert r.shadow_verdict == "BLOCK"


def test_shadow_kill_reduce_only_close_passes(policy_off):
    # 多头 100 卖 50 → close → R1 放行 → 其余规则 PASS → 投影 PASS/0
    close = stock_order(side="sell", quantity=50)
    r = make_engine(_shadow_policy(kill_switch="reduce_only"), [KillSwitchRule()]).check(
        close, portfolio(positions=[POS_LONG_100]), now=NOW
    )
    assert (r.decision, r.exit_code) == ("PASS", 0)
    assert r.shadow_verdict == "PASS"


def test_shadow_kill_reduce_only_option_close_passes(policy_off):
    close = option_order(side="buy_to_close", option={
        "underlying": "AAA", "expiry": "2026-09-18", "strike": 190.0,
        "right": "put", "multiplier": 100,
    })
    short_option = {
        "symbol": "AAA 260918P00190000", "instrument_type": "option",
        "side": "short", "quantity": 10, "market_value": 2500.0,
        "currency": "USD", "option": {
            "underlying": "AAA", "expiry": "2026-09-18", "strike": 190.0,
            "right": "put", "multiplier": 100,
        },
    }
    r = make_engine(_shadow_policy(kill_switch="reduce_only"), [KillSwitchRule()]).check(
        close, portfolio(positions=[short_option]), now=NOW
    )
    assert (r.decision, r.exit_code) == ("PASS", 0)
    assert r.shadow_verdict == "PASS"


# ---------------- shadow × exit 4/5（不得投影成 PASS） ----------------

def test_shadow_exit4_not_projected(policy_off):
    r = make_engine(_shadow_policy(), []).check(
        stock_order(currency="HKD"), portfolio(), now=NOW
    )
    assert (r.decision, r.exit_code) == ("BLOCK", 4)  # shadow 不吞调用方错误
    assert r.shadow_verdict == "BLOCK"


def test_shadow_exit5_not_projected(policy_off, order_buy):
    r = make_engine(_shadow_policy(), [BoomRule()]).check(order_buy, portfolio(), now=NOW)
    assert (r.decision, r.exit_code) == ("BLOCK", 5)  # shadow 不得制造错误 PASS
    assert r.shadow_verdict == "BLOCK"


# ---------------- shadow：内部命中必须可用于审计/报告（对外 PASS 不丢 evidence） ----------------

def test_shadow_projected_pass_keeps_rule_evidence(policy_off, order_buy):
    r = make_engine(_shadow_policy(), [ViolationRule()]).check(order_buy, portfolio(), now=NOW)
    assert r.exit_code == 0
    assert "stub_violation" in r.evidence["rule_evidence"]  # evidence 未因投影丢失
    assert r.evidence["input_hash"]
