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
    compute_record_hash,
    prune_audit,
    read_audit_records,
    sanitize_text,
    utc_shard_path,
    _list_shards,
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


def _collection_text(path: Path) -> str:
    chunks = []
    if path.is_file():
        chunks.append(path.read_text(encoding="utf-8"))
    for _, shard in _list_shards(path):
        chunks.append(shard.read_text(encoding="utf-8"))
    return "".join(chunks)


def _collection_records(path: Path):
    return read_audit_records(path)


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
    recs = _collection_records(tmp_path / "audit.jsonl")
    assert len(recs) == 1
    rec = recs[0]
    assert AUDIT_VALIDATOR.is_valid(rec)
    assert rec["schema_version"] == 2
    assert rec["input_hash"] == result.evidence["input_hash"]  # 与 Result 一致
    assert rec["evaluated_at"] == result.evaluated_at
    assert rec["record_hash"] == compute_record_hash(rec)


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
    records = read_audit_records(path)
    assert len(records) == 100  # 恰好 100 条，零丢失
    ids = [r["record_id"] for r in records]
    assert len(set(ids)) == 100  # record_id 唯一
    for r in records:
        assert AUDIT_VALIDATOR.is_valid(r)  # 每行合法 JSON 且过 Schema
        assert r["schema_version"] == 2


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
    from deadlatch.audit import _state_path
    _state_path(path).unlink(missing_ok=True)
    old_shard = utc_shard_path(path, NOW - timedelta(days=31))
    keep_shard = utc_shard_path(path, NOW)
    future_shard = utc_shard_path(path, NOW + timedelta(days=1))
    old_shard.write_text(_record_at(_ts(timedelta(days=-31)), "old") + "\n", encoding="utf-8")
    keep_shard.write_text(_record_at(_ts(timedelta(seconds=-1)), "keep") + "\n", encoding="utf-8")
    future_shard.write_text(_record_at(_ts(timedelta(days=1)), "fut") + "\n", encoding="utf-8")
    path.write_text(_record_at(_ts(timedelta(days=-40)), "leg") + "\n", encoding="utf-8")
    result = prune_audit(path, now=NOW)
    assert result["removed_segments"] == 1
    assert result["kept_segments"] == 2
    assert result["future_segments"] == 1
    assert result["status"] == "pruned"
    assert not old_shard.exists()
    assert keep_shard.exists() and future_shard.exists()
    assert path.exists()  # legacy 不被自动删除
    assert "leg" in path.read_text(encoding="utf-8")


def test_prune_missing_file_noop(tmp_path):
    result = prune_audit(tmp_path / "nope.jsonl", now=NOW)
    assert result["removed_segments"] == 0
    assert result["kept_segments"] == 0
    assert result["future_segments"] == 0
    assert result["status"] == "clean"


def test_prune_does_not_read_shard_bodies(tmp_path):
    path = tmp_path / "audit.jsonl"
    from deadlatch.audit import _state_path
    _state_path(path).unlink(missing_ok=True)
    old_shard = utc_shard_path(path, NOW - timedelta(days=40))
    old_shard.write_text("not-json-line\n", encoding="utf-8")
    path.write_text("also-not-json\n", encoding="utf-8")
    result = prune_audit(path, now=NOW)
    assert result["removed_segments"] == 1
    assert not old_shard.exists()
    assert path.read_text(encoding="utf-8") == "also-not-json\n"


def test_prune_only_touches_target_shards(tmp_path):
    path = tmp_path / "audit.jsonl"
    from deadlatch.audit import _state_path
    _state_path(path).unlink(missing_ok=True)
    other = tmp_path / "other.jsonl"
    old_shard = utc_shard_path(path, NOW - timedelta(days=31))
    old_shard.write_text(_record_at(_ts(timedelta(days=-31)), "old") + "\n", encoding="utf-8")
    other.write_text("keep-me", encoding="utf-8")
    prune_audit(path, now=NOW)
    assert not old_shard.exists()
    assert other.read_text(encoding="utf-8") == "keep-me"


def test_append_does_not_prune_old_shard(tmp_path):
    path = tmp_path / "audit.jsonl"
    old_shard = utc_shard_path(path, NOW - timedelta(days=31))
    old_shard.write_text(_record_at(_ts(timedelta(days=-31)), "old") + "\n", encoding="utf-8")
    guard = _guard(full_policy(), audit_path=str(path))
    result = guard.check(fresh_order(), fresh_portfolio(), now=NOW)
    assert result.exit_code == 0
    assert old_shard.exists()  # append 不再清理历史
    recs = [r for r in _collection_records(path) if r.get("schema_version") == 2]
    assert len(recs) == 1


def test_append_keeps_legacy_and_future_shards(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write_lines(path, [
        _record_at(_ts(timedelta(days=-30)), "exact-30d"),
        _record_at(_ts(timedelta(days=-31)), "older"),
        _record_at(_ts(timedelta(days=1)), "future"),
    ])
    rec = json.loads(_record_at(_ts(timedelta(seconds=-1)), "new"))
    append_audit(path, rec, now=NOW)
    legacy_ids = [json.loads(l)["record_id"] for l in path.read_text(encoding="utf-8").splitlines()]
    assert "exact-30d" in legacy_ids
    assert "older000" in legacy_ids
    assert "future00" in legacy_ids
    assert any(r["record_id"].startswith("new") or r["record_id"] == "new00000"
               for r in _collection_records(path))


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

def _v2_line_exact_bytes(target: int, prev_hash=None) -> str:
    n = 0
    last_size = None
    while True:
        rec = {
            "schema_version": 2,
            "record_id": "rrrrrrrr",
            "evaluated_at": "2026-08-29T10:00:00Z",
            "input_hash": "0" * 64,
            "decision": "PASS", "shadow_mode": False, "shadow_verdict": None,
            "exit_code": 0, "policy_version": "1.0.0",
            "rule_hits": [{"rule_id": "kill_switch", "severity": "BLOCK", "detail": "x" * n}],
            "prev_hash": prev_hash,
        }
        rec["record_hash"] = compute_record_hash(rec)
        line = json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        size = len(line.encode("utf-8"))
        if size == target:
            return line
        if last_size is not None and size > target:
            raise AssertionError(f"无法构造恰好 {target} 字节的行（当前 {size}）")
        last_size = size
        n += 1


def _line_exact_bytes(target: int) -> str:
    return _v2_line_exact_bytes(target, prev_hash=None)


def _line_with_bytes(target: int) -> str:
    """构造一条序列化后（含换行）字节数 >= target 的合法记录行（精确到目标值）。"""
    return _line_exact_bytes(target)


def test_append_final_size_exact_limit_succeeds(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    monkeypatch.setattr(audit_mod, "MAX_AUDIT_FILE_BYTES", 1200)
    path = tmp_path / "audit.jsonl"
    now = datetime(2026, 8, 29, 10, 0, 1, tzinfo=timezone.utc)
    shard = utc_shard_path(path, now)
    first = _v2_line_exact_bytes(600, prev_hash=None)
    shard.write_text(first, encoding="utf-8")
    first_hash = json.loads(first)["record_hash"]
    second = _v2_line_exact_bytes(600, prev_hash=first_hash)
    append_audit(path, json.loads(second), now=now)
    assert shard.exists()
    total = len(shard.read_bytes())
    assert total == 1200


def test_append_final_size_over_limit_rejected_file_unchanged(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    monkeypatch.setattr(audit_mod, "MAX_AUDIT_FILE_BYTES", 1200)
    path = tmp_path / "audit.jsonl"
    now = datetime(2026, 8, 29, 10, 0, 1, tzinfo=timezone.utc)
    shard = utc_shard_path(path, now)
    first = _v2_line_exact_bytes(600, prev_hash=None)
    shard.write_text(first, encoding="utf-8")
    before = shard.read_bytes()
    first_hash = json.loads(first)["record_hash"]
    big = _v2_line_exact_bytes(601, prev_hash=first_hash)
    with pytest.raises(AuditError) as ei:
        append_audit(path, json.loads(big), now=now)
    assert "大小" in str(ei.value)
    assert shard.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_append_final_size_unicode_multibyte(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    monkeypatch.setattr(audit_mod, "MAX_AUDIT_FILE_BYTES", 1400)
    path = tmp_path / "audit.jsonl"
    now = datetime(2026, 8, 29, 10, 0, 1, tzinfo=timezone.utc)
    shard = utc_shard_path(path, now)
    first = _v2_line_exact_bytes(600, prev_hash=None)
    shard.write_text(first, encoding="utf-8")
    first_hash = json.loads(first)["record_hash"]
    cur = len(first.encode("utf-8"))
    n = 1
    last_rec = None
    last_n = 1
    while True:
        rec = {
            "schema_version": 2, "record_id": "rrrrrrrr",
            "evaluated_at": "2026-08-29T10:00:00Z", "input_hash": "0" * 64,
            "decision": "BLOCK", "shadow_mode": False, "shadow_verdict": None,
            "exit_code": 3, "policy_version": "1.0.0",
            "rule_hits": [{"rule_id": "kill_switch", "severity": "BLOCK",
                           "detail": "中" * n}],
            "prev_hash": first_hash,
        }
        rec["record_hash"] = compute_record_hash(rec)
        line = json.dumps(rec, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        size = len(line.encode("utf-8"))
        if cur + size == 1400:
            last_rec = rec
            last_n = n
            break
        if cur + size > 1400:
            break
        last_rec = rec
        last_n = n
        n += 1
    assert last_rec is not None, "无法构造接近上限的中文记录"
    append_audit(path, last_rec, now=now)
    assert len(shard.read_bytes()) <= 1400
    over = dict(last_rec)
    over["rule_hits"] = [{"rule_id": "kill_switch", "severity": "BLOCK",
                          "detail": "中" * (last_n + 1)}]
    over.pop("record_hash", None)
    over.pop("prev_hash", None)
    before = shard.read_bytes()
    with pytest.raises(AuditError):
        append_audit(path, over, now=now)
    assert shard.read_bytes() == before


def test_append_old_shard_does_not_count_toward_today_cap(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    monkeypatch.setattr(audit_mod, "MAX_AUDIT_FILE_BYTES", 1200)
    path = tmp_path / "audit.jsonl"
    now = datetime(2026, 8, 29, 10, 0, 1, tzinfo=timezone.utc)
    old = utc_shard_path(path, now - timedelta(days=31))
    old.write_text(_v2_line_exact_bytes(600, prev_hash=None), encoding="utf-8")
    new = json.loads(_record_at("2026-08-29T10:00:00Z", "newrec00"))
    append_audit(path, new, now=now)
    today = utc_shard_path(path, now)
    assert today.exists()
    assert old.exists()
    assert len(today.read_bytes()) < 1200


def test_append_over_limit_guard_degrades_warn(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    monkeypatch.setattr(audit_mod, "MAX_AUDIT_FILE_BYTES", 700)
    path = tmp_path / "audit.jsonl"
    shard = utc_shard_path(path, NOW)
    first = _v2_line_exact_bytes(600, prev_hash=None)
    shard.write_text(first, encoding="utf-8")
    before = shard.read_bytes()
    guard = _guard(full_policy(), audit_path=str(path))
    result = guard.check(fresh_order(), fresh_portfolio(), now=NOW)
    assert result.exit_code == 2
    assert any(w["rule_id"] == "audit_write_failed" for w in result.warnings)
    assert shard.read_bytes() == before


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
    kept = [r["record_id"] for r in _collection_records(path)]
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
    raw = _collection_text(tmp_path / "audit.jsonl")
    assert fake not in raw  # 零明文
    assert "<redacted>" in raw  # 脱敏占位符可见
    rec = _collection_records(tmp_path / "audit.jsonl")[0]
    assert AUDIT_VALIDATOR.is_valid(rec)


def test_audit_file_zero_plaintext_absolute_path(tmp_path):
    guard = _guard(full_policy(), audit_path=str(tmp_path / "audit.jsonl"))
    # symbol 形态的绝对路径（schema 允许任意字符串）
    order = fresh_order(symbol="/tmp/evil/secret/path", quantity=1000, price=190.0)
    result = guard.check(order, fresh_portfolio(), now=NOW)
    assert result.exit_code == 3
    raw = _collection_text(tmp_path / "audit.jsonl")
    assert "/tmp/evil/secret/path" not in raw
    assert "input_hash" in raw  # 摘要保留


def test_audit_record_has_no_order_fulltext(tmp_path):
    guard = _guard(full_policy(), audit_path=str(tmp_path / "audit.jsonl"))
    order = fresh_order(quantity=1000, price=190.0)
    guard.check(order, fresh_portfolio(), now=NOW)
    raw = _collection_text(tmp_path / "audit.jsonl")
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
    raw = _collection_text(tmp_path / "audit.jsonl")
    assert "sk-FAKETOKEN1234567890" not in raw
    rec = _collection_records(tmp_path / "audit.jsonl")[0]
    assert "<redacted>" in rec["policy_version"]
    assert AUDIT_VALIDATOR.is_valid(rec)


def test_audit_cookie_in_rule_detail_zero_plaintext(tmp_path):
    # 故意注入 Cookie 形态 symbol → 规则 detail 含之 → 审计零明文
    guard = _guard(full_policy(), audit_path=str(tmp_path / "audit.jsonl"))
    order = fresh_order(symbol="Cookie: sessionid=FAKECOOKIE123456789", quantity=1000, price=190.0)
    result = guard.check(order, fresh_portfolio(), now=NOW)
    assert result.exit_code == 3  # R5 BLOCK（detail 含 symbol）
    raw = _collection_text(tmp_path / "audit.jsonl")
    assert "FAKECOOKIE123456789" not in raw
    assert "Cookie" not in raw
    assert "<redacted>" in raw
    rec = _collection_records(tmp_path / "audit.jsonl")[0]
    assert AUDIT_VALIDATOR.is_valid(rec)


def test_audit_absolute_path_symbol_zero_plaintext(tmp_path):
    # /Applications/Secret App/data.json 形态的 symbol → 审计零明文
    guard = _guard(full_policy(), audit_path=str(tmp_path / "audit.jsonl"))
    order = fresh_order(symbol="/Applications/Secret App/data.json", quantity=1000, price=190.0)
    result = guard.check(order, fresh_portfolio(), now=NOW)
    assert result.exit_code == 3
    raw = _collection_text(tmp_path / "audit.jsonl")
    assert "/Applications/Secret App/data.json" not in raw
    assert "<redacted>" in raw
