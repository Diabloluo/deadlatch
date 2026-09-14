""" §三/§四/§五 审计测试：记录 Schema、写失败降级矩阵、
4 进程并发 100 行原子写、30 天保留边界、malformed fail-closed、脱敏零明文。
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from tests.conftest import (
    NOW,
    fresh_order,
    fresh_portfolio,
    full_policy,
    make_engine,
    make_standard,
    option_order,
    portfolio,
    stock_order,
)
from deadlatch import Guard
from deadlatch._validation import InputValidationError
from deadlatch.audit import (
    AuditError,
    append_audit,
    build_audit_record,
    collect_sensitive_values,
    prune_audit,
    sanitize_text,
)
from deadlatch.guard import resolve_audit_path
from deadlatch.rules.kill_switch import KillSwitchRule
from deadlatch.rules.stubs import BoomRule, ViolationRule, WarnRule

REPO = Path(__file__).resolve().parents[1]
RESULT_SCHEMA = json.loads((REPO / "schemas" / "result.schema.json").read_text())
AUDIT_SCHEMA = json.loads((REPO / "schemas" / "audit-record.schema.json").read_text())
AUDIT_VALIDATOR = Draft202012Validator(AUDIT_SCHEMA)

POS_LONG_100 = {
    "symbol": "AAA",
    "instrument_type": "stock",
    "side": "long",
    "quantity": 100,
    "market_value": 19000.0,
    "currency": "USD",
}


def _bad_audit_path(tmp_path) -> str:
    """不可写的审计路径：父路径是普通文件 → mkdir/open 必失败。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    return str(blocker / "audit.jsonl")


def _guard(policy, rules=None, audit_path=None) -> Guard:
    engine = make_standard(policy) if rules is None else make_engine(policy, rules)
    return Guard(engine, audit_path=audit_path)


# ---------------- 记录构造与 Schema ----------------

def test_record_schema_valid_for_pass():
    guard = _guard(full_policy())
    pf = fresh_portfolio()
    result = guard._engine.check(fresh_order(), pf, now=NOW)
    rec = build_audit_record(fresh_order(), pf, guard.policy, result)
    assert AUDIT_VALIDATOR.is_valid(rec)
    assert rec["decision"] == "PASS" and rec["exit_code"] == 0
    assert rec["shadow_verdict"] is None
    assert rec["input_hash"] == result.evidence["input_hash"]


def test_record_schema_valid_for_block():
    guard = _guard(full_policy())
    order = fresh_order(quantity=1000)
    result = guard._engine.check(order, fresh_portfolio(), now=NOW)
    rec = build_audit_record(order, fresh_portfolio(), guard.policy, result)
    assert AUDIT_VALIDATOR.is_valid(rec)
    assert rec["exit_code"] == 3
    assert any(h["rule_id"] == "max_order_quantity" for h in rec["rule_hits"])


def test_record_schema_valid_for_exit4_and_5():
    guard = _guard(full_policy())
    bad = stock_order(currency="HKD")
    r4 = guard._engine.check(bad, portfolio(), now=NOW)
    rec4 = build_audit_record(bad, portfolio(), guard.policy, r4)
    assert AUDIT_VALIDATOR.is_valid(rec4) and rec4["exit_code"] == 4

    # 强制 exit 5：monkeypatch validate_inputs 抛非 InputValidationError
    import deadlatch.engine as engine_mod

    original = engine_mod.validate_inputs

    def boom(*a, **k):
        raise RuntimeError("boom")

    engine_mod.validate_inputs = boom
    try:
        r5 = guard._engine.check(fresh_order(), portfolio(), now=NOW)
    finally:
        engine_mod.validate_inputs = original
    rec5 = build_audit_record(fresh_order(), portfolio(), guard.policy, r5)
    assert AUDIT_VALIDATOR.is_valid(rec5) and rec5["exit_code"] == 5


def test_shadow_record_keeps_internal_verdict_and_hits():
    from tests.conftest import _policy
    from deadlatch import Policy

    pol = Policy.from_dict(_policy(mode="shadow"))
    guard = _guard(pol, rules=[ViolationRule()])
    result = guard._engine.check(fresh_order(), fresh_portfolio(), now=NOW)
    assert result.exit_code == 0 and result.shadow_verdict == "BLOCK"  # 投影
    rec = build_audit_record(fresh_order(), fresh_portfolio(), guard.policy, result)
    assert AUDIT_VALIDATOR.is_valid(rec)
    assert rec["decision"] == "PASS" and rec["exit_code"] == 0  # 对外
    assert rec["shadow_verdict"] == "BLOCK"  # 内部裁决保留
    assert rec["shadow_mode"] is True
    assert any(h["rule_id"] == "stub_violation" for h in rec["rule_hits"])  # 命中保留


def test_audit_append_and_input_hash_consistency(tmp_path):
    guard = _guard(full_policy(), audit_path=str(tmp_path / "audit.jsonl"))
    pf = fresh_portfolio()
    order = fresh_order()
    result = guard.check(order, pf, now=NOW)
    assert result.exit_code == 0
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert AUDIT_VALIDATOR.is_valid(rec)
    assert rec["input_hash"] == result.evidence["input_hash"]  # 与 Result 一致
    assert rec["evaluated_at"] == result.evaluated_at


def test_default_audit_path_is_documented_and_deterministic(monkeypatch):
    monkeypatch.delenv("DEADLATCH_AUDIT_PATH", raising=False)
    p = resolve_audit_path()
    assert p.name == "audit.jsonl" and ".deadlatch" in p.parts


# ---------------- 写失败降级矩阵（§3.4：严重度只升不降） ----------------

def _assert_degraded(result, expected_exit, expected_decision, shadow_verdict=None):
    assert result.exit_code == expected_exit
    assert result.decision == expected_decision
    assert any(w["rule_id"] == "audit_write_failed" for w in result.warnings)
    assert "audit_write_failed" in result.evidence["rule_evidence"]
    ev = result.evidence["rule_evidence"]["audit_write_failed"]
    assert any(e["name"] == "exception_type" for e in ev)
    assert any(e["name"] == "stage" and e["value"] == "audit" for e in ev)
    if shadow_verdict is not None:
        assert result.shadow_verdict == shadow_verdict
    # 降级后的 Result 仍必须通过 result.schema
    Draft202012Validator(RESULT_SCHEMA).validate(result.to_dict())


def test_degrade_pass_to_warn(tmp_path):
    guard = _guard(full_policy(), audit_path=_bad_audit_path(tmp_path))
    r = guard.check(fresh_order(), fresh_portfolio(), now=NOW)
    _assert_degraded(r, 2, "WARN")  # PASS/0 → WARN/2


def test_degrade_warn_stays_warn(tmp_path):
    guard = _guard(full_policy(), rules=[WarnRule()], audit_path=_bad_audit_path(tmp_path))
    r = guard.check(fresh_order(), fresh_portfolio(), now=NOW)
    _assert_degraded(r, 2, "WARN")


def test_degrade_block_unchanged(tmp_path):
    guard = _guard(full_policy(), rules=[ViolationRule()], audit_path=_bad_audit_path(tmp_path))
    r = guard.check(fresh_order(), fresh_portfolio(), now=NOW)
    _assert_degraded(r, 3, "BLOCK")


def test_degrade_exit4_unchanged(tmp_path):
    guard = _guard(full_policy(), audit_path=_bad_audit_path(tmp_path))
    r = guard.check(stock_order(currency="HKD"), portfolio(), now=NOW)
    _assert_degraded(r, 4, "BLOCK")


def test_degrade_exit5_unchanged(tmp_path):
    guard = _guard(full_policy(), rules=[BoomRule()], audit_path=_bad_audit_path(tmp_path))
    r = guard.check(fresh_order(), fresh_portfolio(), now=NOW)
    _assert_degraded(r, 5, "BLOCK")


def test_degrade_shadow_projected_pass_to_warn_keeps_verdict(tmp_path):
    from tests.conftest import _policy
    from deadlatch import Policy

    pol = Policy.from_dict(_policy(mode="shadow"))
    guard = _guard(pol, rules=[ViolationRule()], audit_path=_bad_audit_path(tmp_path))
    r = guard.check(fresh_order(), fresh_portfolio(), now=NOW)
    assert r.shadow_mode is True
    _assert_degraded(r, 2, "WARN", shadow_verdict="BLOCK")  # 投影 PASS → WARN/2，裁决保留


def test_degrade_kill_switch_block_unchanged(tmp_path):
    from tests.conftest import _policy
    from deadlatch import Policy

    pol = Policy.from_dict(_policy(kill_switch="full"))
    guard = _guard(pol, rules=[KillSwitchRule()], audit_path=_bad_audit_path(tmp_path))
    r = guard.check(fresh_order(), fresh_portfolio(), now=NOW)
    _assert_degraded(r, 3, "BLOCK")  # kill switch 不得因审计失败变 WARN/PASS


def test_degrade_does_not_leak_exception_text(tmp_path):
    guard = _guard(full_policy(), audit_path=_bad_audit_path(tmp_path))
    r = guard.check(fresh_order(), fresh_portfolio(), now=NOW)
    ev = json.dumps(r.evidence, ensure_ascii=False)
    assert "blocker" not in ev  # 路径不得进入 evidence
    assert "audit.jsonl" not in ev


# ---------------- 4 进程并发 100 行原子写（§3.2） ----------------

def _concurrent_script() -> str:
    return (
        "import sys, uuid\n"
        "from deadlatch.audit import append_audit\n"
        "path, n = sys.argv[1], int(sys.argv[2])\n"
        "for _ in range(n):\n"
        "    rec = {'schema_version':1,'record_id':uuid.uuid4().hex,"
        "'evaluated_at':'2026-08-29T10:00:00Z','input_hash':'0'*64,"
        "'decision':'PASS','shadow_mode':False,'shadow_verdict':None,"
        "'exit_code':0,'policy_version':'1.0.0','rule_hits':[]}\n"
        "    append_audit(path, rec)\n"
    )


def test_concurrent_4_processes_100_lines(tmp_path):
    path = tmp_path / "audit.jsonl"
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _concurrent_script(), str(path), "25"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        for _ in range(4)
    ]
    for p in procs:
        _, err = p.communicate(timeout=60)
        assert p.returncode == 0, err.decode()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 100  # 恰好 100 条，零丢失
    records = [json.loads(line) for line in lines]
    ids = [r["record_id"] for r in records]
    assert len(set(ids)) == 100  # record_id 唯一
    for r in records:
        assert AUDIT_VALIDATOR.is_valid(r)  # 每行合法 JSON 且过 Schema
    assert all(line.endswith("}") for line in lines)  # 行完整（无拼接/截断）


# ---------------- 30 天保留（§五） ----------------

def _write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _record_at(ts: str, rid: str) -> str:
    # audit-record.schema：record_id 长度 8–64——短 ID 补零到 8 字符
    if len(rid) < 8:
        rid = rid + "0" * (8 - len(rid))
    return json.dumps(
        {
            "schema_version": 1, "record_id": rid, "evaluated_at": ts,
            "input_hash": "0" * 64, "decision": "PASS", "shadow_mode": False,
            "shadow_verdict": None, "exit_code": 0, "policy_version": "1.0.0",
            "rule_hits": [],
        },
        sort_keys=True, separators=(",", ":"),
    )


def _ts(offset: timedelta) -> str:
    return (NOW + offset).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_prune_30day_boundary(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write_lines(path, [
        _record_at(_ts(timedelta(seconds=-1)), "a"),        # 保留
        _record_at(_ts(timedelta(days=-20)), "b"),          # 保留
        _record_at(_ts(timedelta(days=-30)), "c"),          # 恰好 30 天 → 保留
        _record_at(_ts(timedelta(days=-31)), "d"),          # 早于 30 天 → 删除
        _record_at(_ts(timedelta(days=1)), "e"),            # 未来 → 保留
    ])
    result = prune_audit(path, now=NOW)
    assert result == {"removed": 1, "kept": 4, "future": 1}
    kept = [json.loads(l)["record_id"] for l in path.read_text(encoding="utf-8").splitlines()]
    assert kept == ["a0000000", "b0000000", "c0000000", "e0000000"]  # d 被删，未来记录未误删


def test_prune_missing_file_noop(tmp_path):
    assert prune_audit(tmp_path / "nope.jsonl", now=NOW) == {"removed": 0, "kept": 0, "future": 0}


def test_prune_malformed_fail_closed_keeps_file(tmp_path):
    path = tmp_path / "audit.jsonl"
    good = _record_at(_ts(timedelta(days=-40)), "old")
    path.write_text(good + "\nnot-json-line\n", encoding="utf-8")
    with pytest.raises(AuditError):
        prune_audit(path, now=NOW)
    # 原文件未改动（可恢复），malformed 行未被静默丢弃
    assert path.read_text(encoding="utf-8") == good + "\nnot-json-line\n"


def test_prune_only_touches_target_file(tmp_path):
    path = tmp_path / "audit.jsonl"
    other = tmp_path / "other.jsonl"
    _write_lines(path, [_record_at(_ts(timedelta(days=-31)), "old")])
    other.write_text("keep-me", encoding="utf-8")
    prune_audit(path, now=NOW)
    kept = path.read_text(encoding="utf-8")
    assert "old" not in kept  # 早于 30 天已清理
    assert other.read_text(encoding="utf-8") == "keep-me"  # 不操作其他文件


def test_append_prunes_old_record_small_file(tmp_path):
    # FIX-003-1：小文件（远小于 256KB）append 后同样执行 30 天保留
    path = tmp_path / "audit.jsonl"
    _write_lines(path, [_record_at(_ts(timedelta(days=-31)), "old")])
    assert path.stat().st_size < 256 * 1024

    guard = _guard(full_policy(), audit_path=str(path))
    result = guard.check(fresh_order(), fresh_portfolio(), now=NOW)
    assert result.exit_code == 0  # 审计成功，无降级

    kept = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    assert len(kept) == 1  # 31 天前的旧记录被清理，只剩本次新记录
    assert kept[0]["record_id"] != "old"


def test_append_keeps_exactly_30day_record(tmp_path):
    # 恰好 30 天保留（≥ cutoff）；早于 30 天删除
    path = tmp_path / "audit.jsonl"
    _write_lines(path, [
        _record_at(_ts(timedelta(days=-30)), "exact-30d"),
        _record_at(_ts(timedelta(days=-31)), "older"),
        _record_at(_ts(timedelta(days=1)), "future"),
    ])
    rec = json.loads(_record_at(_ts(timedelta(seconds=-1)), "new"))
    append_audit(path, rec, now=NOW)
    kept = [json.loads(l)["record_id"] for l in path.read_text(encoding="utf-8").splitlines()]
    assert "exact-30d" in kept    # 恰好 30 天保留
    assert "older000" not in kept  # 早于 30 天删除
    assert "future00" in kept      # 未来记录保留
    assert "new00000" in kept


def test_append_malformed_small_file_fail_closed(tmp_path):
    # malformed 小文件：append 事务 fail-closed，原文件不被半写/破坏，Result 可见降级
    path = tmp_path / "audit.jsonl"
    path.write_text("this-is-not-json\n", encoding="utf-8")
    guard = _guard(full_policy(), audit_path=str(path))
    result = guard.check(fresh_order(), fresh_portfolio(), now=NOW)
    assert result.exit_code == 2  # PASS → WARN/2（audit_write_failed）
    assert any(w["rule_id"] == "audit_write_failed" for w in result.warnings)
    assert path.read_text(encoding="utf-8") == "this-is-not-json\n"  # 原文件保持可恢复


@pytest.mark.requires_nonroot
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="requires non-root POSIX permission bits",
)
def test_failed_tx_no_partial_record_no_contradiction(tmp_path):
    # 清理/写入失败：磁盘不得出现本次记录，Result 降级可见 → 两者不矛盾
    d = tmp_path / "ro"
    d.mkdir()
    path = d / "audit.jsonl"
    path.write_text("", encoding="utf-8")  # 已存在；目录 555：可读不可写 → 事务写 tmp 失败
    d.chmod(0o555)
    try:
        guard = _guard(full_policy(), audit_path=str(path))
        result = guard.check(fresh_order(), fresh_portfolio(), now=NOW)
        assert result.exit_code == 2
        assert any(w["rule_id"] == "audit_write_failed" for w in result.warnings)
        # 磁盘无本次 AuditRecord（原文件未被半写/破坏）
        assert path.read_text(encoding="utf-8") == ""
    finally:
        d.chmod(0o755)


# ---------------- FIX-005-3：审计追加最终大小越界（UTF-8 字节数） ----------------

def _line_exact_bytes(target: int) -> str:
    """构造一条序列化后（含换行）字节数恰好 == target 的合法记录行。

    record_id 固定 8 字符，长度由 rule_hits[0].detail（无 maxLength 约束）精确控制。
    """
    n = 0
    while True:
        rec = {
            "schema_version": 1,
            "record_id": "rrrrrrrr",
            "evaluated_at": "2026-08-29T10:00:00Z",
            "input_hash": "0" * 64,
            "decision": "PASS", "shadow_mode": False, "shadow_verdict": None,
            "exit_code": 0, "policy_version": "1.0.0",
            "rule_hits": [{"rule_id": "kill_switch", "severity": "BLOCK", "detail": "x" * n}],
        }
        line = json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n"
        size = len(line.encode("utf-8"))
        if size == target:
            return line
        if size > target:
            raise AssertionError(f"无法构造恰好 {target} 字节的行（当前 {size}）")
        n += 1


def _line_with_bytes(target: int) -> str:
    """构造一条序列化后（含换行）字节数 >= target 的合法记录行（精确到目标值）。"""
    return _line_exact_bytes(target)


def test_append_final_size_exact_limit_succeeds(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    monkeypatch.setattr(audit_mod, "MAX_AUDIT_FILE_BYTES", 800)
    path = tmp_path / "audit.jsonl"
    # 预填 1 条，再追加一条使最终字节数 == 800（恰好等于上限 → 成功）
    first = _line_exact_bytes(400)
    path.write_text(first, encoding="utf-8")
    cur = len(first.encode("utf-8"))
    second = _line_exact_bytes(800 - cur)  # 恰好补足到上限
    append_audit(path, json.loads(second), now=datetime(2026, 8, 29, 10, 0, 1, tzinfo=timezone.utc))
    assert path.exists()
    total = len(path.read_bytes())
    assert total == 800  # 恰好等于上限可成功


def test_append_final_size_over_limit_rejected_file_unchanged(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    monkeypatch.setattr(audit_mod, "MAX_AUDIT_FILE_BYTES", 800)
    path = tmp_path / "audit.jsonl"
    first = _line_exact_bytes(400)
    path.write_text(first, encoding="utf-8")
    before = path.read_bytes()
    # 追加一条使最终 > 800（+1 字节）→ 拒绝
    big = _line_exact_bytes(401)
    with pytest.raises(AuditError) as ei:
        append_audit(path, json.loads(big),
                     now=datetime(2026, 8, 29, 10, 0, 1, tzinfo=timezone.utc))
    assert "大小" in str(ei.value)
    assert path.read_bytes() == before  # 原文件字节级不变
    assert not (tmp_path / "audit.jsonl.tmp").exists()  # 不留临时文件


def test_append_final_size_unicode_multibyte(tmp_path, monkeypatch):
    # Unicode 多字节（中文 detail 每字 3 字节）：最终大小以实际编码字节数为准
    import deadlatch.audit as audit_mod

    monkeypatch.setattr(audit_mod, "MAX_AUDIT_FILE_BYTES", 900)
    path = tmp_path / "audit.jsonl"
    first = _line_exact_bytes(400)
    path.write_text(first, encoding="utf-8")
    cur = len(first.encode("utf-8"))
    # 中文 detail 记录：找 ≤900 的最大构造（n 步进 +3 字节/字）
    n = 1
    last_line = None
    while True:
        rec = {
            "schema_version": 1, "record_id": "rrrrrrrr",
            "evaluated_at": "2026-08-29T10:00:00Z", "input_hash": "0" * 64,
            "decision": "BLOCK", "shadow_mode": False, "shadow_verdict": None,
            "exit_code": 3, "policy_version": "1.0.0",
            "rule_hits": [{"rule_id": "kill_switch", "severity": "BLOCK",
                           "detail": "中" * n}],
        }
        line = json.dumps(rec, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        size = len(line.encode("utf-8"))
        if cur + size == 900:
            last_line = line
            break
        if cur + size > 900:
            break
        last_line = line
        n += 1
    assert last_line is not None, "无法构造接近上限的中文记录"
    append_audit(path, json.loads(last_line),
                 now=datetime(2026, 8, 29, 10, 0, 1, tzinfo=timezone.utc))
    assert len(path.read_bytes()) <= 900  # 以实际编码字节数为准（非字符数）
    # 再 +1 字（3 字节）→ 越界拒绝
    over_rec = json.loads(last_line)
    over_rec["rule_hits"][0]["detail"] = "中" * (n + 1)
    over_line = json.dumps(over_rec, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")) + "\n"
    before = path.read_bytes()
    with pytest.raises(AuditError):
        append_audit(path, json.loads(over_line),
                     now=datetime(2026, 8, 29, 10, 0, 1, tzinfo=timezone.utc))
    assert path.read_bytes() == before


def test_append_final_size_after_prune_falls_under_limit(tmp_path, monkeypatch):
    # 旧记录超过上限但 30 天过滤清理后重落上限内 → 成功（不删 30 天保留）
    import deadlatch.audit as audit_mod

    monkeypatch.setattr(audit_mod, "MAX_AUDIT_FILE_BYTES", 800)
    path = tmp_path / "audit.jsonl"
    # 31 天前旧记录（会被清理）占 600 字节 + 新记录 400 字节 → 过滤后 400 < 800
    old = _line_exact_bytes(600)
    old_rec = json.loads(old)
    old_rec["evaluated_at"] = "2026-07-01T00:00:00Z"  # 31 天前
    old_line = json.dumps(old_rec, sort_keys=True, separators=(",", ":")) + "\n"
    path.write_text(old_line, encoding="utf-8")
    new = _line_exact_bytes(400)
    append_audit(path, json.loads(new), now=datetime(2026, 8, 29, 10, 0, 1, tzinfo=timezone.utc))
    kept = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    assert len(kept) == 1  # 旧记录被 30 天清理删除，新记录成功写入
    assert len(path.read_bytes()) < 800


def test_append_over_limit_guard_degrades_warn(tmp_path, monkeypatch):
    # Guard.check 遇到最终大小越界 → audit_write_failed 降级（WARN/2），不逃逸
    import deadlatch.audit as audit_mod

    monkeypatch.setattr(audit_mod, "MAX_AUDIT_FILE_BYTES", 800)
    path = tmp_path / "audit.jsonl"
    first = _line_exact_bytes(600)
    path.write_text(first, encoding="utf-8")
    before = path.read_bytes()
    guard = _guard(full_policy(), audit_path=str(path))
    result = guard.check(fresh_order(), fresh_portfolio(), now=NOW)
    assert result.exit_code == 2  # PASS → WARN/2（严重度只升不降）
    assert any(w["rule_id"] == "audit_write_failed" for w in result.warnings)
    assert path.read_bytes() == before  # 原文件不变（与返回 Result 不矛盾）


def test_append_existing_schema_invalid_line_fail_closed(tmp_path):
    # 既有行 JSON 合法但 Schema 非法（exit_code=99）→ append 事务 fail-closed
    path = tmp_path / "audit.jsonl"
    bad = json.loads(_record_at(_ts(timedelta(seconds=-1)), "schema-bad"))
    bad["exit_code"] = 99  # audit-record.schema enum 非法
    path.write_text(json.dumps(bad, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8")

    guard = _guard(full_policy(), audit_path=str(path))
    result = guard.check(fresh_order(), fresh_portfolio(), now=NOW)
    assert result.exit_code == 2  # PASS → WARN/2（audit_write_failed 可见降级）
    assert any(w["rule_id"] == "audit_write_failed" for w in result.warnings)
    # 原文件不变（未被半写/覆盖），非法行未静默丢弃
    assert path.read_text(encoding="utf-8") == (
        json.dumps(bad, sort_keys=True, separators=(",", ":")) + "\n"
    )


def test_append_new_record_schema_invalid_rejected(tmp_path):
    # 传入 append 的新记录 Schema 非法 → AuditError，文件不写盘
    path = tmp_path / "audit.jsonl"
    bad = json.loads(_record_at(_ts(timedelta(seconds=-1)), "new-bad"))
    bad["decision"] = "NOT-A-DECISION"  # schema enum 非法
    with pytest.raises(AuditError):
        append_audit(path, bad, now=NOW)
    assert not path.exists() or path.read_text(encoding="utf-8") == ""  # 事务未写盘


def test_append_new_record_schema_valid_written(tmp_path):
    # 对照组：合法新记录正常写入（校验不误伤）
    path = tmp_path / "audit.jsonl"
    rec = json.loads(_record_at(_ts(timedelta(seconds=-1)), "good"))
    append_audit(path, rec, now=NOW)
    kept = [json.loads(l)["record_id"] for l in path.read_text(encoding="utf-8").splitlines()]
    assert kept == ["good0000"]


# ---------------- 脱敏（§四） ----------------

def test_sanitize_known_sensitive_values():
    s = sanitize_text("AAA 单标的敞口超限 190000", ["AAA"])
    assert "AAA" not in s and "<redacted>" in s


def test_sanitize_tokens_paths_and_credentials():
    cases = [
        ("token=abc123def456ghi", "abc123def456ghi"),
        ("api_key: sk-1234567890abcdef1234", "sk-1234567890abcdef1234"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"),
        ("备份在 /Users/DEADLATCH_TEST_USER/.ssh/id_rsa", "/Users/DEADLATCH_TEST_USER/.ssh/id_rsa"),
        ("secret=super-secret-value", "super-secret-value"),
    ]
    for text, secret in cases:
        out = sanitize_text(text, [])
        assert secret not in out, f"{secret!r} 未被过滤: {out}"
        assert "<redacted>" in out


def test_collect_sensitive_includes_symbol_and_underlying():
    order = option_order()
    pf = portfolio(positions=[POS_LONG_100])
    vals = collect_sensitive_values(order, pf)
    assert "AAA" in vals  # symbol
    assert "AAA 260918P00190000" in vals  # 期权 symbol
    assert "USD" not in vals  # 币种不是敏感值


def test_audit_file_zero_plaintext_symbol(tmp_path):
    # 故意注入假 Token 形态 symbol，触发 R5 → 审计文件必须零明文
    fake = "sk-FAKEKEY1234567890abcdef"
    guard = _guard(full_policy(), audit_path=str(tmp_path / "audit.jsonl"))
    order = fresh_order(symbol=fake, quantity=1000, price=190.0)
    result = guard.check(order, fresh_portfolio(), now=NOW)
    assert result.exit_code == 3  # R5 BLOCK（detail 含 symbol）
    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert fake not in raw  # 零明文
    assert "<redacted>" in raw  # 脱敏占位符可见
    rec = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert AUDIT_VALIDATOR.is_valid(rec)


def test_audit_file_zero_plaintext_absolute_path(tmp_path):
    guard = _guard(full_policy(), audit_path=str(tmp_path / "audit.jsonl"))
    # symbol 形态的绝对路径（schema 允许任意字符串）
    order = fresh_order(symbol="/tmp/evil/secret/path", quantity=1000, price=190.0)
    result = guard.check(order, fresh_portfolio(), now=NOW)
    assert result.exit_code == 3
    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "/tmp/evil/secret/path" not in raw
    assert "input_hash" in raw  # 摘要保留


def test_audit_record_has_no_order_fulltext(tmp_path):
    guard = _guard(full_policy(), audit_path=str(tmp_path / "audit.jsonl"))
    order = fresh_order(quantity=1000, price=190.0)
    guard.check(order, fresh_portfolio(), now=NOW)
    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "market_value" not in raw  # 不写 portfolio 全文/持仓列表
    assert '"positions"' not in raw
    assert "day_start_equity" not in raw  # policy/portfolio 字段不落盘
    assert '"limits"' not in raw  # policy 全文不落盘
    assert '"order_summary"' not in raw  # 订单摘要不落盘


# ---------------- FIX-003-2：Cookie / 任意绝对路径 / policy_version 脱敏 ----------------

def test_sanitize_cookie_forms():
    cases = [
        "Cookie: sessionid=FAKECOOKIE123456789",
        "Set-Cookie: auth=FAKECOOKIE123456789; HttpOnly",
        "sessionid=FAKECOOKIE123456789",
        "session=SECRETSESSIONVALUE42",
    ]
    for text in cases:
        out = sanitize_text(text, [])
        assert "FAKECOOKIE123456789" not in out
        assert "SECRETSESSIONVALUE42" not in out
        assert "<redacted>" in out


def test_sanitize_arbitrary_absolute_paths():
    cases = [
        "/Applications/Secret App/data.json",   # 非 /Users|/tmp|/var 前缀 + 含空格
        "/usr/local/bin/deadlatch/private.key",
        "/Users/DEADLATCH_TEST_USER/.ssh/id_rsa",
        "/opt/tools/account-secrets/token.dat",
    ]
    for path in cases:
        out = sanitize_text(f"配置读取自 {path}", [])
        assert path not in out
        assert "<redacted>" in out


def test_sanitize_does_not_break_ratio_text():
    # 规则 detail 的 " / " 单斜杠形态不得被误判为绝对路径
    out = sanitize_text("组合总敞口占比 1.53 > 上限 0.6（post_total_gross 190000.0 / equity 123456.78）", [])
    assert "post_total_gross 190000.0" in out  # 未被破坏
    assert "123456.78" in out


def test_audit_policy_version_token_redacted(tmp_path):
    # policy.version = sk-FAKETOKEN... 不得原样写入审计 policy_version
    pol = full_policy(version="sk-FAKETOKEN1234567890")
    guard = _guard(pol, audit_path=str(tmp_path / "audit.jsonl"))
    guard.check(fresh_order(), fresh_portfolio(), now=NOW)
    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "sk-FAKETOKEN1234567890" not in raw
    rec = json.loads(raw.splitlines()[0])
    assert "<redacted>" in rec["policy_version"]
    assert AUDIT_VALIDATOR.is_valid(rec)


def test_audit_cookie_in_rule_detail_zero_plaintext(tmp_path):
    # 故意注入 Cookie 形态 symbol → 规则 detail 含之 → 审计零明文
    guard = _guard(full_policy(), audit_path=str(tmp_path / "audit.jsonl"))
    order = fresh_order(symbol="Cookie: sessionid=FAKECOOKIE123456789", quantity=1000, price=190.0)
    result = guard.check(order, fresh_portfolio(), now=NOW)
    assert result.exit_code == 3  # R5 BLOCK（detail 含 symbol）
    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "FAKECOOKIE123456789" not in raw
    assert "Cookie" not in raw
    assert "<redacted>" in raw
    rec = json.loads(raw.splitlines()[0])
    assert AUDIT_VALIDATOR.is_valid(rec)


def test_audit_absolute_path_symbol_zero_plaintext(tmp_path):
    # /Applications/Secret App/data.json 形态的 symbol → 审计零明文
    guard = _guard(full_policy(), audit_path=str(tmp_path / "audit.jsonl"))
    order = fresh_order(symbol="/Applications/Secret App/data.json", quantity=1000, price=190.0)
    result = guard.check(order, fresh_portfolio(), now=NOW)
    assert result.exit_code == 3
    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "/Applications/Secret App/data.json" not in raw
    assert "<redacted>" in raw
