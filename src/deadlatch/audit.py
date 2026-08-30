"""本地审计 JSONL（ §三/§四/§五）。

- 每次 Guard.check() 尝试原子追加一条 AuditRecord（单行 UTF-8 JSON，
  排序与序列化确定：sort_keys + 紧凑分隔符）；
- 跨进程文件锁（fcntl.flock 独立锁文件）+ flush/fsync；清理时锁内写临时
  文件、fsync 后 os.replace 原子替换，异常不破坏原文件；
- 30 天保留（免费版固定，不读 license）：每次 append 与 report 入口触发，
  恰好 30 天保留、早于 30 天删除、未来记录不误删（计数返回供报告附注）；
- 脱敏：rule_hits.detail 用本次 order/portfolio 的 symbol/underlying 等
  已知敏感值精确替换，并过滤绝对路径与常见凭据形态；input_hash 保留；
- malformed / Schema 非法行：读取/清理/追加一律 fail-closed（抛 AuditError），
  事务不写盘、原文件保持可恢复、不静默跳过；append 传入的新记录同样先过
  audit-record.schema 校验。
"""

import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from ._resources import schema_dict
from ._timeutil import parse_rfc3339

try:
    import fcntl
except ImportError:  # 非 POSIX 平台退化（ 验收环境为 macOS/POSIX）
    fcntl = None  # type: ignore[assignment]

_AUDIT_VALIDATOR = Draft202012Validator(schema_dict("audit-record"))  # ：包内 Schema

# 默认本地用户状态路径（文档化；DEADLATCH_AUDIT_PATH 环境变量可覆盖）
DEFAULT_AUDIT_PATH = Path.home() / ".deadlatch" / "audit.jsonl"

RETENTION_DAYS = 30
#  T10：审计文件大小上限（30 天窗口量级：约 9MiB/天千条，64MiB 足够）
MAX_AUDIT_FILE_BYTES = 64 * 1024 * 1024


def _check_audit_size(path: Path) -> None:
    """审计文件载入前大小检查：超限 → AuditError（fail-closed，拒绝完整读取）。"""
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size > MAX_AUDIT_FILE_BYTES:
        raise AuditError(f"审计文件超过大小上限 {MAX_AUDIT_FILE_BYTES} 字节（实际 {size} 字节），拒绝读取")

# 脱敏：常见凭据形态与任意 POSIX/macOS 绝对路径（不依赖调用方，不列举少数前缀）
_SENSITIVE_PATTERN_RE = re.compile(
    r"(?i)"
    r"(authorization|proxy-authorization)\s*:\s*bearer\s+\S+"
    r"|\bbearer\s+[a-zA-Z0-9._~+/=-]{8,}"
    r"|\b(api[_-]?key|secret|passwd|password|access[_-]?token|auth[_-]?token|token)\b\s*[=:]\s*\S+"
    r"|\b(sk-|pk-|rk-)[a-zA-Z0-9_-]{12,}"
    r"|\b(?:cookie|set-cookie)\s*[:=][^\r\n]*"        # Cookie / Set-Cookie 头整段
    r"|\b(?:sessionid|session|auth)\b\s*=\s*[^\s;]+"  # session credential 形态
    r"|(?<![A-Za-z0-9_.~])(?:/[\w .\-]+){2,}"          # 任意绝对路径（≥2 段，段内可含空格，如 "/Applications/Secret App/data.json"）
)
_SENSITIVE_KEY_RE = re.compile(r"(?i)symbol|underlying|account|token|api[_-]?key|secret|password|credential|bearer")


class AuditError(Exception):
    """审计读写/校验失败（fail-closed；调用方转可见降级或 exit 5）。"""


# ---------------------------------------------------------------- 脱敏

def collect_sensitive_values(order, portfolio) -> list[str]:
    """收集本次输入中的已知敏感字符串值（symbol/underlying/账户类字段）。"""
    out: list[str] = []

    def _walk(obj, key: str | None) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str):
                    if k in ("symbol", "underlying") or _SENSITIVE_KEY_RE.search(k):
                        out.append(v)
                else:
                    _walk(v, k)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item, key)

    _walk(order.to_dict(), None)
    _walk(portfolio.to_dict(), None)
    # 精确替换需要非空、非纯数字、长度 >= 2 的值（避免误伤数量/比例）
    return sorted({v for v in out if isinstance(v, str) and len(v) >= 2 and not v.isdigit()},
                  key=len, reverse=True)


def sanitize_text(text: str, sensitive: list[str]) -> str:
    """脱敏：先精确替换已知敏感值，再过滤路径/凭据形态。"""
    if not isinstance(text, str):
        return text
    for value in sensitive:
        text = text.replace(value, "<redacted>")
    return _SENSITIVE_PATTERN_RE.sub("<redacted>", text)


def sanitize_hits(hits: list[dict], sensitive: list[str]) -> list[dict]:
    """rule_hits 脱敏：只保留 rule_id/severity/detail（detail 脱敏）。"""
    out = []
    for h in hits:
        rule_id = h.get("rule_id", "")
        severity = h.get("severity", "")
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", str(rule_id)):
            continue
        if severity not in ("BLOCK", "WARN"):
            continue
        out.append(
            {
                "rule_id": str(rule_id),
                "severity": severity,
                "detail": sanitize_text(str(h.get("detail", "")), sensitive),
            }
        )
    return out


# ---------------------------------------------------------------- 记录构造

def build_audit_record(order, portfolio, policy, result) -> dict:
    """构造 AuditRecord（脱敏后；schema 校验失败抛 AuditError）。"""
    sensitive = collect_sensitive_values(order, portfolio)
    hits = []
    for v in result.violations:
        hits.append({"rule_id": v.get("rule_id", ""), "severity": "BLOCK", "detail": v.get("detail", "")})
    for w in result.warnings:
        hits.append({"rule_id": w.get("rule_id", ""), "severity": "WARN", "detail": w.get("detail", "")})
    record = {
        "schema_version": 1,
        "record_id": uuid.uuid4().hex,
        "evaluated_at": result.evaluated_at,
        "input_hash": (result.evidence or {}).get("input_hash", ""),
        "decision": result.decision,
        "shadow_mode": bool(result.shadow_mode),
        "shadow_verdict": result.shadow_verdict,
        "exit_code": result.exit_code,
        # FIX-003-2：policy_version 同样过安全处理，Token/路径/凭据形态不得原样落盘
        "policy_version": sanitize_text(str(policy.version), sensitive),
        "rule_hits": sanitize_hits(hits, sensitive),
    }
    errors = sorted(_AUDIT_VALIDATOR.iter_errors(record), key=lambda e: list(e.path))
    if errors:
        raise AuditError(f"审计记录未通过 audit-record.schema：{errors[0].message}")
    return record


# ---------------------------------------------------------------- 原子追加 / 清理

def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def _locked(path: Path, fn):
    """跨进程文件锁内执行 fn（独立锁文件，避免 replace 换 inode 的竞态）。"""
    lock_file = _lock_path(path)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is not None:
        with open(lock_file, "a+", encoding="utf-8") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                return fn()
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    # 非 POSIX：进程内锁退化为无跨进程保证（验收环境为 POSIX）
    return fn()


def _filter_kept_lines(lines: list[str], now: datetime, cutoff: datetime) -> tuple[list[str], int, int]:
    """按 30 天保留过滤：保留 ≥ cutoff 的行与未来记录。

    每条既有记录除解析 JSON / evaluated_at 外，还必须通过
    audit-record.schema.json——malformed 或 Schema 非法一律 AuditError
    （fail-closed，事务不写盘、原文件不动）。返回 (保留行, removed, future)。
    """
    kept: list[str] = []
    removed = 0
    future = 0
    for lineno, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception as exc:
            raise AuditError(
                f"审计文件第 {lineno} 行 JSON 损坏，操作已中止（原文件未改动）: {type(exc).__name__}"
            ) from exc
        errors = sorted(_AUDIT_VALIDATOR.iter_errors(rec), key=lambda e: list(e.path))
        if errors:
            raise AuditError(
                f"审计文件第 {lineno} 行未通过 audit-record.schema，操作已中止（原文件未改动）: {errors[0].message}"
            )
        dt = parse_rfc3339(rec.get("evaluated_at", ""))
        if dt is None:
            raise AuditError(
                f"审计文件第 {lineno} 行 evaluated_at 缺失/不可解析，操作已中止（原文件未改动）"
            )
        if dt > now:
            future += 1
            kept.append(line)
        elif dt >= cutoff:
            kept.append(line)
        else:
            removed += 1
    return kept, removed, future


def _write_atomic(path: Path, lines: list[str]) -> None:
    """同目录临时文件 → flush/fsync → os.replace 原子替换（异常时原文件保持完整）。"""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(lines)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def append_audit(path, record: dict, now: datetime | None = None) -> dict:
    """原子追加一条记录，同一锁事务内执行 30 天保留（FIX-003-1）。

    - 每次成功 append 都保证早于 (now − 30d) 的记录被清理（不按文件大小决定）；
    - 恰好 30 天保留、早于 30 天删除、未来记录保留；
    - 校验/清理/追加在同一跨进程锁内完成：要么全部成功（单条记录不拆裂），
      要么不写任何东西（malformed/读写/replace 失败 → 抛异常，调用方转
      audit_write_failed 降级；原文件保持可恢复，不会出现"磁盘已写 PASS/0
      但调用方拿到 WARN/2"的矛盾）。
    返回 {"removed", "future"}。
    """
    path = Path(path)
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RETENTION_DAYS)

    def _tx() -> dict:
        # 新记录同样必须通过 audit-record.schema（非法 → 事务不写盘）
        errors = sorted(_AUDIT_VALIDATOR.iter_errors(record), key=lambda e: list(e.path))
        if errors:
            raise AuditError(
                f"审计记录未通过 audit-record.schema，事务未写盘: {errors[0].message}"
            )
        line = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            _check_audit_size(path)  #  T10
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            kept, removed, future = _filter_kept_lines(lines, now, cutoff)
        else:
            kept, removed, future = [], 0, 0
        kept.append(line + "\n")
        # FIX-005-3：最终 UTF-8 字节数（含换行）越界 → 在创建/替换目标文件前拒绝，
        # 原审计文件字节级不变、不留临时文件；== MAX 成功，+1 拒绝
        final_size = sum(len(l.encode("utf-8")) for l in kept)
        if final_size > MAX_AUDIT_FILE_BYTES:
            raise AuditError(
                f"审计文件追加后大小 {final_size} 字节超过上限 {MAX_AUDIT_FILE_BYTES}，"
                f"拒绝写入（原文件未改动）"
            )
        _write_atomic(path, kept)
        return {"removed": removed, "future": future}

    return _locked(path, _tx)


def prune_audit(path, now: datetime | None = None, retention_days: int = RETENTION_DAYS) -> dict:
    """30 天保留：早于 (now − retention_days) 的记录删除；未来记录保留。

    锁内读全文件 → 写同目录临时文件 → fsync → os.replace 原子替换。
    malformed 行 → fail-closed 抛 AuditError，原文件不动（可恢复）。
    返回 {"removed", "kept", "future"}。
    """
    path = Path(path)
    now = now or datetime.now(timezone.utc)
    if not path.exists():
        return {"removed": 0, "kept": 0, "future": 0}
    _check_audit_size(path)  #  T10
    cutoff = now - timedelta(days=retention_days)

    def _prune() -> dict:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        kept, removed, future = _filter_kept_lines(lines, now, cutoff)
        if removed == 0:
            return {"removed": 0, "kept": len(kept), "future": future}
        _write_atomic(path, kept)
        return {"removed": removed, "kept": len(kept), "future": future}

    return _locked(path, _prune)


# ---------------------------------------------------------------- 读取（供报告）

def read_audit_records(path) -> list[dict]:
    """逐行读取并校验（audit-record.schema）。malformed / schema 非法 → AuditError。

    只读操作；失败时原文件不动（调用方 fail-closed，CLI exit 5）。
    """
    path = Path(path)
    try:
        _check_audit_size(path)  #  T10
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as exc:
        raise AuditError(f"审计文件不可读：{type(exc).__name__}") from exc
    records: list[dict] = []
    for lineno, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception as exc:
            raise AuditError(
                f"审计文件第 {lineno} 行 JSON 损坏（fail-closed，不静默跳过）: {type(exc).__name__}"
            ) from exc
        errors = sorted(_AUDIT_VALIDATOR.iter_errors(rec), key=lambda e: list(e.path))
        if errors:
            raise AuditError(
                f"审计文件第 {lineno} 行未通过 audit-record.schema（fail-closed）: {errors[0].message}"
            )
        records.append(rec)
    return records
