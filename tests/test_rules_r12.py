"""R12 missing_data_fail_closed 测试：portfolio 业务数据缺失 → exit 3（不串码）。"""

import pytest

from tests.conftest import NOW, fresh_order, fresh_portfolio, full_policy, make_standard
from deadlatch._validation import InputValidationError
from deadlatch.rules.missing_data_fail_closed import MissingDataFailClosedRule
from deadlatch.rules.registry import standard_rule_registry

POL = full_policy()


def _standard(pf):
    return make_standard(POL).check(fresh_order(), pf, now=NOW)


def test_missing_daily_pnl_exit3():
    pf = fresh_portfolio()
    del pf.data["daily_pnl"]
    r = _standard(pf)
    assert r.exit_code == 3  # 关键业务数据缺失 → 风控语义，非输入错误
    assert any(v["rule_id"] == "missing_data_fail_closed" for v in r.violations)


def test_missing_equity_exit3():
    pf = fresh_portfolio()
    del pf.data["equity"]
    r = _standard(pf)
    assert r.exit_code == 3


def test_null_snapshot_exit3():
    pf = fresh_portfolio(snapshot_at=None)
    r = _standard(pf)
    assert r.exit_code == 3


def test_day_start_zero_exit3():
    # day_start_equity = 0：Schema 接受（FIN-4），R12 判分母非法 → exit 3
    pf = fresh_portfolio(day_start_equity=0)
    r = _standard(pf)
    assert r.exit_code == 3
    assert any("分母非法" in v["detail"] for v in r.violations)


def test_drawdown_inconsistent_exit3():
    # drawdown_ratio 与 equity/peak 不自洽（> 0.5% 偏差）→ 疑似伪造快照 → exit 3
    pf = fresh_portfolio(drawdown_ratio=0.5)
    r = _standard(pf)
    assert r.exit_code == 3
    assert any("自洽" in v["detail"] for v in r.violations)


def test_positions_unusable_exit3():
    pf = fresh_portfolio(
        positions=[{"symbol": "AAA", "instrument_type": "stock", "side": "long",
                    "quantity": "not-a-number", "market_value": 19000.0, "currency": "USD"}]
    )
    r = _standard(pf)
    assert r.exit_code == 3  # 类型非法 → R12（exit 3）


def test_missing_data_is_not_exit4_or_5():
    pf = fresh_portfolio()
    del pf.data["daily_pnl"]
    r = _standard(pf)
    assert r.exit_code not in (4, 5)  # 分诊：业务数据缺失 ≠ 输入错误 / 引擎异常


def test_order_missing_field_is_exit4_not_3():
    # order 结构字段缺失 → exit 4（与 portfolio 业务数据缺失 exit 3 严格区分）
    from deadlatch.model import Order

    bad = Order.from_dict({k: v for k, v in fresh_order().to_dict().items() if k != "currency"})
    r = make_standard(POL).check(bad, fresh_portfolio(), now=NOW)
    assert r.exit_code == 4


# ---- -B §2.1：positions 字段形状（缺失/null/非数组/元素非对象 → exit 3）----

def test_positions_missing_exit3():
    pf = fresh_portfolio()
    del pf.data["positions"]
    r = _standard(pf)
    assert r.exit_code == 3  # 结构缺失 → R12，非 PASS、非 exit 5
    assert any(v["rule_id"] == "missing_data_fail_closed" for v in r.violations)


def test_positions_null_exit3():
    r = _standard(fresh_portfolio(positions=None))
    assert r.exit_code == 3


def test_positions_not_array_exit3():
    r = _standard(fresh_portfolio(positions="not-an-array"))
    assert r.exit_code == 3


def test_positions_element_not_object_exit3():
    r = _standard(fresh_portfolio(positions=[123, "x"]))
    assert r.exit_code == 3
    assert any("positions[0]" in v["detail"] for v in r.violations)


def test_positions_empty_still_valid():
    # 空数组 [] 仍为合法快照（PASS，不被 R12 误拦）
    r = _standard(fresh_portfolio(positions=[]))
    assert r.exit_code == 0


# ---- -B §2.1：币种一致性（exit 4）----

def test_positions_currency_mismatch_exit4():
    pf = fresh_portfolio(
        positions=[
            {"symbol": "AAA", "instrument_type": "stock", "side": "long",
             "quantity": 100, "market_value": 19000.0, "currency": "HKD"}
        ]
    )
    r = _standard(pf)
    assert r.exit_code == 4
    assert any("positions[0].currency" in v["detail"] for v in r.violations)


def test_portfolio_base_currency_mismatch_exit4():
    pf = fresh_portfolio(base_currency="HKD")
    r = _standard(pf)
    assert r.exit_code == 4
    assert any("portfolio.base_currency" in v["detail"] for v in r.violations)


# ---- -B §2.1：R12 evidence 同时记录具体字段与受影响规则 ----

def test_r12_evidence_records_field_and_affected_rules():
    pf = fresh_portfolio()
    del pf.data["daily_pnl"]
    r = _standard(pf)
    ev = r.evidence["rule_evidence"]["missing_data_fail_closed"]
    fields = [e["value"] for e in ev if e["name"] == "missing_field"]
    rules = [e["value"] for e in ev if e["name"] == "affected_rules"]
    assert "portfolio.daily_pnl" in fields
    assert any("R8" in x for x in rules)


def test_r12_evidence_positions_shape_records_rules():
    r = _standard(fresh_portfolio(positions=None))
    ev = r.evidence["rule_evidence"]["missing_data_fail_closed"]
    assert any(
        e["name"] == "missing_field" and e["value"] == "portfolio.positions" for e in ev
    )
    assert any(e["name"] == "affected_rules" and "R5/R6/R7" in e["value"] for e in ev)
