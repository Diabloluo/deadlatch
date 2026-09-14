"""本地审计 JSONL。

- 每次 Guard.check() 尝试原子追加一条 AuditRecord（单行 UTF-8 JSON，
  排序与序列化确定：sort_keys + 紧凑分隔符）；
- 跨进程文件锁（fcntl.flock 独立锁文件）+ flush/fsync；清理时锁内写临时
  文件、fsync 后 os.replace 原子替换，异常不破坏原文件；
- 30 天保留（免费版固定）：每次 append 与 report 入口触发，恰好 30 天保留、
  早于 30 天删除、未来记录不误删（计数返回供报告附注）；
- 脱敏：rule_hits.detail 用本次 order/portfolio 的 symbol/underlying 等
  已知敏感值精确替换，并过滤绝对路径与常见凭据形态；input_hash 保留；
- malformed / Schema 非法行：读取/清理/追加一律 fail-closed（抛 AuditError），
  事务不写盘、原文件保持可恢复、不静默跳过；append 传入的新记录同样先过
  audit-record.schema 校验。
- audit verify 不修改 audit 内容或 quarantine；对已存在的常规日志可能创建
  同步锁 sidecar，以便与 append/repair 共用同一把锁。audit repair
  --quarantine 把坏行隔离到唯一 0600 JSON 文件并在主日志追加一条可识别的
  维护记录。
"""

import base64
import hashlib
import json
import os
import re
import secrets
import stat
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from ._resources import schema_dict
from ._timeutil import parse_rfc3339

try:
    import fcntl
except ImportError:  # 非 POSIX 平台退化（跨进程锁仅 POSIX 保证）
    fcntl = None  # type: ignore[assignment]

_AUDIT_VALIDATOR = Draft202012Validator(schema_dict("audit-record"))
_MAINTENANCE_RESULT_VALIDATOR = Draft202012Validator(
    schema_dict("audit-maintenance-result")
)

# 默认本地用户状态路径（文档化；DEADLATCH_AUDIT_PATH 环境变量可覆盖）
DEFAULT_AUDIT_PATH = Path.home() / ".deadlatch" / "audit.jsonl"

RETENTION_DAYS = 30
# 审计文件大小上限（30 天窗口量级：约 9MiB/天千条，64MiB 足够）
MAX_AUDIT_FILE_BYTES = 64 * 1024 * 1024
MAX_ISSUE_ECHO = 100
MAINTENANCE_POLICY_VERSION = "audit-maintenance/1"
MAINTENANCE_RULE_ID = "audit_repaired"
QUARANTINE_REASON_CODES = (
    "invalid_utf8",
    "invalid_json",
    "schema_invalid",
    "evaluated_at_invalid",
)


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


class AuditMaintenanceError(Exception):
    """verify/repair 路径或内部失败。public_message 不得含路径、原文或凭据。"""

    def __init__(self, exit_code: int, public_message: str):
        super().__init__(public_message)
        self.exit_code = exit_code
        self.public_message = public_message


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
        # policy_version 同样过安全处理，Token/路径/凭据形态不得原样落盘
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
    """原子追加一条记录，同一锁事务内执行 30 天保留。

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
            _check_audit_size(path)  # size limit
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            kept, removed, future = _filter_kept_lines(lines, now, cutoff)
        else:
            kept, removed, future = [], 0, 0
        kept.append(line + "\n")
        # 最终 UTF-8 字节数（含换行）越界 → 在创建/替换目标文件前拒绝，
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
    _check_audit_size(path)  # size limit
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
        _check_audit_size(path)  # size limit
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


# ---------------------------------------------------------------- verify / repair


def is_audit_maintenance_record(record: dict) -> bool:
    """True only when the full maintenance combination is present.

    A lone rule_id=audit_repaired is not enough; incomplete markers stay
    ordinary records so callers cannot hide real orders from reports.
    """
    if not isinstance(record, dict):
        return False
    hits = record.get("rule_hits")
    if not isinstance(hits, list) or len(hits) != 1:
        return False
    hit = hits[0]
    if not isinstance(hit, dict):
        return False
    return (
        record.get("decision") == "WARN"
        and record.get("shadow_mode") is False
        and record.get("shadow_verdict") is None
        and record.get("exit_code") == 2
        and record.get("policy_version") == MAINTENANCE_POLICY_VERSION
        and hit.get("rule_id") == MAINTENANCE_RULE_ID
        and hit.get("severity") == "WARN"
    )


def _issue_echo(issues: list[dict]) -> tuple[list[dict], bool]:
    truncated = len(issues) > MAX_ISSUE_ECHO
    return issues[:MAX_ISSUE_ECHO], truncated


def _maintenance_result(
    operation: str,
    status: str,
    valid_lines: int,
    invalid_lines: int,
    issues: list[dict],
    **extra,
) -> dict:
    echoed, truncated = _issue_echo(issues)
    payload = {
        "schema_version": 1,
        "operation": operation,
        "status": status,
        "valid_lines": valid_lines,
        "invalid_lines": invalid_lines,
        "issues": echoed,
        "issues_truncated": truncated,
    }
    payload.update(extra)
    errors = sorted(_MAINTENANCE_RESULT_VALIDATOR.iter_errors(payload), key=lambda e: list(e.path))
    if errors:
        raise AuditMaintenanceError(5, "internal error: maintenance result invalid")
    return payload


def _classify_audit_line(raw: bytes) -> tuple[str | None, dict | None]:
    """Return (reason_code, record). reason_code is None when the line is valid."""
    body = raw[:-1] if raw.endswith(b"\n") else raw
    if raw.endswith(b"\r\n"):
        body = raw[:-2]
    if not body.strip():
        return None, None
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return "invalid_utf8", None
    try:
        rec = json.loads(text)
    except Exception:
        return "invalid_json", None
    errors = list(_AUDIT_VALIDATOR.iter_errors(rec))
    if errors:
        return "schema_invalid", None
    dt = parse_rfc3339(rec.get("evaluated_at", ""))
    if dt is None or dt.tzinfo is None:
        return "evaluated_at_invalid", None
    return None, rec


def _scan_audit_bytes(data: bytes) -> tuple[int, list[dict], list[bytes], list[dict]]:
    """Scan raw file bytes. Returns valid_count, issues, kept_raw, quarantine_rows."""
    valid = 0
    issues: list[dict] = []
    kept: list[bytes] = []
    quarantined: list[dict] = []
    if not data:
        return 0, [], [], []
    lines = data.splitlines(keepends=True)
    for lineno, raw in enumerate(lines, 1):
        reason, rec = _classify_audit_line(raw)
        if reason is None and rec is None:
            kept.append(raw)
            continue
        if reason is None:
            valid += 1
            kept.append(raw)
            continue
        issues.append({"line_number": lineno, "reason_code": reason})
        quarantined.append({
            "line_number": lineno,
            "reason_code": reason,
            "raw_base64": base64.b64encode(raw).decode("ascii"),
        })
    return valid, issues, kept, quarantined


def _require_regular_audit_file(path: Path) -> None:
    """lstat-only check. Must not create directories, locks, or files."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise AuditMaintenanceError(4, "error: audit file not found") from None
    except OSError as exc:
        raise AuditMaintenanceError(5, f"error: audit stat failed ({type(exc).__name__})") from exc
    if stat.S_ISLNK(info.st_mode):
        raise AuditMaintenanceError(4, "error: audit target is a symbolic link")
    if stat.S_ISDIR(info.st_mode):
        raise AuditMaintenanceError(4, "error: audit target is a directory")
    if not stat.S_ISREG(info.st_mode):
        raise AuditMaintenanceError(4, "error: audit target is not a regular file")


def _read_audit_bytes_locked(path: Path) -> bytes:
    _require_regular_audit_file(path)
    try:
        _check_audit_size(path)
    except AuditError:
        raise AuditMaintenanceError(5, "error: audit file exceeds size limit") from None
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise AuditMaintenanceError(5, f"error: audit read failed ({type(exc).__name__})") from exc


def verify_audit(path) -> dict:
    """Scan audit content without modifying it or writing quarantine.

    Missing/non-regular targets return 4 with no filesystem side effects.
    For an existing regular log, a `.lock` sidecar may be created so verify
    shares the same lock as append/repair.
    """
    path = Path(path)
    _require_regular_audit_file(path)

    def _tx() -> dict:
        data = _read_audit_bytes_locked(path)
        valid, issues, _kept, _rows = _scan_audit_bytes(data)
        status = "invalid" if issues else "clean"
        return _maintenance_result(
            "verify",
            status,
            valid,
            len(issues),
            issues,
            source_sha256=hashlib.sha256(data).hexdigest(),
        )

    try:
        return _locked(path, _tx)
    except AuditMaintenanceError:
        raise
    except OSError as exc:
        raise AuditMaintenanceError(5, f"error: audit lock failed ({type(exc).__name__})") from exc


def _write_exclusive_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _atomic_replace_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        _write_exclusive_bytes(tmp, data, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def _unique_quarantine_name(path: Path, created_at: datetime) -> str:
    stamp = created_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{path.name}.quarantine.{stamp}.{secrets.token_hex(4)}.json"


def _publish_exclusive_bytes(final: Path, data: bytes) -> None:
    """Write a hidden tmp, fsync, then publish `final` without overwriting."""
    tmp = final.with_name(f".{final.name}.{secrets.token_hex(8)}.tmp")
    try:
        _write_exclusive_bytes(tmp, data, 0o600)
        os.link(tmp, final)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise
    try:
        tmp.unlink()
    except OSError:
        pass


def _publish_quarantine_file(path: Path, created_at: datetime, data: bytes) -> Path:
    """Atomically publish a unique quarantine JSON. Never overwrite an existing file."""
    last_exists = False
    for _ in range(32):
        qpath = path.with_name(_unique_quarantine_name(path, created_at))
        try:
            _publish_exclusive_bytes(qpath, data)
            return qpath
        except FileExistsError:
            last_exists = True
            continue
        except OSError as exc:
            raise AuditMaintenanceError(
                5, f"error: quarantine write failed ({type(exc).__name__})"
            ) from exc
    if last_exists:
        raise AuditMaintenanceError(5, "error: quarantine name allocation failed")
    raise AuditMaintenanceError(5, "error: quarantine write failed")


def _join_kept_with_marker(kept: list[bytes], marker_line: str) -> bytes:
    """Append one marker line. If kept bytes lack a JSONL terminator, add `\\n` only."""
    body = b"".join(kept)
    marker = marker_line.encode("utf-8")
    if body and not body.endswith(b"\n"):
        body += b"\n"
    return body + marker


def _build_maintenance_record(
    *,
    evaluated_at: str,
    source_sha256: str,
    quarantined_lines: int,
    quarantine_sha256: str,
) -> dict:
    record = {
        "schema_version": 1,
        "record_id": uuid.uuid4().hex,
        "evaluated_at": evaluated_at,
        "input_hash": source_sha256,
        "decision": "WARN",
        "shadow_mode": False,
        "shadow_verdict": None,
        "exit_code": 2,
        "policy_version": MAINTENANCE_POLICY_VERSION,
        "rule_hits": [{
            "rule_id": MAINTENANCE_RULE_ID,
            "severity": "WARN",
            "detail": (
                f"quarantined_lines={quarantined_lines}; "
                f"quarantine_sha256={quarantine_sha256}"
            ),
        }],
    }
    errors = sorted(_AUDIT_VALIDATOR.iter_errors(record), key=lambda e: list(e.path))
    if errors:
        raise AuditMaintenanceError(5, "internal error: maintenance record invalid")
    return record


def repair_audit(path, *, now: datetime | None = None) -> dict:
    """Rescan under lock, quarantine bad lines, rewrite kept bytes + one marker.

    Missing/non-regular targets return 4 with no filesystem side effects.
    For an existing regular log, a `.lock` sidecar may be created so repair
    shares the same lock as append/verify. Does not modify audit content when
    the snapshot is already clean.
    """
    path = Path(path)
    _require_regular_audit_file(path)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    def _tx() -> dict:
        data = _read_audit_bytes_locked(path)
        source_sha256 = hashlib.sha256(data).hexdigest()
        valid, issues, kept, rows = _scan_audit_bytes(data)
        if not issues:
            return _maintenance_result(
                "repair",
                "clean",
                valid,
                0,
                [],
                source_sha256=source_sha256,
            )
        created_at = now.astimezone(timezone.utc).replace(microsecond=0)
        created_at_text = created_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        quarantine_doc = {
            "format_version": 1,
            "created_at": created_at_text,
            "source_sha256": source_sha256,
            "issues": rows,
        }
        quarantine_bytes = (
            json.dumps(quarantine_doc, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False) + "\n"
        ).encode("utf-8")
        qpath = _publish_quarantine_file(path, created_at, quarantine_bytes)
        quarantine_sha256 = hashlib.sha256(quarantine_bytes).hexdigest()
        marker = _build_maintenance_record(
            evaluated_at=created_at_text,
            source_sha256=source_sha256,
            quarantined_lines=len(issues),
            quarantine_sha256=quarantine_sha256,
        )
        marker_line = json.dumps(
            marker, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ) + "\n"
        rewritten = _join_kept_with_marker(kept, marker_line)
        try:
            _atomic_replace_bytes(path, rewritten)
        except OSError as exc:
            raise AuditMaintenanceError(
                5, f"error: audit replace failed ({type(exc).__name__})"
            ) from exc
        return _maintenance_result(
            "repair",
            "repaired",
            valid,
            len(issues),
            issues,
            quarantine_name=qpath.name,
            quarantine_sha256=quarantine_sha256,
            source_sha256=source_sha256,
        )

    try:
        return _locked(path, _tx)
    except AuditMaintenanceError:
        raise
    except OSError as exc:
        raise AuditMaintenanceError(5, f"error: audit lock failed ({type(exc).__name__})") from exc
