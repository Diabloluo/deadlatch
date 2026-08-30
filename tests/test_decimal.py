"""S-7 数值摄取测试：Decimal(str(v))、非有限拒绝、精确性。"""

from decimal import Decimal

import pytest

from deadlatch import DecimalInputError, as_decimal


def test_0_1_plus_0_2_exact():
    assert as_decimal(0.1) + as_decimal(0.2) == Decimal("0.3")


def test_float_int_str_inputs():
    assert as_decimal(190.0) == Decimal("190")
    assert as_decimal(20) == Decimal("20")
    assert as_decimal("2.50") == Decimal("2.50")
    assert as_decimal("190") == Decimal("190")


def test_no_float_roundtrip_loss():
    # 浮点字面量 0.1 经 str() 摄取后是精确的 "0.1"
    assert as_decimal(0.1) == Decimal("0.1")


def test_nan_rejected():
    with pytest.raises(DecimalInputError):
        as_decimal(float("nan"))


def test_infinity_rejected():
    with pytest.raises(DecimalInputError):
        as_decimal(float("inf"))
    with pytest.raises(DecimalInputError):
        as_decimal(float("-inf"))


def test_garbage_string_rejected():
    with pytest.raises(DecimalInputError):
        as_decimal("not-a-number")


def test_check_finite_rejects_non_finite():
    # check_finite 包装路径（name 前缀 + 异常传播）
    from deadlatch._decimal import check_finite

    with pytest.raises(DecimalInputError):
        check_finite(float("nan"), "order.price")
    with pytest.raises(DecimalInputError):
        check_finite(float("inf"), "order.price")
    with pytest.raises(DecimalInputError):
        check_finite(float("-inf"), "order.price")
    # 有限值不抛
    check_finite(190.0, "order.price")
    check_finite(Decimal("2.50"), "order.option.strike")
