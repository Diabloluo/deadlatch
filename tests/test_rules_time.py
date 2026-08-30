"""R10 / R11 时间类规则测试（UTC 纪元秒、等值 PASS、未来防护、不豁免）。

时间戳一律由 NOW（conftest 导入时刻）经 timedelta 派生，测试不随墙上时间漂移。
"""

from datetime import timedelta

from tests.conftest import NOW, fresh_order, fresh_portfolio, full_policy, make_engine
from deadlatch.rules.data_freshness import DataFreshnessRule
from deadlatch.rules.order_time_validity import OrderTimeValidityRule

POL = full_policy()


def _ts(**delta) -> str:
    return (NOW + timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_time(order, portfolio):
    return make_engine(POL, [OrderTimeValidityRule(), DataFreshnessRule()]).check(
        order, portfolio, now=NOW
    )


# ---------------- R10 ----------------

def test_r10_fresh_pass():
    assert _check_time(fresh_order(), fresh_portfolio()).exit_code == 0


def test_r10_old_block():
    order = fresh_order(created_at=_ts(seconds=-3601))  # age 3601s > 300
    r = _check_time(order, fresh_portfolio())
    assert r.exit_code == 3
    assert any(v["rule_id"] == "order_time_validity" for v in r.violations)


def test_r10_equal_limit_passes():
    order = fresh_order(created_at=_ts(seconds=-300))  # age = 300s == limit → PASS
    assert _check_time(order, fresh_portfolio()).exit_code == 0


def test_r10_future_gt300_block():
    order = fresh_order(created_at=_ts(seconds=539))  # future 539s > 300
    r = _check_time(order, fresh_portfolio())
    assert r.exit_code == 3
    assert any(e["name"] == "future_skew_seconds" for e in r.evidence["rule_evidence"]["order_time_validity"])


def test_r10_future_within300_passes():
    order = fresh_order(created_at=_ts(seconds=119))  # future 119s ≤ 300 → age=0
    assert _check_time(order, fresh_portfolio()).exit_code == 0


def test_r10_close_not_exempt():
    # 平仓订单同样不豁免（陈旧订单任何方向都不可执行）
    order = fresh_order(side="sell", quantity=50, created_at=_ts(seconds=-3601))
    pf = fresh_portfolio(
        positions=[
            {"symbol": "AAA", "instrument_type": "stock", "side": "long",
             "quantity": 100, "market_value": 19000.0, "currency": "USD"}
        ]
    )
    r = _check_time(order, pf)
    assert r.exit_code == 3


# ---------------- R11 ----------------

def test_r11_fresh_pass():
    assert _check_time(fresh_order(), fresh_portfolio()).exit_code == 0


def test_r11_stale_block():
    pf = fresh_portfolio(snapshot_at=_ts(seconds=-7201))  # age 7201s > 300
    r = _check_time(fresh_order(), pf)
    assert r.exit_code == 3
    assert any(v["rule_id"] == "data_freshness" for v in r.violations)


def test_r11_equal_limit_passes():
    pf = fresh_portfolio(snapshot_at=_ts(seconds=-300))  # age = 300s == limit → PASS
    assert _check_time(fresh_order(), pf).exit_code == 0


def test_r11_future_snapshot_block():
    pf = fresh_portfolio(snapshot_at=_ts(seconds=3600))  # 未来
    r = _check_time(fresh_order(), pf)
    assert r.exit_code == 3


def test_r11_close_not_exempt():
    order = fresh_order(side="sell", quantity=50)
    pf = fresh_portfolio(
        snapshot_at=_ts(seconds=-7201),
        positions=[
            {"symbol": "AAA", "instrument_type": "stock", "side": "long",
             "quantity": 100, "market_value": 19000.0, "currency": "USD"}
        ],
    )
    r = _check_time(order, pf)
    assert r.exit_code == 3
