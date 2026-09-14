"""影子报告聚合。

- 读取窗口内合法 AuditRecord，输出符合 shadow-report.schema.json 的 ShadowReport；
- would_block/would_warn 取"内部裁决"：shadow 记录用 shadow_verdict，
  enforce 记录用 decision（shadow_verdict 为 null 时等价于 decision）；
- Decimal 聚合，零分母为 0；by_rule 排序：block_count 降序、warn_count 降序、
  rule_id 字典序；worst_case 只保留脱敏 detail（不伪造 triggered_value/limit）；
- 空窗口输出合法零值报告；文件不存在输出空报告 + 附注；
  文件不可读 / JSONL 损坏 / Schema 不合法 → AuditError（CLI exit 5，fail-closed）；
- 未来时间戳记录不计入窗口并在 notes 附注；报告入口先触发 30 天清理（锁内原子）。
"""

import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from jsonschema import Draft202012Validator

from ._resources import schema_dict
from ._timeutil import parse_rfc3339
from .audit import AuditError, is_audit_maintenance_record, prune_audit, read_audit_records

_REPORT_VALIDATOR = Draft202012Validator(schema_dict("shadow-report"))  # package Schema


def parse_since(text: str) -> timedelta:
    """解析 --since 窗口（30d / 7d / 24h / 30m / 60s / 2w）。非法 → ValueError。"""
    m = re.fullmatch(r"\s*(\d+)\s*([smhdw])\s*", str(text))
    if not m:
        raise ValueError(f"无法解析 --since: {text!r}（支持如 30d / 7d / 24h / 30m / 60s）")
    n = int(m.group(1))
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
    return timedelta(seconds=n * mult)


def _internal_verdict(record: dict) -> str:
    """内部裁决：shadow 记录用 shadow_verdict；enforce 记录用 decision。"""
    return record.get("shadow_verdict") or record.get("decision", "PASS")


def _rec_dt(record: dict) -> datetime:
    dt = parse_rfc3339(record.get("evaluated_at", ""))
    assert dt is not None  # read_audit_records 已保证
    return dt


def _empty_report(window_start, window_end, now, notes) -> dict:
    return {
        "schema_version": 1,
        "window_start": window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_end": window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "totals": {
            "orders_evaluated": 0,
            "would_block": 0,
            "would_warn": 0,
            "block_rate": 0,
            "warn_rate": 0,
        },
        "by_rule": [],
        "notes": notes,
    }


def build_shadow_report(path: Path, since: timedelta, now: datetime | None = None) -> dict:
    """构建 ShadowReport。文件不存在 → 空报告（附注说明）；损坏 → AuditError。"""
    now = now or datetime.now(timezone.utc)
    window_start = now - since
    window_end = now
    notes: list[str] = []

    if not path.exists():
        notes.append(f"审计文件不存在：{path}（输出空报告）")
        return _empty_report(window_start, window_end, now, notes)

    # 报告入口触发 30 天清理（锁内原子；malformed → AuditError fail-closed）
    prune_audit(path, now=now)

    records = read_audit_records(path)
    maintenance = [r for r in records if is_audit_maintenance_record(r)]
    orders = [r for r in records if not is_audit_maintenance_record(r)]
    if maintenance:
        notes.append(f"audit maintenance events excluded from order totals: {len(maintenance)}")
    future = [r for r in orders if _rec_dt(r) > window_end]
    if future:
        notes.append(f"发现 {len(future)} 条未来时间戳记录，不计入窗口")
    window = [r for r in orders if window_start <= _rec_dt(r) <= window_end]

    orders = len(window)
    would_block = sum(1 for r in window if _internal_verdict(r) == "BLOCK")
    would_warn = sum(1 for r in window if _internal_verdict(r) == "WARN")
    block_rate = Decimal(would_block) / Decimal(orders) if orders else Decimal("0")
    warn_rate = Decimal(would_warn) / Decimal(orders) if orders else Decimal("0")

    by_rule: dict[str, dict] = {}
    for r in window:
        for hit in r.get("rule_hits", []):
            rid = hit.get("rule_id", "")
            severity = hit.get("severity", "")
            agg = by_rule.setdefault(rid, {"block_count": 0, "warn_count": 0, "worst_detail": None})
            if severity == "BLOCK":
                agg["block_count"] += 1
                if agg["worst_detail"] is None:
                    agg["worst_detail"] = hit.get("detail", "")  # 第一条 BLOCK detail（文件顺序确定）
            elif severity == "WARN":
                agg["warn_count"] += 1

    rows = [
        {
            "rule_id": rid,
            "block_count": agg["block_count"],
            "warn_count": agg["warn_count"],
            "worst_case": {"detail": agg["worst_detail"]} if agg["worst_detail"] is not None else None,
        }
        for rid, agg in by_rule.items()
    ]
    rows.sort(key=lambda r: (-r["block_count"], -r["warn_count"], r["rule_id"]))

    report = {
        "schema_version": 1,
        "window_start": window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_end": window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "totals": {
            "orders_evaluated": orders,
            "would_block": would_block,
            "would_warn": would_warn,
            "block_rate": float(block_rate),
            "warn_rate": float(warn_rate),
        },
        "by_rule": rows,
        "notes": notes,
    }
    errors = sorted(_REPORT_VALIDATOR.iter_errors(report), key=lambda e: list(e.path))
    if errors:
        raise AuditError(f"报告未通过 shadow-report.schema：{errors[0].message}")
    return report
