"""R3–R9 规则级测试：PASS / 触发 / 边界（恰好等于阈值）/ 平仓豁免 / 权益非正。

单规则测试用 make_engine([该规则]) —— 引擎自动补齐 R1/R2/R12 强制集。
"""

from tests.conftest import (
    NOW,
    fresh_order,
    fresh_portfolio,
    full_policy,
    make_engine,
    make_standard,
    option_order,
    portfolio,
    stock_order,
)
from deadlatch.rules.cash_margin_check import CashMarginCheckRule
from deadlatch.rules.max_daily_loss import MaxDailyLossRule
from deadlatch.rules.max_drawdown import MaxDrawdownRule
from deadlatch.rules.max_order_quantity import MaxOrderQuantityRule
from deadlatch.rules.max_order_value import MaxOrderValueRule
from deadlatch.rules.max_symbol_exposure import MaxSymbolExposureRule
from deadlatch.rules.max_total_exposure import MaxTotalExposureRule

POL = full_policy()
POS_LONG_100 = {
    "symbol": "AAA",
    "instrument_type": "stock",
    "side": "long",
    "quantity": 100,
    "market_value": 19000.0,
    "currency": "USD",
}


# ---------------- R3 max_order_quantity ----------------

def test_r3_pass():
    r = make_engine(POL, [MaxOrderQuantityRule()]).check(fresh_order(quantity=20), fresh_portfolio())
    assert r.exit_code == 0


def test_r3_trigger_gt_limit():
    r = make_engine(POL, [MaxOrderQuantityRule()]).check(fresh_order(quantity=501), fresh_portfolio())
    assert r.exit_code == 3
    assert any(v["rule_id"] == "max_order_quantity" for v in r.violations)


def test_r3_boundary_equal_passes():
    r = make_engine(POL, [MaxOrderQuantityRule()]).check(fresh_order(quantity=500), fresh_portfolio())
    assert r.exit_code == 0  # 恰好相等 → PASS（严格 >）


def test_r3_close_within_position_exempt():
    r = make_engine(POL, [MaxOrderQuantityRule()]).check(
        fresh_order(side="sell", quantity=80), fresh_portfolio(positions=[POS_LONG_100])
    )
    assert r.exit_code == 0  # 80 ≤ 100 多头 → close → 豁免


# ---------------- R4 max_order_value ----------------

def test_r4_pass():
    r = make_engine(POL, [MaxOrderValueRule()]).check(fresh_order(quantity=20, price=190.0), fresh_portfolio())
    assert r.exit_code == 0  # 3800 ≤ 5000


def test_r4_trigger():
    r = make_engine(POL, [MaxOrderValueRule()]).check(fresh_order(quantity=30, price=190.0), fresh_portfolio())
    assert r.exit_code == 3  # 5700 > 5000


def test_r4_boundary_equal_passes():
    r = make_engine(POL, [MaxOrderValueRule()]).check(fresh_order(quantity=25, price=200.0), fresh_portfolio())
    assert r.exit_code == 0  # 5000 == 5000 → PASS


def test_r4_option_uses_multiplier():
    # 期权：order_value = price × M × qty = 2.5 × 100 × 10 = 2500 ≤ 5000 → PASS
    r = make_engine(POL, [MaxOrderValueRule()]).check(option_order(), fresh_portfolio())
    assert r.exit_code == 0
    ev = r.evidence["rule_evidence"]["max_order_value"]
    assert any(e["name"] == "used_multiplier" and e["value"] == 100 for e in ev)


# ---------------- R5 max_symbol_exposure ----------------

def test_r5_pass():
    r = make_engine(POL, [MaxSymbolExposureRule()]).check(fresh_order(), fresh_portfolio())
    assert r.exit_code == 0  # 3800 / 123456.78 = 3.08% ≤ 10%


def test_r5_trigger_seller_option_strike_basis():
    # 卖 10 张 strike 190 看跌：Δ = 190 × 100 × 10 = 190,000（行权价口径，非权利金 2,500）
    r = make_engine(POL, [MaxSymbolExposureRule()]).check(option_order(), fresh_portfolio())
    assert r.exit_code == 3
    ev = r.evidence["rule_evidence"]["max_symbol_exposure"]
    # Decimal 精确值：190.0 × 100 × 10 = 190000.0（行权价口径，非权利金 2500）
    assert any(e["name"] == "order_delta" and e["value"] == "190000.0" for e in ev)


def test_r5_equity_zero_blocks():
    r = make_engine(POL, [MaxSymbolExposureRule()]).check(fresh_order(), fresh_portfolio(equity=0))
    assert r.exit_code == 3  # 分母非法，fail-closed


def test_r5_equity_negative_blocks():
    r = make_engine(POL, [MaxSymbolExposureRule()]).check(fresh_order(), fresh_portfolio(equity=-5000.0))
    assert r.exit_code == 3


def test_r5_close_exempt():
    r = make_engine(POL, [MaxSymbolExposureRule()]).check(
        fresh_order(side="sell", quantity=50), fresh_portfolio(positions=[POS_LONG_100])
    )
    assert r.exit_code == 0


# ---------------- R6 max_total_exposure ----------------

def test_r6_pass():
    r = make_engine(POL, [MaxTotalExposureRule()]).check(fresh_order(), fresh_portfolio())
    assert r.exit_code == 0  # 3800 / 123456.78 = 3.08% ≤ 60%


def test_r6_trigger():
    r = make_engine(POL, [MaxTotalExposureRule()]).check(
        fresh_order(quantity=1000, price=190.0), fresh_portfolio()
    )
    assert r.exit_code == 3  # 190000 / 123456.78 = 154% > 60%


def test_r6_equity_zero_blocks():
    r = make_engine(POL, [MaxTotalExposureRule()]).check(fresh_order(), fresh_portfolio(equity=0))
    assert r.exit_code == 3


# ---------------- R7 cash_margin_check ----------------

def test_r7a_pass():
    r = make_engine(POL, [CashMarginCheckRule()]).check(fresh_order(), fresh_portfolio())
    assert r.exit_code == 0  # 30000 − 3800 = 26200 ≥ 0


def test_r7a_trigger_cash_shortfall():
    r = make_engine(POL, [CashMarginCheckRule()]).check(
        fresh_order(quantity=200, price=190.0), fresh_portfolio(cash=1000.0)
    )
    assert r.exit_code == 3  # 1000 − 38000 < 0


def test_r7a_min_cash_negative_allowed():
    # min_cash = -1000（允许保证金借款）：post = 26200 ≥ -1000 → PASS
    r = make_engine(full_policy(limits={"min_cash": -1000.0}), [CashMarginCheckRule()]).check(
        fresh_order(), fresh_portfolio()
    )
    assert r.exit_code == 0


def test_r7b_trigger_short_margin():
    # 卖 10 张 strike 190 put：margin = 187,500；权益 123,456.78 × 0.35 = 43,209.87 → BLOCK
    r = make_engine(POL, [CashMarginCheckRule()]).check(option_order(), fresh_portfolio())
    assert r.exit_code == 3
    assert any(v["rule_id"] == "cash_margin_check" for v in r.violations)


def test_unproven_option_close_uses_opening_exposure_and_margin():
    """A close label without holdings must not retain close-side risk math."""
    ghost_sell = option_order(side="sell_to_close", quantity=1, price=0.01)
    pf = fresh_portfolio(equity=10000.0, cash=10000.0, positions=[])

    r5 = make_engine(POL, [MaxSymbolExposureRule()]).check(ghost_sell, pf)
    assert r5.exit_code == 3
    evidence = r5.evidence["rule_evidence"]["max_symbol_exposure"]
    assert any(e["name"] == "order_delta" and e["value"] == "19000.0" for e in evidence)

    r7 = make_engine(POL, [CashMarginCheckRule()]).check(ghost_sell, pf)
    assert r7.exit_code == 3
    evidence = r7.evidence["rule_evidence"]["cash_margin_check"]
    assert any(e["name"] == "new_short_margin" and e["value"] == "18999.00" for e in evidence)

    ghost_buy = option_order(side="buy_to_close", quantity=1, price=200.0)
    r7_buy = make_engine(POL, [CashMarginCheckRule()]).check(ghost_buy, pf)
    assert r7_buy.exit_code == 3
    evidence = r7_buy.evidence["rule_evidence"]["cash_margin_check"]
    assert any(e["name"] == "outflow" and e["value"] == "20000.0" for e in evidence)


# ---------------- R8 max_daily_loss ----------------

def test_r8_pass():
    r = make_engine(POL, [MaxDailyLossRule()]).check(fresh_order(), fresh_portfolio())
    assert r.exit_code == 0  # -1200.5 / 125000 = -0.96% > -3%


def test_r8_trigger_reaches_limit():
    # daily_pnl = -3750 → -3.0% == -limit → 达到即触发 → BLOCK
    r = make_engine(POL, [MaxDailyLossRule()]).check(
        fresh_order(), fresh_portfolio(daily_pnl=-3750.0, day_start_equity=125000.0)
    )
    assert r.exit_code == 3


def test_r8_close_exempt():
    r = make_engine(POL, [MaxDailyLossRule()]).check(
        fresh_order(side="sell", quantity=50),
        fresh_portfolio(positions=[POS_LONG_100], daily_pnl=-10000.0),
    )
    assert r.exit_code == 0  # 平仓豁免：日亏熔断不拦减仓


# ---------------- R9 max_drawdown ----------------

def test_r9_pass():
    r = make_engine(POL, [MaxDrawdownRule()]).check(fresh_order(), fresh_portfolio())
    assert r.exit_code == 0  # 3.55% < 15%


def test_r9_trigger_reaches_limit():
    # drawdown_ratio = 0.15 == limit → 达到即触发 → BLOCK
    r = make_engine(POL, [MaxDrawdownRule()]).check(
        fresh_order(), fresh_portfolio(drawdown_ratio=0.15)
    )
    assert r.exit_code == 3


def test_r9_peak_zero_blocks():
    r = make_engine(POL, [MaxDrawdownRule()]).check(
        fresh_order(), fresh_portfolio(peak_equity=0)
    )
    assert r.exit_code == 3


# ---------------- 阈值等值总览（上限类 PASS / 熔断类 BLOCK） ----------------

def test_threshold_equality_caps_pass():
    # R3/R4/R5/R6/R7 恰好等于上限 → PASS（上限包含式）
    # equity=38000 → 敞口 3800/38000 = 0.1 恰等于 ratio 上限；订单现金 3800 == 上限
    pol = full_policy(
        limits={
            "max_order_quantity": 500,
            "max_order_value": 3800.0,
            "max_symbol_exposure_ratio": 0.1,
            "max_total_exposure_ratio": 0.1,
            "min_cash": 26200.0,
        }
    )
    pf = fresh_portfolio(
        equity=38000.0, peak_equity=39398.65, drawdown_ratio=0.0355
    )  # 回撤与权益自洽（(39398.65−38000)/39398.65 ≈ 0.0355），避免 R12 自洽校验误报
    r = make_engine(
        pol,
        [
            MaxOrderQuantityRule(),
            MaxOrderValueRule(),
            MaxSymbolExposureRule(),
            MaxTotalExposureRule(),
            CashMarginCheckRule(),
        ],
    ).check(fresh_order(quantity=20, price=190.0), pf)
    assert r.exit_code == 0


def test_threshold_equality_circuit_breakers_block():
    # R8/R9 恰好达到熔断线 → BLOCK（包含式）
    pol = full_policy(limits={"max_daily_loss_ratio": 0.009604, "max_drawdown_ratio": 0.0355})
    r = make_engine(pol, [MaxDailyLossRule(), MaxDrawdownRule()]).check(
        fresh_order(), fresh_portfolio()
    )
    assert r.exit_code == 3
    assert {v["rule_id"] for v in r.violations} == {"max_daily_loss", "max_drawdown"}


# ---------------- 卖方期权反向算例（NEW-9） ----------------

def test_seller_option_r4_pass_r5_block():
    # 卖 10 张 strike 190 put，权利金 2.5：R4 现金流 2,500 ≤ 5,000 → PASS；
    # R5 敞口 190,000 > 10% 权益 → BLOCK
    r = make_engine(POL, [MaxOrderValueRule(), MaxSymbolExposureRule()]).check(
        option_order(), fresh_portfolio()
    )
    assert r.exit_code == 3
    assert any(v["rule_id"] == "max_symbol_exposure" for v in r.violations)
    assert not any(v["rule_id"] == "max_order_value" for v in r.violations)


# ---------------- -B §2.3：毛敞口永不为负（R5/R6） ----------------

def _neg_mv_long(qty=100, symbol="AAA", mv=-19000.0):
    return {"symbol": symbol, "instrument_type": "stock", "side": "long",
            "quantity": qty, "market_value": mv, "currency": "USD"}


def _short_pos(qty=50, symbol="BBB", mv=-9000.0):
    return {"symbol": symbol, "instrument_type": "stock", "side": "short",
            "quantity": qty, "market_value": mv, "currency": "USD"}


def test_r6_negative_market_value_normalized_to_abs():
    # 负 market_value：pre_total_gross 取绝对值（19000），永不为负 → PASS
    r = make_engine(POL, [MaxTotalExposureRule()]).check(
        fresh_order(), fresh_portfolio(positions=[_neg_mv_long()])
    )
    assert r.exit_code == 0
    ev = r.evidence["rule_evidence"]["max_total_exposure"]
    assert any(e["name"] == "pre_total_gross" and e["value"] == "19000.0" for e in ev)


def test_r6_long_short_mix_gross_sum():
    # 多空组合：毛敞口 = 19000 + 9000 = 28000（空头负市值取绝对值求和）
    r = make_engine(POL, [MaxTotalExposureRule()]).check(
        fresh_order(), fresh_portfolio(positions=[_neg_mv_long(), _short_pos()])
    )
    assert r.exit_code == 0  # 31800 / 123456.78 = 25.8% ≤ 60%
    ev = r.evidence["rule_evidence"]["max_total_exposure"]
    assert any(e["name"] == "pre_total_gross" and e["value"] == "28000.0" for e in ev)
    assert not any(e["name"] == "pre_total_gross" and "-" in e["value"] for e in ev)


def test_r6_threshold_boundary_equal_passes():
    # 阈值边界：post/equity 恰好等于上限 → PASS（严格 >）
    # equity 改 38000 时 peak_equity 同步自洽（(39398.65−38000)/39398.65 ≈ 0.0355），避免 R12 自洽校验误报
    pol = full_policy(limits={"max_total_exposure_ratio": 0.1})
    r = make_engine(pol, [MaxTotalExposureRule()]).check(
        fresh_order(quantity=20, price=190.0),
        fresh_portfolio(equity=38000.0, peak_equity=39398.65),
    )
    assert r.exit_code == 0  # 3800 / 38000 = 0.1 == 上限
    ev = r.evidence["rule_evidence"]["max_total_exposure"]
    assert any(e["name"] == "ratio" and e["value"] == "0.1" for e in ev)


def test_r5_negative_market_value_uses_abs():
    # R5 同口径：负市值持仓 → pre_exposure = 19000（abs），open 加仓后超限 → BLOCK
    r = make_engine(POL, [MaxSymbolExposureRule()]).check(
        fresh_order(), fresh_portfolio(positions=[_neg_mv_long()])
    )
    assert r.exit_code == 3  # 22800 / 123456.78 = 18.5% > 10%
    ev = r.evidence["rule_evidence"]["max_symbol_exposure"]
    assert any(e["name"] == "pre_exposure" and e["value"] == "19000.0" for e in ev)


def test_r5_threshold_boundary_equal_passes():
    # R5 阈值边界：post/equity 恰好等于上限 → PASS（严格 >）
    # equity 改 38000 时 peak_equity 同步自洽（见 R6 边界测试注释）
    pol = full_policy(limits={"max_symbol_exposure_ratio": 0.1})
    r = make_engine(pol, [MaxSymbolExposureRule()]).check(
        fresh_order(quantity=20, price=190.0),
        fresh_portfolio(equity=38000.0, peak_equity=39398.65),
    )
    assert r.exit_code == 0  # 3800 / 38000 = 0.1 == 上限


# ---------------- -B §2.4：陈旧快照不得获得 R3–R9 平仓豁免 ----------------

def _stale_pf(**overrides):
    from datetime import timedelta

    return fresh_portfolio(
        snapshot_at=(NOW - timedelta(seconds=3600)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **overrides,
    )


def test_r5_close_exempt_not_granted_on_stale_snapshot():
    # 新鲜快照下 sell 50 vs 多头 100 → close 豁免；陈旧快照 → open，不豁免 → R5 BLOCK
    r = make_engine(POL, [MaxSymbolExposureRule()]).check(
        fresh_order(side="sell", quantity=50),
        _stale_pf(positions=[POS_LONG_100]),
    )
    assert r.exit_code == 3
    ev = r.evidence["rule_evidence"]["max_symbol_exposure"]
    assert not any(e["name"] == "exempted" for e in ev)  # 未给豁免
    assert any(v["rule_id"] == "max_symbol_exposure" for v in r.violations)


def test_r8_close_exempt_not_granted_on_stale_snapshot():
    # 日亏已熔断（-8%）：新鲜快照下平仓豁免放行；陈旧快照 → 不豁免 → R8 BLOCK
    r = make_engine(POL, [MaxDailyLossRule()]).check(
        fresh_order(side="sell", quantity=50),
        _stale_pf(positions=[POS_LONG_100], daily_pnl=-10000.0),
    )
    assert r.exit_code == 3
    ev = r.evidence["rule_evidence"]["max_daily_loss"]
    assert not any(e["name"] == "exempted" for e in ev)
    assert any(v["rule_id"] == "max_daily_loss" for v in r.violations)


# ----------------  分支补强（input_validity / cash_margin） ----------------

def test_r2_option_strike_non_numeric_exit4():
    # R2 纵深防御：期权 strike 非数值（字符串）→ 金额有限性检查 → exit 4
    from tests.conftest import option_order

    bad = option_order(option={"underlying": "AAA", "expiry": "2026-09-18",
                               "strike": "not-a-number", "right": "put", "multiplier": 100})
    r = make_standard(POL).check(bad, fresh_portfolio(), now=NOW)
    assert r.exit_code == 4
    assert any("strike" in v["detail"] for v in r.violations)


def test_r7b_zero_equity_blocks_and_missing_multiplier_defensive():
    from deadlatch.rules.cash_margin_check import CashMarginCheckRule

    # R7b equity <= 0 → BLOCK（分母非法 fail-closed）
    r = make_engine(POL, [CashMarginCheckRule()]).check(
        fresh_order(), fresh_portfolio(equity=0), now=NOW)
    assert r.exit_code == 3
    assert any(v["rule_id"] == "cash_margin_check" for v in r.violations)
    # 期权 sell_to_open 缺 multiplier → schema 类型非法 → 输入错误 exit 4（R2 拦截，不抛异常）
    from tests.conftest import option_order

    bad = option_order(option={"underlying": "AAA", "expiry": "2026-09-18",
                               "strike": 190.0, "right": "put", "multiplier": None})
    r = make_engine(POL, [CashMarginCheckRule()]).check(bad, fresh_portfolio(), now=NOW)
    assert r.exit_code == 4  # 输入错误分诊（不抛异常、不 PASS）
