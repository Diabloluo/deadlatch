""" §五 并发与确定性（T5/T6）。

- 混合结果（PASS/WARN/BLOCK）4 进程 × 25：审计恰好 100 行、record_id 唯一、
  各类结果计数精确；
- append 与 prune/report 并发：不损坏、不静默丢记录；
- 固定输入/固定 policy/固定 now 连续 100 次：除 record_id/evaluated_at 外
  规范化 Result 字节级一致；固定审计输入的报告 100 次字节级一致。
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from tests.conftest import NOW, fresh_order, fresh_portfolio, full_policy, make_standard
from deadlatch.audit import append_audit, prune_audit, read_audit_records
from deadlatch.report import build_shadow_report, parse_since

REPO = Path(__file__).resolve().parents[1]
AUDIT_SCHEMA = json.loads((REPO / "schemas" / "audit-record.schema.json").read_text())
AUDIT_VALIDATOR = Draft202012Validator(AUDIT_SCHEMA)

POL = full_policy()


def _ts(offset: timedelta) -> str:
    return (NOW + offset).strftime("%Y-%m-%dT%H:%M:%SZ")


def _audit_line(rid: str, decision: str, exit_code: int) -> str:
    return json.dumps({
        "schema_version": 1, "record_id": rid, "evaluated_at": "2026-08-29T10:00:00Z",
        "input_hash": "0" * 64, "decision": decision, "shadow_mode": False,
        "shadow_verdict": None, "exit_code": exit_code, "policy_version": "1.0.0",
        "rule_hits": [{"rule_id": "max_order_value", "severity": "BLOCK", "detail": "x"}]
        if decision == "BLOCK" else [],
    }, sort_keys=True, separators=(",", ":"))


# ---------------- 混合结果并发 ----------------

_MIXED_SCRIPT = """
import sys, json
from deadlatch.audit import append_audit
path = sys.argv[1]
lines = json.loads(sys.argv[2])  # [(rid, decision, exit_code), ...]
for rid, decision, exit_code in lines:
    rec = {
        "schema_version": 1, "record_id": rid,
        "evaluated_at": "2026-08-29T10:00:00Z",
        "input_hash": "0" * 64, "decision": decision, "shadow_mode": False,
        "shadow_verdict": None, "exit_code": exit_code, "policy_version": "1.0.0",
        "rule_hits": [{"rule_id": "max_order_value", "severity": "BLOCK", "detail": "x"}]
        if decision == "BLOCK" else [],
    }
    append_audit(path, rec)
"""


def test_concurrent_mixed_results_exact_counts(tmp_path):
    """4 进程 × 25 混合输入：审计恰好 100 行、唯一、计数精确。"""
    import uuid

    path = tmp_path / "audit.jsonl"
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    procs = []
    for p in range(4):
        # 每进程 25 条：10 PASS / 8 WARN / 7 BLOCK（混合）
        lines = []
        for i in range(25):
            rid = uuid.uuid4().hex
            if i < 10:
                lines.append([rid, "PASS", 0])
            elif i < 18:
                lines.append([rid, "WARN", 2])
            else:
                lines.append([rid, "BLOCK", 3])
        procs.append(subprocess.Popen(
            [sys.executable, "-c", _MIXED_SCRIPT, str(path), json.dumps(lines)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
    for p in procs:
        _, err = p.communicate(timeout=60)
        assert p.returncode == 0, err.decode()
    records = read_audit_records(path)
    assert len(records) == 100  # 恰好 100 行
    ids = [r["record_id"] for r in records]
    assert len(set(ids)) == 100  # record_id 唯一
    from collections import Counter

    counts = Counter((r["decision"], r["exit_code"]) for r in records)
    assert counts[("PASS", 0)] == 40
    assert counts[("WARN", 2)] == 32
    assert counts[("BLOCK", 3)] == 28


def test_concurrent_append_and_prune_report(tmp_path):
    """append 与 prune/report 并发：不损坏、不静默丢记录。"""
    import time
    import uuid

    path = tmp_path / "audit.jsonl"
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    # 预填 200 条窗口内记录
    for i in range(200):
        append_audit(path, json.loads(_audit_line(uuid.uuid4().hex, "PASS", 0)))
    # 3 个 append 进程（各 30 条）+ 主进程交替 prune/report
    procs = [subprocess.Popen(
        [sys.executable, "-c", _MIXED_SCRIPT, str(path),
         json.dumps([[uuid.uuid4().hex, "PASS", 0] for _ in range(30)])],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
    for _ in range(5):  # 并发 prune/report 循环
        prune_audit(path, now=datetime(2026, 8, 29, 10, 0, 1, tzinfo=timezone.utc))
        report = build_shadow_report(path, parse_since("30d"),
                                     now=datetime(2026, 8, 29, 10, 0, 1, tzinfo=timezone.utc))
        assert report["totals"]["orders_evaluated"] >= 200
        time.sleep(0.02)
    for p in procs:
        _, err = p.communicate(timeout=60)
        assert p.returncode == 0, err.decode()
    # 最终：全部记录合法（read_audit_records 已校验 Schema），总数 = 200 + 90
    records = read_audit_records(path)
    assert len(records) == 290  # 无丢失、无重复
    assert len({r["record_id"] for r in records}) == 290


# ---------------- 固定输入确定性 ----------------

def test_guard_100_runs_deterministic_except_audit_fields(tmp_path):
    """固定输入/固定 now 连续 100 次：Result 除 record_id/evaluated_at 外字节级一致。"""
    pol = full_policy()
    eng = make_standard(pol)
    order = fresh_order()
    pf = fresh_portfolio()
    first = None
    for _ in range(100):
        r = eng.check(order, pf, now=NOW)
        d = r.to_dict()
        # 去掉允许变化的字段后规范化
        d.pop("evaluated_at", None)
        blob = json.dumps(d, sort_keys=True, default=str)
        if first is None:
            first = blob
        assert blob == first  # 固定输入 + 固定时钟 → 字节级一致（不删证据字段）


def test_report_100_runs_deterministic(tmp_path):
    """固定审计输入的报告 100 次字节级一致。"""
    path = tmp_path / "audit.jsonl"
    for i in range(50):
        append_audit(path, json.loads(_audit_line(f"rec{i:08d}", "BLOCK" if i % 2 else "PASS",
                                                  3 if i % 2 else 0)))
    now = datetime(2026, 8, 29, 10, 0, 1, tzinfo=timezone.utc)
    first = None
    for _ in range(100):
        report = build_shadow_report(path, parse_since("30d"), now=now)
        blob = json.dumps(report, sort_keys=True)
        if first is None:
            first = blob
        assert blob == first


# ---------------- FIX-005-4：真实 Guard.check 多进程（不允许直造 AuditRecord） ----------------

_GUARD_WORKER = """
import json, sys
from datetime import datetime, timezone
from deadlatch import Guard, Order, Portfolio

policy_path, portfolio_path, audit_path, orders_json, out_path = sys.argv[1:6]
guard = Guard.from_policy(policy_path, audit_path=audit_path)
pf = Portfolio.from_dict(json.load(open(portfolio_path, encoding="utf-8")))
now = datetime(2026, 8, 29, 10, 0, 1, tzinfo=timezone.utc)
orders = json.loads(orders_json)
results = []
for o in orders:
    r = guard.check(Order.from_dict(o), pf, now=now)
    results.append(r.to_dict())
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(results, f)
"""


def _run_guard_workers(tmp_path, orders, n_procs=4, per_proc=25, worker: str = _GUARD_WORKER):
    """4 子进程 × per_proc 次 Guard.check（同一 policy/portfolio/audit）。

    返回 (结果文件列表, 审计路径)。worker 为子进程脚本（默认真实标准规则集；
    FIX-005-4 三分类场景注入测试专用 Warning Rule 的变体脚本）。"""
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
    portfolio = tmp_path / "portfolio.json"
    portfolio.write_text(json.dumps({
        "schema_version": 3, "equity": 123456.78, "cash": 30000.0,
        "day_start_equity": 125000.0, "peak_equity": 128000.0,
        "daily_pnl": -1200.5, "drawdown_ratio": 0.0355,
        "snapshot_at": "2026-08-29T09:59:00Z", "base_currency": "USD", "positions": [],
    }), encoding="utf-8")
    audit = tmp_path / "audit.jsonl"
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    orders_json = json.dumps(orders)
    out_files = []
    procs = []
    for p in range(n_procs):
        out = tmp_path / f"results_{p}.json"
        out_files.append(out)
        procs.append(subprocess.Popen(
            [sys.executable, "-c", worker, str(policy), str(portfolio),
             str(audit), orders_json, str(out)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ))
    for p in procs:
        _, err = p.communicate(timeout=60)
        assert p.returncode == 0, err.decode()
    return out_files, audit


def _order(**overrides) -> dict:
    d = {"schema_version": 2, "symbol": "AAA", "instrument_type": "stock",
         "side": "buy", "quantity": 20, "price": 190.0, "order_type": "limit",
         "currency": "USD", "created_at": "2026-08-29T10:00:00Z"}
    d.update(overrides)
    return d


def test_guard_concurrent_same_input_100_consistent_results(tmp_path):
    """同输入 4×25：100 个 Result 除允许变化字段外一致；审计 100 行且与 Result 对账。"""
    out_files, audit = _run_guard_workers(tmp_path, [_order()] * 25)
    all_results = []
    for f in out_files:
        all_results.extend(json.loads(f.read_text(encoding="utf-8")))
    assert len(all_results) == 100
    # 规范化后除 evaluated_at 外字节级一致
    blobs = set()
    for r in all_results:
        r.pop("evaluated_at", None)
        blobs.add(json.dumps(r, sort_keys=True, default=str))
    assert len(blobs) == 1, "100 个同输入 Result 必须一致"
    first = all_results[0]
    assert first["decision"] == "PASS" and first["exit_code"] == 0
    # 审计恰好 100 行、record_id 唯一、每行过 Schema、input_hash 与 Result 一致
    records = read_audit_records(audit)
    assert len(records) == 100
    assert len({r["record_id"] for r in records}) == 100
    for rec in records:
        assert AUDIT_VALIDATOR.is_valid(rec)
        assert rec["input_hash"] == first["evidence"]["input_hash"]
        assert rec["decision"] == first["decision"] and rec["exit_code"] == first["exit_code"]


def test_guard_concurrent_mixed_results_reconciled(tmp_path):
    """混合输入 4×25：PASS/BLOCK 计数与 100 条审计精确对账；WARN 走审计降级路径单验。"""
    mixed = []
    for i in range(25):
        if i % 2 == 0:
            mixed.append(_order())                    # PASS
        else:
            mixed.append(_order(quantity=1000))       # BLOCK（R3/R4/R5 触发）
    out_files, audit = _run_guard_workers(tmp_path, mixed)
    all_results = []
    for f in out_files:
        all_results.extend(json.loads(f.read_text(encoding="utf-8")))
    assert len(all_results) == 100
    from collections import Counter

    result_counts = Counter((r["decision"], r["exit_code"]) for r in all_results)
    assert result_counts[("PASS", 0)] == 52   # 13 PASS × 4
    assert result_counts[("BLOCK", 3)] == 48  # 12 BLOCK × 4
    # 审计与 Result 精确对账（决策 + input_hash）
    records = read_audit_records(audit)
    assert len(records) == 100
    audit_counts = Counter((r["decision"], r["exit_code"]) for r in records)
    assert audit_counts == result_counts
    rec_by_hash = {r["input_hash"]: (r["decision"], r["exit_code"]) for r in records}
    for r in all_results:
        key = r["evidence"]["input_hash"]
        assert rec_by_hash[key] == (r["decision"], r["exit_code"])
    # 真实 Guard 的 WARN 路径 = 审计写失败降级（）：只读审计路径 → WARN/2
    from deadlatch import Guard

    policy = tmp_path / "policy.yaml"
    ro_dir = tmp_path / "ro"
    ro_dir.mkdir()
    ro_audit = ro_dir / "audit.jsonl"
    ro_audit.write_text("", encoding="utf-8")
    ro_dir.chmod(0o555)
    try:
        from deadlatch import Guard, Order, Portfolio

        guard = Guard.from_policy(str(policy), audit_path=str(ro_audit))
        r = guard.check(Order.from_dict(_order()), Portfolio.from_dict(
            json.loads((tmp_path / "portfolio.json").read_text())),
            now=datetime(2026, 8, 29, 10, 0, 1, tzinfo=timezone.utc))
        assert r.exit_code == 2 and r.decision == "WARN"  # 三类结果中的 WARN 真实路径
        assert any(w["rule_id"] == "audit_write_failed" for w in r.warnings)
    finally:
        ro_dir.chmod(0o755)


# ---------------- FIX-005-4：真实 4×25 三分类（PASS/WARN/BLOCK 同场景） ----------------

_GUARD_WORKER_MIXED = """
import json, sys
from datetime import datetime, timezone
from deadlatch import Guard, Order, Portfolio
from deadlatch.engine import GuardEngine
from deadlatch.guard import load_policy_file
from deadlatch.rules.base import Rule, RuleOutcome
from deadlatch.rules.registry import standard_rule_registry

class TestWarnRule(Rule):
    # FIX-005-4：测试专用 Warning Rule，仅注入测试子进程；不修改任何生产规则语义
    rule_id = 'test_warn_rule'

    def evaluate(self, ctx):
        out = RuleOutcome(rule_id=self.rule_id)
        if str(ctx.order.symbol).startswith('WARN'):
            out.warnings.append({'rule_id': self.rule_id, 'severity': 'WARN',
                                 'detail': 'test-only warning (FIX-005-4)'})
        return out

policy_path, portfolio_path, audit_path, orders_json, out_path = sys.argv[1:6]
policy = load_policy_file(policy_path)
pf = Portfolio.from_dict(json.load(open(portfolio_path, encoding='utf-8')))
# 标准 12 条规则全部执行（强制规则集完整），另注入一条测试专用 Warning Rule
engine = GuardEngine(policy, rules=standard_rule_registry() + [TestWarnRule()])
guard = Guard(engine, audit_path=audit_path)
now = datetime(2026, 8, 29, 10, 0, 1, tzinfo=timezone.utc)
results = []
for o in json.loads(orders_json):
    r = guard.check(Order.from_dict(o), pf, now=now)
    results.append(r.to_dict())
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(results, f)
"""


def test_guard_concurrent_three_way_mixed_reconciled(tmp_path):
    """FIX-005-4：真实 4 进程 × 25 次 Guard.check()，100 个 Result 同时出现
    PASS/WARN/BLOCK，审计恰好 100 行、record_id 唯一、每行过 Schema，
    (input_hash, decision, exit_code) 与 Result 逐条精确对账。

    禁止直造 AuditRecord 或直接调用 append_audit()；WARN 由测试专用
    Warning Rule 经引擎 WARN/2 合成 + Guard 审计并发管线产生。"""
    mixed = []
    for i in range(25):
        if i < 13:
            mixed.append(_order())                 # PASS/0（真实标准规则全过）
        elif i < 19:
            mixed.append(_order(symbol="WARN1"))   # WARN/2（测试专用 Warning Rule）
        else:
            mixed.append(_order(quantity=1000))    # BLOCK/3（R3 真实拦截）
    out_files, audit = _run_guard_workers(tmp_path, mixed, worker=_GUARD_WORKER_MIXED)
    all_results = []
    for f in out_files:
        all_results.extend(json.loads(f.read_text(encoding="utf-8")))
    assert len(all_results) == 100
    from collections import Counter

    result_counts = Counter((r["decision"], r["exit_code"]) for r in all_results)
    assert result_counts[("PASS", 0)] == 52    # 13 × 4
    assert result_counts[("WARN", 2)] == 24    # 6 × 4
    assert result_counts[("BLOCK", 3)] == 24   # 6 × 4
    # 三分类必须同场景出现
    assert set(result_counts) == {("PASS", 0), ("WARN", 2), ("BLOCK", 3)}
    # 审计：恰好 100 行、record_id 唯一、每行过 Schema
    records = read_audit_records(audit)
    assert len(records) == 100
    assert len({r["record_id"] for r in records}) == 100
    for rec in records:
        assert AUDIT_VALIDATOR.is_valid(rec)
    # 逐条对账：审计 (input_hash, decision, exit_code) 计数与 100 个真实 Result 完全一致
    audit_sig = Counter((rec["input_hash"], rec["decision"], rec["exit_code"]) for rec in records)
    result_sig = Counter(
        (r["evidence"]["input_hash"], r["decision"], r["exit_code"]) for r in all_results
    )
    assert audit_sig == result_sig
