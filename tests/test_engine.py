"""S-1 决策合成与 S-2 异常捕获的测试（真值表全覆盖）+ 第 2 轮修复测试。"""

import pytest

from deadlatch import GuardEngine, Policy
from deadlatch._validation import InputValidationError
from deadlatch.rules.data_freshness import DataFreshnessRule
from deadlatch.rules.input_validity import InputValidityRule
from deadlatch.rules.kill_switch import KillSwitchRule
from deadlatch.rules.max_daily_loss import MaxDailyLossRule
from deadlatch.rules.max_drawdown import MaxDrawdownRule
from deadlatch.rules.missing_data_fail_closed import MissingDataFailClosedRule
from deadlatch.rules.order_time_validity import OrderTimeValidityRule
from deadlatch.rules.registry import MANDATORY_RULE_IDS
from deadlatch.rules.stubs import (
    AlwaysPassRule,
    BoomRule,
    ViolationRule,
    WarnRule,
)
from tests.conftest import NOW, make_engine, option_order, portfolio, stock_order

# 与 registry.MANDATORY_RULE_IDS 对应的规则实例（构造期强制集测试用）
_MANDATORY_INSTANCES = [
    KillSwitchRule(),
    InputValidityRule(),
    MaxDailyLossRule(),
    MaxDrawdownRule(),
    OrderTimeValidityRule(),
    DataFreshnessRule(),
    MissingDataFailClosedRule(),
]


def test_all_pass_exit0(policy_off, order_buy):
    r = make_engine(policy_off, [AlwaysPassRule()]).check(order_buy, portfolio())
    assert r.decision == "PASS"
    assert r.exit_code == 0
    assert r.violations == []
    assert r.warnings == []


def test_boom_exit5_with_evidence(policy_off, order_buy):
    r = make_engine(policy_off, [BoomRule()]).check(order_buy, portfolio())
    assert r.decision == "BLOCK"
    assert r.exit_code == 5
    ev = r.evidence["rule_evidence"]["stub_boom"]
    types = [e["value"] for e in ev if e["name"] == "exception_type"]
    assert "RuntimeError" in types


def test_boom_plus_violation_exit5_both_present(policy_off, order_buy):
    r = make_engine(policy_off, [BoomRule(), ViolationRule()]).check(order_buy, portfolio())
    assert r.exit_code == 5  # 优先级 1 高于 3
    assert r.decision == "BLOCK"
    # 两条信息都在结果中：违规未被异常掩盖
    assert any(v["rule_id"] == "stub_violation" for v in r.violations)
    assert "stub_boom" in r.evidence["rule_evidence"]


def test_violation_plus_warn_exit3(policy_off, order_buy):
    r = make_engine(policy_off, [ViolationRule(), WarnRule()]).check(order_buy, portfolio())
    assert r.decision == "BLOCK"
    assert r.exit_code == 3
    assert any(v["rule_id"] == "stub_violation" for v in r.violations)
    assert any(w["rule_id"] == "stub_warn" for w in r.warnings)


def test_warn_only_exit2(policy_off, order_buy):
    r = make_engine(policy_off, [WarnRule()]).check(order_buy, portfolio())
    assert r.decision == "WARN"
    assert r.exit_code == 2
    assert r.violations == []


def test_multiple_violations_all_collected_no_shortcircuit(policy_full, order_buy):
    # kill_switch(full) + stub_violation：两条不同规则的违规，全部收集（不短路）
    r = make_engine(policy_full, [ViolationRule()]).check(order_buy, portfolio())
    assert r.exit_code == 3
    rules_hit = {v["rule_id"] for v in r.violations}
    assert rules_hit == {"kill_switch", "stub_violation"}


def test_explain_distinguishes_input_error_vs_risk(policy_off):
    # 输入错误（币种不匹配）→ exit 4：明确标注"非风控拦截"，并给出 input_validity 条目
    bad = stock_order(currency="HKD")
    r4 = make_engine(policy_off, [AlwaysPassRule()]).check(bad, portfolio())
    assert r4.exit_code == 4
    assert "非风控拦截" in r4.explain()
    assert "input_validity" in r4.explain()
    # 风控拦截 → exit 3：标注"风控拦截"，无"非风控拦截"
    from deadlatch.rules.stubs import ViolationRule as VR

    r3 = make_engine(policy_off, [VR()]).check(stock_order(), portfolio())
    assert r3.exit_code == 3
    assert "风控拦截" in r3.explain()
    assert "非风控拦截" not in r3.explain()


def test_currency_mismatch_exit4(policy_off, order_buy):
    bad = stock_order(currency="HKD")
    r = make_engine(policy_off, [AlwaysPassRule()]).check(bad, portfolio())
    assert r.decision == "BLOCK"
    assert r.exit_code == 4
    detail = " ".join(v["detail"] for v in r.violations)
    assert "currency mismatch" in detail


def test_nan_price_exit4(policy_off, order_buy):
    bad = stock_order(price=float("nan"))
    r = make_engine(policy_off, [AlwaysPassRule()]).check(bad, portfolio())
    assert r.exit_code == 4
    assert any("非有限数值" in v["detail"] for v in r.violations)


def test_infinity_price_exit4(policy_off, order_buy):
    bad = stock_order(price=float("inf"))
    r = make_engine(policy_off, [AlwaysPassRule()]).check(bad, portfolio())
    assert r.exit_code == 4


def test_determinism_100_runs(policy_off, order_buy):
    # -B §2.5：固定输入、固定时钟的确定性测试，连续 100 次字节级一致
    eng = make_engine(policy_off, [AlwaysPassRule(), WarnRule()])
    first = None
    for _ in range(100):
        r = eng.check(order_buy, portfolio(), now=NOW)
        assert r.exit_code in (0, 2)  # 输入合法：PASS 或 仅警告
        blob = bytes(repr(r.to_dict()), "utf-8")
        if first is None:
            first = blob
        assert blob == first  # 同一输入 + 同一时钟 → 字节级一致


def test_invalid_kill_switch_rejected_at_construction(policy_off):
    from deadlatch import Policy

    bad_policy = Policy.from_dict({**policy_off.to_dict(), "kill_switch": "enable"})
    with pytest.raises(InputValidationError):
        make_engine(bad_policy, [])


# ---- DEF-A2 / -B §2.2：空规则集 / 缺强制规则 → 拒绝构造 ----
# 强制规则集合唯一来源 = registry.MANDATORY_RULE_IDS（R1/R2/R8–R12 共 7 条）

def test_empty_ruleset_rejected(policy_off):
    with pytest.raises(InputValidationError) as ei:
        GuardEngine(policy_off)  # 不传 rules → 构造失败，不产生任何 Result
    assert "缺少强制规则" in str(ei.value)


def test_missing_mandatory_rules_rejected(policy_off):
    with pytest.raises(InputValidationError) as ei:
        GuardEngine(policy_off, rules=[KillSwitchRule()])  # 缺 input_validity 等 6 条强制规则
    assert "input_validity" in str(ei.value)
    assert "missing_data_fail_closed" in str(ei.value)
    assert "max_daily_loss" in str(ei.value)


def test_only_r1_r2_r12_rejected(policy_off):
    # 重点回归（-B §2.2）：只注入 R1/R2/R12 必须拒绝构造，不能绕过 R8–R11
    with pytest.raises(InputValidationError) as ei:
        GuardEngine(
            policy_off,
            rules=[KillSwitchRule(), InputValidityRule(), MissingDataFailClosedRule()],
        )
    msg = str(ei.value)
    for rid in ("max_daily_loss", "max_drawdown", "order_time_validity", "data_freshness"):
        assert rid in msg


def test_mandatory_set_present_ok(policy_off, order_buy):
    eng = GuardEngine(policy_off, rules=list(_MANDATORY_INSTANCES))
    r = eng.check(order_buy, portfolio())
    assert r.decision == "PASS"
    assert r.exit_code == 0


def test_mandatory_matches_registry(policy_off):
    # 构造期强制集与 registry.MANDATORY_RULE_IDS 完全一致：全量 OK，逐一缺一拒绝
    assert len(MANDATORY_RULE_IDS) == 7
    for rid in MANDATORY_RULE_IDS:
        rules = [inst for inst in _MANDATORY_INSTANCES if inst.rule_id != rid]
        with pytest.raises(InputValidationError) as ei:
            GuardEngine(policy_off, rules=rules)
        assert rid in str(ei.value)


# ---- DEF-A3：check() 最外层异常兜底 → exit 5，不向上抛 ----

def test_fallback_catches_non_input_exception_from_validation(policy_off, order_buy, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("validate_inputs 内部非 InputValidationError 异常")

    monkeypatch.setattr("deadlatch.engine.validate_inputs", boom)
    r = make_engine(policy_off, [KillSwitchRule()]).check(order_buy, portfolio())
    assert r.decision == "BLOCK"
    assert r.exit_code == 5  # 未逃逸，得到 exit 5 Result
    ev = r.evidence["rule_evidence"]["engine"]
    assert any(e["name"] == "exception_type" and e["value"] == "RuntimeError" for e in ev)
    assert any(e["name"] == "stage" and e["value"] == "validation" for e in ev)


def test_fallback_catches_order_summary_boom(policy_off, order_buy, monkeypatch):
    def boom(order):
        raise ValueError("order_summary 抛错")

    monkeypatch.setattr("deadlatch.engine._order_summary", boom)
    r = make_engine(policy_off, [KillSwitchRule()]).check(order_buy, portfolio())
    assert r.decision == "BLOCK"
    assert r.exit_code == 5
    assert any(
        e["name"] == "stage" and e["value"] == "post_rules"
        for e in r.evidence["rule_evidence"]["engine"]
    )


# ---- MIN-A2：rule_id 唯一性 ----

def test_duplicate_rule_id_rejected(policy_off):
    # 强制集齐全后仍重复注入 KillSwitchRule → 唯一性校验拒绝（MIN-A2）
    with pytest.raises(InputValidationError) as ei:
        GuardEngine(
            policy_off,
            rules=[*_MANDATORY_INSTANCES, KillSwitchRule()],
        )
    assert "rule_id 重复" in str(ei.value)
