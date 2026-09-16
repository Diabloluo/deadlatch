"""G3B UTC shards, shared lock, filename filter, and cross-day append."""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import subprocess
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from deadlatch.audit import (
    MAX_RECORD_BYTES,
    RETENTION_DAYS,
    TAIL_READ_BYTES,
    AuditError,
    AuditMaintenanceError,
    append_audit,
    build_audit_record,
    collection_notes,
    compute_record_hash,
    initialize_audit_state,
    is_audit_maintenance_record,
    prune_audit,
    read_audit_records,
    repair_audit,
    sanitize_hits,
    sanitize_text,
    utc_shard_path,
    verify_audit,
    _in_retention_window,
    _lock_path,
    _parse_utc_date,
    _probe_previous_record,
    _read_last_complete_line,
    _require_regular_audit_file,
    _state_path,
    _scan_audit_bytes,
)

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)


def _rec(rid: str | None = None, ts: str = "2026-09-14T12:00:00Z") -> dict:
    return {
        "schema_version": 1,
        "record_id": rid or uuid.uuid4().hex,
        "evaluated_at": ts,
        "input_hash": "0" * 64,
        "decision": "PASS",
        "shadow_mode": False,
        "shadow_verdict": None,
        "exit_code": 0,
        "policy_version": "1.0.0",
        "rule_hits": [],
    }


def test_shard_naming_default_env_and_explicit(tmp_path, monkeypatch):
    explicit = tmp_path / "audit.jsonl"
    append_audit(explicit, _rec(), now=NOW)
    shard = utc_shard_path(explicit, NOW)
    assert shard.name == "audit-2026-09-14.jsonl"
    assert shard.is_file()
    assert not explicit.exists()

    env_base = tmp_path / "from-env.jsonl"
    monkeypatch.setenv("DEADLATCH_AUDIT_PATH", str(env_base))
    from deadlatch.guard import resolve_audit_path

    resolved = resolve_audit_path()
    initialize_audit_state(resolved)
    append_audit(resolved, _rec(), now=NOW)
    assert utc_shard_path(resolved, NOW).name == "from-env-2026-09-14.jsonl"

    bare = tmp_path / "mylog"
    initialize_audit_state(bare)
    append_audit(bare, _rec("bare0001"), now=NOW)
    assert (tmp_path / "mylog-2026-09-14.jsonl").is_file()
    recs = read_audit_records(bare, now=NOW)
    assert recs[0]["record_id"] == "bare0001"
    assert verify_audit(bare, now=NOW)["status"] == "clean"


def test_utc_day_roll_uses_now_not_evaluated_at(tmp_path):
    path = tmp_path / "audit.jsonl"
    rec = _rec(ts="2026-09-13T23:00:00Z")
    append_audit(path, rec, now=NOW)
    assert utc_shard_path(path, NOW).is_file()
    assert not utc_shard_path(path, NOW - timedelta(days=1)).exists()


def test_filename_filter_ignores_quarantine_tmp_prefix_and_symlink(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("a" * 32), now=NOW)
    (tmp_path / "audit.jsonl.quarantine.20260914T000000Z.deadbeef.json").write_text("{}", encoding="utf-8")
    (tmp_path / "audit-2026-09-14.jsonl.tmp").write_text("{}\n", encoding="utf-8")
    (tmp_path / "audit-extra-2026-09-14.jsonl").write_text("{}\n", encoding="utf-8")
    decoy = tmp_path / "other-2026-09-14.jsonl"
    decoy.write_text("{}\n", encoding="utf-8")
    link = tmp_path / "audit-2026-09-13.jsonl"
    link.symlink_to(decoy)
    records = read_audit_records(path, now=NOW)
    assert len(records) == 1
    assert records[0]["record_id"] == "a" * 32
    result = verify_audit(path)
    assert result["status"] == "clean"
    assert result["valid_lines"] == 1


def test_shared_lock_sidecar_name(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec(), now=NOW)
    verify_audit(path)
    assert (tmp_path / "audit.jsonl.lock").is_file()
    assert not (utc_shard_path(path, NOW).with_name(utc_shard_path(path, NOW).name + ".lock")).exists()


_MIDNIGHT_PRE = datetime(2026, 9, 15, 23, 59, 59, tzinfo=timezone.utc)
_MIDNIGHT_POST = datetime(2026, 9, 16, 0, 0, 1, tzinfo=timezone.utc)


def _midnight_record(rid: str) -> dict:
    return {
        "schema_version": 1,
        "record_id": rid,
        "evaluated_at": "2026-09-15T23:59:59Z",
        "input_hash": "0" * 64,
        "decision": "BLOCK",
        "shadow_mode": False,
        "shadow_verdict": None,
        "exit_code": 3,
        "policy_version": "synthetic",
        "rule_hits": [],
    }


def _delayed_pre_midnight_writer(base, ready, release, results) -> None:
    """Child: sample default clock, pause before the collection lock, then append."""
    import deadlatch.audit as audit_mod

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            source = _MIDNIGHT_POST if release.is_set() else _MIDNIGHT_PRE
            return cls.fromisoformat(source.isoformat())

    original_locked = audit_mod._locked

    def delayed_lock(path, fn, **kwargs):
        ready.set()
        if not release.wait(20):
            raise RuntimeError("scheduling barrier timeout")
        return original_locked(path, fn, **kwargs)

    try:
        with patch.object(audit_mod, "datetime", Frozen), patch.object(
            audit_mod, "_locked", delayed_lock
        ):
            audit_mod.append_audit(base, _midnight_record("synthetic-delayed"))
        results.put("success")
    except Exception as exc:
        results.put(type(exc).__name__)


def test_four_process_cross_day_no_lost_lines(tmp_path):
    path = tmp_path / "audit.jsonl"
    script = r"""
import sys, uuid
from datetime import datetime, timezone
from deadlatch.audit import append_audit
path = sys.argv[1]
day = sys.argv[2]
n = int(sys.argv[3])
now = datetime.fromisoformat(day)
for _ in range(n):
    rec = {
        "schema_version": 1, "record_id": uuid.uuid4().hex,
        "evaluated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "input_hash": "d" * 64,
        "decision": "PASS", "shadow_mode": False, "shadow_verdict": None,
        "exit_code": 0, "policy_version": "1.0.0", "rule_hits": [],
    }
    append_audit(path, rec, now=now)
"""
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    day_a = "2026-09-14T12:00:00+00:00"
    day_b = "2026-09-15T01:00:00+00:00"
    # Concurrent within a UTC day; the next day starts only after day A is
    # complete so write order stays monotonic with the visible chain.
    procs_a = [
        subprocess.Popen([sys.executable, "-c", script, str(path), day_a, "20"], env=env)
        for _ in range(2)
    ]
    assert all(p.wait(timeout=60) == 0 for p in procs_a)
    procs_b = [
        subprocess.Popen([sys.executable, "-c", script, str(path), day_b, "20"], env=env)
        for _ in range(2)
    ]
    assert all(p.wait(timeout=60) == 0 for p in procs_b)
    day_a_dt = datetime(2026, 9, 14, tzinfo=timezone.utc)
    day_b_dt = datetime(2026, 9, 15, tzinfo=timezone.utc)
    recs = []
    for day in (day_a_dt, day_b_dt):
        shard = utc_shard_path(path, day)
        assert shard.is_file()
        text = shard.read_text(encoding="utf-8")
        assert not text or text.endswith("\n")
        for line in text.splitlines():
            if line.strip():
                recs.append(json.loads(line))
    assert len(recs) == 80
    ids = [r["record_id"] for r in recs]
    assert len(set(ids)) == 80
    trusted = read_audit_records(path, now=day_b_dt)
    assert len(trusted) == 80
    assert len({r["record_id"] for r in trusted}) == 80
    result = verify_audit(path, now=day_b_dt)
    assert result["status"] == "clean"
    assert result["chained_records"] == 80
    assert result["invalid_lines"] == 0


def test_pre_midnight_sample_delayed_until_after_next_day_write(tmp_path):
    """Default-clock append must resample UTC day under the collection lock.

    A writer that reaches the lock boundary before midnight, waits until the
    next UTC day has already been written, then resumes, must still produce a
    verifiable chain. evaluated_at is left as the caller supplied it.
    """
    path = tmp_path / "audit.jsonl"
    append_audit(path, _midnight_record("synthetic-seed"), now=_MIDNIGHT_PRE)
    ctx = mp.get_context("spawn")
    ready, release, results = ctx.Event(), ctx.Event(), ctx.Queue()
    worker = ctx.Process(
        target=_delayed_pre_midnight_writer, args=(str(path), ready, release, results)
    )
    worker.start()
    try:
        assert ready.wait(20), "old writer never reached lock boundary"

        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromisoformat(_MIDNIGHT_POST.isoformat())

        import deadlatch.audit as audit_mod

        with patch.object(audit_mod, "datetime", Frozen):
            append_audit(path, _midnight_record("synthetic-new-day"))
        release.set()
        worker.join(20)
        assert not worker.is_alive(), "old writer did not finish"
        assert worker.exitcode == 0
        assert results.get(timeout=5) == "success"
        recs = read_audit_records(path, now=_MIDNIGHT_POST)
        assert [r["record_id"] for r in recs] == [
            "synthetic-seed",
            "synthetic-new-day",
            "synthetic-delayed",
        ]
        assert recs[1]["prev_hash"] == recs[0]["record_hash"]
        assert recs[2]["prev_hash"] == recs[1]["record_hash"]
        assert recs[2]["evaluated_at"] == "2026-09-15T23:59:59Z"
        result = verify_audit(path, now=_MIDNIGHT_POST)
        assert result["status"] == "clean"
        assert result["chained_records"] == 3
        assert result["invalid_lines"] == 0
    finally:
        release.set()
        if worker.is_alive():
            worker.terminate()
            worker.join(5)


def test_explicit_earlier_now_after_later_day_refused_bytes_unchanged(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("seed0001"), now=NOW)
    later_now = NOW + timedelta(days=1)
    append_audit(path, _rec("nextday1"), now=later_now)
    earlier = utc_shard_path(path, NOW)
    later = utc_shard_path(path, later_now)
    before_early = earlier.read_bytes()
    before_later = later.read_bytes()
    with pytest.raises(AuditError, match="watermark"):
        append_audit(path, _rec("toolate1"), now=NOW)
    assert earlier.read_bytes() == before_early
    assert later.read_bytes() == before_later
    recs = read_audit_records(path, now=later_now)
    assert [r["record_id"] for r in recs] == ["seed0001", "nextday1"]
    assert verify_audit(path, now=later_now)["status"] == "clean"


def test_empty_later_shard_does_not_block_earlier_day_append(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("seed0002"), now=NOW)
    later_now = NOW + timedelta(days=1)
    utc_shard_path(path, later_now).write_text("\n", encoding="utf-8")
    append_audit(path, _rec("same0002"), now=NOW)
    append_audit(path, _rec("later002"), now=later_now)
    recs = read_audit_records(path, now=later_now)
    assert [r["record_id"] for r in recs] == ["seed0002", "same0002", "later002"]
    assert recs[-1]["prev_hash"] == recs[-2]["record_hash"]
    assert verify_audit(path, now=later_now)["status"] == "clean"


def test_append_rejects_later_day_symlink_at_init(tmp_path):
    path = tmp_path / "coll.jsonl"
    decoy = tmp_path / "decoy.jsonl"
    decoy.write_text("{}\n", encoding="utf-8")
    utc_shard_path(path, NOW + timedelta(days=1)).symlink_to(decoy)
    from deadlatch.audit import initialize_audit_state
    with pytest.raises(AuditError, match="not a regular file"):
        initialize_audit_state(path, adopt_existing=True)


def test_truncated_later_shard_refuses_earlier_append_bytes_unchanged(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("seed0003"), now=NOW)
    later_now = NOW + timedelta(days=1)
    append_audit(path, _rec("later003"), now=later_now)
    later = utc_shard_path(path, later_now)
    later.write_bytes(b"{partial")
    earlier = utc_shard_path(path, NOW)
    before_early = earlier.read_bytes()
    before_later = later.read_bytes()
    with pytest.raises(AuditError, match="watermark"):
        append_audit(path, _rec("toolate2"), now=NOW)
    assert earlier.read_bytes() == before_early
    assert later.read_bytes() == before_later


def test_legacy_not_rewritten_on_first_append(tmp_path):
    path = tmp_path / "audit.jsonl"
    legacy = json.dumps(_rec("legacy01", "2026-09-01T00:00:00Z"), sort_keys=True, separators=(",", ":")) + "\n"
    path.write_text(legacy, encoding="utf-8")
    before = path.read_bytes()
    append_audit(path, _rec("newrec01"), now=NOW)
    assert path.read_bytes() == before
    recs = read_audit_records(path, now=NOW)
    assert recs[0]["schema_version"] == 1
    assert recs[-1]["schema_version"] == 2
    assert recs[-1]["prev_hash"]


def test_prune_keeps_future_and_legacy(tmp_path):
    path = tmp_path / "audit.jsonl"
    _state_path(path).unlink(missing_ok=True)
    path.write_text(json.dumps(_rec("leg"), sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    old = utc_shard_path(path, NOW - timedelta(days=40))
    fut = utc_shard_path(path, NOW + timedelta(days=2))
    old.write_text("{}\n", encoding="utf-8")
    fut.write_text(json.dumps(_rec("fut"), sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    q = tmp_path / "audit.jsonl.quarantine.x.json"
    q.write_text("keep", encoding="utf-8")
    result = prune_audit(path, now=NOW)
    assert result["removed_segments"] == 1
    assert result["future_segments"] == 1
    assert path.exists()
    assert q.exists()
    assert not old.exists()
    assert fut.exists()


def test_append_10000_structural_o1(tmp_path, monkeypatch):
    import builtins
    import deadlatch.audit as audit_mod

    path = tmp_path / "audit.jsonl"
    now = NOW
    real_open = builtins.open
    readlines_hits = []

    def wrapped_open(file, mode="r", *args, **kwargs):
        handle = real_open(file, mode, *args, **kwargs)
        name = str(file)
        if "audit-" in Path(name).name and str(mode).startswith("r"):
            orig = handle.readlines

            def boom(*a, **k):
                readlines_hits.append(name)
                return orig(*a, **k)

            handle.readlines = boom  # type: ignore[method-assign]
        return handle

    real_os_read = os.read
    read_requests: list[int] = []
    listdir_hits = []
    real_listdir = os.listdir

    def wrapped_read(fd, n, *args, **kwargs):
        read_requests.append(n)
        return real_os_read(fd, n, *args, **kwargs)

    def wrapped_listdir(target, *args, **kwargs):
        listdir_hits.append(str(target))
        return real_listdir(target, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", wrapped_open)
    monkeypatch.setattr(audit_mod.os, "read", wrapped_read)
    monkeypatch.setattr(audit_mod.os, "listdir", wrapped_listdir)
    from deadlatch.audit import MAX_STATE_BYTES, STATE_READ_LIMIT, _state_path
    for i in range(10000):
        append_audit(path, _rec(f"{i:032d}"), now=now)
        if i in (0, 9999):
            assert not readlines_hits
            assert not listdir_hits
            assert all(n <= max(TAIL_READ_BYTES, STATE_READ_LIMIT) for n in read_requests[-12:])
            assert all(n <= STATE_READ_LIMIT or n <= TAIL_READ_BYTES for n in read_requests[-12:])
            assert _state_path(path).stat().st_size <= MAX_STATE_BYTES
    recs = read_audit_records(path, now=now)
    assert len(recs) == 10000
    shard = utc_shard_path(path, now)
    assert shard.stat().st_size > 1000
    assert RETENTION_DAYS == 30


def test_empty_existing_shard_verify_clean(tmp_path):
    path = tmp_path / "coll.jsonl"
    shard = utc_shard_path(path, NOW)
    shard.write_text("", encoding="utf-8")
    result = verify_audit(path)
    assert result["status"] == "clean"
    assert result["valid_lines"] == 0
    assert result["segments_scanned"] == 1


def test_append_rejects_shard_symlink(tmp_path):
    path = tmp_path / "audit.jsonl"
    target = tmp_path / "other.jsonl"
    target.write_text("x\n", encoding="utf-8")
    shard = utc_shard_path(path, NOW)
    shard.symlink_to(target)
    from deadlatch.audit import AuditError
    with pytest.raises(AuditError):
        append_audit(path, _rec(), now=NOW)


def test_append_rejects_oversize_record(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod
    from deadlatch.audit import AuditError

    monkeypatch.setattr(audit_mod, "MAX_RECORD_BYTES", 200)
    path = tmp_path / "audit.jsonl"
    rec = _rec()
    rec["rule_hits"] = [{"rule_id": "kill_switch", "severity": "BLOCK", "detail": "x" * 400}]
    with pytest.raises(AuditError):
        append_audit(path, rec, now=NOW)


def test_cli_prune_json(tmp_path, capsys):
    from deadlatch.cli import main

    path = tmp_path / "audit.jsonl"
    _state_path(path).unlink(missing_ok=True)
    old = utc_shard_path(path, NOW - timedelta(days=40))
    old.write_text("{}\n", encoding="utf-8")
    rc = main(["audit", "prune", "--audit-path", str(path), "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert rc == 0
    assert payload["operation"] == "prune"
    assert payload["removed_segments"] == 1
    assert not old.exists()


def test_cli_prune_text(tmp_path, capsys):
    from deadlatch.cli import main

    path = tmp_path / "audit.jsonl"
    _state_path(path).unlink(missing_ok=True)
    utc_shard_path(path, NOW).write_text("{}\n", encoding="utf-8")
    rc = main(["audit", "prune", "--audit-path", str(path)])
    captured = capsys.readouterr()
    assert rc == 0
    assert "prune clean" in captured.out or "kept_segments=" in captured.out


def test_read_missing_collection_fail_closed(tmp_path):
    from deadlatch.audit import AuditError

    with pytest.raises(AuditError):
        read_audit_records(tmp_path / "nope.jsonl")


def test_append_truncated_tail_fail_closed(tmp_path):
    from deadlatch.audit import AuditError

    path = tmp_path / "audit.jsonl"
    shard = utc_shard_path(path, NOW)
    append_audit(path, _rec("keep0001"), now=NOW)
    shard.write_bytes(shard.read_bytes() + b"{partial")
    before = shard.read_bytes()
    with pytest.raises(AuditError):
        append_audit(path, _rec("next0001"), now=NOW)
    assert shard.read_bytes() == before


def test_append_rejects_lock_symlink(tmp_path):
    from deadlatch.audit import AuditError, _lock_path

    path = tmp_path / "audit.jsonl"
    target = tmp_path / "lock-target"
    target.write_text("x", encoding="utf-8")
    lock = _lock_path(path)
    lock.unlink(missing_ok=True)
    lock.symlink_to(target)
    with pytest.raises(AuditError):
        append_audit(path, _rec(), now=NOW)


def test_naive_now_and_invalid_shard_date_ignored(tmp_path):
    path = tmp_path / "audit.jsonl"
    naive = datetime(2026, 9, 14, 12, 0, 0)
    append_audit(path, _rec("naive001"), now=naive)
    (tmp_path / "audit-2026-02-30.jsonl").write_text("{}\n", encoding="utf-8")
    recs = read_audit_records(path, now=datetime(2026, 9, 14, 12, tzinfo=timezone.utc))
    assert recs[0]["record_id"] == "naive001"


def test_prune_missing_parent_noop(tmp_path):
    result = prune_audit(tmp_path / "missing" / "audit.jsonl", now=NOW)
    assert result["status"] == "clean"
    assert not (tmp_path / "missing").exists()


def test_verify_symlink_shard_ignored_legacy_ok(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("onlyone1"), now=NOW)
    decoy = tmp_path / "decoy.jsonl"
    decoy.write_text("x\n", encoding="utf-8")
    utc_shard_path(path, NOW - timedelta(days=1)).symlink_to(decoy)
    result = verify_audit(path)
    assert result["status"] == "clean"
    assert result["chained_records"] == 1


def test_utc_shard_path_date_naive_and_invalid_when():
    base = Path("audit.jsonl")
    naive = datetime(2026, 9, 14, 12, 0, 0)
    assert utc_shard_path(base, naive).name == "audit-2026-09-14.jsonl"
    assert utc_shard_path(base, date(2026, 9, 14)).name == "audit-2026-09-14.jsonl"
    with pytest.raises(TypeError):
        utc_shard_path(base, "2026-09-14")
    assert utc_shard_path(Path("mylog"), NOW).name == "mylog-2026-09-14.jsonl"
    assert _parse_utc_date("not-a-date") is None
    assert _parse_utc_date("2026-02-30") is None
    assert _in_retention_window(date(2026, 9, 14), NOW) is True
    assert _in_retention_window(date(2026, 8, 1), NOW) is False


def test_empty_today_shard_chains_from_previous_day(tmp_path):
    path = tmp_path / "audit.jsonl"
    yesterday = NOW - timedelta(days=1)
    append_audit(path, _rec("yest0001"), now=yesterday)
    utc_shard_path(path, NOW).write_text("", encoding="utf-8")
    append_audit(path, _rec("today001"), now=NOW)
    recs = read_audit_records(path, now=NOW)
    assert recs[-1]["prev_hash"] == recs[0]["record_hash"]


def test_append_rejects_legacy_symlink_when_no_shards(tmp_path):
    path = tmp_path / "audit.jsonl"
    target = tmp_path / "real.jsonl"
    target.write_text("{}\n", encoding="utf-8")
    path.symlink_to(target)
    with pytest.raises(AuditError, match="symbolic link"):
        append_audit(path, _rec(), now=NOW)


def test_append_rejects_previous_day_symlink(tmp_path):
    path = tmp_path / "audit.jsonl"
    decoy = tmp_path / "decoy.jsonl"
    decoy.write_text("{}\n", encoding="utf-8")
    utc_shard_path(path, NOW - timedelta(days=1)).symlink_to(decoy)
    with pytest.raises(AuditError, match="symbolic link"):
        append_audit(path, _rec(), now=NOW)


def test_append_open_failure_does_not_report_success(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "audit.jsonl"

    def boom(*a, **k):
        raise AuditError("审计打开失败（OSError）")

    monkeypatch.setattr(audit_mod, "_append_line_fsync", boom)
    with pytest.raises(AuditError, match="审计打开失败"):
        append_audit(path, _rec(), now=NOW)


def test_append_short_write_does_not_report_success(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "audit.jsonl"

    def boom(path, line):
        raise AuditError("审计写入发生 short write，拒绝报告成功")

    monkeypatch.setattr(audit_mod, "_append_line_fsync", boom)
    with pytest.raises(AuditError, match="short write"):
        append_audit(path, _rec(), now=NOW)
    shard = utc_shard_path(path, NOW)
    assert not shard.exists() or shard.stat().st_size == 0


def test_append_fsync_failure_does_not_report_success(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "audit.jsonl"

    def boom(*a, **k):
        raise AuditError("审计写入失败（OSError）")

    monkeypatch.setattr(audit_mod, "_append_line_fsync", boom)
    with pytest.raises(AuditError, match="审计写入失败"):
        append_audit(path, _rec(), now=NOW)


def test_append_fchmod_oserror_is_ignored(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    def boom(fd, mode):
        raise OSError("fchmod denied")

    monkeypatch.setattr(audit_mod.os, "fchmod", boom, raising=False)
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("chmod001"), now=NOW)
    assert read_audit_records(path, now=NOW)[0]["record_id"] == "chmod001"


def test_append_rejects_oversize_existing_shard(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("size0001"), now=NOW)
    shard = utc_shard_path(path, NOW)
    monkeypatch.setattr(audit_mod, "MAX_AUDIT_FILE_BYTES", shard.stat().st_size - 1)
    with pytest.raises(AuditError, match="超过大小上限"):
        append_audit(path, _rec("size0002"), now=NOW)


def test_incoming_non_object_rejected(tmp_path):
    path = tmp_path / "audit.jsonl"
    with pytest.raises(AuditError, match="not an object"):
        append_audit(path, ["not", "a", "dict"], now=NOW)  # type: ignore[arg-type]


def test_prune_delete_and_dir_fsync_failures(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "audit.jsonl"
    _state_path(path).unlink(missing_ok=True)
    old = utc_shard_path(path, NOW - timedelta(days=40))
    old.write_text("{}\n", encoding="utf-8")

    def boom_unlink(target):
        raise OSError("unlink denied")

    monkeypatch.setattr(audit_mod.os, "unlink", boom_unlink)
    with pytest.raises(AuditError, match="分片删除失败"):
        prune_audit(path, now=NOW)
    assert old.exists()

    monkeypatch.undo()
    old.write_text("{}\n", encoding="utf-8")
    calls = {"n": 0}
    real_unlink = audit_mod.os.unlink

    def missing(target):
        calls["n"] += 1
        raise FileNotFoundError("already gone")

    monkeypatch.setattr(audit_mod.os, "unlink", missing)
    result = prune_audit(path, now=NOW)
    assert result["status"] == "clean"
    assert calls["n"] == 1
    monkeypatch.setattr(audit_mod.os, "unlink", real_unlink)

    old.write_text("{}\n", encoding="utf-8")

    def boom_fsync(fd):
        raise OSError("dir fsync denied")

    monkeypatch.setattr(audit_mod.os, "fsync", boom_fsync)
    with pytest.raises(AuditError, match="目录 fsync 失败"):
        prune_audit(path, now=NOW)


def test_prune_naive_now_and_listdir_oserror(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "audit.jsonl"
    _state_path(path).unlink(missing_ok=True)
    utc_shard_path(path, NOW).write_text("{}\n", encoding="utf-8")
    naive = datetime(2026, 9, 14, 12, 0, 0)
    result = prune_audit(path, now=naive)
    assert result["kept_segments"] == 1

    def boom(_parent):
        raise OSError("listdir denied")

    monkeypatch.setattr(audit_mod.os, "listdir", boom)
    with pytest.raises(AuditError, match="审计目录不可读"):
        prune_audit(path, now=NOW)


def test_read_utf8_and_crlf_and_blank_lines(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("crlf0001"), now=NOW)
    shard = utc_shard_path(path, NOW)
    line = shard.read_bytes().rstrip(b"\n")
    shard.write_bytes(line + b"\r\n\n")
    recs = read_audit_records(path, now=NOW)
    assert recs[0]["record_id"] == "crlf0001"
    shard.write_bytes(b"\xff\xfe not utf8\n")
    with pytest.raises(AuditError, match="UnicodeDecodeError"):
        read_audit_records(path, now=NOW)


def test_read_oserror_and_naive_now(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("naive002"), now=NOW)
    recs = read_audit_records(path, now=datetime(2026, 9, 14, 12, 0, 0))
    assert recs[0]["record_id"] == "naive002"

    def boom(_self):
        raise OSError("read denied")

    monkeypatch.setattr(Path, "read_bytes", boom)
    with pytest.raises(AuditError, match="审计文件不可读"):
        read_audit_records(path, now=NOW)


def test_tail_helpers_symlink_missing_and_non_regular(tmp_path):
    missing = tmp_path / "nope.jsonl"
    assert _read_last_complete_line(missing) is None
    link = tmp_path / "link.jsonl"
    target = tmp_path / "real.jsonl"
    target.write_text("x\n", encoding="utf-8")
    link.symlink_to(target)
    with pytest.raises(AuditError, match="symbolic link"):
        _read_last_complete_line(link)
    directory = tmp_path / "as-dir.jsonl"
    directory.mkdir()
    with pytest.raises(AuditError, match="not a regular file"):
        _read_last_complete_line(directory)
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert _read_last_complete_line(empty) is None


def test_tail_oversize_window_fail_closed(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "audit.jsonl"
    shard = utc_shard_path(path, NOW)
    monkeypatch.setattr(audit_mod, "MAX_RECORD_BYTES", 32)
    shard.write_bytes(b"y" * 80 + b"\n")
    with pytest.raises(AuditError, match="超过单条大小上限"):
        append_audit(path, _rec(), now=NOW)


def test_tail_hash_mismatch_and_bad_utf8_fail_closed(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("okhash01"), now=NOW)
    shard = utc_shard_path(path, NOW)
    rec = json.loads(shard.read_text(encoding="utf-8"))
    rec["record_hash"] = "a" * 64
    shard.write_text(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    with pytest.raises(AuditError, match="record_hash"):
        append_audit(path, _rec("next0002"), now=NOW)
    shard.write_bytes(b"\xff\xfe\n")
    with pytest.raises(AuditError, match="UTF-8"):
        append_audit(path, _rec("next0003"), now=NOW)


def test_tail_last_line_over_record_cap(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    monkeypatch.setattr(audit_mod, "MAX_RECORD_BYTES", 40)
    path = tmp_path / "audit.jsonl"
    shard = utc_shard_path(path, NOW)
    shard.write_bytes(b"{}\n" + b"z" * 50 + b"\n")
    with pytest.raises(AuditError, match="超过单条大小上限"):
        append_audit(path, _rec(), now=NOW)


def test_lstat_oserror_treated_as_absent(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "audit.jsonl"
    real = audit_mod.os.lstat

    def boom(target, *a, **k):
        if Path(target).name.startswith("audit-"):
            raise OSError("lstat denied")
        return real(target, *a, **k)

    monkeypatch.setattr(audit_mod.os, "lstat", boom)
    append_audit(path, _rec("lstat001"), now=NOW)


def test_sanitize_and_maintenance_marker_guards():
    assert sanitize_text(12, []) == 12  # type: ignore[arg-type]
    assert sanitize_hits([{"rule_id": "BAD", "severity": "BLOCK", "detail": "x"}], []) == []
    assert sanitize_hits([{"rule_id": "kill_switch", "severity": "INFO", "detail": "x"}], []) == []
    assert is_audit_maintenance_record("nope") is False  # type: ignore[arg-type]
    assert is_audit_maintenance_record({"rule_hits": "x"}) is False
    assert is_audit_maintenance_record({"rule_hits": ["x"]}) is False
    notes = collection_notes(Path("/no/such/audit.jsonl"), now=NOW)
    assert notes["future_segments"] == 0


def test_require_regular_audit_file_error_classes(tmp_path):
    missing = tmp_path / "missing.jsonl"
    with pytest.raises(AuditMaintenanceError) as ei:
        _require_regular_audit_file(missing)
    assert ei.value.exit_code == 4
    d = tmp_path / "adir"
    d.mkdir()
    with pytest.raises(AuditMaintenanceError) as ei:
        _require_regular_audit_file(d)
    assert "directory" in ei.value.public_message
    target = tmp_path / "real.jsonl"
    target.write_text("x\n", encoding="utf-8")
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    with pytest.raises(AuditMaintenanceError) as ei:
        _require_regular_audit_file(link)
    assert "symbolic link" in ei.value.public_message


def test_verify_lock_symlink_maps_to_maintenance_error(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("lock0001"), now=NOW)
    lock = _lock_path(path)
    lock.unlink()
    decoy = tmp_path / "lock-target"
    decoy.write_text("x", encoding="utf-8")
    lock.symlink_to(decoy)
    with pytest.raises(AuditMaintenanceError) as ei:
        verify_audit(path)
    assert ei.value.exit_code == 4
    assert "symbolic link" in ei.value.public_message


def test_repair_refuses_mid_shard_structural_damage(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("keep000a"), now=NOW - timedelta(days=1))
    append_audit(path, _rec("keep000b"), now=NOW)
    yesterday = utc_shard_path(path, NOW - timedelta(days=1))
    yesterday.write_bytes(yesterday.read_bytes() + b"{bad-json\n")
    result = repair_audit(path, now=NOW)
    assert result["status"] == "invalid"
    assert yesterday.read_bytes().endswith(b"{bad-json\n")


def test_verify_first_v2_prefix_anchor_prev_hash_clean(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("firstv201"), now=NOW)
    shard = utc_shard_path(path, NOW)
    rec = json.loads(shard.read_text(encoding="utf-8"))
    rec["prev_hash"] = "a" * 64
    rec["record_hash"] = compute_record_hash(rec)
    shard.write_text(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    result = verify_audit(path)
    assert result["status"] == "clean"
    assert result["chained_records"] == 1


def test_cli_audit_error_paths_and_repair_refusal(tmp_path, capsys, monkeypatch):
    from deadlatch.cli import main

    rc = main(["audit"])
    captured = capsys.readouterr()
    assert rc == 4
    assert "audit command required" in captured.err

    missing = tmp_path / "nope.jsonl"
    rc = main(["audit", "prune", "--audit-path", str(missing), "--json"])
    captured = capsys.readouterr()
    # missing parent is a clean no-op for prune
    payload = json.loads(captured.out)
    assert rc == 0
    assert payload["operation"] == "prune"

    path = tmp_path / "audit.jsonl"
    utc_shard_path(path, NOW).write_text("", encoding="utf-8")
    decoy = tmp_path / "lock-target"
    decoy.write_text("x", encoding="utf-8")
    lock = _lock_path(path)
    lock.unlink(missing_ok=True)
    lock.symlink_to(decoy)
    rc = main(["audit", "prune", "--audit-path", str(path), "--json"])
    captured = capsys.readouterr()
    assert rc == 5
    assert json.loads(captured.out)["operation"] == "prune"
    assert json.loads(captured.out)["status"] == "error"

    import deadlatch.cli as cli_mod

    def boom(*a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cli_mod, "verify_audit", boom)
    rc = main(["audit", "verify", "--audit-path", str(path), "--json"])
    captured = capsys.readouterr()
    assert rc == 5
    assert "internal error" in captured.err
    assert json.loads(captured.out)["status"] == "error"
    monkeypatch.undo()

    good = tmp_path / "good.jsonl"
    initialize_audit_state(good)
    append_audit(good, _rec("cli00001"), now=NOW)
    rc = main(["audit", "verify", "--audit-path", str(good)])
    captured = capsys.readouterr()
    assert rc == 0
    assert "verify clean" in captured.out

    shard = utc_shard_path(good, NOW)
    rec = json.loads(shard.read_text(encoding="utf-8"))
    rec["decision"] = "BLOCK"
    rec["exit_code"] = 3
    shard.write_text(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    rc = main(["audit", "verify", "--audit-path", str(good)])
    captured = capsys.readouterr()
    assert rc == 3
    assert "record_hash_mismatch" in captured.out
    assert shard.name in captured.out
    rc = main(["audit", "repair", "--quarantine", "--audit-path", str(good)])
    captured = capsys.readouterr()
    assert rc == 3
    assert "检测到链完整性问题、拒绝自动重链" in captured.err
    assert not list(tmp_path.glob("*.quarantine.*"))

    from deadlatch import cli as cli_module

    fake = SimpleNamespace(command="nope")
    monkeypatch.setattr(cli_module, "build_parser", lambda: SimpleNamespace(
        parse_args=lambda argv=None: fake
    ))
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 4
    assert "unknown command" in captured.err


def _pad_rec_to_line_size(rec: dict, prev_hash, target: int) -> dict:
    import deadlatch.audit as audit_mod

    n = 0
    while True:
        candidate = dict(rec)
        candidate["rule_hits"] = [{
            "rule_id": "kill_switch",
            "severity": "BLOCK",
            "detail": "x" * n,
        }]
        final = audit_mod._finalize_v2(
            audit_mod._incoming_business_record(candidate), prev_hash
        )
        line = (
            json.dumps(final, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            + "\n"
        ).encode("utf-8")
        if len(line) == target:
            return candidate
        if len(line) > target:
            raise AssertionError(
                f"cannot hit exactly {target} bytes (got {len(line)} at n={n})"
            )
        n += 1


def test_unpruned_expired_shard_verify_without_prune_is_clean(tmp_path):
    path = tmp_path / "audit.jsonl"
    expired = NOW - timedelta(days=31)
    append_audit(path, _rec("old31aaa"), now=expired)
    append_audit(path, _rec("today001"), now=NOW)
    expired_shard = utc_shard_path(path, expired)
    assert expired_shard.exists()
    recs = read_audit_records(path, now=NOW)
    today = [r for r in recs if r["record_id"] == "today001"]
    assert len(today) == 1
    assert today[0]["prev_hash"] is None
    result = verify_audit(path, now=NOW)
    assert result["status"] == "clean"
    assert result["chained_records"] == 1
    assert result["segments_scanned"] == 1
    assert expired_shard.exists()


def test_mid_window_chain_break_detected_without_prune(tmp_path):
    path = tmp_path / "audit.jsonl"
    mid = NOW - timedelta(days=5)
    append_audit(path, _rec("midwin01"), now=mid)
    append_audit(path, _rec("today002"), now=NOW)
    assert verify_audit(path, now=NOW)["status"] == "clean"
    today = utc_shard_path(path, NOW)
    rec = json.loads(today.read_text(encoding="utf-8"))
    rec["prev_hash"] = None
    rec["record_hash"] = compute_record_hash(rec)
    today.write_text(
        json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    result = verify_audit(path, now=NOW)
    assert result["status"] == "invalid"
    assert any(i["reason_code"] == "prev_hash_mismatch" for i in result["issues"])
    assert utc_shard_path(path, mid).exists()


def test_empty_jsonl_lone_newline_is_legal_empty_collection(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text("\n", encoding="utf-8")
    before = path.read_bytes()
    assert before == b"\n"
    append_audit(path, _rec("empty001"), now=NOW)
    recs = read_audit_records(path, now=NOW)
    assert recs[-1]["record_id"] == "empty001"
    assert recs[-1]["prev_hash"] is None
    assert path.read_bytes() == before


@pytest.mark.parametrize("terminator", [b"\n", b"\r\n"])
def test_append_single_terminating_newline_keeps_chain(tmp_path, terminator):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("keephead"), now=NOW)
    shard = utc_shard_path(path, NOW)
    first = json.loads(shard.read_text(encoding="utf-8").splitlines()[0])
    payload = shard.read_bytes().rstrip(b"\r\n")
    shard.write_bytes(payload + terminator)
    append_audit(path, _rec("afterterm"), now=NOW)
    recs = read_audit_records(path, now=NOW)
    assert recs[-1]["record_id"] == "afterterm"
    assert recs[-1]["prev_hash"] == first["record_hash"]


@pytest.mark.parametrize(
    "suffix",
    [b"\n\n", b"\r\n\r\n", b"\n\n\n", b"\n\r\n\n"],
)
def test_append_trailing_extra_blank_lines_refused_file_unchanged(tmp_path, suffix):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("keephead"), now=NOW)
    shard = utc_shard_path(path, NOW)
    payload = shard.read_bytes().rstrip(b"\r\n")
    shard.write_bytes(payload + suffix)
    before = shard.read_bytes()
    with pytest.raises(AuditError, match="空行"):
        append_audit(path, _rec("afterblk1"), now=NOW)
    assert shard.read_bytes() == before


def test_append_refuses_when_tail_chain_head_unconfirmed(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    monkeypatch.setattr(audit_mod, "MAX_RECORD_BYTES", 32)
    path = tmp_path / "audit.jsonl"
    shard = utc_shard_path(path, NOW)
    known = b'{"ok":true}\n'
    shard.write_bytes(known + b"\n" * 80)
    before = shard.read_bytes()
    with pytest.raises(AuditError, match="空行|链头"):
        append_audit(path, _rec("refuse01"), now=NOW)
    assert shard.read_bytes() == before


def test_tail_record_size_below_equal_and_over_cap(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    cap = 512
    monkeypatch.setattr(audit_mod, "MAX_RECORD_BYTES", cap)
    path = tmp_path / "audit.jsonl"

    below = _rec("below001")
    append_audit(path, below, now=NOW)
    append_audit(path, _rec("below002"), now=NOW)
    recs = read_audit_records(path, now=NOW)
    assert recs[-1]["prev_hash"] == recs[-2]["record_hash"]
    assert len(
        (json.dumps(recs[-1], sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
    ) < cap

    shard = utc_shard_path(path, NOW)
    shard.write_bytes(b"")
    first_exact = _pad_rec_to_line_size(_rec("equal001"), None, cap)
    append_audit(path, first_exact, now=NOW)
    first_written = read_audit_records(path, now=NOW)[0]
    assert len(
        (json.dumps(first_written, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
    ) == cap
    second_exact = _pad_rec_to_line_size(
        _rec("equal002"), first_written["record_hash"], cap
    )
    append_audit(path, second_exact, now=NOW)
    recs = read_audit_records(path, now=NOW)
    assert recs[-1]["prev_hash"] == recs[0]["record_hash"]
    assert recs[-1]["record_id"] == "equal002"
    assert len(
        (json.dumps(recs[-1], sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
    ) == cap

    before = shard.read_bytes()
    over = _rec("over0001")
    over["rule_hits"] = [{"rule_id": "kill_switch", "severity": "BLOCK", "detail": "x" * cap}]
    with pytest.raises(AuditError, match="超过单条大小上限"):
        append_audit(path, over, now=NOW)
    assert shard.read_bytes() == before

    shard.write_bytes(before + b"z" * (cap + 8) + b"\n")
    over_before = shard.read_bytes()
    with pytest.raises(AuditError, match="超过单条大小上限"):
        append_audit(path, _rec("over0002"), now=NOW)
    assert shard.read_bytes() == over_before


def test_prefix_plus_exact_max_record_then_append_stays_contiguous(tmp_path):
    """Prefix + true 64KiB last line must still chain; wiping the shard hides the bug.

    ``test_tail_record_size_below_equal_and_over_cap`` empties the shard before the
    equal-cap pair, so ``start==0`` and a window of exactly ``MAX_RECORD_BYTES``
    still sees a complete line. Production shards already have a prefix: the
    bounded tail read must include the preceding delimiter to distinguish a
    legal max-size record from a truncated one.
    """
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("pfx00001"), now=NOW)
    shard = utc_shard_path(path, NOW)
    prefix_bytes = shard.read_bytes()
    assert prefix_bytes.endswith(b"\n")
    prefix = json.loads(prefix_bytes.splitlines()[0])
    prefix_hash = prefix["record_hash"]

    exact = _pad_rec_to_line_size(_rec("cap64kib"), prefix_hash, MAX_RECORD_BYTES)
    append_audit(path, exact, now=NOW)
    after_cap = shard.read_bytes()
    assert after_cap.startswith(prefix_bytes)
    cap_line = after_cap[len(prefix_bytes):]
    assert len(cap_line) == MAX_RECORD_BYTES
    assert cap_line.endswith(b"\n")
    cap_rec = json.loads(cap_line)
    assert cap_rec["record_id"] == "cap64kib"
    assert cap_rec["prev_hash"] == prefix_hash
    cap_hash = cap_rec["record_hash"]
    assert shard.stat().st_size > MAX_RECORD_BYTES

    append_audit(path, _rec("after64k"), now=NOW)
    recs = read_audit_records(path, now=NOW)
    assert [r["record_id"] for r in recs] == ["pfx00001", "cap64kib", "after64k"]
    assert recs[2]["prev_hash"] == cap_hash == recs[1]["record_hash"]
    result = verify_audit(path, now=NOW)
    assert result["status"] == "clean"
    assert result["chained_records"] == 3
    assert result["invalid_lines"] == 0


def test_tail_read_window_is_record_cap_plus_delimiter():
    assert TAIL_READ_BYTES == MAX_RECORD_BYTES + 1


def test_only_expired_shards_verify_clean_not_missing_collection(tmp_path):
    """Expired-only collection is not a missing path. After retention filter,
    zero in-window records → verify clean with segments_scanned=0, not exit 4.

    The existing verify JSON fields are the hint: status=clean, valid_lines=0,
    segments_scanned=0. Missing collection remains ``error: audit file not found``.
    """
    path = tmp_path / "audit.jsonl"
    expired = NOW - timedelta(days=31)
    append_audit(path, _rec("onlyold1"), now=expired)
    expired_shard = utc_shard_path(path, expired)
    assert expired_shard.exists()
    result = verify_audit(path, now=NOW)
    assert result["status"] == "clean"
    assert result["segments_scanned"] == 0
    assert result["valid_lines"] == 0
    assert result["chained_records"] == 0
    assert result["invalid_lines"] == 0
    repaired = repair_audit(path, now=NOW)
    assert repaired["status"] == "clean"
    assert expired_shard.exists()
    recs = read_audit_records(path, now=NOW)
    assert recs == []


def test_verify_missing_collection_is_exit_4(tmp_path):
    missing = tmp_path / "nope.jsonl"
    with pytest.raises(AuditMaintenanceError) as ei:
        verify_audit(missing, now=NOW)
    assert ei.value.exit_code == 4
    assert "not found" in ei.value.public_message
    assert not missing.exists()
    assert not (tmp_path / "nope.jsonl.lock").exists()


def test_suffixless_base_read_and_invalid_shard_names_ignored(tmp_path):
    path = tmp_path / "mylog"
    initialize_audit_state(path)
    append_audit(path, _rec("bare0002"), now=NOW)
    (tmp_path / "mylog-2026-09-14.jsonl.bak").write_text("{}\n", encoding="utf-8")
    (tmp_path / "mylog-2026-99-99.jsonl").write_text("{}\n", encoding="utf-8")
    recs = read_audit_records(path, now=NOW)
    assert [r["record_id"] for r in recs] == ["bare0002"]


def test_fcntl_none_still_appends(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    monkeypatch.setattr(audit_mod, "fcntl", None)
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("nofcntl1"), now=NOW)
    recs = read_audit_records(path, now=NOW)
    assert recs[0]["record_id"] == "nofcntl1"


def test_lock_symlink_after_flock_is_rejected(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "audit.jsonl"
    decoy = tmp_path / "lock-target"
    decoy.write_text("x", encoding="utf-8")
    real_flock = audit_mod.fcntl.flock

    def hijack(fd, op):
        real_flock(fd, op)
        lock = _lock_path(path)
        if lock.exists() and not lock.is_symlink():
            lock.unlink()
            lock.symlink_to(decoy)

    monkeypatch.setattr(audit_mod.fcntl, "flock", hijack)
    with pytest.raises(AuditError, match="symbolic link"):
        append_audit(path, _rec("toctou01"), now=NOW)


def test_flock_oserror_maps_to_maintenance_lock_failure(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("lockok01"), now=NOW)

    def boom(_fd, _op):
        raise OSError("flock denied")

    monkeypatch.setattr(audit_mod.fcntl, "flock", boom)
    with pytest.raises(AuditMaintenanceError) as ei:
        verify_audit(path, now=NOW)
    assert ei.value.exit_code == 5
    assert "lock failed" in ei.value.public_message


def test_maintenance_generic_auditerror_is_lock_failure(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("lockok02"), now=NOW)

    def boom(*_a, **_k):
        raise AuditError("flock denied")

    monkeypatch.setattr(audit_mod, "_locked", boom)
    with pytest.raises(AuditMaintenanceError) as ei:
        verify_audit(path, now=NOW)
    assert ei.value.exit_code == 5
    assert "lock failed" in ei.value.public_message


def test_repair_outer_auditerror_symlink_and_generic(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("outer001"), now=NOW)

    def boom_symlink(*_a, **_k):
        raise AuditError("error: audit target is a symbolic link")

    monkeypatch.setattr(audit_mod, "_locked_maintenance", boom_symlink)
    with pytest.raises(AuditMaintenanceError) as ei:
        repair_audit(path, now=NOW)
    assert ei.value.exit_code == 4
    assert "symbolic link" in ei.value.public_message

    def boom_generic(*_a, **_k):
        raise AuditError("flock denied")

    monkeypatch.setattr(audit_mod, "_locked_maintenance", boom_generic)
    with pytest.raises(AuditMaintenanceError) as ei:
        repair_audit(path, now=NOW)
    assert ei.value.exit_code == 5
    assert "lock failed" in ei.value.public_message


def test_tail_short_os_read_fail_closed(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("short001"), now=NOW)
    shard = utc_shard_path(path, NOW)
    before = shard.read_bytes()
    real_open = audit_mod.os.open
    real_read = audit_mod.os.read
    shard_fds: set[int] = set()

    def wrapped_open(target, *a, **k):
        fd = real_open(target, *a, **k)
        if Path(str(target)).resolve() == shard.resolve():
            shard_fds.add(fd)
        return fd

    def wrapped_read(fd, n, *a, **k):
        if fd in shard_fds:
            return b""
        return real_read(fd, n, *a, **k)

    monkeypatch.setattr(audit_mod.os, "open", wrapped_open)
    monkeypatch.setattr(audit_mod.os, "read", wrapped_read)
    with pytest.raises(AuditError, match="截断"):
        append_audit(path, _rec("short002"), now=NOW)
    assert shard.read_bytes() == before


def test_tail_window_all_blanks_with_prefix_fail_closed(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("blankhd1"), now=NOW)
    shard = utc_shard_path(path, NOW)
    shard.write_bytes(shard.read_bytes() + b"\n" * (TAIL_READ_BYTES + 4))
    before = shard.read_bytes()
    with pytest.raises(AuditError, match="链头|空行"):
        append_audit(path, _rec("blanknx1"), now=NOW)
    assert shard.read_bytes() == before


def test_tail_last_line_fills_window_fail_closed(tmp_path):
    path = tmp_path / "audit.jsonl"
    shard = utc_shard_path(path, NOW)
    shard.write_bytes(b"HEAD\n" + b"Z" * (TAIL_READ_BYTES - 1) + b"\n")
    before = shard.read_bytes()
    with pytest.raises(AuditError, match="超过单条大小上限"):
        append_audit(path, _rec("winfill1"), now=NOW)
    assert shard.read_bytes() == before


def test_probe_legacy_symlink_and_read_symlink_base_with_shard(tmp_path):
    path = tmp_path / "audit.jsonl"
    target = tmp_path / "legacy-target.jsonl"
    target.write_text("{}\n", encoding="utf-8")
    path.symlink_to(target)
    with pytest.raises(AuditError, match="symbolic link"):
        _probe_previous_record(path, NOW)

    path.unlink()
    append_audit(path, _rec("realsh01"), now=NOW)
    path.symlink_to(target)
    with pytest.raises(AuditError, match="symbolic link"):
        read_audit_records(path, now=NOW)


def test_append_without_fchmod_and_write_oserror(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    monkeypatch.delattr(audit_mod.os, "fchmod", raising=False)
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("nofchm01"), now=NOW)
    assert read_audit_records(path, now=NOW)[0]["record_id"] == "nofchm01"

    def boom(_fd, _data):
        raise OSError("write denied")

    monkeypatch.setattr(audit_mod.os, "write", boom)
    with pytest.raises(AuditError, match="审计写入失败"):
        append_audit(path, _rec("writefail"), now=NOW)


def test_verify_and_repair_accept_naive_now(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("naivev01"), now=NOW)
    naive = datetime(2026, 9, 14, 12, 0, 0)
    assert verify_audit(path, now=naive)["status"] == "clean"
    assert repair_audit(path, now=naive)["status"] == "clean"


def test_require_regular_file_success_and_fifo(tmp_path):
    regular = tmp_path / "ok.jsonl"
    regular.write_text("{}\n", encoding="utf-8")
    _require_regular_audit_file(regular)

    fifo = tmp_path / "fifo.jsonl"
    os.mkfifo(fifo)
    with pytest.raises(AuditMaintenanceError) as ei:
        _require_regular_audit_file(fifo)
    assert ei.value.exit_code == 4
    assert "not a regular file" in ei.value.public_message

    with pytest.raises(AuditMaintenanceError) as ei:
        verify_audit(fifo, now=NOW)
    assert ei.value.exit_code == 4
    assert "not a regular file" in ei.value.public_message


def test_require_regular_lstat_oserror(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "audit.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    real = audit_mod.os.lstat

    def boom(target, *a, **k):
        if Path(target) == path:
            raise OSError("lstat denied")
        return real(target, *a, **k)

    monkeypatch.setattr(audit_mod.os, "lstat", boom)
    with pytest.raises(AuditMaintenanceError) as ei:
        _require_regular_audit_file(path)
    assert ei.value.exit_code == 5


def test_scan_audit_bytes_empty_and_unlabeled_invalid():
    assert _scan_audit_bytes(b"") == (0, [], [], [])
    valid, issues, kept, quarantined = _scan_audit_bytes(b"{not-json\n")
    assert valid == 0
    assert kept == []
    assert issues == [{"line_number": 1, "reason_code": "invalid_json"}]
    assert quarantined[0]["reason_code"] == "invalid_json"
    assert "segment_name" not in issues[0]


def test_verify_injected_symlink_shard_is_rejected(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("keepsh01"), now=NOW)
    decoy = tmp_path / "decoy.jsonl"
    decoy.write_text("{}\n", encoding="utf-8")
    link = tmp_path / "audit-2026-09-13.jsonl"
    link.symlink_to(decoy)

    def fake_list(_base):
        return [(date(2026, 9, 13), link)]

    monkeypatch.setattr(audit_mod, "_list_shards", fake_list)
    with pytest.raises(AuditMaintenanceError) as ei:
        verify_audit(path, now=NOW)
    assert ei.value.exit_code == 4
    assert "symbolic link" in ei.value.public_message


def test_repair_refuses_non_tail_damage_on_last_shard(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("keep000a"), now=NOW)
    append_audit(path, _rec("keep000b"), now=NOW)
    shard = utc_shard_path(path, NOW)
    lines = shard.read_bytes().splitlines(keepends=True)
    lines[0] = b"{bad-json-mid\n"
    shard.write_bytes(b"".join(lines))
    before = shard.read_bytes()
    result = repair_audit(path, now=NOW)
    assert result["status"] == "invalid"
    assert shard.read_bytes() == before
    assert list(tmp_path.glob("*.quarantine.*")) == []


def test_repair_last_shard_tail_links_prior_segment_hash(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("yestkeep"), now=NOW - timedelta(days=1))
    yesterday = utc_shard_path(path, NOW - timedelta(days=1))
    prior = json.loads(yesterday.read_text(encoding="utf-8").splitlines()[0])
    append_audit(path, _rec("todaytmp"), now=NOW)
    today = utc_shard_path(path, NOW)
    today.write_bytes(b"\n{not-json\n")
    result = repair_audit(path, now=NOW)
    assert result["status"] == "repaired"
    recs = read_audit_records(path, now=NOW)
    marker = recs[-1]
    assert is_audit_maintenance_record(marker)
    assert marker["prev_hash"] == prior["record_hash"]
    assert verify_audit(path, now=NOW)["status"] == "clean"


def test_repair_only_truncated_shard_has_null_prev_hash(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("tmp00001"), now=NOW)
    shard = utc_shard_path(path, NOW)
    shard.write_bytes(b"{truncated-only\n")
    result = repair_audit(path, now=NOW)
    assert result["status"] == "repaired"
    recs = read_audit_records(path, now=NOW)
    assert is_audit_maintenance_record(recs[-1])
    assert recs[-1]["prev_hash"] is None


def test_atomic_replace_cleans_missing_tmp(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("keep000c"), now=NOW)
    shard = utc_shard_path(path, NOW)
    shard.write_bytes(shard.read_bytes() + b"{partial")
    before = shard.read_bytes()
    real_replace = audit_mod.os.replace

    def boom(src, dst, *a, **k):
        Path(src).unlink(missing_ok=True)
        raise OSError("replace denied")

    monkeypatch.setattr(audit_mod.os, "replace", boom)
    with pytest.raises(AuditMaintenanceError) as ei:
        repair_audit(path, now=NOW)
    assert ei.value.exit_code == 5
    assert shard.read_bytes() == before
    del real_replace


def test_quarantine_name_allocation_exhausted(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec("keep000d"), now=NOW)
    shard = utc_shard_path(path, NOW)
    shard.write_bytes(shard.read_bytes() + b"{partial")
    before = shard.read_bytes()

    def always_exists(*_a, **_k):
        raise FileExistsError("name taken")

    monkeypatch.setattr(audit_mod, "_publish_exclusive_bytes", always_exists)
    with pytest.raises(AuditMaintenanceError) as ei:
        repair_audit(path, now=NOW)
    assert ei.value.exit_code == 5
    assert "allocation failed" in ei.value.public_message
    assert shard.read_bytes() == before
    assert list(tmp_path.glob("*.quarantine.*")) == []


def test_build_audit_record_schema_failure():
    from tests.conftest import fresh_order, fresh_portfolio, full_policy

    result = SimpleNamespace(
        evaluated_at="not-rfc3339",
        violations=[],
        warnings=[],
        evidence={"input_hash": "0" * 64},
        decision="PASS",
        shadow_mode=False,
        shadow_verdict=None,
        exit_code=0,
    )
    with pytest.raises(AuditError, match="未通过 audit-record.schema"):
        build_audit_record(fresh_order(), fresh_portfolio(), full_policy(), result)
