"""S-7 数值摄取：金额与比例的唯一入口。

硬性要求：
- Decimal(str(value))，禁止 Decimal(float)；
- float / int / str 输入均须正确处理（str(float) 对 0.1 → "0.1"，精确）；
- NaN / Infinity / -Infinity → DecimalInputError（引擎映射 exit 4）；
- 金额与比例不得参与任何 float 运算。
"""

from decimal import Decimal, InvalidOperation


class DecimalInputError(ValueError):
    """非有限或不可解析的数值输入（映射 exit 4，输入错误语义）。"""


def as_decimal(value) -> Decimal:
    """单一入口。接受 int / float / str / Decimal。"""
    if isinstance(value, Decimal):
        d = value
    else:
        try:
            d = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            # 消息不回显输入值（details/evidence 共用，防回显注入）
            raise DecimalInputError("无法解析为 Decimal（输入必须是有穷数字）") from exc
    if not d.is_finite():
        raise DecimalInputError("非有限数值（NaN/Infinity 不接受）")
    return d


def check_finite(value, name: str) -> None:
    """校验有限性（不返回 Decimal，仅检查；用于输入门）。"""
    try:
        as_decimal(value)
    except DecimalInputError as exc:
        raise DecimalInputError(f"{name}: {exc}") from exc
