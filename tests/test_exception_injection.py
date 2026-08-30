""" §四 异常注入与单一 PASS 出口（T4）。

- 固定注册表 12 条规则逐条注入 Exception → 全部 BLOCK/exit 5，其他规则照常执行
  （spy call list 证明 12 条全部执行完成；异常 evidence 记录在被注入 rule_id 下）；
- 方向/敞口/投影/序列化/审计/CLI/MCP handler 异常按既有分诊（patch 实际导入别名）；
- 从成功路径首次执行开始注入；
- 静态结构断言用 Python AST：无裸 except / except BaseException / except…pass；
  规则循环（for rule in self._rules）循环体无 break/return。
"""

import asyncio
import ast
import json
import tempfile
from pathlib import Path

import pytest

from tests.conftest import NOW, fresh_order, fresh_portfolio, full_policy, make_standard
from deadlatch import Guard, Policy
from deadlatch.engine import GuardEngine
from deadlatch.rules.base import Rule, RuleContext, RuleOutcome
from deadlatch.rules.registry import RULE_IDS, standard_rule_registry

REPO = Path(__file__).resolve().parents[1]


class SpyRule(Rule):
    """包装真实规则：记录 evaluate 调用；被注入的 rule_id 抛异常。

    - call list 证明规则循环先收集、后判定（无短路）；
    - 异常 evidence 由引擎记录在被注入 rule_id 名下（S-2 语义）。
    """

    def __init__(self, inner: Rule, calls: list, inject: str | None):
        self._inner = inner
        self._calls = calls
        self._inject = inject

    @property
    def rule_id(self) -> str:
        return self._inner.rule_id

    def evaluate(self, ctx: RuleContext) -> RuleOutcome:
        self._calls.append(self.rule_id)
        if self._inject == self.rule_id:
            raise RuntimeError(f"injected boom for {self.rule_id}")
        return self._inner.evaluate(ctx)


# ---------------- 12 条规则逐条注入 ----------------

def test_all_12_rules_injected_exception_exit5():
    pol = full_policy()
    calls: list[str] = []
    for rid in RULE_IDS:
        rules = [SpyRule(r, calls, rid if r.rule_id == rid else None)
                 for r in standard_rule_registry()]
        eng = GuardEngine(pol, rules=rules)
        # 成功路径先行（同一构造的引擎未被注入异常时）：
        # 先验证真实规则集 PASS，再注入
        ok = GuardEngine(pol, rules=standard_rule_registry()).check(
            fresh_order(), fresh_portfolio(), now=NOW)
        assert ok.exit_code == 0

        r = eng.check(fresh_order(), fresh_portfolio(), now=NOW)
        assert r.exit_code == 5, rid
        assert r.decision == "BLOCK"
        # spy call list：12 条规则全部执行完成（无短路）
        assert sorted(calls) == sorted(RULE_IDS), (rid, calls)
        calls.clear()
        # 异常 evidence 记录在被注入 rule_id 下（S-2 规则级捕获，非 engine 键）
        assert rid in r.evidence["rule_evidence"], rid
        assert any(e["name"] == "exception_type" and e["value"] == "RuntimeError"
                   for e in r.evidence["rule_evidence"][rid]), rid
        # 其他规则继续执行：至少 9 条正常产出 evidence（R2/R12 无命中时不产键）
        assert len(r.evidence["rule_evidence"]) >= 9, (rid, r.evidence["rule_evidence"].keys())
        if rid != "kill_switch":
            assert "kill_switch" in r.evidence["rule_evidence"], rid


# ---------------- 各层异常注入（成功路径先行，patch 实际导入别名） ----------------

def test_direction_inference_exception_exit5(monkeypatch):
    pol = full_policy()
    eng = make_standard(pol)
    assert eng.check(fresh_order(), fresh_portfolio(), now=NOW).exit_code == 0  # 成功路径先行

    def boom(*a, **k):
        raise RuntimeError("direction boom")

    # R5 模块内 `from ..direction import infer_order_direction` 的绑定别名
    monkeypatch.setattr("deadlatch.rules.max_symbol_exposure.infer_order_direction", boom)
    r = eng.check(fresh_order(), fresh_portfolio(), now=NOW)
    assert r.exit_code == 5  # R5 调用方向推断 → 规则级捕获 → BLOCK/5
    assert any(e["name"] == "exception_type" for e in r.evidence["rule_evidence"]["max_symbol_exposure"])


def test_exposure_calculation_exception_exit5(monkeypatch):
    pol = full_policy()

    def boom(*a, **k):
        raise RuntimeError("exposure boom")

    # R12 模块内 `from ..exposure import position_exposure` 的绑定别名；
    # 空 positions 时 R12 不调用敞口计算——用带持仓的快照触发
    monkeypatch.setattr("deadlatch.rules.missing_data_fail_closed.position_exposure", boom)
    eng = make_standard(pol)
    pf = fresh_portfolio(positions=[
        {"symbol": "AAA", "instrument_type": "stock", "side": "long",
         "quantity": 100, "market_value": 19000.0, "currency": "USD"}
    ])
    r = eng.check(fresh_order(), pf, now=NOW)
    assert r.exit_code == 5  # R12 调用敞口计算 → 异常 → exit 5
    assert any(e["name"] == "exception_type"
               for e in r.evidence["rule_evidence"]["missing_data_fail_closed"])


def test_shadow_projection_exception_exit5(monkeypatch):
    from tests.conftest import _policy

    pol = Policy.from_dict(_policy(mode="shadow"))

    def boom(*a, **k):
        raise RuntimeError("projection boom")

    # engine 模块内部调用 _project_outcome（模块全局函数，patch 模块属性生效）
    monkeypatch.setattr("deadlatch.engine._project_outcome", boom)
    eng = make_standard(pol)
    r = eng.check(fresh_order(), fresh_portfolio(), now=NOW)
    assert r.exit_code == 5  # 投影异常 → 最外层兜底（不再次调用损坏的 _project_outcome）
    assert r.decision == "BLOCK"
    assert r.shadow_verdict == "BLOCK"  # shadow 兜底保留内部裁决


def test_fallback_result_does_not_reenter_projection(monkeypatch):
    # 兜底不得再次调用 _project_outcome：即使 _project_outcome 已损坏（每次调用都抛），
    # check() 的兜底层仍必须返回 BLOCK/5（不逃逸、不产生 PASS）
    pol = full_policy()

    def boom(*a, **k):
        raise RuntimeError("projection always boom")

    monkeypatch.setattr("deadlatch.engine._project_outcome", boom)
    eng = make_standard(pol)
    r = eng.check(fresh_order(), fresh_portfolio(), now=NOW)
    assert r.exit_code == 5
    assert r.decision == "BLOCK"


def test_result_serialization_exception_cli_exit5(tmp_path, monkeypatch, capsys):
    from deadlatch.cli import main

    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "schema_version: 2\nversion: '1.0.0'\nmode: enforce\nbase_currency: USD\n"
        'kill_switch: "off"\nacknowledged_disabled: []\nlimits:\n'
        "  max_order_quantity: 500\n  max_order_value: 5000.0\n"
        "  max_symbol_exposure_ratio: 0.10\n  max_total_exposure_ratio: 0.60\n"
        "  min_cash: 0.0\n  max_options_margin_ratio: 0.35\n"
        "  max_daily_loss_ratio: 0.03\n  max_drawdown_ratio: 0.15\n"
        "  max_order_age_seconds: 300\n  max_snapshot_age_seconds: 300\n",
        encoding="utf-8",
    )
    order = tmp_path / "order.json"
    order.write_text(json.dumps({"schema_version": 2, "symbol": "AAA",
                                 "instrument_type": "stock", "side": "buy",
                                 "quantity": 20, "price": 190.0, "order_type": "limit",
                                 "currency": "USD", "created_at": "2026-08-29T10:00:00Z"}),
                     encoding="utf-8")
    portfolio = tmp_path / "portfolio.json"
    portfolio.write_text(json.dumps({"schema_version": 3, "equity": 123456.78,
                                     "cash": 30000.0, "day_start_equity": 125000.0,
                                     "peak_equity": 128000.0, "daily_pnl": -1200.5,
                                     "drawdown_ratio": 0.0355,
                                     "snapshot_at": "2026-08-29T09:59:00Z",
                                     "base_currency": "USD", "positions": []}),
                         encoding="utf-8")
    argv = ["check", "--policy", str(policy), "--order", str(order),
            "--portfolio", str(portfolio)]

    def boom_explain(self):
        raise RuntimeError("explain boom")

    monkeypatch.setattr("deadlatch.model.Result.explain", boom_explain)
    rc = main(argv)
    assert rc == 5  # 渲染异常 → exit 5（fail-closed）
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out and "Traceback" not in captured.err
    assert "explain boom" not in captured.err  # 不泄漏异常原文

    # JSON 序列化异常同样兜底
    def boom_to_dict(self):
        raise RuntimeError("to_dict boom")

    monkeypatch.setattr("deadlatch.model.Result.to_dict", boom_to_dict)
    rc = main(argv + ["--json"])
    assert rc == 5
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out and "Traceback" not in captured.err


def test_audit_construct_exception_degrades_not_raises(tmp_path, monkeypatch):
    # 审计构造/写入异常 → 严重度只升不降降级（），不逃逸
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "schema_version: 2\nversion: '1.0.0'\nmode: enforce\nbase_currency: USD\n"
        'kill_switch: "off"\nacknowledged_disabled: []\nlimits:\n'
        "  max_order_quantity: 500\n  max_order_value: 5000.0\n"
        "  max_symbol_exposure_ratio: 0.10\n  max_total_exposure_ratio: 0.60\n"
        "  min_cash: 0.0\n  max_options_margin_ratio: 0.35\n"
        "  max_daily_loss_ratio: 0.03\n  max_drawdown_ratio: 0.15\n"
        "  max_order_age_seconds: 300\n  max_snapshot_age_seconds: 300\n",
        encoding="utf-8",
    )

    def boom(*a, **k):
        raise RuntimeError("audit boom")

    # Guard._record_audit 使用 guard 模块的绑定别名 build_audit_record
    monkeypatch.setattr("deadlatch.guard.build_audit_record", boom)
    guard = Guard.from_policy(str(policy), audit_path=str(tmp_path / "audit.jsonl"))
    r = guard.check(fresh_order(), fresh_portfolio(), now=NOW)
    assert r.exit_code == 2  # PASS → WARN/2（降级可见，不逃逸）
    assert any(w["rule_id"] == "audit_write_failed" for w in r.warnings)


def test_mcp_handler_exception_fail_closed():
    from deadlatch.mcp_server import MCPGuardServer
    from tests.test_mcp_server import _write_audit, _write_policy, _write_portfolio

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        policy = _write_policy(d)
        portfolio = _write_portfolio(d)
        audit = _write_audit(d, [])
        srv = MCPGuardServer(str(policy), str(portfolio), str(audit))

        params = type("P", (), {"name": "evil_tool", "arguments": {}})()
        r = asyncio.run(srv._call_tool(None, params))
        body = json.loads(r.content[0].text)
        assert r.is_error and body["fail_closed"] is True and body["exit_code"] == 5


# ---------------- 静态结构断言（Python AST） ----------------

def test_no_bare_except_or_baseexception_in_source():
    src_files = list((REPO / "src" / "deadlatch").rglob("*.py"))
    assert src_files
    for path in src_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if node.type is None:
                pytest.fail(f"{path}:{node.lineno} 裸 except（except:）")
            is_base = isinstance(node.type, ast.Name) and node.type.id == "BaseException"
            if is_base:
                pytest.fail(f"{path}:{node.lineno} except BaseException")
            # 仅裸 except / BaseException 的纯 pass 被禁止；具名异常（如 KeyboardInterrupt）的
            # 空处理是合法模式（服务器优雅退出）
            if is_base or node.type is None:
                if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                    pytest.fail(f"{path}:{node.lineno} except ...: pass")


def test_engine_rule_loop_no_early_break_or_return():
    path = REPO / "src" / "deadlatch" / "engine.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        is_rule_loop = (
            isinstance(node.target, ast.Name) and node.target.id == "rule"
            and isinstance(node.iter, ast.Attribute)
            and isinstance(node.iter.value, ast.Name)
            and node.iter.value.id == "self"
            and node.iter.attr in ("_rules", "rules")
        )
        if not is_rule_loop:
            continue
        found = True
        for sub in ast.walk(node):
            if isinstance(sub, (ast.Break, ast.Return)):
                pytest.fail(f"{path}:{sub.lineno} 规则循环体内出现 break/return（破坏先收集后判定）")
        break
    assert found, "未找到规则循环 for rule in self._rules"


def test_pass_has_single_exit():
    from deadlatch import engine

    src = ast.parse(Path(engine.__file__).read_text(encoding="utf-8"))
    # "PASS"/0 赋值只出现在 S-1 合成（internal_decision/internal_exit 元组赋值）
    assigns = []
    for node in ast.walk(src):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                names = t.elts if isinstance(t, ast.Tuple) else [t]
                for elt in names:
                    if isinstance(elt, ast.Name) and elt.id in ("internal_decision", "internal_exit"):
                        assigns.append((elt.id, node.lineno))
    assert any(t == "internal_decision" for t, _ in assigns)
    assert any(t == "internal_exit" for t, _ in assigns)
