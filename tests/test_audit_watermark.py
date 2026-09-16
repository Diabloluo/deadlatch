"""Write-date watermark, explicit init, and crash/regression coverage."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from jsonschema import Draft202012Validator

from deadlatch.audit import (
    AUDIT_ADOPT_REQUIRED,
    AUDIT_DATE_REGRESSION,
    AUDIT_STATE_INCONSISTENT,
    AUDIT_STATE_INVALID,
    AUDIT_STATE_IO_ERROR,
    AUDIT_STATE_MISSING,
    MAX_STATE_BYTES,
    STATE_READ_LIMIT,
    AuditError,
    AuditMaintenanceError,
    append_audit,
    collection_exists,
    initialize_audit_state,
    prune_audit,
    read_audit_records,
    repair_audit,
    utc_shard_path,
    verify_audit,
    _inspect_matching_names,
    _raise_maintenance_from_state,
    _state_path,
)
from deadlatch.cli import main

REPO = Path(__file__).resolve().parents[1]
DAY = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
STATE_RESULT_SCHEMA = json.loads(
    (REPO / "schemas" / "audit-state-result.schema.json").read_text(encoding="utf-8")
)
STATE_RESULT_VALIDATOR = Draft202012Validator(STATE_RESULT_SCHEMA)
MAINTENANCE_RESULT_SCHEMA = json.loads(
    (REPO / "schemas" / "audit-maintenance-result.schema.json").read_text(encoding="utf-8")
)
MAINTENANCE_RESULT_VALIDATOR = Draft202012Validator(MAINTENANCE_RESULT_SCHEMA)


def _rec(rid: str, evaluated_at: str = "2026-09-16T12:00:00Z") -> dict:
    return {
        "schema_version": 1,
        "record_id": rid,
        "evaluated_at": evaluated_at,
        "input_hash": "0" * 64,
        "decision": "BLOCK",
        "shadow_mode": False,
        "shadow_verdict": None,
        "exit_code": 3,
        "policy_version": "synthetic",
        "rule_hits": [],
    }


def _state_bytes(path: Path) -> bytes:
    return _state_path(path).read_bytes()


def test_missing_state_refuses_append_without_directory_scan(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "virgin.jsonl"
    hits = []
    real = os.listdir

    def wrapped(target, *a, **k):
        hits.append(str(target))
        return real(target, *a, **k)

    monkeypatch.setattr(audit_mod.os, "listdir", wrapped)
    with pytest.raises(AuditError) as exc:
        append_audit(path, _rec("missing01"), now=DAY)
    assert exc.value.code == AUDIT_STATE_MISSING
    assert not hits
    assert not utc_shard_path(path, DAY).exists()


@pytest.mark.parametrize("gap", (1, 29, 30, 31, 365))
def test_explicit_now_regression_old_fail_new_pass(tmp_path, gap):
    path = tmp_path / "gaps.jsonl"
    initialize_audit_state(path)
    append_audit(path, _rec("later0001"), now=DAY + timedelta(days=gap))
    before_logs = {p.name: p.read_bytes() for p in path.parent.glob("*.jsonl")}
    before_state = _state_bytes(path)
    assert verify_audit(path, now=DAY)["status"] == "clean"
    with pytest.raises(AuditError) as exc:
        append_audit(path, _rec("earlier01"), now=DAY)
    assert exc.value.code == AUDIT_DATE_REGRESSION
    after_logs = {p.name: p.read_bytes() for p in path.parent.glob("*.jsonl")}
    assert after_logs == before_logs
    assert _state_bytes(path) == before_state
    assert verify_audit(path, now=DAY)["status"] == "clean"
    read_audit_records(path, now=DAY)


def test_default_clock_rollback_refused_bytes_unchanged(tmp_path):
    import deadlatch.audit as audit_mod

    path = tmp_path / "clock.jsonl"
    initialize_audit_state(path)
    append_audit(path, _rec("later0002"), now=DAY + timedelta(days=30))
    before_logs = {p.name: p.read_bytes() for p in path.parent.glob("*.jsonl")}
    before_state = _state_bytes(path)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromisoformat(DAY.isoformat())

    with patch.object(audit_mod, "datetime", Frozen):
        with pytest.raises(AuditError) as exc:
            append_audit(path, _rec("earlier02"))
    assert exc.value.code == AUDIT_DATE_REGRESSION
    after_logs = {p.name: p.read_bytes() for p in path.parent.glob("*.jsonl")}
    assert after_logs == before_logs
    assert _state_bytes(path) == before_state
    assert verify_audit(path, now=DAY)["status"] == "clean"


def test_same_day_and_forward_dates_succeed(tmp_path):
    path = tmp_path / "forward.jsonl"
    initialize_audit_state(path)
    append_audit(path, _rec("day000001"), now=DAY)
    append_audit(path, _rec("day000002"), now=DAY)
    append_audit(path, _rec("nextday01"), now=DAY + timedelta(days=1))
    recs = read_audit_records(path, now=DAY + timedelta(days=1))
    assert [r["record_id"] for r in recs] == ["day000001", "day000002", "nextday01"]
    assert recs[1]["prev_hash"] == recs[0]["record_hash"]
    assert recs[2]["prev_hash"] == recs[1]["record_hash"]
    assert verify_audit(path, now=DAY + timedelta(days=1))["status"] == "clean"
    payload = json.loads(_state_bytes(path))
    assert payload["reserved_through"] == (DAY + timedelta(days=1)).date().isoformat()


def test_init_empty_legacy_v2_and_far_future(tmp_path):
    empty = tmp_path / "empty.jsonl"
    result = initialize_audit_state(empty)
    STATE_RESULT_VALIDATOR.validate(result)
    assert result["status"] == "initialized"
    assert result["reserved_through"] is None
    assert verify_audit(empty)["status"] == "clean"
    assert read_audit_records(empty) == []

    again = initialize_audit_state(empty)
    assert again["status"] == "unchanged"
    first_state = _state_bytes(empty)
    assert _state_bytes(empty) == first_state

    legacy = tmp_path / "legacy.jsonl"
    row = json.dumps(_rec("legacy001"), sort_keys=True, separators=(",", ":")) + "\n"
    legacy.write_text(row, encoding="utf-8")
    before = legacy.read_bytes()
    with pytest.raises(AuditError) as exc:
        initialize_audit_state(legacy)
    assert exc.value.exit_code == 4
    adopted = initialize_audit_state(legacy, adopt_existing=True)
    assert adopted["status"] == "initialized"
    assert adopted["reserved_through"] is None
    assert legacy.read_bytes() == before

    planted = tmp_path / "planted.jsonl"
    far = utc_shard_path(planted, DAY + timedelta(days=365))
    far.write_text("", encoding="utf-8")
    empty_tail = utc_shard_path(planted, DAY)
    empty_tail.write_bytes(b"{broken")
    before_files = {p.name: p.read_bytes() for p in planted.parent.iterdir() if p.is_file()}
    result = initialize_audit_state(planted, adopt_existing=True)
    assert result["reserved_through"] == (DAY + timedelta(days=365)).date().isoformat()
    after_files = {p.name: p.read_bytes() for p in planted.parent.iterdir() if p.is_file()}
    for name, blob in before_files.items():
        assert after_files[name] == blob
    assert verify_audit(planted, now=DAY)["status"] != "clean"


def test_init_does_not_claim_damaged_logs_are_clean(tmp_path):
    path = tmp_path / "broken.jsonl"
    utc_shard_path(path, DAY).write_bytes(b"{nope\n")
    initialize_audit_state(path, adopt_existing=True)
    assert verify_audit(path, now=DAY)["status"] == "invalid"


def test_init_rejects_matching_symlink_and_does_not_overwrite_bad_state(tmp_path):
    path = tmp_path / "sym.jsonl"
    decoy = tmp_path / "decoy.jsonl"
    decoy.write_text("x\n", encoding="utf-8")
    utc_shard_path(path, DAY).symlink_to(decoy)
    with pytest.raises(AuditError, match="not a regular file"):
        initialize_audit_state(path, adopt_existing=True)

    other = tmp_path / "bad-state.jsonl"
    initialize_audit_state(other)
    _state_path(other).write_text("{not-json", encoding="utf-8")
    with pytest.raises(AuditError) as exc:
        initialize_audit_state(other)
    assert exc.value.code == AUDIT_STATE_INVALID


@pytest.mark.parametrize(
    "payload",
    (
        b"{" + b"x" * (MAX_STATE_BYTES + 1),
        b'{"protocol_version":1,"base_name":"x.jsonl","reserved_through":null,'
        b'"extra":1}\n',
        b'{"protocol_version":2,"base_name":"x.jsonl","reserved_through":null}\n',
        b'{"protocol_version":1,"base_name":"other.jsonl","reserved_through":null}\n',
        b'{"protocol_version":1,"base_name":"x.jsonl","reserved_through":"2026-13-40"}\n',
        b"\xff\xfe",
    ),
)
def test_invalid_state_refuses_append(tmp_path, payload):
    path = tmp_path / "x.jsonl"
    initialize_audit_state(path)
    _state_path(path).write_bytes(payload)
    with pytest.raises(AuditError) as exc:
        append_audit(path, _rec("badstate1"), now=DAY)
    assert exc.value.code == AUDIT_STATE_INVALID
    assert not utc_shard_path(path, DAY).exists()


def test_duplicate_json_key_state_rejected(tmp_path):
    path = tmp_path / "dup.jsonl"
    initialize_audit_state(path)
    _state_path(path).write_bytes(
        b'{"protocol_version":1,"base_name":"dup.jsonl","reserved_through":null,'
        b'"base_name":"dup.jsonl"}\n'
    )
    with pytest.raises(AuditError) as exc:
        append_audit(path, _rec("dupkey001"), now=DAY)
    assert exc.value.code == AUDIT_STATE_INVALID


def test_state_size_boundary_4096(tmp_path):
    path = tmp_path / "edge.jsonl"
    initialize_audit_state(path)
    _state_path(path).write_bytes(b"{" + b"a" * MAX_STATE_BYTES)
    with pytest.raises(AuditError) as exc:
        append_audit(path, _rec("oversize1"), now=DAY)
    assert exc.value.code == AUDIT_STATE_INVALID
    assert STATE_READ_LIMIT == MAX_STATE_BYTES + 1


def test_state_symlink_and_directory_rejected(tmp_path):
    path = tmp_path / "linkcol.jsonl"
    initialize_audit_state(path)
    target = tmp_path / "real-state.json"
    target.write_text("{}", encoding="utf-8")
    _state_path(path).unlink()
    _state_path(path).symlink_to(target)
    with pytest.raises(AuditError) as exc:
        append_audit(path, _rec("link00001"), now=DAY)
    assert exc.value.code == AUDIT_STATE_INVALID

    other = tmp_path / "dircoll.jsonl"
    _state_path(other).mkdir()
    with pytest.raises(AuditError):
        append_audit(other, _rec("dir000001"), now=DAY)


def test_invalid_record_does_not_advance_watermark(tmp_path):
    path = tmp_path / "invalid.jsonl"
    initialize_audit_state(path)
    before = _state_bytes(path)
    with pytest.raises(AuditError):
        append_audit(path, ["not-a-record"], now=DAY)  # type: ignore[arg-type]
    assert _state_bytes(path) == before
    append_audit(path, _rec("okrecord1"), now=DAY + timedelta(days=2))
    after = json.loads(_state_bytes(path))
    assert after["reserved_through"] == (DAY + timedelta(days=2)).date().isoformat()
    with pytest.raises(AuditError) as exc:
        append_audit(path, _rec("too-early"), now=DAY)
    assert exc.value.code == AUDIT_DATE_REGRESSION


def test_reserve_then_write_failure_keeps_raised_h(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "crashy.jsonl"
    initialize_audit_state(path)
    real = audit_mod._append_line_fsync

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(audit_mod, "_append_line_fsync", boom)
    with pytest.raises(AuditError):
        append_audit(path, _rec("failwrite"), now=DAY)
    payload = json.loads(_state_bytes(path))
    assert payload["reserved_through"] == DAY.date().isoformat()
    assert not utc_shard_path(path, DAY).exists()
    monkeypatch.setattr(audit_mod, "_append_line_fsync", real)
    append_audit(path, _rec("same-day2"), now=DAY)
    with pytest.raises(AuditError) as exc:
        append_audit(path, _rec("yesterday"), now=DAY - timedelta(days=1))
    assert exc.value.code == AUDIT_DATE_REGRESSION


def test_prune_keeps_watermark(tmp_path):
    path = tmp_path / "pruned.jsonl"
    initialize_audit_state(path)
    expired = DAY - timedelta(days=40)
    append_audit(path, _rec("old000001", expired.strftime("%Y-%m-%dT12:00:00Z")), now=expired)
    append_audit(path, _rec("today0001"), now=DAY)
    before = json.loads(_state_bytes(path))
    result = prune_audit(path, now=DAY)
    assert result["removed_segments"] == 1
    after = json.loads(_state_bytes(path))
    assert after == before
    assert after["reserved_through"] == DAY.date().isoformat()


def test_repair_does_not_lower_watermark(tmp_path):
    path = tmp_path / "repair.jsonl"
    initialize_audit_state(path)
    append_audit(path, _rec("head00001"), now=DAY)
    shard = utc_shard_path(path, DAY)
    shard.write_bytes(shard.read_bytes() + b"{trunc")
    before = _state_bytes(path)
    result = repair_audit(path, now=DAY)
    assert result["status"] == "repaired"
    assert _state_bytes(path) == before


def test_legacy_verify_without_state_still_works(tmp_path):
    path = tmp_path / "old.jsonl"
    path.write_text(
        json.dumps(_rec("legacy002"), sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    assert verify_audit(path)["status"] == "clean"
    with pytest.raises(AuditError) as exc:
        append_audit(path, _rec("new000001"), now=DAY)
    assert exc.value.code == AUDIT_STATE_MISSING


def test_inconsistent_state_fail_closed_on_read_report(tmp_path):
    path = tmp_path / "lag.jsonl"
    initialize_audit_state(path)
    utc_shard_path(path, DAY + timedelta(days=10)).write_text("\n", encoding="utf-8")
    with pytest.raises(AuditError) as exc:
        read_audit_records(path, now=DAY)
    assert exc.value.code == AUDIT_STATE_INCONSISTENT
    with pytest.raises(AuditMaintenanceError):
        verify_audit(path, now=DAY)


def test_cli_init_json_success_and_adopt_required(tmp_path, capsys):
    path = tmp_path / "cli.jsonl"
    rc = main(["audit", "init", "--audit-path", str(path), "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    body = json.loads(captured.out)
    STATE_RESULT_VALIDATOR.validate(body)
    assert body["status"] == "initialized"
    utc_shard_path(tmp_path / "need-adopt.jsonl", DAY).write_text("", encoding="utf-8")
    rc = main(["audit", "init", "--audit-path", str(tmp_path / "need-adopt.jsonl"), "--json"])
    captured = capsys.readouterr()
    assert rc == 4
    body = json.loads(captured.out)
    STATE_RESULT_VALIDATOR.validate(body)
    assert body["status"] == "error"
    assert body["reserved_through"] is None
    assert body["error_code"]


def test_subprocess_crash_after_state_before_log(tmp_path):
    path = tmp_path / "subcrash.jsonl"
    initialize_audit_state(path)
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    script = r"""
import os, sys
from datetime import datetime, timezone
from deadlatch import audit
base = sys.argv[1]
now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
rec = dict(schema_version=1, record_id="crash0001", evaluated_at="2026-09-16T12:00:00Z",
           input_hash="0"*64, decision="PASS", shadow_mode=False, shadow_verdict=None,
           exit_code=0, policy_version="synthetic", rule_hits=[])
def boom(*a, **k):
    os._exit(17)
audit._append_line_fsync = boom
audit.append_audit(base, rec, now=now)
"""
    proc = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        env=env, capture_output=True, timeout=30,
    )
    assert proc.returncode == 17
    payload = json.loads(_state_bytes(path))
    assert payload["reserved_through"] == "2026-09-16"
    assert not utc_shard_path(path, DAY).exists()
    append_audit(path, _rec("aftercrash"), now=DAY)
    assert utc_shard_path(path, DAY).exists()
    with pytest.raises(AuditError) as exc:
        append_audit(path, _rec("too-early"), now=DAY - timedelta(days=1))
    assert exc.value.code == AUDIT_DATE_REGRESSION


def test_subprocess_crash_before_state_replace(tmp_path):
    path = tmp_path / "replace-crash.jsonl"
    initialize_audit_state(path)
    before = _state_bytes(path)
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    script = r"""
import os, sys
from datetime import datetime, timezone
from deadlatch import audit
base = sys.argv[1]
now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
rec = dict(schema_version=1, record_id="crash0002", evaluated_at="2026-09-16T12:00:00Z",
           input_hash="0"*64, decision="PASS", shadow_mode=False, shadow_verdict=None,
           exit_code=0, policy_version="synthetic", rule_hits=[])
real = os.replace
def boom(src, dst):
    if str(dst).endswith(".state.json"):
        os._exit(19)
    return real(src, dst)
os.replace = boom
audit.append_audit(base, rec, now=now)
"""
    proc = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        env=env, capture_output=True, timeout=30,
    )
    assert proc.returncode == 19
    assert _state_bytes(path) == before
    assert not utc_shard_path(path, DAY).exists()
    append_audit(path, _rec("after-repl"), now=DAY)
    assert json.loads(_state_bytes(path))["reserved_through"] == "2026-09-16"


def test_dir_fsync_failure_after_replace_does_not_write_log(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "dirfsync.jsonl"
    initialize_audit_state(path)
    real_fsync = audit_mod.os.fsync
    state_replaced = {"done": False}
    real_replace = audit_mod.os.replace

    def wrapped_replace(src, dst):
        result = real_replace(src, dst)
        if str(dst).endswith(".state.json"):
            state_replaced["done"] = True
        return result

    def boom(fd):
        if state_replaced["done"]:
            raise OSError("dir fsync denied")
        return real_fsync(fd)

    monkeypatch.setattr(audit_mod.os, "replace", wrapped_replace)
    monkeypatch.setattr(audit_mod.os, "fsync", boom)
    with pytest.raises(AuditError):
        append_audit(path, _rec("nofsync01"), now=DAY)
    assert json.loads(_state_bytes(path))["reserved_through"] == "2026-09-16"
    assert not utc_shard_path(path, DAY).exists()
    monkeypatch.setattr(audit_mod.os, "fsync", real_fsync)
    append_audit(path, _rec("aftersync"), now=DAY)
    assert utc_shard_path(path, DAY).exists()


def test_concurrent_init_serializes(tmp_path):
    path = tmp_path / "race.jsonl"
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    script = r"""
import sys
from deadlatch.audit import initialize_audit_state
print(initialize_audit_state(sys.argv[1])["status"])
"""
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(path)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(4)
    ]
    statuses = []
    for proc in procs:
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, err
        statuses.append(out.strip())
    assert statuses.count("initialized") == 1
    assert statuses.count("unchanged") == 3
    assert json.loads(_state_bytes(path))["reserved_through"] is None


def test_cli_init_text_success(tmp_path, capsys):
    path = tmp_path / "cli-text.jsonl"
    rc = main(["audit", "init", "--audit-path", str(path)])
    captured = capsys.readouterr()
    assert rc == 0
    assert "initialized" in captured.out
    assert "reserved_through=None" in captured.out


def test_cli_init_text_adopt_required(tmp_path, capsys):
    path = tmp_path / "need-text.jsonl"
    utc_shard_path(path, DAY).write_text("", encoding="utf-8")
    rc = main(["audit", "init", "--audit-path", str(path)])
    captured = capsys.readouterr()
    assert rc == 4
    assert "adopt-existing" in captured.err


def test_cli_init_unexpected_exception_json(tmp_path, capsys, monkeypatch):
    import deadlatch.cli as cli_mod

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli_mod, "initialize_audit_state", boom)
    rc = main(["audit", "init", "--audit-path", str(tmp_path / "x.jsonl"), "--json"])
    captured = capsys.readouterr()
    assert rc == 5
    assert "internal error" in captured.err
    body = json.loads(captured.out)
    assert body["status"] == "error"


def test_empty_initialized_collection_is_readable(tmp_path):
    path = tmp_path / "empty-init.jsonl"
    initialize_audit_state(path)
    assert collection_exists(path)
    assert read_audit_records(path, now=DAY) == []
    assert verify_audit(path, now=DAY)["status"] == "clean"


def test_init_rejects_invalid_calendar_shard_name(tmp_path):
    path = tmp_path / "bad-cal.jsonl"
    (tmp_path / "bad-cal-2026-02-30.jsonl").write_text("x", encoding="utf-8")
    with pytest.raises(AuditError) as exc:
        initialize_audit_state(path, adopt_existing=True)
    assert exc.value.code == AUDIT_STATE_INVALID


def test_init_rejects_directory_and_fifo_base(tmp_path):
    as_dir = tmp_path / "dirbase.jsonl"
    as_dir.mkdir()
    with pytest.raises(AuditError) as exc:
        initialize_audit_state(as_dir)
    assert exc.value.exit_code == 4
    fifo = tmp_path / "fifo.jsonl"
    os.mkfifo(fifo)
    with pytest.raises(AuditError) as exc:
        initialize_audit_state(fifo)
    assert exc.value.code == AUDIT_STATE_INVALID


def test_inspect_missing_parent_is_empty(tmp_path):
    missing = tmp_path / "no-such-dir" / "x.jsonl"
    assert _inspect_matching_names(missing) == (None, False, False)


def test_inspect_listdir_oserror(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "listdeny.jsonl"

    def boom(_p):
        raise OSError("listdir denied")

    monkeypatch.setattr(audit_mod.os, "listdir", boom)
    with pytest.raises(AuditError) as exc:
        initialize_audit_state(path)
    assert exc.value.code == AUDIT_STATE_IO_ERROR


def test_inspect_candidate_lstat_errors(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "cand.jsonl"
    shard = utc_shard_path(path, DAY)
    shard.write_text("\n", encoding="utf-8")
    real_lstat = audit_mod.os.lstat

    def vanish(p, *a, **k):
        if Path(p).name == shard.name:
            raise FileNotFoundError(p)
        return real_lstat(p)

    monkeypatch.setattr(audit_mod.os, "lstat", vanish)
    result = initialize_audit_state(path, adopt_existing=True)
    assert result["status"] == "initialized"
    monkeypatch.setattr(audit_mod.os, "lstat", real_lstat)

    def deny(p, *a, **k):
        if Path(p).name == shard.name:
            raise OSError("lstat denied")
        return real_lstat(p)

    _state_path(path).unlink()
    monkeypatch.setattr(audit_mod.os, "lstat", deny)
    with pytest.raises(AuditError) as exc:
        initialize_audit_state(path, adopt_existing=True)
    assert exc.value.code == AUDIT_STATE_IO_ERROR


def test_inspect_base_lstat_oserror(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "baseio.jsonl"
    real_lstat = audit_mod.os.lstat

    def deny(p, *a, **k):
        if Path(p) == path:
            raise OSError("base lstat denied")
        return real_lstat(p)

    monkeypatch.setattr(audit_mod.os, "lstat", deny)
    with pytest.raises(AuditError) as exc:
        initialize_audit_state(path)
    assert exc.value.code == AUDIT_STATE_IO_ERROR


def test_init_state_lstat_oserror(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "stlstat.jsonl"
    initialize_audit_state(path)
    real_lstat = audit_mod.os.lstat

    def deny(p, *a, **k):
        if str(p).endswith(".state.json"):
            raise OSError("state lstat denied")
        return real_lstat(p)

    monkeypatch.setattr(audit_mod.os, "lstat", deny)
    with pytest.raises(AuditError) as exc:
        initialize_audit_state(path)
    assert exc.value.code == AUDIT_STATE_IO_ERROR


def test_reject_existing_wrong_type_oserror(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "wrongio.jsonl"
    initialize_audit_state(path)
    real_lstat = audit_mod.os.lstat

    def deny(p, *a, **k):
        if str(p).endswith(".state.json"):
            raise OSError("state lstat denied")
        return real_lstat(p)

    monkeypatch.setattr(audit_mod.os, "lstat", deny)
    with pytest.raises(AuditError) as exc:
        append_audit(path, _rec("wrongio01"), now=DAY)
    assert exc.value.code == AUDIT_STATE_IO_ERROR


def test_publish_short_write_refuses(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "shortst.jsonl"
    initialize_audit_state(path)
    monkeypatch.setattr(audit_mod.os, "write", lambda *a, **k: 0)
    with pytest.raises(AuditError) as exc:
        append_audit(path, _rec("shortst01"), now=DAY)
    assert exc.value.code == AUDIT_STATE_IO_ERROR
    assert not utc_shard_path(path, DAY).exists()


def test_encode_state_rejects_oversize(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "tiny.jsonl"
    monkeypatch.setattr(audit_mod, "MAX_STATE_BYTES", 8)
    with pytest.raises(AuditError) as exc:
        initialize_audit_state(path)
    assert exc.value.code == AUDIT_STATE_INVALID


def test_open_nofollow_without_flag(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "nofollow.jsonl"
    initialize_audit_state(path)
    monkeypatch.delattr(audit_mod.os, "O_NOFOLLOW", raising=False)
    append_audit(path, _rec("nofollow1"), now=DAY)
    assert utc_shard_path(path, DAY).exists()


def test_open_nofollow_fstat_and_nonregular(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod
    import stat as statmod

    path = tmp_path / "fstat.jsonl"
    initialize_audit_state(path)
    real_open = audit_mod.os.open
    real_fstat = audit_mod.os.fstat
    tracked = {"fd": None}

    def wrap_open(p, flags, mode=0o777):
        fd = real_open(p, flags, mode)
        if str(p).endswith(".state.json") or str(p).endswith(".state.tmp"):
            tracked["fd"] = fd
        return fd

    def boom_fstat(fd):
        if fd == tracked["fd"]:
            raise OSError("fstat denied")
        return real_fstat(fd)

    monkeypatch.setattr(audit_mod.os, "open", wrap_open)
    monkeypatch.setattr(audit_mod.os, "fstat", boom_fstat)
    with pytest.raises(AuditError) as exc:
        append_audit(path, _rec("fstat0001"), now=DAY)
    assert exc.value.code == AUDIT_STATE_IO_ERROR

    class FakeStat:
        st_mode = statmod.S_IFDIR

    def dir_fstat(fd):
        if fd == tracked["fd"]:
            return FakeStat()
        return real_fstat(fd)

    monkeypatch.setattr(audit_mod.os, "fstat", dir_fstat)
    with pytest.raises(AuditError) as exc:
        append_audit(path, _rec("fstatdir1"), now=DAY)
    assert exc.value.code == AUDIT_STATE_INVALID


def test_open_nofollow_open_oserror(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "openio.jsonl"
    initialize_audit_state(path)
    real_open = audit_mod.os.open

    def boom(p, flags, mode=0o777):
        if str(p).endswith(".state.tmp"):
            raise OSError("open denied")
        return real_open(p, flags, mode)

    monkeypatch.setattr(audit_mod.os, "open", boom)
    with pytest.raises(AuditError) as exc:
        append_audit(path, _rec("openio001"), now=DAY)
    assert exc.value.code == AUDIT_STATE_IO_ERROR


def test_read_state_bytes_oserror(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "readst.jsonl"
    initialize_audit_state(path)
    real_open = audit_mod.os.open
    real_read = audit_mod.os.read
    tracked = {"fd": None}

    def wrap_open(p, flags, mode=0o777):
        fd = real_open(p, flags, mode)
        if str(p).endswith(".state.json"):
            tracked["fd"] = fd
        return fd

    def boom(fd, n):
        if fd == tracked["fd"]:
            raise OSError("read denied")
        return real_read(fd, n)

    monkeypatch.setattr(audit_mod.os, "open", wrap_open)
    monkeypatch.setattr(audit_mod.os, "read", boom)
    with pytest.raises(AuditError) as exc:
        append_audit(path, _rec("readst001"), now=DAY)
    assert exc.value.code == AUDIT_STATE_IO_ERROR


def test_same_day_state_fsync_failure(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "sfsync.jsonl"
    initialize_audit_state(path)
    append_audit(path, _rec("first0001"), now=DAY)
    real_fsync = audit_mod.os.fsync
    calls = {"n": 0}

    def boom(fd):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("state fsync denied")
        return real_fsync(fd)

    monkeypatch.setattr(audit_mod.os, "fsync", boom)
    with pytest.raises(AuditError) as exc:
        append_audit(path, _rec("second002"), now=DAY)
    assert exc.value.code == AUDIT_STATE_IO_ERROR


def test_same_day_dir_fsync_failure(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "sdfsync.jsonl"
    initialize_audit_state(path)
    append_audit(path, _rec("first0001"), now=DAY)
    real_fsync = audit_mod.os.fsync
    calls = {"n": 0}

    def boom(fd):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("dir fsync denied")
        return real_fsync(fd)

    monkeypatch.setattr(audit_mod.os, "fsync", boom)
    with pytest.raises(AuditError) as exc:
        append_audit(path, _rec("second002"), now=DAY)
    assert exc.value.code == AUDIT_STATE_IO_ERROR


def test_new_shard_parent_fsync_failure(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "nsync.jsonl"
    initialize_audit_state(path)
    real_fsync = audit_mod.os.fsync
    real_replace = audit_mod.os.replace
    state_done = {"n": False}
    log_fsyncs = {"n": 0}

    def wrap_replace(src, dst):
        result = real_replace(src, dst)
        if str(dst).endswith(".state.json"):
            state_done["n"] = True
        return result

    def boom(fd):
        if state_done["n"]:
            log_fsyncs["n"] += 1
            # publish already fsynced tmp+dir; next is log then parent dir
            if log_fsyncs["n"] >= 3:
                raise OSError("parent fsync denied")
        return real_fsync(fd)

    monkeypatch.setattr(audit_mod.os, "replace", wrap_replace)
    monkeypatch.setattr(audit_mod.os, "fsync", boom)
    with pytest.raises(AuditError, match="fsync"):
        append_audit(path, _rec("nsync0001"), now=DAY)


def test_append_line_oserror_wrapped(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "wrap.jsonl"
    initialize_audit_state(path)

    def boom(*a, **k):
        raise OSError("append denied")

    monkeypatch.setattr(audit_mod, "_append_line_fsync", boom)
    with pytest.raises(AuditError, match="写入失败"):
        append_audit(path, _rec("wrap00001"), now=DAY)
    assert json.loads(_state_bytes(path))["reserved_through"] == "2026-09-16"
    assert not utc_shard_path(path, DAY).exists()


def test_init_locked_oserror(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "lockio.jsonl"

    def boom(*a, **k):
        raise OSError("lock denied")

    monkeypatch.setattr(audit_mod, "_locked", boom)
    with pytest.raises(AuditError) as exc:
        initialize_audit_state(path)
    assert exc.value.code == AUDIT_STATE_IO_ERROR


def test_prune_keeps_unattributed_state_tmp_and_other_collections(tmp_path):
    path = tmp_path / "ptmp.jsonl"
    other = tmp_path / "other.jsonl"
    initialize_audit_state(path)
    initialize_audit_state(other)
    tmp = tmp_path / f".{'ab' * 16}.state.tmp"
    tmp.write_text("junk", encoding="utf-8")
    other_tmp = tmp_path / f".{'ef' * 16}.state.tmp"
    other_tmp.write_text("other-in-use", encoding="utf-8")
    link = tmp_path / f".{'cd' * 16}.state.tmp"
    link.symlink_to(tmp)
    before_other_state = _state_bytes(other)
    result = prune_audit(path, now=DAY)
    assert result["status"] == "clean"
    assert tmp.exists()
    assert other_tmp.exists()
    assert link.is_symlink()
    assert _state_bytes(other) == before_other_state
    assert _state_path(other).is_file()


def test_prune_a_does_not_delete_b_in_flight_state_tmp(tmp_path):
    import deadlatch.audit as audit_mod

    parent = tmp_path / "two-collections"
    parent.mkdir()
    a, b = parent / "a.jsonl", parent / "b.jsonl"
    initialize_audit_state(a)
    initialize_audit_state(b)
    ready, release = threading.Event(), threading.Event()
    output = {}
    real_replace = audit_mod.os.replace

    def paused_replace(src, dst):
        if str(dst) == str(_state_path(b)):
            output["tmp"] = Path(src)
            ready.set()
            if not release.wait(15):
                raise RuntimeError("barrier timeout")
        return real_replace(src, dst)

    def writer():
        try:
            append_audit(b, _rec("synthetic-b"), now=DAY)
            output["writer"] = "success"
        except AuditError as exc:
            output["writer"] = exc.code

    with patch.object(audit_mod.os, "replace", paused_replace):
        worker = threading.Thread(target=writer)
        worker.start()
        try:
            assert ready.wait(15), "writer not paused"
            assert output["tmp"].exists()
            prune_result = prune_audit(a, now=DAY)
            deleted = not output["tmp"].exists()
        finally:
            release.set()
            worker.join(15)
        assert not worker.is_alive()
    assert prune_result["status"] == "clean"
    assert deleted is False
    assert output["writer"] == "success"
    assert utc_shard_path(b, DAY).exists()


def _damaged_collection_after_failed_adopt_dir_sync(root: Path) -> Path:
    import deadlatch.audit as audit_mod

    source = root / "source.jsonl"
    initialize_audit_state(source)
    append_audit(source, _rec("synthetic-original"), now=DAY)
    shard = utc_shard_path(source, DAY)
    shard.write_bytes(shard.read_bytes() + b"{broken\n")
    candidate = root / "failed-adoption" / "audit.jsonl"
    candidate.parent.mkdir()
    utc_shard_path(candidate, DAY).write_bytes(shard.read_bytes())

    def failed_dir_sync(*_args):
        raise OSError("synthetic directory sync failure")

    with patch.object(audit_mod, "_fsync_dir", failed_dir_sync):
        with pytest.raises(AuditError) as exc:
            initialize_audit_state(candidate, adopt_existing=True)
        assert exc.value.code == AUDIT_STATE_IO_ERROR
    assert _state_path(candidate).is_file()
    return candidate


def test_v2_repair_refuses_when_state_sync_barrier_fails(tmp_path):
    import deadlatch.audit as audit_mod

    candidate = _damaged_collection_after_failed_adopt_dir_sync(tmp_path)
    shard = utc_shard_path(candidate, DAY)
    before_log = shard.read_bytes()
    before_state = _state_bytes(candidate)
    calls = []

    def failed_sync(*_args):
        calls.append("called")
        raise AuditError("synthetic state sync failure", code=AUDIT_STATE_IO_ERROR)

    with patch.object(audit_mod, "_fsync_existing_state", failed_sync):
        with pytest.raises(AuditMaintenanceError, match="synthetic state sync") as exc:
            repair_audit(candidate, now=DAY)
    assert exc.value.exit_code == 5
    assert calls == ["called"]
    assert shard.read_bytes() == before_log
    assert _state_bytes(candidate) == before_state
    assert json.loads(before_state)["reserved_through"] == DAY.date().isoformat()
    assert list(candidate.parent.glob("*.quarantine.*")) == []


def test_v2_repair_after_failed_adopt_completes_when_sync_works(tmp_path):
    candidate = _damaged_collection_after_failed_adopt_dir_sync(tmp_path)
    before_h = json.loads(_state_bytes(candidate))["reserved_through"]
    result = repair_audit(candidate, now=DAY)
    assert result["status"] == "repaired"
    assert json.loads(_state_bytes(candidate))["reserved_through"] == before_h
    assert list(candidate.parent.glob("*.quarantine.*"))
    assert verify_audit(candidate, now=DAY)["status"] == "clean"
    append_audit(candidate, _rec("after-fix"), now=DAY)
    assert verify_audit(candidate, now=DAY)["status"] == "clean"


def test_clean_repair_does_not_run_state_sync_barrier(tmp_path):
    import deadlatch.audit as audit_mod

    path = tmp_path / "clean-repair.jsonl"
    initialize_audit_state(path)
    append_audit(path, _rec("ok000001"), now=DAY)
    before = (_state_bytes(path), utc_shard_path(path, DAY).read_bytes())

    def boom(*_a, **_k):
        raise AuditError("barrier should not run", code=AUDIT_STATE_IO_ERROR)

    with patch.object(audit_mod, "_fsync_existing_state", boom):
        result = repair_audit(path, now=DAY)
    assert result["status"] == "clean"
    assert (_state_bytes(path), utc_shard_path(path, DAY).read_bytes()) == before
    assert list(tmp_path.glob("*.quarantine.*")) == []


def test_chain_refuse_repair_does_not_run_state_sync_barrier(tmp_path):
    import deadlatch.audit as audit_mod

    path = tmp_path / "chain-repair.jsonl"
    initialize_audit_state(path)
    append_audit(path, _rec("head00001"), now=DAY)
    append_audit(path, _rec("tail00002"), now=DAY)
    shard = utc_shard_path(path, DAY)
    lines = shard.read_text(encoding="utf-8").splitlines()
    last = json.loads(lines[-1])
    last["record_hash"] = "0" * 64
    lines[-1] = json.dumps(last, sort_keys=True, separators=(",", ":"))
    shard.write_text("\n".join(lines) + "\n", encoding="utf-8")
    before = (_state_bytes(path), shard.read_bytes())

    def boom(*_a, **_k):
        raise AuditError("barrier should not run", code=AUDIT_STATE_IO_ERROR)

    with patch.object(audit_mod, "_fsync_existing_state", boom):
        result = repair_audit(path, now=DAY)
    assert result["status"] == "invalid"
    assert (_state_bytes(path), shard.read_bytes()) == before
    assert list(tmp_path.glob("*.quarantine.*")) == []


def test_cli_repair_state_sync_failure_json_exit_5(tmp_path, capsys):
    import deadlatch.audit as audit_mod

    candidate = _damaged_collection_after_failed_adopt_dir_sync(tmp_path)
    before_log = utc_shard_path(candidate, DAY).read_bytes()
    before_state = _state_bytes(candidate)

    def failed_sync(*_args):
        raise AuditError("error: audit write state I/O failed", code=AUDIT_STATE_IO_ERROR)

    with patch.object(audit_mod, "_fsync_existing_state", failed_sync):
        rc = main([
            "audit", "repair", "--quarantine", "--audit-path", str(candidate), "--json",
        ])
    captured = capsys.readouterr()
    assert rc == 5
    payload = json.loads(captured.out)
    MAINTENANCE_RESULT_VALIDATOR.validate(payload)
    assert payload["status"] == "error"
    assert payload["operation"] == "repair"
    assert utc_shard_path(candidate, DAY).read_bytes() == before_log
    assert _state_bytes(candidate) == before_state
    assert list(candidate.parent.glob("*.quarantine.*")) == []
    assert "I/O failed" in captured.err


def test_prune_state_error_without_code(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "pcode.jsonl"
    initialize_audit_state(path)

    def boom(_p):
        raise AuditError("lock is a symbolic link")

    monkeypatch.setattr(audit_mod, "_assert_state_consistent_if_present", boom)
    with pytest.raises(AuditError, match="symbolic link"):
        prune_audit(path, now=DAY)


def test_v2_repair_refuses_without_state(tmp_path):
    path = tmp_path / "repair-miss.jsonl"
    initialize_audit_state(path)
    append_audit(path, _rec("ok000001"), now=DAY)
    shard = utc_shard_path(path, DAY)
    shard.write_bytes(shard.read_bytes() + b"{broken\n")
    _state_path(path).unlink()
    with pytest.raises(AuditMaintenanceError, match="missing"):
        repair_audit(path, now=DAY)


def test_verify_two_in_window_shards(tmp_path):
    path = tmp_path / "two.jsonl"
    initialize_audit_state(path)
    append_audit(path, _rec("d1aaaaaa"), now=DAY)
    append_audit(path, _rec("d2bbbbbb"), now=DAY + timedelta(days=1))
    result = verify_audit(path, now=DAY + timedelta(days=1))
    assert result["status"] == "clean"
    assert result["chained_records"] == 2


def test_raise_maintenance_from_adopt_required():
    with pytest.raises(AuditMaintenanceError) as exc:
        _raise_maintenance_from_state(
            AuditError("need adopt", code=AUDIT_ADOPT_REQUIRED, exit_code=4)
        )
    assert exc.value.exit_code == 4


def test_locked_maintenance_state_code(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "lmaint.jsonl"
    initialize_audit_state(path)

    def boom(*a, **k):
        raise AuditError("missing", code=AUDIT_STATE_MISSING, exit_code=5)

    monkeypatch.setattr(audit_mod, "_locked", boom)
    with pytest.raises(AuditMaintenanceError, match="missing"):
        verify_audit(path, now=DAY)


def test_locked_maintenance_oserror(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "lmaintio.jsonl"
    initialize_audit_state(path)

    def boom(*a, **k):
        raise OSError("flock denied")

    monkeypatch.setattr(audit_mod, "_locked", boom)
    with pytest.raises(AuditMaintenanceError, match="lock failed"):
        verify_audit(path, now=DAY)


def test_check_audit_size_oserror_is_ignored(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "sz.jsonl"
    path.write_text("x", encoding="utf-8")

    def boom(_p):
        raise OSError("lstat denied")

    monkeypatch.setattr(audit_mod.os, "lstat", boom)
    audit_mod._check_audit_size(path)


def test_publish_tmp_cleanup_on_replace_failure(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "replfail.jsonl"
    initialize_audit_state(path)
    real_replace = audit_mod.os.replace

    def boom(src, dst):
        if str(dst).endswith(".state.json"):
            raise OSError("replace denied")
        return real_replace(src, dst)

    monkeypatch.setattr(audit_mod.os, "replace", boom)
    with pytest.raises(AuditError) as exc:
        append_audit(path, _rec("repl00001"), now=DAY)
    assert exc.value.code == AUDIT_STATE_IO_ERROR
    leftovers = list(tmp_path.glob(".*.state.tmp"))
    assert leftovers == []
