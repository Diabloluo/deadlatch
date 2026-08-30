""" §3 边界、时间与 Decimal 专项（T3/T7/T10）。

- R3–R9 阈值低于/等于/高于一单位（Decimal 字符串构造预期值，不经二进制 float）；
- 0.1+0.2 类临界反例、极大小数、等价字符串表示 → 相同 decision/exit/hits/input_hash；
- 时间：固定注入 now；Z 与 ±offset 同一时间点、跨日、闰日、DST 表示、无时区、
  非法日期、未来时间；R10 299/300/301 秒；R11 未来/陈旧边界。
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tests.conftest import (
    NOW,
    fresh_order,
    fresh_portfolio,
    full_policy,
    make_engine,
    make_standard,
)
from deadlatch.exposure import order_cash_value
from deadlatch.rules.max_daily_loss import MaxDailyLossRule
from deadlatch.rules.max_drawdown import MaxDrawdownRule
from deadlatch.rules.max_order_quantity import MaxOrderQuantityRule
from deadlatch.rules.max_order_value import MaxOrderValueRule
from deadlatch.rules.max_symbol_exposure import MaxSymbolExposureRule
from deadlatch.rules.max_total_exposure import MaxTotalExposureRule

POL = full_policy()
POS_LONG_100 = {"symbol": "AAA", "instrument_type": "stock", "side": "long",
                "quantity": 100, "market_value": 19000.0, "currency": "USD"}


# ---------------- 数值边界：低于/等于/高于一单位（Decimal 字符串构造） ----------------

def _ts(offset_seconds: int) -> str:
    return (NOW + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_r3_quantity_boundary_one_unit():
    # limit=500：499（低于一单位 PASS）、500（等于 PASS）、501（高于一单位 BLOCK）
    for qty, expected in ((499, 0), (500, 0), (501, 3)):
        r = make_engine(POL, [MaxOrderQuantityRule()]).check(
            fresh_order(quantity=qty), fresh_portfolio(), now=NOW)
        assert r.exit_code == expected, qty


def test_r4_value_boundary_one_unit_decimal():
    # limit=5000：order value 用 Decimal 字符串构造（19.5×...）——精确等值边界
    limit = Decimal("5000")
    unit = Decimal("1")
    # value = price×qty；构造 price 使 value 恰为 5000 与 4999/5001
    for value, expected in ((limit - unit, 0), (limit, 0), (limit + unit, 3)):
        price = value / Decimal("20")  # qty=20
        r = make_engine(POL, [MaxOrderValueRule()]).check(
            fresh_order(quantity=20, price=float(price)), fresh_portfolio(), now=NOW)
        assert r.exit_code == expected, value


def test_r5_r6_ratio_boundary_decimal():
    # equity=38000、limit=0.1：敞口 3800 == 上限 → PASS；3799/3801 边界
    pol = full_policy(limits={"max_symbol_exposure_ratio": 0.1, "max_total_exposure_ratio": 0.1})
    for rule_cls, qty, expected in (
        (MaxSymbolExposureRule, 19, 0),   # 3610/38000 < 0.1
        (MaxSymbolExposureRule, 20, 0),   # 3800/38000 == 0.1 → PASS
        (MaxSymbolExposureRule, 21, 3),   # 3990/38000 > 0.1
        (MaxTotalExposureRule, 20, 0),
        (MaxTotalExposureRule, 21, 3),
    ):
        r = make_engine(pol, [rule_cls()]).check(
            fresh_order(quantity=qty, price=190.0),
            fresh_portfolio(equity=38000.0, peak_equity=39398.65), now=NOW)
        assert r.exit_code == expected, (rule_cls.__name__, qty)


def test_r8_r9_circuit_breaker_one_unit():
    pol = full_policy()
    # R8：daily_loss_ratio 恰达 -3%（-3750/125000）→ BLOCK；-3749.99 → PASS
    r = make_engine(pol, [MaxDailyLossRule()]).check(
        fresh_order(), fresh_portfolio(daily_pnl=-3749.99), now=NOW)
    assert r.exit_code == 0
    r = make_engine(pol, [MaxDailyLossRule()]).check(
        fresh_order(), fresh_portfolio(daily_pnl=-3750.0), now=NOW)
    assert r.exit_code == 3
    # R9：drawdown_ratio 恰达 0.15 → BLOCK；0.14999 → PASS（peak 与 drawdown 自洽，避开 R12 自洽校验）
    r = make_engine(pol, [MaxDrawdownRule()]).check(
        fresh_order(), fresh_portfolio(drawdown_ratio=0.14999, peak_equity=145239.7), now=NOW)
    assert r.exit_code == 0
    r = make_engine(pol, [MaxDrawdownRule()]).check(
        fresh_order(), fresh_portfolio(drawdown_ratio=0.15, peak_equity=145243.3), now=NOW)
    assert r.exit_code == 3


def test_extreme_and_nonpositive_values():
    pol = full_policy()
    # 极大有限值 → 触发 BLOCK（不溢出、不异常）
    r = make_engine(pol, [MaxOrderValueRule()]).check(
        fresh_order(quantity=10**9, price=10**9), fresh_portfolio(), now=NOW)
    assert r.exit_code == 3
    # 非正权益 → R5/R6 BLOCK（分母非法 fail-closed）
    for rule_cls in (MaxSymbolExposureRule, MaxTotalExposureRule):
        r = make_engine(pol, [rule_cls()]).check(
            fresh_order(), fresh_portfolio(equity=0), now=NOW)
        assert r.exit_code == 3
        r = make_engine(pol, [rule_cls()]).check(
            fresh_order(), fresh_portfolio(equity=-5000.0), now=NOW)
        assert r.exit_code == 3
    # 零分母：day_start_equity=0 → R12 exit 3
    r = make_standard(pol).check(fresh_order(), fresh_portfolio(day_start_equity=0), now=NOW)
    assert r.exit_code == 3
    # 零 quantity/price → 输入错误 exit 4
    r = make_standard(pol).check(fresh_order(quantity=0), fresh_portfolio(), now=NOW)
    assert r.exit_code == 4
    r = make_standard(pol).check(fresh_order(price=0), fresh_portfolio(), now=NOW)
    assert r.exit_code == 4
    # 负 quantity/price → exit 4
    r = make_standard(pol).check(fresh_order(quantity=-5), fresh_portfolio(), now=NOW)
    assert r.exit_code == 4


def test_seller_option_strike_basis_not_premium():
    # 卖方期权仍按 strike×M×qty（不回退权利金口径）：190×100×10=190000
    from tests.conftest import option_order

    r = make_engine(POL, [MaxSymbolExposureRule()]).check(
        option_order(quantity=10, price=2.5), fresh_portfolio(), now=NOW)
    assert r.exit_code == 3
    ev = r.evidence["rule_evidence"]["max_symbol_exposure"]
    assert any(e["name"] == "order_delta" and e["value"] == "190000.0" for e in ev)


# ---------------- Decimal 精度 ----------------

def test_01_plus_02_critical_equality():
    # price=0.1 qty=3 → value 0.3；limit=0.3：float 会误判 0.30000000000000004 > 0.3，
    # Decimal(str(0.1))*3 == 0.3 → 恰好等于 → PASS（严格 >）
    pol = full_policy(limits={"max_order_value": 0.3})
    r = make_engine(pol, [MaxOrderValueRule()]).check(
        fresh_order(quantity=3, price=0.1), fresh_portfolio(), now=NOW)
    assert r.exit_code == 0
    ev = r.evidence["rule_evidence"]["max_order_value"]
    assert any(e["name"] == "order_value" and e["value"] == "0.3" for e in ev)
    # 尾差忠实性：0.1+1e-16 是不同 float 值（str 为 "0.10000000000000002"）→
    # Decimal ×3 = 0.30000000000000006 > 0.3 → BLOCK（引擎忠实反映，不误 PASS）
    r2 = make_engine(pol, [MaxOrderValueRule()]).check(
        fresh_order(quantity=3, price=0.1 + 1e-16), fresh_portfolio(), now=NOW)
    assert r2.exit_code == 3
    # 0.1+0.2 类临界：float 0.1+0.2 的 str 是 0.30000000000000004 → 严格 > 0.3 → BLOCK
    r3 = make_engine(pol, [MaxOrderValueRule()]).check(
        fresh_order(quantity=3, price=(0.1 + 0.2) / 3), fresh_portfolio(), now=NOW)
    assert r3.exit_code == 3


def test_equivalent_string_representations_same_result():
    pol = full_policy(limits={"max_order_value": 0.3})
    # 数值等价表示（0.1 / 0.10 / 1e-1）：Decimal 规范化后同一输入 → 相同结果
    inputs = [
        fresh_order(quantity=3, price=0.1),
        fresh_order(quantity=3, price=0.10),
        fresh_order(quantity=3, price=1e-1),
    ]
    first = None
    for order in inputs:
        r = make_engine(pol, [MaxOrderValueRule()]).check(order, fresh_portfolio(), now=NOW)
        blob = (r.decision, r.exit_code,
                tuple(json_dumps_fallback(v) for v in r.violations),
                r.evidence["input_hash"])
        if first is None:
            first = blob
        assert blob == first  # 同一规范化输入 → 相同 decision/exit/hits/hash
    # 字符串表示不是 JSON number → 输入错误 exit 4（分诊正确，不猜测类型）
    r = make_standard(pol).check(fresh_order(quantity=3, price="0.1"), fresh_portfolio(), now=NOW)
    assert r.exit_code == 4


def json_dumps_fallback(v):
    import json

    return json.dumps(v, sort_keys=True, default=str)


def test_extreme_decimal_no_overflow():
    # 极大有限值：JSON number 摄取（int 大整数），Decimal 精确比较无溢出/负值/异常
    pol = full_policy(limits={"max_order_value": 10**30})
    r = make_engine(pol, [MaxOrderValueRule()]).check(
        fresh_order(quantity=10**15, price=10**15), fresh_portfolio(), now=NOW)
    assert r.exit_code == 0  # 10^15 × 10^15 = 10^30 == 上限（严格 > 才触发）
    # 超过上限一个单位 → BLOCK（比较仍精确）
    pol2 = full_policy(limits={"max_order_value": 10**30 - 1})
    r2 = make_engine(pol2, [MaxOrderValueRule()]).check(
        fresh_order(quantity=10**15, price=10**15), fresh_portfolio(), now=NOW)
    assert r2.exit_code == 3


def test_order_cash_value_decimal_exact():
    v = order_cash_value(fresh_order(quantity=3, price=0.1))
    assert v == Decimal("0.3")
    assert v != float(0.1) * 3  # 不经二进制 float 计算


# ---------------- 时间与时区 ----------------

def test_same_instant_offsets_equivalent():
    # Z 与 +08:00 表示同一时间点 → R10/R11 判定一致
    pol = full_policy()
    same_instant = [
        ("2026-08-29T10:00:00Z", "2026-08-29T18:00:00+08:00"),
        ("2026-08-29T10:00:00Z", "2026-08-29T02:00:00-08:00"),
    ]
    for created_z, created_off in same_instant:
        r_z = make_standard(pol).check(
            fresh_order(created_at=created_z), fresh_portfolio(
                snapshot_at="2026-08-29T09:59:00Z"), now=NOW)
        r_off = make_standard(pol).check(
            fresh_order(created_at=created_off), fresh_portfolio(
                snapshot_at="2026-08-29T09:59:00Z"), now=NOW)
        assert (r_z.decision, r_z.exit_code) == (r_off.decision, r_off.exit_code)


def test_cross_day_leap_day_and_dst_representations():
    pol = full_policy()
    now = datetime(2026, 3, 2, 10, 0, 0, tzinfo=timezone.utc)  # 跨日/跨月参考
    cases = [
        # 跨日：昨天 23:00Z（age 11h > 300s → BLOCK）
        (datetime(2026, 3, 1, 23, 0, tzinfo=timezone.utc), 3),
        # 闰日：2024-02-29 合法日期（远旧 → BLOCK）
        (datetime(2024, 2, 29, 10, 0, tzinfo=timezone.utc), 3),
        # DST 表示：+02:00（CEST）与 Z 同一时刻
        (datetime(2026, 3, 2, 12, 0, tzinfo=timezone(timedelta(hours=2))), 0),  # == 10:00Z 新鲜
    ]
    for dt, expected in cases:
        created = dt.strftime("%Y-%m-%dT%H:%M:%S%z")
        created = created[:-2] + ":" + created[-2:]  # +0200 → +02:00
        r = make_standard(pol).check(
            fresh_order(created_at=created),
            fresh_portfolio(snapshot_at="2026-03-02T09:59:00Z"), now=now)
        assert r.exit_code == expected, dt


def test_naive_time_rejected_and_invalid_date_fail_closed():
    pol = full_policy()
    # 无时区 → schema pattern 拒绝 → exit 4
    r = make_standard(pol).check(
        fresh_order(created_at="2026-08-29T10:00:00"), fresh_portfolio(), now=NOW)
    assert r.exit_code == 4
    # 非法日期（pattern 合法但 fromisoformat 失败）→ R10 无法解析 → R12 exit 3
    r = make_standard(pol).check(
        fresh_order(created_at="2026-02-30T10:00:00Z"), fresh_portfolio(), now=NOW)
    assert r.exit_code == 3


def test_r10_future_skew_299_300_301():
    pol = full_policy()
    from deadlatch.rules.order_time_validity import OrderTimeValidityRule

    for skew, expected in ((299, 0), (300, 0), (301, 3)):
        created = (NOW + timedelta(seconds=skew)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = make_engine(pol, [OrderTimeValidityRule()]).check(
            fresh_order(created_at=created), fresh_portfolio(), now=NOW)
        assert r.exit_code == expected, skew


def test_r11_future_and_stale_boundaries():
    pol = full_policy()
    from deadlatch.rules.data_freshness import DataFreshnessRule

    # 未来快照 → BLOCK（不得 stale=false 成功）
    future = (NOW + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = make_engine(pol, [DataFreshnessRule()]).check(
        fresh_order(), fresh_portfolio(snapshot_at=future), now=NOW)
    assert r.exit_code == 3
    # 陈旧边界：age == 300 → PASS；301 → BLOCK
    for age, expected in ((300, 0), (301, 3)):
        snap = (NOW - timedelta(seconds=age)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = make_engine(pol, [DataFreshnessRule()]).check(
            fresh_order(), fresh_portfolio(snapshot_at=snap), now=NOW)
        assert r.exit_code == expected, age


def test_extreme_timezone_offset():
    pol = full_policy()
    # 极端时区偏移 +14:00（合法 RFC3339）
    created = "2026-08-29T23:00:00+14:00"  # == 09:00Z（NOW-3601s → R10 BLOCK）
    r = make_standard(pol).check(
        fresh_order(created_at=created),
        fresh_portfolio(snapshot_at="2026-08-29T09:59:00Z"), now=NOW)
    assert r.exit_code == 3
    # 非法偏移 +25:00 → schema pattern 通过但无法解析 → R10 fail-closed BLOCK（exit 3）
    r = make_standard(pol).check(
        fresh_order(created_at="2026-08-29T10:00:00+25:00"), fresh_portfolio(), now=NOW)
    assert r.exit_code == 3
