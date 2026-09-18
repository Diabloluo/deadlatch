"""v0.1.2 audit platform support matrix: Linux/macOS writes, others refuse."""

from __future__ import annotations

import errno
import json
import os
import stat
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from deadlatch import Guard
from deadlatch.audit import (
    AUDIT_PLATFORM_UNSUPPORTED,
    AUDIT_PLATFORM_UNSUPPORTED_MESSAGE,
    AUDIT_STATE_IO_ERROR,
    AuditError,
    append_audit,
    audit_writes_supported,
    compute_record_hash,
    initialize_audit_state,
    prune_audit,
    read_audit_records,
    repair_audit,
    utc_shard_path,
    verify_audit,
)
from deadlatch.cli import main
from deadlatch.mcp_server import MCPGuardServer
from deadlatch.report import build_shadow_report, parse_since
from deadlatch.rules.stubs import BoomRule, WarnRule
from tests.conftest import (
    NOW,
    fresh_order,
    fresh_portfolio,
    full_policy,
    make_engine,
)
from tests.test_mcp_server import _order, _write_policy, _write_portfolio

REPO = Path(__file__).resolve().parents[1]
DAY = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
STATE_SCHEMA = json.loads((REPO / "schemas" / "audit-state-result.schema.json").read_text(encoding="utf-8"))
MAINT_SCHEMA = json.loads(
    (REPO / "schemas" / "audit-maintenance-result.schema.json").read_text(encoding="utf-8")
)
PRUNE_SCHEMA = json.loads((REPO / "schemas" / "audit-prune-result.schema.json").read_text(encoding="utf-8"))
RESULT_SCHEMA = json.loads((REPO / "schemas" / "result.schema.json").read_text(encoding="utf-8"))


def _set_platform(monkeypatch, name: str) -> None:
    import deadlatch.audit as audit_mod

    monkeypatch.setattr(audit_mod, "audit_write_platform", lambda: name)


def _snapshot(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    if not root.exists():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        dirnames.sort()
        for name in sorted(dirnames):
            key = name if rel_dir == "." else str(Path(rel_dir) / name)
            out[key.replace("\\", "/") + "/"] = b""
        for name in sorted(filenames):
            p = Path(dirpath) / name
            key = name if rel_dir == "." else str(Path(rel_dir) / name)
            out[key.replace("\\", "/")] = p.read_bytes()
    return out


def _state_path(base: Path) -> Path:
    return base.with_name(base.name + ".state.json")


def _write_state(base: Path, reserved: str | None = "2026-09-16") -> None:
    payload = {
        "protocol_version": 1,
        "base_name": base.name,
        "reserved_through": reserved,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    _state_path(base).write_text(blob, encoding="utf-8")


def _v2_record(**overrides) -> dict:
    rec = {
        "schema_version": 2,
        "record_id": "rec00001",
        "evaluated_at": "2026-09-16T12:00:00Z",
        "input_hash": "0" * 64,
        "decision": "PASS",
        "shadow_mode": False,
        "shadow_verdict": None,
        "exit_code": 0,
        "policy_version": "synthetic",
        "rule_hits": [],
        "prev_hash": None,
    }
    rec.update(overrides)
    rec.pop("record_hash", None)
    rec["record_hash"] = compute_record_hash(rec)
    return rec


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in records),
        encoding="utf-8",
    )


def _business_rec(rid: str = "newrec01") -> dict:
    return {
        "schema_version": 1,
        "record_id": rid,
        "evaluated_at": "2026-09-16T12:00:00Z",
        "input_hash": "0" * 64,
        "decision": "PASS",
        "shadow_mode": False,
        "shadow_verdict": None,
        "exit_code": 0,
        "policy_version": "synthetic",
        "rule_hits": [],
    }


def _expect_platform_error(exc: AuditError) -> None:
    assert exc.code == AUDIT_PLATFORM_UNSUPPORTED
    assert exc.exit_code == 5
    assert str(exc) == AUDIT_PLATFORM_UNSUPPORTED_MESSAGE
    assert "/Users/" not in str(exc)
    assert "sk-" not in str(exc)


def _unsupported_write_raises(fn, *args, **kwargs) -> None:
    with pytest.raises(AuditError) as exc:
        fn(*args, **kwargs)
    _expect_platform_error(exc.value)


# ---------------------------------------------------------------- helper

@pytest.mark.parametrize(
    "name,supported",
    [
        ("linux", True),
        ("linux2", True),
        ("darwin", True),
        ("win32", False),
        ("cygwin", False),
        ("msys", False),
        ("freebsd13", False),
        ("aix", False),
        ("plan9", False),
        ("", False),
    ],
)
def test_platform_helper_matrix(name, supported):
    assert audit_writes_supported(platform=name) is supported


def test_helper_does_not_infer_platform_from_permission_error(monkeypatch):
    import deadlatch.audit as audit_mod

    pe = PermissionError(errno.EACCES, "Permission denied")
    assert audit_writes_supported(platform="linux") is True
    assert audit_writes_supported(platform="win32") is False
    monkeypatch.setattr(audit_mod, "audit_write_platform", lambda: "darwin")
    assert audit_mod.audit_writes_supported() is True
    monkeypatch.setattr(audit_mod, "audit_write_platform", lambda: "win32")
    assert audit_mod.audit_writes_supported() is False
    assert isinstance(pe, PermissionError)


# ---------------------------------------------------------------- write entries: refuse + zero side effects

def test_init_append_repair_prune_report_refuse_missing_parent(tmp_path, monkeypatch):
    _set_platform(monkeypatch, "win32")
    missing = tmp_path / "no-such-parent" / "audit.jsonl"
    before = _snapshot(tmp_path)
    _unsupported_write_raises(initialize_audit_state, missing)
    _unsupported_write_raises(append_audit, missing, _business_rec(), now=DAY)
    _unsupported_write_raises(repair_audit, missing)
    _unsupported_write_raises(prune_audit, missing, now=DAY)
    _unsupported_write_raises(build_shadow_report, missing, parse_since("30d"), now=DAY)
    assert _snapshot(tmp_path) == before
    assert not (tmp_path / "no-such-parent").exists()


def test_write_entries_refuse_existing_valid_state(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    _write_state(path, reserved="2026-09-16")
    shard = utc_shard_path(path, date(2026, 9, 16))
    _write_jsonl(shard, [_v2_record()])
    before = _snapshot(tmp_path)
    _set_platform(monkeypatch, "win32")
    _unsupported_write_raises(initialize_audit_state, path)
    _unsupported_write_raises(initialize_audit_state, path, adopt_existing=True)
    _unsupported_write_raises(append_audit, path, _business_rec(), now=DAY)
    _unsupported_write_raises(repair_audit, path)
    _unsupported_write_raises(prune_audit, path, now=DAY)
    _unsupported_write_raises(build_shadow_report, path, parse_since("30d"), now=DAY)
    assert _snapshot(tmp_path) == before


def test_write_entries_refuse_noop_and_legacy_and_future(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    _write_state(path, reserved=None)
    legacy = {
        "schema_version": 1,
        "record_id": "legacy01",
        "evaluated_at": "2026-09-16T12:00:00Z",
        "input_hash": "0" * 64,
        "decision": "PASS",
        "shadow_mode": False,
        "shadow_verdict": None,
        "exit_code": 0,
        "policy_version": "synthetic",
        "rule_hits": [],
    }
    _write_jsonl(path, [legacy])
    future = utc_shard_path(path, date(2099, 1, 1))
    rec = _v2_record(record_id="future01", evaluated_at="2099-01-01T00:00:00Z")
    _write_jsonl(future, [rec])
    before = _snapshot(tmp_path)
    _set_platform(monkeypatch, "win32")
    _unsupported_write_raises(initialize_audit_state, path)
    _unsupported_write_raises(repair_audit, path)
    _unsupported_write_raises(prune_audit, path, now=DAY)
    assert _snapshot(tmp_path) == before


def test_write_entries_refuse_damaged_state_and_shards(tmp_path, monkeypatch):
    path = tmp_path / "broken.jsonl"
    _state_path(path).write_text("{not json", encoding="utf-8")
    shard = utc_shard_path(path, date(2026, 9, 16))
    shard.write_text("garbage\n", encoding="utf-8")
    before = _snapshot(tmp_path)
    _set_platform(monkeypatch, "win32")
    _unsupported_write_raises(initialize_audit_state, path)
    _unsupported_write_raises(append_audit, path, _business_rec(), now=DAY)
    _unsupported_write_raises(repair_audit, path)
    _unsupported_write_raises(prune_audit, path, now=DAY)
    assert _snapshot(tmp_path) == before


def test_empty_collection_report_does_not_succeed(tmp_path, monkeypatch):
    _set_platform(monkeypatch, "win32")
    path = tmp_path / "empty.jsonl"
    before = _snapshot(tmp_path)
    _unsupported_write_raises(build_shadow_report, path, parse_since("30d"), now=DAY)
    assert _snapshot(tmp_path) == before


# ---------------------------------------------------------------- Guard / CLI check / MCP

def _platform_warning_locked(result) -> None:
    warns = [w for w in result.warnings if w["rule_id"] == "audit_write_failed"]
    assert warns
    assert "平台不支持" in warns[0]["detail"]
    assert "Traceback" not in warns[0]["detail"]
    evidence = result.evidence["rule_evidence"]["audit_write_failed"]
    assert {"name": "error_code", "value": AUDIT_PLATFORM_UNSUPPORTED} in evidence
    Draft202012Validator(RESULT_SCHEMA).validate(result.to_dict())


def test_guard_matrix_on_unsupported_platform(tmp_path, monkeypatch):
    _set_platform(monkeypatch, "win32")
    audit = tmp_path / "g.jsonl"
    order = fresh_order()
    portfolio = fresh_portfolio()

    pass_guard = Guard(make_engine(full_policy(), []), audit_path=str(audit))
    passed = pass_guard.check(order, portfolio, now=NOW)
    assert passed.decision == "WARN" and passed.exit_code == 2
    _platform_warning_locked(passed)

    warn_guard = Guard(make_engine(full_policy(), [WarnRule()]), audit_path=str(audit))
    warned = warn_guard.check(order, portfolio, now=NOW)
    assert warned.decision == "WARN" and warned.exit_code == 2
    _platform_warning_locked(warned)

    block_guard = Guard.from_policy(
        _write_policy(tmp_path, name="block.yaml"), audit_path=str(audit)
    )
    blocked = block_guard.check(fresh_order(quantity=1000), portfolio, now=NOW)
    assert blocked.decision == "BLOCK" and blocked.exit_code == 3
    _platform_warning_locked(blocked)

    input_guard = Guard.from_policy(
        _write_policy(tmp_path, name="input.yaml"), audit_path=str(audit)
    )
    invalid = input_guard.check(fresh_order(currency="HKD"), portfolio, now=NOW)
    assert invalid.exit_code == 4 and invalid.decision == "BLOCK"
    _platform_warning_locked(invalid)

    boom_guard = Guard(make_engine(full_policy(), [BoomRule()]), audit_path=str(audit))
    boom = boom_guard.check(order, portfolio, now=NOW)
    assert boom.exit_code == 5 and boom.decision == "BLOCK"
    _platform_warning_locked(boom)

    shadow_guard = Guard.from_policy(
        _write_policy(tmp_path, name="shadow.yaml", mode="shadow"), audit_path=str(audit)
    )
    shadow = shadow_guard.check(fresh_order(quantity=1000), portfolio, now=NOW)
    assert shadow.decision == "WARN" and shadow.exit_code == 2
    assert shadow.shadow_verdict == "BLOCK"
    _platform_warning_locked(shadow)

    from tests.conftest import assert_no_audit_artifacts

    assert_no_audit_artifacts(audit)


def test_cli_check_and_audit_json_on_unsupported(tmp_path, monkeypatch, capsys):
    _set_platform(monkeypatch, "win32")
    policy = _write_policy(tmp_path)
    order = tmp_path / "order.json"
    order.write_text(json.dumps(_order()), encoding="utf-8")
    big = tmp_path / "big.json"
    big.write_text(json.dumps(_order(quantity=1000)), encoding="utf-8")
    portfolio = _write_portfolio(tmp_path)
    audit = str(tmp_path / "cli.jsonl")
    before = _snapshot(tmp_path)

    rc = main(["check", "--policy", str(policy), "--order", str(order),
               "--portfolio", str(portfolio), "--json", "--audit-path", audit])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 2 and doc["decision"] == "WARN" and doc["exit_code"] == 2
    Draft202012Validator(RESULT_SCHEMA).validate(doc)

    rc = main(["check", "--policy", str(policy), "--order", str(big),
               "--portfolio", str(portfolio), "--json", "--audit-path", audit])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 3 and doc["decision"] == "BLOCK"
    Draft202012Validator(RESULT_SCHEMA).validate(doc)

    rc = main(["audit", "init", "--json", "--audit-path", audit])
    captured = capsys.readouterr()
    assert rc == 5
    body = json.loads(captured.out)
    Draft202012Validator(STATE_SCHEMA).validate(body)
    assert body["status"] == "error" and body["error_code"] == AUDIT_PLATFORM_UNSUPPORTED
    assert AUDIT_PLATFORM_UNSUPPORTED in captured.err or "Linux" in captured.err

    rc = main(["audit", "repair", "--quarantine", "--json", "--audit-path", audit])
    captured = capsys.readouterr()
    assert rc == 5
    body = json.loads(captured.out)
    Draft202012Validator(MAINT_SCHEMA).validate(body)
    assert body["status"] == "error" and body["error_code"] == AUDIT_PLATFORM_UNSUPPORTED

    rc = main(["audit", "prune", "--json", "--audit-path", audit])
    captured = capsys.readouterr()
    assert rc == 5
    body = json.loads(captured.out)
    Draft202012Validator(PRUNE_SCHEMA).validate(body)
    assert body["status"] == "error" and body["error_code"] == AUDIT_PLATFORM_UNSUPPORTED

    rc = main(["shadow", "report", "--since", "30d", "--audit-path", audit])
    captured = capsys.readouterr()
    assert rc == 5
    assert "audit_platform_unsupported" in captured.err
    assert captured.out == ""

    assert _snapshot(tmp_path) == before


def test_cli_syntax_and_help_unchanged(tmp_path, monkeypatch, capsys):
    _set_platform(monkeypatch, "win32")
    rc = main(["audit", "repair", "--audit-path", str(tmp_path / "a.jsonl")])
    assert rc == 4
    assert "repair requires --quarantine" in capsys.readouterr().err
    with pytest.raises(SystemExit) as ei:
        main(["audit", "--help"])
    assert ei.value.code == 0
    with pytest.raises(SystemExit) as ei:
        main(["--help"])
    assert ei.value.code == 0


def test_mcp_check_order_and_recent_decisions(tmp_path, monkeypatch):
    import asyncio

    _set_platform(monkeypatch, "win32")
    policy = _write_policy(tmp_path)
    portfolio = _write_portfolio(tmp_path)
    audit = tmp_path / "mcp.jsonl"
    srv = MCPGuardServer(str(policy), str(portfolio), str(audit))

    def _call(name, arguments):
        params = type("P", (), {"name": name, "arguments": arguments})()
        r = asyncio.run(srv._call_tool(None, params))
        return r.is_error, json.loads(r.content[0].text)

    err, body = _call("check_order", {"order": _order()})
    assert err is False
    assert body["decision"] == "WARN" and body["exit_code"] == 2
    assert body["decision"] != "PASS"
    Draft202012Validator(RESULT_SCHEMA).validate(body)
    assert {"name": "error_code", "value": AUDIT_PLATFORM_UNSUPPORTED} in (
        body["evidence"]["rule_evidence"]["audit_write_failed"]
    )

    err, body = _call("check_order", {"order": _order(quantity=1000)})
    assert err is False and body["decision"] == "BLOCK" and body["exit_code"] == 3

    err, body = _call("get_policy", {})
    assert err is False and "limits" in body

    rec = _v2_record()
    shard = utc_shard_path(audit, date(2026, 9, 16))
    _write_jsonl(shard, [rec])
    _write_state(audit, reserved="2026-09-16")
    err, body = _call("recent_decisions", {"limit": 10})
    assert err is False
    assert body["count"] == 1
    assert body["records"][0]["record_hash"] == rec["record_hash"]
    assert not Path(str(audit) + ".state.json.tmp").exists()


# ---------------------------------------------------------------- offline read/verify

def test_offline_verify_read_on_unsupported(tmp_path, monkeypatch):
    path = tmp_path / "offline.jsonl"
    rec = _v2_record()
    shard = utc_shard_path(path, date(2026, 9, 16))
    _write_jsonl(shard, [rec])
    _write_state(path, reserved="2026-09-16")
    _set_platform(monkeypatch, "win32")
    result = verify_audit(path, now=DAY)
    assert result["status"] == "clean"
    records = read_audit_records(path, now=DAY)
    assert len(records) == 1
    assert records[0]["record_hash"] == rec["record_hash"]

    tampered = dict(rec)
    tampered["decision"] = "BLOCK"
    _write_jsonl(shard, [tampered])
    checked = verify_audit(path, now=DAY)
    assert checked["status"] == "invalid"
    assert any(i["reason_code"] == "record_hash_mismatch" for i in checked["issues"])
    with pytest.raises(AuditError):
        read_audit_records(path, now=DAY)

    rec2 = _v2_record(record_id="rec00002", prev_hash="a" * 64)
    _write_jsonl(shard, [rec, rec2])
    checked = verify_audit(path, now=DAY)
    assert checked["status"] == "invalid"
    assert any(i["reason_code"] == "prev_hash_mismatch" for i in checked["issues"])

    v1 = {
        "schema_version": 1,
        "record_id": "legacy01",
        "evaluated_at": "2026-09-16T12:00:00Z",
        "input_hash": "0" * 64,
        "decision": "PASS",
        "shadow_mode": False,
        "shadow_verdict": None,
        "exit_code": 0,
        "policy_version": "synthetic",
        "rule_hits": [],
    }
    _write_jsonl(shard, [rec, v1])
    checked = verify_audit(path, now=DAY)
    assert checked["status"] == "invalid"
    assert any(i["reason_code"] == "schema_version_downgrade" for i in checked["issues"])


# ---------------------------------------------------------------- POSIX fsync still fail-closed when writes are in scope

def _is_dir_readonly_open(name, flags) -> bool:
    writeish = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT
    return (flags & writeish) == 0 and Path(name).is_dir()


@pytest.mark.parametrize("err", [
    OSError(errno.EACCES, "Permission denied"),
    OSError(errno.EPERM, "Operation not permitted"),
    OSError(errno.EIO, "Input/output error"),
    PermissionError(errno.EACCES, "Permission denied"),
])
def test_dir_and_file_fsync_errors_not_swallowed(tmp_path, monkeypatch, err):
    import deadlatch.audit as audit_mod

    _set_platform(monkeypatch, "darwin")
    path = tmp_path / f"fsync-{err.errno}.jsonl"
    real_fsync = audit_mod.os.fsync
    real_open = audit_mod.os.open

    def boom_fsync(fd):
        raise OSError(err.errno, os.strerror(err.errno)) if err.errno else err

    monkeypatch.setattr(audit_mod.os, "fsync", boom_fsync)
    with pytest.raises(AuditError) as exc:
        initialize_audit_state(path)
    assert exc.value.code == AUDIT_STATE_IO_ERROR

    monkeypatch.setattr(audit_mod.os, "fsync", real_fsync)

    def boom_open(name, flags, *rest):
        if _is_dir_readonly_open(name, flags):
            raise PermissionError(errno.EACCES, "Permission denied", name)
        return real_open(name, flags, *rest)

    monkeypatch.setattr(audit_mod.os, "open", boom_open)
    with pytest.raises(AuditError) as exc:
        initialize_audit_state(path)
    assert exc.value.code == AUDIT_STATE_IO_ERROR


def test_file_fsync_eacces_during_append_not_swallowed(tmp_path, monkeypatch):
    import deadlatch.audit as audit_mod

    path = tmp_path / "append-fsync.jsonl"
    _write_state(path, reserved="2026-09-16")
    _set_platform(monkeypatch, "linux")
    real_fsync = audit_mod.os.fsync

    def boom(fd):
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EACCES, "Permission denied")
        return real_fsync(fd)

    monkeypatch.setattr(audit_mod.os, "fsync", boom)
    with pytest.raises(AuditError):
        append_audit(path, _business_rec(), now=DAY)
    assert not utc_shard_path(path, DAY).exists()


def test_supported_platforms_still_init(tmp_path):
    path = tmp_path / "ok.jsonl"
    if not audit_writes_supported():
        _unsupported_write_raises(initialize_audit_state, path)
        return
    result = initialize_audit_state(path)
    assert result["status"] == "initialized"
    append_audit(path, _business_rec(), now=datetime.now(timezone.utc))
    assert verify_audit(path)["status"] == "clean"
