"""G3B hash chain golden vectors, tamper PoCs, and repair refusal."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from deadlatch.audit import (
    AuditError,
    append_audit,
    compute_record_hash,
    read_audit_records,
    repair_audit,
    utc_shard_path,
    verify_audit,
)
from deadlatch.cli import main
from tests.conftest import NOW, fresh_order, fresh_portfolio, full_policy, make_standard
from deadlatch import Guard

REPO = Path(__file__).resolve().parents[1]
AUDIT_SCHEMA = json.loads((REPO / "schemas" / "audit-record.schema.json").read_text(encoding="utf-8"))
DAY_A = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
DAY_B = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)


def _rec(**overrides) -> dict:
    rec = {
        "schema_version": 1,
        "record_id": uuid.uuid4().hex,
        "evaluated_at": "2026-09-14T12:00:00Z",
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


def test_canonical_hash_golden_unicode_and_key_order():
    rec = {
        "schema_version": 2,
        "record_id": "rec00001",
        "evaluated_at": "2026-08-29T10:00:00Z",
        "input_hash": "0" * 64,
        "decision": "PASS",
        "shadow_mode": False,
        "shadow_verdict": None,
        "exit_code": 0,
        "policy_version": "1.0.0",
        "rule_hits": [{"rule_id": "kill_switch", "severity": "BLOCK", "detail": "中文Ω"}],
        "prev_hash": None,
    }
    payload = json.dumps(
        {k: v for k, v in rec.items() if k != "record_hash"},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert compute_record_hash(rec) == expected
    rec["record_hash"] = expected
    shuffled = {k: rec[k] for k in reversed(list(rec))}
    assert compute_record_hash(shuffled) == expected
    spaced = json.dumps({k: v for k, v in rec.items() if k != "record_hash"}, ensure_ascii=False)
    assert hashlib.sha256(spaced.encode("utf-8")).hexdigest() != expected
    Draft202012Validator(AUDIT_SCHEMA).validate(rec)


def test_self_reported_hashes_are_overwritten(tmp_path):
    path = tmp_path / "audit.jsonl"
    rec = _rec(record_id="selfhash1")
    rec["schema_version"] = 2
    rec["prev_hash"] = "a" * 64
    rec["record_hash"] = "b" * 64
    append_audit(path, rec, now=DAY_A)
    written = read_audit_records(path, now=DAY_A)[0]
    assert written["prev_hash"] is None
    assert written["record_hash"] == compute_record_hash(written)
    assert written["record_hash"] != "b" * 64


def test_cross_day_and_legacy_to_v2_chain(tmp_path):
    path = tmp_path / "audit.jsonl"
    legacy = _rec(record_id="legacyrec", evaluated_at="2026-09-01T00:00:00Z")
    path.write_text(json.dumps(legacy, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    append_audit(path, _rec(record_id="day-a-rec"), now=DAY_A)
    append_audit(path, _rec(record_id="day-b-rec"), now=DAY_B)
    recs = read_audit_records(path, now=DAY_B)
    assert recs[0]["schema_version"] == 1
    assert recs[1]["prev_hash"] == compute_record_hash(recs[0])
    assert recs[2]["prev_hash"] == recs[1]["record_hash"]
    result = verify_audit(path)
    assert result["status"] == "clean"
    assert result["legacy_records"] == 1
    assert result["chained_records"] == 2
    assert result["chain_start"] == recs[1]["record_hash"]
    assert result["chain_head"] == recs[2]["record_hash"]


def _verify_cli(path: Path, capsys):
    rc = main(["audit", "verify", "--audit-path", str(path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    return rc, payload


def test_poc_mutate_last_record(tmp_path, capsys):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec(record_id="firstrec1"), now=DAY_A)
    append_audit(path, _rec(record_id="lastrec01"), now=DAY_A)
    shard = utc_shard_path(path, DAY_A)
    lines = shard.read_text(encoding="utf-8").splitlines()
    last = json.loads(lines[-1])
    last["decision"] = "BLOCK"
    last["exit_code"] = 3
    lines[-1] = json.dumps(last, sort_keys=True, separators=(",", ":"))
    shard.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rc, payload = _verify_cli(path, capsys)
    assert rc == 3
    assert payload["status"] == "invalid"
    codes = [i["reason_code"] for i in payload["issues"]]
    assert "record_hash_mismatch" in codes
    assert payload["issues"][0]["segment_name"] == shard.name
    assert payload["issues"][0]["line_number"] == 2
    before = list(tmp_path.iterdir())
    repaired = repair_audit(path)
    assert repaired["status"] == "invalid"
    assert list(tmp_path.iterdir()) == before or not list(tmp_path.glob("*.quarantine.*"))
    assert not list(tmp_path.glob("*.quarantine.*"))


def test_poc_delete_middle_record(tmp_path, capsys):
    path = tmp_path / "audit.jsonl"
    for rid in ("aaaaaaa1", "bbbbbbb2", "ccccccc3"):
        append_audit(path, _rec(record_id=rid), now=DAY_A)
    shard = utc_shard_path(path, DAY_A)
    lines = shard.read_text(encoding="utf-8").splitlines()
    del lines[1]
    shard.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rc, payload = _verify_cli(path, capsys)
    assert rc == 3
    assert any(i["reason_code"] == "prev_hash_mismatch" for i in payload["issues"])
    assert payload["issues"][0]["segment_name"] == shard.name
    repaired = repair_audit(path)
    assert repaired["status"] == "invalid"
    assert not list(tmp_path.glob("*.quarantine.*"))


def test_poc_swap_cross_shard_line(tmp_path, capsys):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec(record_id="sharda01"), now=DAY_A)
    append_audit(path, _rec(record_id="shardb01"), now=DAY_B)
    a = utc_shard_path(path, DAY_A)
    b = utc_shard_path(path, DAY_B)
    a_line = a.read_text(encoding="utf-8")
    b_line = b.read_text(encoding="utf-8")
    a.write_text(b_line, encoding="utf-8")
    b.write_text(a_line, encoding="utf-8")
    rc, payload = _verify_cli(path, capsys)
    assert rc == 3
    codes = {i["reason_code"] for i in payload["issues"]}
    assert "prev_hash_mismatch" in codes or "record_hash_mismatch" in codes
    assert {i["segment_name"] for i in payload["issues"]} <= {a.name, b.name}
    repaired = repair_audit(path)
    assert repaired["status"] == "invalid"
    assert not list(tmp_path.glob("*.quarantine.*"))


def test_poc_duplicate_record_id(tmp_path, capsys):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec(record_id="dup00001"), now=DAY_A)
    append_audit(path, _rec(record_id="dup00001"), now=DAY_A)
    rc, payload = _verify_cli(path, capsys)
    assert rc == 3
    assert any(i["reason_code"] == "duplicate_record_id" for i in payload["issues"])
    assert payload["issues"][0]["segment_name"] == utc_shard_path(path, DAY_A).name
    repaired = repair_audit(path)
    assert repaired["status"] == "invalid"
    assert not list(tmp_path.glob("*.quarantine.*"))


def test_truncated_tail_repair_then_guard_append(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec(record_id="keep0001"), now=DAY_A)
    shard = utc_shard_path(path, DAY_A)
    shard.write_bytes(shard.read_bytes() + b"{not-json")
    result = repair_audit(path, now=DAY_A)
    assert result["status"] == "repaired"
    assert verify_audit(path)["status"] == "clean"
    guard = Guard(make_standard(full_policy()), audit_path=str(path))
    checked = guard.check(fresh_order(), fresh_portfolio(), now=NOW)
    assert checked.exit_code == 0
    recs = read_audit_records(path, now=NOW)
    assert recs[-1]["decision"] == "PASS"
    assert recs[-1]["prev_hash"] == recs[-2]["record_hash"]


def test_cli_repair_truncated_tail_text_and_issues(tmp_path, capsys):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec(record_id="keeptext1"), now=DAY_A)
    shard = utc_shard_path(path, DAY_A)
    shard.write_bytes(shard.read_bytes() + b"{partial")
    rc = main(["audit", "repair", "--quarantine", "--audit-path", str(path)])
    captured = capsys.readouterr()
    assert rc == 2
    assert "repair repaired" in captured.out
    assert "quarantine_name=" in captured.out
    assert "invalid_json" in captured.out or "invalid_utf8" in captured.out


def test_illegal_hash_and_version_spoof_fail_closed(tmp_path, capsys):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec(record_id="okrecord"), now=DAY_A)
    shard = utc_shard_path(path, DAY_A)
    rec = json.loads(shard.read_text(encoding="utf-8"))
    rec["record_hash"] = "not-a-hash"
    shard.write_text(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    rc, payload = _verify_cli(path, capsys)
    assert rc == 3
    assert payload["issues"][0]["reason_code"] in {"schema_invalid", "record_hash_mismatch"}
    rec["record_hash"] = "c" * 64
    rec["schema_version"] = 1
    rec.pop("prev_hash", None)
    shard.write_text(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    rc, payload = _verify_cli(path, capsys)
    assert rc == 3
    assert payload["issues"][0]["reason_code"] == "schema_invalid"


def test_unpruned_today_minus_31_plus_today_verify_clean(tmp_path):
    path = tmp_path / "audit.jsonl"
    expired = DAY_A - timedelta(days=31)
    append_audit(path, _rec(record_id="expired1"), now=expired)
    append_audit(path, _rec(record_id="todayrec"), now=DAY_A)
    recs = read_audit_records(path, now=DAY_A)
    assert recs[-1]["record_id"] == "todayrec"
    assert recs[-1]["prev_hash"] is None
    result = verify_audit(path, now=DAY_A)
    assert result["status"] == "clean"
    assert utc_shard_path(path, expired).exists()
    assert result["chained_records"] == 1


def test_append_single_lf_or_crlf_terminator_stays_contiguous(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec(record_id="headrec1"), now=DAY_A)
    shard = utc_shard_path(path, DAY_A)
    head = json.loads(shard.read_text(encoding="utf-8").splitlines()[0])
    payload = shard.read_bytes().rstrip(b"\r\n")
    for terminator in (b"\n", b"\r\n"):
        shard.write_bytes(payload + terminator)
        append_audit(path, _rec(record_id="contrec1"), now=DAY_A)
        recs = read_audit_records(path, now=DAY_A)
        assert recs[-1]["prev_hash"] == head["record_hash"]
        shard.write_bytes(payload + b"\n")


def test_append_extra_lf_crlf_consecutive_blanks_refused(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec(record_id="headrec1"), now=DAY_A)
    shard = utc_shard_path(path, DAY_A)
    payload = shard.read_bytes().rstrip(b"\r\n")
    for suffix in (b"\n\n", b"\r\n\r\n", b"\n\n\n"):
        shard.write_bytes(payload + suffix)
        before = shard.read_bytes()
        with pytest.raises(AuditError, match="空行"):
            append_audit(path, _rec(record_id="contrec1"), now=DAY_A)
        assert shard.read_bytes() == before


def test_trailing_blanks_verify_clean_is_not_appendable(tmp_path):
    """Extra trailing blanks: verify/repair clean; append fail-closed; bytes unchanged.

    ``verify clean`` here is hash-chain integrity of complete records (blank lines
    skipped), not a claim that the file is a legal appendable log. The write path
    is the fail-closed signal.
    """
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec(record_id="headrec1"), now=DAY_A)
    shard = utc_shard_path(path, DAY_A)
    payload = shard.read_bytes().rstrip(b"\r\n")
    shard.write_bytes(payload + b"\n\n")
    before = shard.read_bytes()
    verified = verify_audit(path, now=DAY_A)
    assert verified["status"] == "clean"
    assert verified["valid_lines"] == 1
    assert verified["invalid_lines"] == 0
    repaired = repair_audit(path, now=DAY_A)
    assert repaired["status"] == "clean"
    assert shard.read_bytes() == before
    assert list(tmp_path.glob("*.quarantine.*")) == []
    with pytest.raises(AuditError, match="空行"):
        append_audit(path, _rec(record_id="afterblk1"), now=DAY_A)
    assert shard.read_bytes() == before


def _v1_from_v2_without_hashes(rec: dict, **overrides) -> dict:
    """Codex P1-1: valid v1 payload stripped of chain fields (no rehash)."""
    body = {k: v for k, v in rec.items() if k not in ("prev_hash", "record_hash")}
    body["schema_version"] = 1
    body.update(overrides)
    return body


def _write_jsonl(path: Path, rec: dict) -> None:
    path.write_text(
        json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def test_v2_shard_downgraded_to_v1_refused_no_silent_write(tmp_path, capsys):
    """P1-1: rewriting a v2 shard as schema-valid v1 must not look clean."""
    path = tmp_path / "audit.jsonl"
    append_audit(
        path,
        _rec(record_id="blockrec1", decision="BLOCK", exit_code=3),
        now=DAY_A,
    )
    shard = utc_shard_path(path, DAY_A)
    original = json.loads(shard.read_text(encoding="utf-8"))
    downgraded = _v1_from_v2_without_hashes(
        original, decision="PASS", exit_code=0,
    )
    _write_jsonl(shard, downgraded)
    before = shard.read_bytes()

    rc, payload = _verify_cli(path, capsys)
    assert rc == 3
    assert payload["status"] == "invalid"
    assert payload["legacy_records"] == 0
    assert any(i["reason_code"] == "schema_version_downgrade" for i in payload["issues"])
    assert payload["issues"][0]["segment_name"] == shard.name

    repaired = repair_audit(path, now=DAY_A)
    assert repaired["status"] == "invalid"
    assert shard.read_bytes() == before
    assert list(tmp_path.glob("*.quarantine.*")) == []

    with pytest.raises(AuditError, match="降级"):
        append_audit(path, _rec(record_id="nextrec01"), now=DAY_A)
    assert shard.read_bytes() == before

    with pytest.raises(AuditError, match="schema_version_downgrade"):
        read_audit_records(path, now=DAY_A)
    from deadlatch.report import build_shadow_report
    with pytest.raises(AuditError, match="schema_version_downgrade"):
        build_shadow_report(path, timedelta(days=30), now=DAY_A)


def test_later_day_shard_v1_after_prior_v2_is_downgrade(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec(record_id="daya0001"), now=DAY_A)
    append_audit(path, _rec(record_id="daybtmp1"), now=DAY_B)
    later = utc_shard_path(path, DAY_B)
    _write_jsonl(later, _rec(record_id="dayb0001", evaluated_at="2026-09-15T12:00:00Z"))
    result = verify_audit(path, now=DAY_B)
    assert result["status"] == "invalid"
    assert any(i["reason_code"] == "schema_version_downgrade" for i in result["issues"])
    assert any(i["segment_name"] == later.name for i in result["issues"])
    before = later.read_bytes()
    with pytest.raises(AuditError, match="降级"):
        append_audit(path, _rec(record_id="dayb0002"), now=DAY_B)
    assert later.read_bytes() == before
    with pytest.raises(AuditError, match="schema_version_downgrade"):
        read_audit_records(path, now=DAY_B)


def test_v1_after_v2_in_legacy_file_is_downgrade(tmp_path):
    path = tmp_path / "audit.jsonl"
    v1 = _rec(record_id="legacyv11")
    v2 = _rec(record_id="legacyv21")
    v2["schema_version"] = 2
    v2["prev_hash"] = compute_record_hash(v1)
    v2["record_hash"] = compute_record_hash(v2)
    trail = _rec(record_id="legacyv12")
    path.write_text(
        json.dumps(v1, sort_keys=True, separators=(",", ":")) + "\n"
        + json.dumps(v2, sort_keys=True, separators=(",", ":")) + "\n"
        + json.dumps(trail, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    result = verify_audit(path, now=DAY_A)
    assert result["status"] == "invalid"
    assert any(i["reason_code"] == "schema_version_downgrade" for i in result["issues"])
    assert result["legacy_records"] == 1
    with pytest.raises(AuditError, match="schema_version_downgrade"):
        read_audit_records(path, now=DAY_A)


def test_incoming_business_v1_after_v2_upgrades_not_downgrade(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec(record_id="firstv201"), now=DAY_A)
    incoming = _rec(record_id="upgrade01")
    assert incoming["schema_version"] == 1
    append_audit(path, incoming, now=DAY_A)
    recs = read_audit_records(path, now=DAY_A)
    assert [r["schema_version"] for r in recs] == [2, 2]
    assert recs[1]["prev_hash"] == recs[0]["record_hash"]
    assert verify_audit(path, now=DAY_A)["status"] == "clean"


def test_read_duplicate_record_id_fail_closed(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_audit(path, _rec(record_id="dup00001"), now=DAY_A)
    append_audit(path, _rec(record_id="dup00001"), now=DAY_A)
    with pytest.raises(AuditError, match="duplicate_record_id"):
        read_audit_records(path, now=DAY_A)


def test_read_hash_mismatch_is_not_trusted_history(tmp_path, capsys):
    """P1-2: mutating decision while keeping record_hash must not be readable."""
    path = tmp_path / "audit.jsonl"
    append_audit(
        path,
        _rec(record_id="blockrec1", decision="BLOCK", exit_code=3),
        now=DAY_A,
    )
    shard = utc_shard_path(path, DAY_A)
    rec = json.loads(shard.read_text(encoding="utf-8"))
    rec["decision"] = "PASS"
    rec["exit_code"] = 0
    _write_jsonl(shard, rec)

    rc, payload = _verify_cli(path, capsys)
    assert rc == 3
    assert payload["status"] == "invalid"
    assert any(i["reason_code"] == "record_hash_mismatch" for i in payload["issues"])

    with pytest.raises(AuditError, match="record_hash_mismatch"):
        read_audit_records(path, now=DAY_A)

    from deadlatch.report import build_shadow_report

    with pytest.raises(AuditError, match="record_hash_mismatch"):
        build_shadow_report(path, timedelta(days=30), now=DAY_A)

    rc = main(["shadow", "report", "--since", "30d", "--audit-path", str(path)])
    captured = capsys.readouterr()
    assert rc == 5
    assert "record_hash_mismatch" in captured.err
    assert "PASS" not in captured.out
