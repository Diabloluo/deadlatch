"""敞口与金额口径（rules-spec.md §0.1 统一口径，R4–R7 共用）。

硬性约束：
- 一切金额/比例经 Decimal(str(value)) 摄取（as_decimal），禁止 float 参与计算；
- 期权乘数一律从输入 option.multiplier 读取，禁止硬编码 100；
- 业务数值字段（portfolio 侧）要求 JSON number（int/float、非 bool）且有限；
  缺失/null/类型非法/非有限 → 返回 None，由 R12 判定为数据缺失（exit 3）；
- 订单侧字段（order 侧）已由 R2 输入门保证合法，规则内防御性摄取。

持仓敞口（引擎自算、不信任输入）：
| 持仓 | 敞口 |
| 股票 long/short | market_value |
| 期权 long | avg_cost × M × qty（avg_cost 缺失 → market_value）|
| 期权 short | strike × M × qty（裸卖假设，保守）|

订单 Δ(order)（§0.1 方向表）：
| buy(开) | +price×qty |
| sell(开) | +price×qty |
| buy_to_open | +price×M×qty |
| sell_to_open | +strike×M×qty（行权价口径！）|
| buy_to_close | 已证实 close: -strike×M×qty；否则按 buy open: +price×M×qty |
| sell_to_close | 已证实 close: -price×M×qty；否则按 sell open: +strike×M×qty |

订单现金价值（R4，现金流口径）：price × qty（股票）；price × M × qty（期权）。
"""

from decimal import Decimal

from ._decimal import DecimalInputError, as_decimal


def business_number(value) -> Decimal | None:
    """业务数值字段摄取：JSON number（int/float、非 bool）且有限 → Decimal；否则 None。"""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return as_decimal(value)
    except DecimalInputError:
        return None


def business_int(value) -> int | None:
    """业务整数字段摄取：int、非 bool、> 0 → int；否则 None。"""
    if value is None or isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def position_group_key(position: dict) -> str:
    """归组键：正股按 symbol；期权按 option.underlying（R5 单标的口径）。"""
    if not isinstance(position, dict):
        return ""
    if position.get("instrument_type") == "option":
        opt = position.get("option") or {}
        return opt.get("underlying") or position.get("symbol") or ""
    return position.get("symbol") or ""


def option_multiplier(order) -> int | None:
    """订单期权乘数（从输入读取；禁止硬编码 100）。"""
    if order.instrument_type != "option":
        return None
    try:
        mult = int((order.option or {}).get("multiplier"))
    except (TypeError, ValueError):
        return None
    return mult if mult > 0 else None


def position_exposure(position: dict) -> Decimal | None:
    """§0.1 持仓敞口表。数据不完整（必需字段缺失/非法）→ None（R12 判定）。

    毛敞口口径：一切持仓敞口取非负值（abs），pre_total_gross 永不为负
    （-B 原单修补 §3）。
    """
    if not isinstance(position, dict):
        return None
    it = position.get("instrument_type")
    qty = business_int(position.get("quantity"))
    if qty is None:
        return None

    if it == "stock":
        mv = business_number(position.get("market_value"))
        return abs(mv) if mv is not None else None

    if it == "option":
        opt = position.get("option") or {}
        side = position.get("side")
        if side == "short":
            strike = business_number(opt.get("strike"))
            mult = business_int(opt.get("multiplier"))
            if strike is None or mult is None:
                return None
            return strike * mult * qty  # 恒非负（strike/mult/qty 均为正）
        # long：avg_cost × M × qty（avg_cost 缺失 → market_value），取绝对值
        mult = business_int(opt.get("multiplier"))
        if mult is None:
            return None
        avg_cost = business_number(position.get("avg_cost"))
        if avg_cost is not None:
            return abs(avg_cost * mult * qty)
        mv = business_number(position.get("market_value"))
        return abs(mv) if mv is not None else None

    return None


def order_cash_value(order) -> Decimal | None:
    """R4 口径：单笔订单现金流转金额。股票=price×qty；期权=price×M×qty。"""
    price = business_number(order.price)
    qty = business_int(order.quantity)
    if price is None or qty is None:
        return None
    if order.instrument_type == "option":
        mult = option_multiplier(order)
        if mult is None:
            return None
        return price * mult * qty
    return price * qty


def order_exposure_delta(order, direction: str) -> Decimal | None:
    """§0.1 Δ(order) 方向表。数据不完整 → None。

    ``direction`` 是快照推断后的事实。期权 ``*_to_close`` 只是调用方意图；
    当快照不能证明 close 时，按对应的 buy/sell 开仓风险保守计算。
    """
    price = business_number(order.price)
    qty = business_int(order.quantity)
    if price is None or qty is None:
        return None

    if order.instrument_type == "option":
        side = order.side
        opt = order.option or {}
        strike = business_number(opt.get("strike"))
        mult = business_int(opt.get("multiplier"))
        if strike is None or mult is None:
            return None
        if side in ("buy_to_open", "buy_to_close") and direction == "open":
            return price * mult * qty
        if side in ("sell_to_open", "sell_to_close") and direction == "open":
            return strike * mult * qty  # 行权价口径，裸卖保守假设
        if side == "buy_to_close" and direction == "close":
            return -(strike * mult * qty)
        if side == "sell_to_close" and direction == "close":
            return -(price * mult * qty)
        return None

    if direction == "close":
        return -(price * qty)
    return price * qty


def short_option_margin(strike: Decimal, mult: int, qty: int, premium: Decimal | None) -> Decimal:
    """裸卖期权保证金占用：max(strike × M × qty − 已收权利金, 0)。"""
    gross = strike * mult * qty
    if premium is not None:
        gross -= premium * mult * qty
    return gross if gross > 0 else Decimal("0")
