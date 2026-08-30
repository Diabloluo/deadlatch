"""exposure.py 全分支单测（核心覆盖率补充； 全包 ≥90% 门槛）。"""

from decimal import Decimal

from deadlatch.exposure import (
    business_int,
    business_number,
    option_multiplier,
    order_cash_value,
    order_exposure_delta,
    position_exposure,
    position_group_key,
    short_option_margin,
)
from tests.conftest import fresh_order, option_order, stock_order


def _stock_pos(**kw):
    d = {"symbol": "AAA", "instrument_type": "stock", "side": "long",
         "quantity": 100, "market_value": 19000.0, "currency": "USD"}
    d.update(kw)
    return d


def _opt_pos(**kw):
    d = {"symbol": "AAA 260918P00190000", "instrument_type": "option", "side": "short",
         "quantity": 10, "market_value": 2500.0, "currency": "USD",
         "option": {"underlying": "AAA", "expiry": "2026-09-18", "strike": 190.0,
                    "right": "put", "multiplier": 100}}
    d.update(kw)
    return d


# ---------------- business_number / business_int ----------------

def test_business_number_accepts_json_numbers():
    assert business_number(190.0) == Decimal("190.0")
    assert business_number(20) == Decimal("20")
    assert business_number(0) == Decimal("0")
    assert business_number(-5.5) == Decimal("-5.5")


def test_business_number_rejects_non_numbers():
    assert business_number(None) is None
    assert business_number(True) is None  # bool 不是业务数值
    assert business_number(False) is None
    assert business_number("190") is None  # 字符串不是 JSON number
    assert business_number(float("nan")) is None
    assert business_number(float("inf")) is None


def test_business_int_rules():
    assert business_int(5) == 5
    assert business_int(0) is None  # ≤0 非法
    assert business_int(-3) is None
    assert business_int(2.5) is None  # 非整数
    assert business_int(True) is None
    assert business_int(None) is None
    assert business_int("5") is None


# ---------------- position_group_key ----------------

def test_group_key_stock_by_symbol_option_by_underlying():
    assert position_group_key(_stock_pos()) == "AAA"
    assert position_group_key(_opt_pos()) == "AAA"  # 期权按 underlying
    assert position_group_key(_opt_pos(option=None)) == "AAA 260918P00190000"  # 无 option → symbol
    assert position_group_key(_opt_pos(option={"underlying": ""})) == "AAA 260918P00190000"
    assert position_group_key(123) == ""  # 非 dict → 空键（调用方防崩溃）
    assert position_group_key({}) == ""


# ---------------- position_exposure（§0.1 持仓敞口表 + abs 规范化） ----------------

def test_stock_exposure_abs_negative_mv():
    assert position_exposure(_stock_pos(market_value=-19000.0)) == Decimal("19000.0")


def test_stock_exposure_missing_mv_none():
    assert position_exposure(_stock_pos(market_value=None)) is None
    assert position_exposure(_stock_pos()) == Decimal("19000.0")


def test_option_short_exposure_strike_basis():
    # 裸卖：strike × M × qty = 190 × 100 × 10 = 190000
    assert position_exposure(_opt_pos()) == Decimal("190000.0")


def test_option_short_missing_strike_none():
    pos = _opt_pos()
    pos["option"] = {"underlying": "AAA", "expiry": "2026-09-18", "strike": None,
                     "right": "put", "multiplier": 100}
    assert position_exposure(pos) is None


def test_option_long_exposure_avg_cost_or_mv():
    long_pos = _opt_pos(side="long", avg_cost=2.5)
    assert position_exposure(long_pos) == Decimal("2500.0")  # avg_cost × M × qty
    long_no_cost = _opt_pos(side="long")  # 无 avg_cost → 退化 market_value abs
    assert position_exposure(long_no_cost) == Decimal("2500.0")
    neg = _opt_pos(side="long", avg_cost=-2.5)
    assert position_exposure(neg) == Decimal("2500.0")  # 负 avg_cost → abs


def test_position_exposure_bad_inputs():
    assert position_exposure(None) is None
    assert position_exposure("x") is None
    assert position_exposure(_stock_pos(quantity="x")) is None
    assert position_exposure({"instrument_type": "future", "quantity": 5}) is None
    assert position_exposure(_opt_pos(quantity=0)) is None
    assert position_exposure(_opt_pos(option={"underlying": "AAA", "multiplier": None})) is None


# ---------------- option_multiplier / order_cash_value ----------------

def test_option_multiplier_only_for_options():
    assert option_multiplier(stock_order()) is None
    assert option_multiplier(option_order()) == 100
    assert option_multiplier(option_order(option={"multiplier": "x"})) is None
    assert option_multiplier(option_order(option={"multiplier": -1})) is None


def test_order_cash_value_stock_and_option():
    assert order_cash_value(fresh_order(quantity=20, price=190.0)) == Decimal("3800.0")
    assert order_cash_value(option_order(quantity=10, price=2.5)) == Decimal("2500.0")
    assert order_cash_value(fresh_order(price=None)) is None
    assert order_cash_value(fresh_order(quantity=0)) is None
    assert order_cash_value(option_order(option={"multiplier": None})) is None


# ---------------- order_exposure_delta（§0.1 方向表） ----------------

def test_stock_delta_open_close():
    o = fresh_order(side="buy", quantity=20, price=190.0)
    assert order_exposure_delta(o, "open") == Decimal("3800.0")
    assert order_exposure_delta(o, "close") == Decimal("-3800.0")
    assert order_exposure_delta(fresh_order(price=None), "open") is None
    assert order_exposure_delta(fresh_order(quantity="x"), "open") is None


def test_option_delta_four_sides():
    opt = option_order
    bto = opt(side="buy_to_open", quantity=10, price=2.5)
    sto = opt(side="sell_to_open", quantity=10, price=2.5)
    btc = opt(side="buy_to_close", quantity=10, price=2.5)
    stc = opt(side="sell_to_close", quantity=10, price=2.5)
    assert order_exposure_delta(bto, "open") == Decimal("2500.0")   # price×M×qty
    assert order_exposure_delta(sto, "open") == Decimal("190000.0")  # strike×M×qty（行权价口径）
    assert order_exposure_delta(btc, "open") == Decimal("-190000.0")  # −strike×M×qty
    assert order_exposure_delta(stc, "open") == Decimal("-2500.0")   # −price×M×qty
    assert order_exposure_delta(opt(side="bad_side"), "open") is None
    assert order_exposure_delta(opt(option={"strike": None}), "open") is None


# ---------------- short_option_margin ----------------

def test_short_option_margin_with_and_without_premium():
    assert short_option_margin(Decimal("190"), 100, 10, None) == Decimal("190000.0")
    assert short_option_margin(Decimal("190"), 100, 10, Decimal("2.5")) == Decimal("187500.0")
    # 权利金过大 → 下限 0
    assert short_option_margin(Decimal("190"), 100, 10, Decimal("500")) == Decimal("0")
    assert short_option_margin(Decimal("10"), 1, 1, Decimal("0")) == Decimal("10.0")
