"""方向推断测试：正股六组合 + 期权快照校验 + 不确定→open。

infer_order_direction / is_close_order 为纯函数，直接单测。
-B §2.4：快照陈旧/不可解析/未来 → fail-closed open（不得产生可信 close）。
"""

from datetime import timedelta

import pytest

from tests.conftest import NOW, fresh_order, fresh_portfolio, full_policy, option_order
from deadlatch.direction import infer_order_direction, is_close_order

POL = full_policy()


def _pf(positions):
    return fresh_portfolio(positions=positions)


def _stale_pf(positions=None, **overrides):
    """陈旧快照（snapshot_at = NOW − 3600s，超过 300s 新鲜窗口）。"""
    return fresh_portfolio(
        snapshot_at=(NOW - timedelta(seconds=3600)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        positions=positions,
        **overrides,
    )


def _long(qty):
    return {"symbol": "AAA", "instrument_type": "stock", "side": "long",
            "quantity": qty, "market_value": 19000.0, "currency": "USD"}


def _short(qty):
    return {"symbol": "AAA", "instrument_type": "stock", "side": "short",
            "quantity": qty, "market_value": -19000.0, "currency": "USD"}


def _option_position(side, qty, **option_overrides):
    option = {
        "underlying": "AAA", "expiry": "2026-09-18", "strike": 190.0,
        "right": "put", "multiplier": 100,
    }
    option.update(option_overrides)
    return {
        "symbol": "AAA 260918P00190000", "instrument_type": "option",
        "side": side, "quantity": qty, "market_value": 2500.0,
        "currency": "USD", "option": option,
    }


# ---- 正股六组合 ----

def test_buy_with_short_within_close():
    assert infer_order_direction(fresh_order(side="buy", quantity=50), _pf([_short(100)])) == "close"


def test_buy_with_short_over_open():
    assert infer_order_direction(fresh_order(side="buy", quantity=150), _pf([_short(100)])) == "open"


def test_buy_flat_open():
    assert infer_order_direction(fresh_order(side="buy", quantity=20), _pf([])) == "open"


def test_buy_with_long_open():
    assert infer_order_direction(fresh_order(side="buy", quantity=20), _pf([_long(100)])) == "open"


def test_sell_with_long_within_close():
    assert infer_order_direction(fresh_order(side="sell", quantity=50), _pf([_long(100)])) == "close"


def test_sell_with_long_over_open():
    assert infer_order_direction(fresh_order(side="sell", quantity=150), _pf([_long(100)])) == "open"


def test_sell_flat_open():
    assert infer_order_direction(fresh_order(side="sell", quantity=20), _pf([])) == "open"


def test_sell_with_short_open():
    assert infer_order_direction(fresh_order(side="sell", quantity=20), _pf([_short(100)])) == "open"


# ---- 不确定 → open（fail-closed）----

def test_ambiguous_position_data_open():
    bad = {"symbol": "AAA", "instrument_type": "stock", "side": "long",
           "quantity": "x", "market_value": 19000.0, "currency": "USD"}
    assert infer_order_direction(fresh_order(side="sell", quantity=20), _pf([bad])) == "open"


# ---- 期权：四值 side 只是意图，close 必须由快照持仓证明 ----

def test_option_four_directions():
    assert infer_order_direction(
        option_order(side="buy_to_open"), _pf([])
    ) == "open"
    assert infer_order_direction(
        option_order(side="sell_to_open"), _pf([])
    ) == "open"
    assert infer_order_direction(
        option_order(side="buy_to_close"), _pf([])
    ) == "open"
    assert infer_order_direction(
        option_order(side="sell_to_close"), _pf([])
    ) == "open"
    assert infer_order_direction(
        option_order(side="buy_to_close", quantity=5),
        _pf([_option_position("short", 10)]),
    ) == "close"
    assert infer_order_direction(
        option_order(side="sell_to_close", quantity=5),
        _pf([_option_position("long", 10)]),
    ) == "close"


def test_option_claimed_close_without_position_is_open():
    assert infer_order_direction(
        option_order(side="sell_to_close", quantity=1), _pf([])
    ) == "open"


def test_option_claimed_close_with_opposite_position_is_open():
    assert infer_order_direction(
        option_order(side="sell_to_close", quantity=1),
        _pf([_option_position("short", 10)]),
    ) == "open"


def test_option_claimed_close_over_position_is_open():
    assert infer_order_direction(
        option_order(side="sell_to_close", quantity=11),
        _pf([_option_position("long", 10)]),
    ) == "open"


def test_option_contract_identity_must_match_snapshot():
    assert infer_order_direction(
        option_order(side="sell_to_close", quantity=1),
        _pf([_option_position("long", 10, strike=195.0)]),
    ) == "open"


@pytest.mark.parametrize(
    "quantity,positions",
    [
        (True, [_option_position("long", 10)]),
        (0, [_option_position("long", 10)]),
        (1, ["not-an-object"]),
        (1, [{**_option_position("long", 10), "instrument_type": "stock"}]),
        (1, [{**_option_position("long", 10), "side": "invalid"}]),
        (1, [{**_option_position("long", 10), "quantity": True}]),
        (1, [{**_option_position("long", 10), "quantity": 0}]),
    ],
)
def test_option_ambiguous_claimed_close_is_open(quantity, positions):
    assert infer_order_direction(
        option_order(side="sell_to_close", quantity=quantity), _pf(positions)
    ) == "open"


def test_option_close_can_sum_matching_positions_and_ignore_other_symbols():
    other = {**_option_position("long", 99), "symbol": "BBB 260918P00190000"}
    assert infer_order_direction(
        option_order(side="sell_to_close", quantity=10),
        _pf([other, _option_position("long", 4), _option_position("long", 6)]),
    ) == "close"


def test_is_close_order_helpers():
    assert is_close_order(fresh_order(side="sell", quantity=50), _pf([_long(100)])) is True
    assert is_close_order(fresh_order(side="buy", quantity=20), _pf([])) is False


# ---- -B §2.4：快照陈旧/不可解析/未来 → fail-closed open ----

def test_stale_snapshot_direction_open():
    # 本可推断 close（多头 100 卖 50），快照陈旧 → open
    assert infer_order_direction(
        fresh_order(side="sell", quantity=50), _stale_pf([_long(100)]), POL, NOW
    ) == "open"


def test_future_snapshot_direction_open():
    pf = fresh_portfolio(
        snapshot_at=(NOW + timedelta(seconds=3600)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        positions=[_long(100)],
    )
    assert infer_order_direction(fresh_order(side="sell", quantity=50), pf, POL, NOW) == "open"


def test_unparseable_snapshot_direction_open():
    pf = fresh_portfolio(snapshot_at="not-a-date", positions=[_long(100)])
    # 无 policy/now 也判 open（时间不可解析即不可信）
    assert infer_order_direction(fresh_order(side="sell", quantity=50), pf) == "open"


def test_missing_snapshot_direction_open():
    pf = fresh_portfolio(positions=[_long(100)])
    del pf.data["snapshot_at"]
    assert infer_order_direction(fresh_order(side="sell", quantity=50), pf, POL, NOW) == "open"


def test_stale_snapshot_option_close_becomes_open():
    # 期权四值语义同样受快照可信门约束：buy_to_close + 陈旧快照 → open
    assert infer_order_direction(
        option_order(side="buy_to_close"), _stale_pf(), POL, NOW
    ) == "open"


def test_fresh_snapshot_with_policy_still_close():
    # 门不过度收紧：新鲜快照 + policy/now → 正常 close
    assert infer_order_direction(
        fresh_order(side="sell", quantity=50), _pf([_long(100)]), POL, NOW
    ) == "close"


def test_stale_snapshot_is_close_order_false():
    # 平仓豁免入口同样不放行陈旧快照
    assert is_close_order(fresh_order(side="sell", quantity=50), _stale_pf([_long(100)]), POL, NOW) is False
