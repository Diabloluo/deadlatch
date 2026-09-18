"""Guard 门面测试（M-3 库 API）：from_policy + 关键字构造 + 完整规则集。"""

import json
from datetime import timedelta

from tests.conftest import NOW, assert_no_audit_artifacts, assert_platform_guard_result, audit_writes_ok, full_policy
from deadlatch import Guard, Order, Portfolio


def _ts(**delta) -> str:
    return (NOW + timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_guard_from_policy_yaml(tmp_path):
    p = tmp_path / "policy.yaml"
    p.write_text(
        "schema_version: 2\nversion: '1.0.0'\nmode: enforce\nbase_currency: USD\n"
        "kill_switch: \"off\"\nacknowledged_disabled: []\nlimits:\n"
        "  max_order_quantity: 500\n  max_order_value: 5000.0\n"
        "  max_symbol_exposure_ratio: 0.10\n  max_total_exposure_ratio: 0.60\n"
        "  min_cash: 0.0\n  max_options_margin_ratio: 0.35\n"
        "  max_daily_loss_ratio: 0.03\n  max_drawdown_ratio: 0.15\n"
        "  max_order_age_seconds: 300\n  max_snapshot_age_seconds: 300\n",
        encoding="utf-8",
    )
    guard = Guard.from_policy(str(p))
    order = Order(
        symbol="AAA", instrument_type="stock", side="buy", quantity=20,
        price=190.0, order_type="limit", currency="USD",
        created_at=_ts(seconds=-1),
    )
    pf = Portfolio(
        schema_version=3, equity=123456.78, cash=30000.0, day_start_equity=125000.0,
        peak_equity=128000.0, daily_pnl=-1200.5, drawdown_ratio=0.0355,
        snapshot_at=_ts(seconds=-61), base_currency="USD", positions=[],
    )
    result = guard.check(order, pf, now=NOW)
    assert_platform_guard_result(result, computed_decision="PASS", computed_exit=0)
    assert result.explain().startswith("Deadlatch — check result")
    if not audit_writes_ok():
        assert_no_audit_artifacts(guard.audit_path)


def test_guard_result_to_dict_matches_schema():
    from tests.conftest import make_standard, fresh_order, fresh_portfolio

    r = make_standard(full_policy()).check(fresh_order(), fresh_portfolio(), now=NOW)
    d = r.to_dict()
    # 结构符合 result.schema.json：必需键齐全、inactive_rules 真实反映 acknowledged_disabled
    assert set(d) >= {
        "schema_version", "decision", "shadow_mode", "shadow_verdict", "exit_code",
        "evaluated_at", "violations", "warnings", "evidence",
    }
    assert d["evidence"]["inactive_rules"] == []
    assert d["evidence"]["input_hash"] and len(d["evidence"]["input_hash"]) == 64


def test_guard_inactive_rules_reflects_acknowledged_disabled():
    from tests.conftest import make_engine, fresh_order, fresh_portfolio
    from deadlatch.rules.registry import standard_rule_registry

    pol = full_policy(acknowledged_disabled=["max_order_quantity"])
    pol.data["limits"].pop("max_order_quantity", None)  # 缺键 + 已承认 → 合法禁用
    # full_policy 的 acknowledged_disabled 覆盖后，需同步移除对应键
    engine = make_engine(pol, standard_rule_registry())
    r = engine.check(fresh_order(), fresh_portfolio(), now=NOW)
    assert "max_order_quantity" in r.evidence["inactive_rules"]
    assert "kill_switch" not in r.evidence["inactive_rules"]


def test_guard_check_kwargs_only_order_portfolio():
    from tests.conftest import make_standard, fresh_order, fresh_portfolio

    engine = make_standard(full_policy())
    r = engine.check(order=fresh_order(), portfolio=fresh_portfolio(), now=NOW)
    assert r.exit_code == 0
