""" §六 影子报告测试：聚合准确性、排序、零分母、worst_case、
空窗口、文件不存在、malformed fail-closed、未来记录附注、CLI 输出。
"""

import json
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from deadlatch.audit import AuditError
from deadlatch.report import build_shadow_report, parse_since

REPO = Path(__file__).resolve().parents[1]
REPORT_SCHEMA = json.loads((REPO / "schemas" / "shadow-report.schema.json").read_text())
REPORT_VALIDATOR = Draft202012Validator(REPORT_SCHEMA)

NOW = datetime(2026, 8, 29, 10, 0, 1, tzinfo=timezone.utc)

_VERDICT_EXIT = {"PASS": 0, "WARN": 2, "BLOCK": 3}


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(verdict: str, hits: list[dict], ts: str | None = None, shadow: bool = True) -> dict:
    """一条合法 AuditRecord（verdict：内部裁决；hits：rule_hits）。"""
    return {
        "schema_version": 1,
        "record_id": uuid.uuid4().hex,
        "evaluated_at": ts or _ts(NOW - timedelta(seconds=60)),
        "input_hash": "0" * 64,
        "decision": "PASS" if shadow else verdict,
        "shadow_mode": shadow,
        "shadow_verdict": verdict if shadow else None,
        "exit_code": _VERDICT_EXIT[verdict],
        "policy_version": "1.0.0",
        "rule_hits": hits,
    }


def _write_records(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, sort_keys=True, separators=(",", ":")) for r in records) + "\n",
        encoding="utf-8",
    )


def _run_cli(*args, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "deadlatch.cli", *args],
        capture_output=True, text=True, cwd=str(cwd),
        env={"PYTHONPATH": str(REPO / "src")}, timeout=60,
    )


# ---------------- --since 解析 ----------------

@pytest.mark.parametrize("text,seconds", [
    ("30d", 30 * 86400), ("7d", 7 * 86400), ("24h", 24 * 3600),
    ("30m", 30 * 60), ("60s", 60), ("2w", 14 * 86400),
])
def test_parse_since_ok(text, seconds):
    assert parse_since(text) == timedelta(seconds=seconds)


@pytest.mark.parametrize("bad", ["30", "d30", "30x", "", "  ", "-1d"])
def test_parse_since_invalid(bad):
    with pytest.raises(ValueError):
        parse_since(bad)


# ---------------- 空窗口 / 文件不存在 ----------------

def test_empty_file_zero_report(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text("", encoding="utf-8")
    report = build_shadow_report(path, parse_since("30d"), now=NOW)
    REPORT_VALIDATOR.validate(report)
    assert report["totals"] == {
        "orders_evaluated": 0, "would_block": 0, "would_warn": 0,
        "block_rate": 0, "warn_rate": 0,
    }
    assert report["by_rule"] == []


def test_missing_file_empty_report_with_note(tmp_path):
    report = build_shadow_report(tmp_path / "nope.jsonl", parse_since("30d"), now=NOW)
    REPORT_VALIDATOR.validate(report)
    assert report["totals"]["orders_evaluated"] == 0
    assert any("不存在" in n for n in report["notes"])


# ---------------- 500 条聚合准确性 ----------------

def test_500_records_aggregation_accurate(tmp_path):
    path = tmp_path / "audit.jsonl"
    records = []
    for i in range(500):
        ts = _ts(NOW - timedelta(seconds=500 - i))  # 窗口内，时间顺序递增
        if i < 80:
            rec = _record("BLOCK", [{"rule_id": "max_symbol_exposure", "severity": "BLOCK",
                                     "detail": f"单标的 AAA 敞口超限 case-{i}"}], ts)
        elif i < 100:
            rec = _record("BLOCK", [{"rule_id": "kill_switch", "severity": "BLOCK",
                                     "detail": "kill switch full"}], ts)
        elif i < 130:
            rec = _record("WARN", [{"rule_id": "max_order_quantity", "severity": "WARN",
                                    "detail": "数量接近上限"}], ts)
        elif i < 150:
            rec = _record("WARN", [{"rule_id": "max_order_value", "severity": "WARN",
                                    "detail": "金额接近上限"}], ts)
        else:
            rec = _record("PASS", [], ts)
        records.append(rec)
    _write_records(path, records)

    report = build_shadow_report(path, parse_since("30d"), now=NOW)
    REPORT_VALIDATOR.validate(report)
    t = report["totals"]
    assert t["orders_evaluated"] == 500
    assert t["would_block"] == 100  # 80 + 20
    assert t["would_warn"] == 50    # 30 + 20
    assert t["block_rate"] == 0.2
    assert t["warn_rate"] == 0.1
    rows = report["by_rule"]
    assert [r["rule_id"] for r in rows] == [
        "max_symbol_exposure", "kill_switch", "max_order_quantity", "max_order_value",
    ]
    by_id = {r["rule_id"]: r for r in rows}
    assert by_id["max_symbol_exposure"]["block_count"] == 80
    assert by_id["kill_switch"]["block_count"] == 20
    assert by_id["max_order_quantity"]["warn_count"] == 30
    assert by_id["max_order_value"]["warn_count"] == 20
    # worst_case：第一条 BLOCK 的脱敏 detail（不伪造 triggered_value/limit）
    wc = by_id["max_symbol_exposure"]["worst_case"]
    assert wc["detail"] == "单标的 AAA 敞口超限 case-0"
    assert "triggered_value" not in wc and "limit" not in wc
    assert by_id["kill_switch"]["worst_case"]["detail"] == "kill switch full"
    assert by_id["max_order_quantity"]["worst_case"] is None  # 无 BLOCK → 无 worst_case


def test_by_rule_sort_order_block_then_warn_then_rule_id(tmp_path):
    path = tmp_path / "audit.jsonl"
    records = [
        _record("WARN", [{"rule_id": "z_rule", "severity": "WARN", "detail": "w"}],
                _ts(NOW - timedelta(seconds=1))),
        _record("BLOCK", [{"rule_id": "a_rule", "severity": "BLOCK", "detail": "b"}],
                _ts(NOW - timedelta(seconds=2))),
        _record("BLOCK", [{"rule_id": "a_rule", "severity": "BLOCK", "detail": "b2"}],
                _ts(NOW - timedelta(seconds=3))),
        _record("BLOCK", [{"rule_id": "m_rule", "severity": "BLOCK", "detail": "m"}],
                _ts(NOW - timedelta(seconds=4))),
    ]
    _write_records(path, records)
    report = build_shadow_report(path, parse_since("30d"), now=NOW)
    assert [r["rule_id"] for r in report["by_rule"]] == ["a_rule", "m_rule", "z_rule"]


def test_enforce_records_use_decision_as_verdict(tmp_path):
    # enforce 记录（shadow_mode=False，shadow_verdict=null）→ 用 decision 聚合
    path = tmp_path / "audit.jsonl"
    records = [
        _record("BLOCK", [{"rule_id": "max_drawdown", "severity": "BLOCK", "detail": "d"}],
                _ts(NOW - timedelta(seconds=1)), shadow=False),
        _record("WARN", [{"rule_id": "max_daily_loss", "severity": "WARN", "detail": "l"}],
                _ts(NOW - timedelta(seconds=2)), shadow=False),
        _record("PASS", [], _ts(NOW - timedelta(seconds=3)), shadow=False),
    ]
    _write_records(path, records)
    report = build_shadow_report(path, parse_since("30d"), now=NOW)
    assert report["totals"]["orders_evaluated"] == 3
    assert report["totals"]["would_block"] == 1
    assert report["totals"]["would_warn"] == 1


def test_future_records_excluded_with_note(tmp_path):
    path = tmp_path / "audit.jsonl"
    records = [
        _record("BLOCK", [{"rule_id": "kill_switch", "severity": "BLOCK", "detail": "k"}],
                _ts(NOW + timedelta(days=1))),  # 未来
        _record("PASS", [], _ts(NOW - timedelta(seconds=60))),
    ]
    _write_records(path, records)
    report = build_shadow_report(path, parse_since("30d"), now=NOW)
    assert report["totals"]["orders_evaluated"] == 1  # 未来记录不计入窗口
    assert any("未来" in n for n in report["notes"])


# ---------------- 损坏文件 fail-closed ----------------

def test_malformed_line_fail_closed(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(
        json.dumps(_record("PASS", []), sort_keys=True, separators=(",", ":")) + "\n"
        + "this-is-not-json\n",
        encoding="utf-8",
    )
    with pytest.raises(AuditError):
        build_shadow_report(path, parse_since("30d"), now=NOW)
    # 原文件未改动
    assert "this-is-not-json" in path.read_text(encoding="utf-8")


def test_schema_invalid_line_fail_closed(tmp_path):
    path = tmp_path / "audit.jsonl"
    bad = _record("PASS", [])
    bad["exit_code"] = 99  # schema enum 非法
    path.write_text(json.dumps(bad, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8")
    with pytest.raises(AuditError):
        build_shadow_report(path, parse_since("30d"), now=NOW)


def test_hash_mismatch_and_shard_v1_downgrade_fail_closed(tmp_path):
    from deadlatch.audit import append_audit, utc_shard_path

    path = tmp_path / "audit.jsonl"
    rec = _record("BLOCK", [{"rule_id": "kill_switch", "severity": "BLOCK", "detail": "k"}])
    rec["shadow_mode"] = False
    rec["shadow_verdict"] = None
    append_audit(path, rec, now=NOW)
    shard = utc_shard_path(path, NOW)
    stored = json.loads(shard.read_text(encoding="utf-8"))
    stored["decision"] = "PASS"
    stored["exit_code"] = 0
    shard.write_text(json.dumps(stored, sort_keys=True, separators=(",", ":")) + "\n",
                     encoding="utf-8")
    with pytest.raises(AuditError, match="record_hash_mismatch"):
        build_shadow_report(path, parse_since("30d"), now=NOW)
    r = _run_cli("shadow", "report", "--since", "30d", "--audit-path", str(path), cwd=tmp_path)
    assert r.returncode == 5
    assert "record_hash_mismatch" in r.stderr
    assert "would_block" not in r.stdout

    body = {k: v for k, v in stored.items() if k not in ("prev_hash", "record_hash")}
    body["schema_version"] = 1
    shard.write_text(json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n",
                     encoding="utf-8")
    with pytest.raises(AuditError, match="schema_version_downgrade"):
        build_shadow_report(path, parse_since("30d"), now=NOW)
    r = _run_cli("shadow", "report", "--since", "30d", "--audit-path", str(path), cwd=tmp_path)
    assert r.returncode == 5
    assert "schema_version_downgrade" in r.stderr


# ---------------- CLI ----------------

def test_cli_report_human_output(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write_records(path, [
        _record("BLOCK", [{"rule_id": "kill_switch", "severity": "BLOCK", "detail": "k"}],
                _ts(NOW - timedelta(seconds=1))),
    ])
    r = _run_cli("shadow", "report", "--since", "30d", "--audit-path", str(path), cwd=tmp_path)
    assert r.returncode == 0
    assert "orders_evaluated: 1" in r.stdout
    assert "would_block: 1" in r.stdout
    assert "kill_switch" in r.stdout
    assert r.stderr == ""


def test_cli_report_json_stdout_only_json(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write_records(path, [
        _record("WARN", [{"rule_id": "max_order_value", "severity": "WARN", "detail": "v"}],
                _ts(NOW - timedelta(seconds=1))),
    ])
    r = _run_cli("shadow", "report", "--since", "30d", "--json", "--audit-path", str(path),
                 cwd=tmp_path)
    assert r.returncode == 0
    doc = json.loads(r.stdout)  # stdout 只含 JSON
    REPORT_VALIDATOR.validate(doc)
    assert doc["totals"]["would_warn"] == 1


def test_cli_report_missing_file_empty(tmp_path):
    r = _run_cli("shadow", "report", "--since", "30d",
                 "--audit-path", str(tmp_path / "nope.jsonl"), cwd=tmp_path)
    assert r.returncode == 0
    assert "orders_evaluated: 0" in r.stdout


def test_cli_report_corrupt_exit5_no_traceback(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text("garbage-line\n", encoding="utf-8")
    r = _run_cli("shadow", "report", "--since", "30d", "--audit-path", str(path), cwd=tmp_path)
    assert r.returncode == 5  # fail-closed
    assert "error:" in r.stderr
    assert "Traceback" not in r.stdout and "Traceback" not in r.stderr


def test_cli_report_bad_since_exit5(tmp_path):
    r = _run_cli("shadow", "report", "--since", "30x",
                 "--audit-path", str(tmp_path / "audit.jsonl"), cwd=tmp_path)
    assert r.returncode == 5
    assert "error:" in r.stderr
    assert "Traceback" not in r.stdout
