"""audit verify / repair: exit codes, isolation, atomicity, and no leakage."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from deadlatch.audit import (
    AuditMaintenanceError,
    append_audit,
    is_audit_maintenance_record,
    read_audit_records,
    repair_audit,
    verify_audit,
)
from deadlatch.cli import main
from deadlatch.report import build_shadow_report
from tests.conftest import NOW, fresh_order, fresh_portfolio, full_policy, make_standard
from deadlatch import Guard

REPO = Path(__file__).resolve().parents[1]
RESULT_SCHEMA = json.loads(
    (REPO / "schemas" / "audit-maintenance-result.schema.json").read_text(encoding="utf-8")
)
RESULT_VALIDATOR = Draft202012Validator(RESULT_SCHEMA)
AUDIT_SCHEMA = json.loads(
    (REPO / "schemas" / "audit-record.schema.json").read_text(encoding="utf-8")
)
AUDIT_VALIDATOR = Draft202012Validator(AUDIT_SCHEMA)

_TOKEN_PROBE = "sk-" + "exampletokenvalue"
_ACCOUNT_PROBE = "acct-" + "hidden"


def _valid_record(**overrides) -> dict:
    rec = {
        "schema_version": 1,
        "record_id": uuid.uuid4().hex,
        "evaluated_at": "2026-08-29T10:00:00Z",
        "input_hash": "0" * 64,
        "decision": "PASS",
        "shadow_mode": False,
        "shadow_verdict": None,
        "exit_code": 0,
        "policy_version": "1.0.0",
        "rule_hits": [],
    }
    rec.update(overrides)
    return rec


def _line(rec: dict) -> str:
    return json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_cli(*args, cwd: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO / "src"),
        "DEADLATCH_AUDIT_PATH": str(cwd / "audit.jsonl"),
    }
    return subprocess.run(
        [sys.executable, "-m", "deadlatch.cli", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
        timeout=60,
    )


def _assert_no_leak(blob: str, raws: list[bytes], path: Path) -> None:
    lowered = blob.lower()
    assert _TOKEN_PROBE.lower() not in lowered
    assert _ACCOUNT_PROBE.lower() not in lowered
    assert str(path.resolve()) not in blob
    for raw in raws:
        text = raw.decode("utf-8", "replace")
        if text.strip():
            assert text.strip() not in blob
        assert base64.b64encode(raw).decode("ascii") not in blob


def _write_mixed(path: Path) -> tuple[bytes, bytes, bytes, bytes]:
    good1 = _line(_valid_record()).encode("utf-8")
    good2 = _line(_valid_record()).encode("utf-8")
    bad_json = b"{not-json " + _TOKEN_PROBE.encode("ascii") + b" " + _ACCOUNT_PROBE.encode("ascii") + b"}\n"
    bad_utf8 = b"\xff\xfe damaged\n"
    path.write_bytes(good1 + bad_json + good2 + bad_utf8)
    return good1, good2, bad_json, bad_utf8


# ---------------- verify ----------------


def test_verify_valid_and_empty_file(tmp_path, capsys):
    path = tmp_path / "audit.jsonl"
    path.write_text(_line(_valid_record()), encoding="utf-8")
    before = _sha(path)
    rc = main(["audit", "verify", "--audit-path", str(path), "--json"])
    out = capsys.readouterr()
    assert rc == 0
    payload = json.loads(out.out)
    RESULT_VALIDATOR.validate(payload)
    assert payload["status"] == "clean"
    assert payload["valid_lines"] == 1
    assert payload["invalid_lines"] == 0
    assert payload["issues"] == []
    assert payload["issues_truncated"] is False
    assert _sha(path) == before
    path.write_bytes(b"")
    rc = main(["audit", "verify", "--audit-path", str(path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    RESULT_VALIDATOR.validate(payload)
    assert rc == 0
    assert payload["valid_lines"] == 0
    assert payload["invalid_lines"] == 0


def test_verify_missing_directory_symlink(tmp_path, capsys):
    missing = tmp_path / "nope.jsonl"
    before_names = {p.name for p in tmp_path.iterdir()}
    rc = main(["audit", "verify", "--audit-path", str(missing), "--json"])
    captured = capsys.readouterr()
    assert rc == 4
    _assert_no_leak(captured.out + captured.err, [], missing)
    payload = json.loads(captured.out)
    assert payload["status"] == "error"
    assert {p.name for p in tmp_path.iterdir()} == before_names
    assert not missing.exists()
    assert not (tmp_path / "nope.jsonl.lock").exists()

    nested = tmp_path / "missing-parent" / "audit.jsonl"
    rc = main(["audit", "verify", "--audit-path", str(nested), "--json"])
    captured = capsys.readouterr()
    assert rc == 4
    json.loads(captured.out)
    assert not (tmp_path / "missing-parent").exists()

    d = tmp_path / "adir"
    d.mkdir()
    dir_before = {p.name for p in tmp_path.iterdir()}
    rc = main(["audit", "verify", "--audit-path", str(d), "--json"])
    captured = capsys.readouterr()
    assert rc == 4
    _assert_no_leak(captured.out + captured.err, [], d)
    assert {p.name for p in tmp_path.iterdir()} == dir_before

    target = tmp_path / "real.jsonl"
    target.write_text(_line(_valid_record()), encoding="utf-8")
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    rc = main(["audit", "verify", "--audit-path", str(link), "--json"])
    captured = capsys.readouterr()
    assert rc == 4
    _assert_no_leak(captured.out + captured.err, [], link)
    assert not (tmp_path / "link.jsonl.lock").exists()


def test_verify_four_damage_classes_and_readonly(tmp_path, capsys):
    path = tmp_path / "audit.jsonl"
    good = _line(_valid_record())
    schema_bad = _line(_valid_record(exit_code=99))
    time_bad = _line(_valid_record(evaluated_at="not-a-time"))
    # evaluated_at fails schema pattern first; force a schema-valid clock string
    # that parse_rfc3339 still rejects by using a timezone-less value after
    # relaxing via a JSON object that passes schema? pattern requires timezone.
    # Use schema-invalid for evaluated_at and a separate schema_invalid.
    path.write_bytes(
        good.encode("utf-8")
        + b"\xff\xfe\n"
        + b"{not json}\n"
        + schema_bad.encode("utf-8")
        + time_bad.encode("utf-8")
    )
    before = _sha(path)
    rc = main(["audit", "verify", "--audit-path", str(path), "--json"])
    captured = capsys.readouterr()
    assert rc == 3
    payload = json.loads(captured.out)
    RESULT_VALIDATOR.validate(payload)
    assert payload["status"] == "invalid"
    assert payload["valid_lines"] == 1
    assert payload["invalid_lines"] == 4
    codes = [i["reason_code"] for i in payload["issues"]]
    assert codes == [
        "invalid_utf8",
        "invalid_json",
        "schema_invalid",
        "schema_invalid",
    ]
    assert captured.out == json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    assert _sha(path) == before
    _assert_no_leak(captured.out + captured.err, [b"\xff\xfe\n", b"{not json}\n"], path)


def test_verify_evaluated_at_invalid_when_schema_passes(tmp_path, monkeypatch, capsys):
    from deadlatch import audit as audit_mod

    original = audit_mod._AUDIT_VALIDATOR

    class _Accept:
        def iter_errors(self, _rec):
            return []

    monkeypatch.setattr(audit_mod, "_AUDIT_VALIDATOR", _Accept())
    path = tmp_path / "audit.jsonl"
    path.write_text(_line(_valid_record(evaluated_at="not-a-time")), encoding="utf-8")
    before = _sha(path)
    rc = main(["audit", "verify", "--audit-path", str(path), "--json"])
    captured = capsys.readouterr()
    monkeypatch.setattr(audit_mod, "_AUDIT_VALIDATOR", original)
    assert rc == 3
    payload = json.loads(captured.out)
    assert payload["issues"][0]["reason_code"] == "evaluated_at_invalid"
    assert _sha(path) == before


def test_verify_truncates_issues_at_100(tmp_path, capsys):
    path = tmp_path / "audit.jsonl"
    path.write_bytes(b"{bad}\n" * 105)
    rc = main(["audit", "verify", "--audit-path", str(path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 3
    RESULT_VALIDATOR.validate(payload)
    assert payload["invalid_lines"] == 105
    assert len(payload["issues"]) == 100
    assert payload["issues_truncated"] is True
    assert payload["issues"][-1]["line_number"] == 100


# ---------------- repair ----------------


def test_repair_requires_quarantine_and_clean_noop(tmp_path, capsys):
    path = tmp_path / "audit.jsonl"
    path.write_text(_line(_valid_record()), encoding="utf-8")
    before = path.read_bytes()
    rc = main(["audit", "repair", "--audit-path", str(path)])
    captured = capsys.readouterr()
    assert rc == 4
    assert path.read_bytes() == before
    assert list(tmp_path.glob("*.quarantine.*")) == []
    _assert_no_leak(captured.out + captured.err, [], path)

    rc = main(["audit", "repair", "--quarantine", "--audit-path", str(path), "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)
    RESULT_VALIDATOR.validate(payload)
    assert payload["status"] == "clean"
    assert path.read_bytes() == before
    assert list(tmp_path.glob("*.quarantine.*")) == []


def test_repair_quarantines_all_bad_lines_and_keeps_order(tmp_path, capsys):
    path = tmp_path / "audit.jsonl"
    good1, good2, bad_json, bad_utf8 = _write_mixed(path)
    source = path.read_bytes()
    source_sha = hashlib.sha256(source).hexdigest()
    rc = main(["audit", "repair", "--quarantine", "--audit-path", str(path), "--json"])
    captured = capsys.readouterr()
    assert rc == 2
    payload = json.loads(captured.out)
    RESULT_VALIDATOR.validate(payload)
    assert payload["status"] == "repaired"
    assert payload["valid_lines"] == 2
    assert payload["invalid_lines"] == 2
    assert payload["source_sha256"] == source_sha
    assert payload["quarantine_name"] == Path(payload["quarantine_name"]).name
    qpath = tmp_path / payload["quarantine_name"]
    assert qpath.is_file()
    if os.name == "posix":
        assert stat.S_IMODE(qpath.stat().st_mode) == 0o600
    qdoc = json.loads(qpath.read_text(encoding="utf-8"))
    assert qdoc["source_sha256"] == source_sha
    recovered = [base64.b64decode(item["raw_base64"]) for item in qdoc["issues"]]
    assert recovered == [bad_json, bad_utf8]
    rebuilt = path.read_bytes()
    assert rebuilt.startswith(good1 + good2)
    last = rebuilt[len(good1 + good2):].decode("utf-8")
    marker = json.loads(last)
    assert AUDIT_VALIDATOR.is_valid(marker)
    assert is_audit_maintenance_record(marker)
    assert marker["input_hash"] == source_sha
    assert f"quarantine_sha256={payload['quarantine_sha256']}" in marker["rule_hits"][0]["detail"]
    assert "quarantined_lines=2" in marker["rule_hits"][0]["detail"]
    _assert_no_leak(captured.out + captured.err + last, [bad_json, bad_utf8], path)
    assert payload["quarantine_name"] not in last


def test_repair_second_pass_is_noop_and_unique_quarantine(tmp_path, capsys):
    path = tmp_path / "audit.jsonl"
    _write_mixed(path)
    rc1 = main(["audit", "repair", "--quarantine", "--audit-path", str(path), "--json"])
    first = json.loads(capsys.readouterr().out)
    assert rc1 == 2
    after = path.read_bytes()
    rc2 = main(["audit", "repair", "--quarantine", "--audit-path", str(path), "--json"])
    second = json.loads(capsys.readouterr().out)
    assert rc2 == 0
    assert second["status"] == "clean"
    assert path.read_bytes() == after
    names = {p.name for p in tmp_path.glob("*.quarantine.*.json")}
    assert first["quarantine_name"] in names
    assert len(names) == 1


def test_repair_replace_failure_keeps_original_and_quarantine(tmp_path, monkeypatch, capsys):
    path = tmp_path / "audit.jsonl"
    original = path
    _write_mixed(path)
    before = path.read_bytes()

    def boom(src, dst):
        if Path(dst) == path:
            raise OSError("replace-denied")
        return os.replace(src, dst)

    monkeypatch.setattr("deadlatch.audit.os.replace", boom)
    with pytest.raises(AuditMaintenanceError) as exc:
        repair_audit(path)
    assert exc.value.exit_code == 5
    assert path.read_bytes() == before
    assert list(tmp_path.glob("*.quarantine.*.json"))


def test_repair_quarantine_write_failure(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    _write_mixed(path)
    before = path.read_bytes()

    def deny_open(*args, **kwargs):
        raise OSError("open-denied")

    monkeypatch.setattr("deadlatch.audit.os.open", deny_open)
    with pytest.raises(AuditMaintenanceError) as exc:
        repair_audit(path)
    assert exc.value.exit_code == 5
    assert path.read_bytes() == before
    assert list(tmp_path.glob("*.quarantine.*.json")) == []


def test_repair_then_guard_appends_again(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write_mixed(path)
    result = repair_audit(path)
    assert result["status"] == "repaired"
    guard = Guard(make_standard(full_policy()), audit_path=str(path))
    checked = guard.check(fresh_order(), fresh_portfolio(), now=NOW)
    assert checked.exit_code == 0
    records = read_audit_records(path)
    orders = [r for r in records if not is_audit_maintenance_record(r) and r.get("decision") == "PASS"]
    assert orders
    last = orders[-1]
    assert last["decision"] == "PASS"
    assert not is_audit_maintenance_record(last)


def test_shadow_report_excludes_strict_marker_not_spoof(tmp_path):
    path = tmp_path / "audit.jsonl"
    order_rec = _valid_record(decision="BLOCK", exit_code=3, rule_hits=[
        {"rule_id": "max_order_quantity", "severity": "BLOCK", "detail": "qty"}
    ])
    marker = _valid_record(
        decision="WARN",
        exit_code=2,
        shadow_mode=False,
        shadow_verdict=None,
        policy_version="audit-maintenance/1",
        input_hash="a" * 64,
        rule_hits=[{"rule_id": "audit_repaired", "severity": "WARN", "detail": "quarantined_lines=1; quarantine_sha256=" + "b" * 64}],
    )
    spoof = _valid_record(
        decision="WARN",
        exit_code=2,
        policy_version="1.0.0",
        rule_hits=[{"rule_id": "audit_repaired", "severity": "WARN", "detail": "hide-me"}],
    )
    path.write_text(_line(order_rec) + _line(marker) + _line(spoof), encoding="utf-8")
    from datetime import timedelta

    report = build_shadow_report(path, timedelta(days=30), now=datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc))
    assert report["totals"]["orders_evaluated"] == 2
    assert report["totals"]["would_block"] == 1
    assert report["totals"]["would_warn"] == 1
    rule_ids = {row["rule_id"] for row in report["by_rule"]}
    assert "max_order_quantity" in rule_ids
    assert "audit_repaired" in rule_ids
    assert any("maintenance events excluded" in n for n in report["notes"])


def test_repair_four_process_no_lost_lines(tmp_path):
    path = tmp_path / "audit.jsonl"
    goods = [_line(_valid_record(record_id=f"{i:08d}xxxx")) for i in range(8)]
    path.write_text("".join(goods), encoding="utf-8")
    script = r"""
import sys
from pathlib import Path
from deadlatch.audit import verify_audit, repair_audit, append_audit
path = Path(sys.argv[1])
mode = sys.argv[2]
if mode == "verify":
    verify_audit(path)
elif mode == "repair":
    repair_audit(path)
else:
    rec = {
        "schema_version": 1, "record_id": sys.argv[3],
        "evaluated_at": "2026-08-29T10:00:00Z", "input_hash": "c" * 64,
        "decision": "PASS", "shadow_mode": False, "shadow_verdict": None,
        "exit_code": 0, "policy_version": "1.0.0", "rule_hits": [],
    }
    append_audit(path, rec)
"""
    procs = []
    rec_ids = [uuid.uuid4().hex for _ in range(2)]
    args_list = [
        ["verify"],
        ["repair"],
        ["append", rec_ids[0]],
        ["append", rec_ids[1]],
    ]
    for extra in args_list:
        procs.append(subprocess.Popen(
            [sys.executable, "-c", script, str(path), *extra],
            cwd=str(REPO),
            env={**os.environ, "PYTHONPATH": str(REPO / "src")},
        ))
    codes = [p.wait(timeout=60) for p in procs]
    assert all(c == 0 for c in codes)
    records = read_audit_records(path)
    kept_ids = {r["record_id"] for r in records if not is_audit_maintenance_record(r)}
    assert {f"{i:08d}xxxx" for i in range(8)} <= kept_ids
    assert set(rec_ids) <= kept_ids
    for rec in records:
        assert AUDIT_VALIDATOR.is_valid(rec)


def test_cli_help_and_text_output_hides_raw(tmp_path):
    r = _run_cli("audit", "--help", cwd=tmp_path)
    assert r.returncode == 0
    assert "verify" in r.stdout and "repair" in r.stdout and "prune" in r.stdout
    path = tmp_path / "audit.jsonl"
    _write_mixed(path)
    r = _run_cli("audit", "verify", "--audit-path", str(path), cwd=tmp_path)
    assert r.returncode == 3
    _assert_no_leak(r.stdout + r.stderr, [], path)
    assert "invalid_json" in r.stdout or "invalid_utf8" in r.stdout


def _assert_repair_roundtrip(path: Path, expected_good: bytes):
    result = repair_audit(path)
    assert result["status"] == "repaired"
    after = verify_audit(path)
    assert after["status"] == "clean"
    records = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    goods = [r for r in records if not is_audit_maintenance_record(r)]
    markers = [r for r in records if is_audit_maintenance_record(r)]
    assert len(goods) == 1
    assert len(markers) == 1
    rebuilt = path.read_bytes()
    assert expected_good in rebuilt
    assert expected_good + b"{" not in rebuilt
    qpath = path.with_name(result["quarantine_name"])
    assert hashlib.sha256(qpath.read_bytes()).hexdigest() == result["quarantine_sha256"]
    guard = Guard(make_standard(full_policy()), audit_path=str(path))
    checked = guard.check(fresh_order(), fresh_portfolio(), now=NOW)
    assert checked.exit_code == 0


def test_repair_valid_final_record_without_newline(tmp_path):
    path = tmp_path / "audit.jsonl"
    good = _line(_valid_record()).rstrip("\n").encode("utf-8")
    path.write_bytes(b"{not-json}\n" + good)
    _assert_repair_roundtrip(path, good)


def test_repair_valid_then_bad_final_without_newline(tmp_path):
    path = tmp_path / "audit.jsonl"
    good = _line(_valid_record()).encode("utf-8")
    path.write_bytes(good + b"{not-json}")
    _assert_repair_roundtrip(path, good.rstrip(b"\n"))


def test_repair_crlf_and_trailing_blank_line(tmp_path):
    path = tmp_path / "audit.jsonl"
    rec = _line(_valid_record()).rstrip("\n")
    crlf_good = rec.encode("utf-8") + b"\r\n"
    path.write_bytes(b"{not-json}\r\n" + rec.encode("utf-8"))
    result = repair_audit(path)
    assert result["status"] == "repaired"
    assert verify_audit(path)["status"] == "clean"
    body = path.read_bytes()
    assert rec.encode("utf-8") in body
    assert not body.startswith(rec.encode("utf-8") + b"{")

    path.write_bytes(b"{not-json}\n" + crlf_good + b"\n")
    result = repair_audit(path)
    assert result["status"] == "repaired"
    assert verify_audit(path)["status"] == "clean"
    assert crlf_good in path.read_bytes() or rec.encode("utf-8") in path.read_bytes()


def test_verify_existing_file_may_create_lock_only(tmp_path, capsys):
    path = tmp_path / "audit.jsonl"
    path.write_text(_line(_valid_record()), encoding="utf-8")
    before = _sha(path)
    before_names = {p.name for p in tmp_path.iterdir()}
    rc = main(["audit", "verify", "--audit-path", str(path), "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    RESULT_VALIDATOR.validate(json.loads(captured.out))
    assert _sha(path) == before
    created = {p.name for p in tmp_path.iterdir()} - before_names
    assert created <= {path.name + ".lock"}
    assert list(tmp_path.glob("*.quarantine.*")) == []


def test_repair_quarantine_write_interrupt_leaves_no_final(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    _write_mixed(path)
    before = path.read_bytes()
    real_fdopen = os.fdopen

    def wrapped(fd, mode="r", *args, **kwargs):
        handle = real_fdopen(fd, mode, *args, **kwargs)
        if "w" in str(mode):
            def boom(_data):
                raise OSError("interrupted")
            handle.write = boom  # type: ignore[method-assign]
        return handle

    monkeypatch.setattr("deadlatch.audit.os.fdopen", wrapped)
    with pytest.raises(AuditMaintenanceError) as exc:
        repair_audit(path)
    assert exc.value.exit_code == 5
    assert path.read_bytes() == before
    assert list(tmp_path.glob("*.quarantine.*.json")) == []


def test_repair_quarantine_publish_conflict_does_not_overwrite(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    _write_mixed(path)
    now = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)
    existing = tmp_path / "audit.jsonl.quarantine.20260829T100000Z.aaaaaaaa.json"
    existing.write_text("KEEP-ME", encoding="utf-8")
    names4 = iter(["aaaaaaaa", "bbbbbbbb"])
    names8 = iter(["1111111111111111", "2222222222222222", "3333333333333333"])

    def fake_hex(n):
        if n == 4:
            return next(names4)
        return next(names8)

    monkeypatch.setattr("deadlatch.audit.secrets.token_hex", fake_hex)
    result = repair_audit(path, now=now)
    assert result["status"] == "repaired"
    assert existing.read_text(encoding="utf-8") == "KEEP-ME"
    assert result["quarantine_name"] != existing.name
    qpath = tmp_path / result["quarantine_name"]
    assert qpath.is_file()
    assert hashlib.sha256(qpath.read_bytes()).hexdigest() == result["quarantine_sha256"]
    assert verify_audit(path)["status"] == "clean"


def test_oversize_verify_and_repair_json_contract(tmp_path, monkeypatch, capsys):
    path = tmp_path / "audit.jsonl"
    path.write_bytes(b"{}\n" * 8)
    monkeypatch.setattr("deadlatch.audit.MAX_AUDIT_FILE_BYTES", 8)
    rc = main(["audit", "verify", "--audit-path", str(path), "--json"])
    captured = capsys.readouterr()
    assert rc == 5
    payload = json.loads(captured.out)
    RESULT_VALIDATOR.validate(payload)
    assert payload["status"] == "error"
    assert payload["operation"] == "verify"
    _assert_no_leak(captured.out + captured.err, [], path)
    assert "internal error: audit maintenance failed" not in captured.err

    rc = main(["audit", "repair", "--quarantine", "--audit-path", str(path), "--json"])
    captured = capsys.readouterr()
    assert rc == 5
    payload = json.loads(captured.out)
    RESULT_VALIDATOR.validate(payload)
    assert payload["status"] == "error"
    assert payload["operation"] == "repair"
    _assert_no_leak(captured.out + captured.err, [], path)
