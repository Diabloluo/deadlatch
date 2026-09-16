"""本地审计 JSONL 集合。

逻辑基准路径（--audit-path / DEADLATCH_AUDIT_PATH / ~/.deadlatch/audit.jsonl）
表示整个集合，不再是新记录的唯一承载文件：

- audit.jsonl              可选 legacy v1 单文件，只读兼容，不自动迁移或删除
- audit-YYYY-MM-DD.jsonl   v0.1.2 UTC 日分片；新记录只追加到写入日分片
- audit.jsonl.lock         全集合共用的一把锁
- audit.jsonl.state.json   固定大小写入日期水位；append 前必须显式 init

v0.1.2 起新记录为 AuditRecord v2（prev_hash / record_hash 本地 SHA-256 链）。
哈希链是 tamper-evident，不是数字签名，也不是 tamper-proof。无外部不可变锚时，
删除当前最后一条或整个可见集合无法仅靠链内数据可靠区分。日期水位只约束遵守
协议的本地写入者，不是防篡改证明，也不能从日志重建已预留但未写入的日期。

热路径 append 只做：校验新记录、在集合锁内决定 UTC 写入日（默认时钟持锁后
采样；链头探测与分片选择共用这一次决策）、有界读取固定大小状态、按水位拒绝
日期倒退、必要时原子抬升水位、有界反向读取当天分片尾部（若无则探测固定 30
个日期候选 + 可选 legacy 尾部）、计算链头、追加一行并 flush/fsync。
不改写记录的 evaluated_at。禁止 readlines、全历史解析、整文件 hash、整文件
重写、无界目录扫描或在 append 中重建状态。30 天保留不再发生在 append；由
shadow report 入口与 `deadlatch audit prune` 按分片文件删除。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import stat
import uuid
from datetime import date, datetime, timedelta, timezone
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
_PRUNE_RESULT_VALIDATOR = Draft202012Validator(schema_dict("audit-prune-result"))
_WRITE_STATE_VALIDATOR = Draft202012Validator(schema_dict("audit-write-state"))
_STATE_RESULT_VALIDATOR = Draft202012Validator(schema_dict("audit-state-result"))

DEFAULT_AUDIT_PATH = Path.home() / ".deadlatch" / "audit.jsonl"

RETENTION_DAYS = 30
MAX_AUDIT_FILE_BYTES = 64 * 1024 * 1024
MAX_RECORD_BYTES = 64 * 1024
# One extra byte for the preceding JSONL delimiter so a max-size last line is
# distinguishable from a record truncated by the read window. Do not treat this
# as an arbitrary enlargement of the record cap.
TAIL_READ_BYTES = MAX_RECORD_BYTES + 1
MAX_STATE_BYTES = 4096
STATE_READ_LIMIT = MAX_STATE_BYTES + 1
STATE_PROTOCOL_VERSION = 1
MAX_ISSUE_ECHO = 100
AUDIT_STATE_MISSING = "audit_state_missing"
AUDIT_STATE_INVALID = "audit_state_invalid"
AUDIT_STATE_INCONSISTENT = "audit_state_inconsistent"
AUDIT_DATE_REGRESSION = "audit_date_regression"
AUDIT_STATE_IO_ERROR = "audit_state_io_error"
AUDIT_ADOPT_REQUIRED = "audit_adopt_required"
MAINTENANCE_POLICY_VERSION = "audit-maintenance/1"
MAINTENANCE_RULE_ID = "audit_repaired"
CHAIN_REFUSE_MESSAGE = "检测到链完整性问题、拒绝自动重链"
CHAIN_INTEGRITY_CODES = frozenset({
    "record_hash_mismatch",
    "prev_hash_mismatch",
    "duplicate_record_id",
    "schema_version_downgrade",
})
STRUCTURAL_CODES = frozenset({
    "invalid_utf8",
    "invalid_json",
    "schema_invalid",
    "evaluated_at_invalid",
})
QUARANTINE_REASON_CODES = (
    "invalid_utf8",
    "invalid_json",
    "schema_invalid",
    "evaluated_at_invalid",
)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_DATE_TOKEN = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _check_audit_size(path: Path) -> None:
    """审计文件载入前大小检查：超限 → AuditError（fail-closed，拒绝完整读取）。"""
    try:
        size = os.lstat(path).st_size
    except OSError:
        return
    if size > MAX_AUDIT_FILE_BYTES:
        raise AuditError(
            f"审计文件超过大小上限 {MAX_AUDIT_FILE_BYTES} 字节（实际 {size} 字节），拒绝读取"
        )


_SENSITIVE_PATTERN_RE = re.compile(
    r"(?i)"
    r"(authorization|proxy-authorization)\s*:\s*bearer\s+\S+"
    r"|\bbearer\s+[a-zA-Z0-9._~+/=-]{8,}"
    r"|\b(api[_-]?key|secret|passwd|password|access[_-]?token|auth[_-]?token|token)\b\s*[=:]\s*\S+"
    r"|\b(sk-|pk-|rk-)[a-zA-Z0-9_-]{12,}"
    r"|\b(?:cookie|set-cookie)\s*[:=][^\r\n]*"
    r"|\b(?:sessionid|session|auth)\b\s*=\s*[^\s;]+"
    r"|(?<![A-Za-z0-9_.~])(?:/[\w .\-]+){2,}"
)
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)symbol|underlying|account|token|api[_-]?key|secret|password|credential|bearer"
)


class AuditError(Exception):
    """审计读写/校验失败（fail-closed；调用方转可见降级或 exit 5）。"""

    def __init__(self, message: str, *, code: str | None = None, exit_code: int = 5):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


class AuditMaintenanceError(Exception):
    """verify/repair/prune 路径或内部失败。public_message 不得含路径、原文或凭据。"""

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
    return sorted(
        {v for v in out if isinstance(v, str) and len(v) >= 2 and not v.isdigit()},
        key=len,
        reverse=True,
    )


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


# ---------------------------------------------------------------- 哈希 / 分片路径

def canonical_record_bytes(record: dict) -> bytes:
    """规范化 JSON 字节：去掉 record_hash，sort_keys，紧凑分隔符，UTF-8。"""
    payload = {k: v for k, v in record.items() if k != "record_hash"}
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def compute_record_hash(record: dict) -> str:
    """SHA-256 小写十六进制。v2 record_hash 与 v1 过渡锚使用同一规范化。"""
    return hashlib.sha256(canonical_record_bytes(record)).hexdigest()


def chain_head_of(record: dict) -> str:
    """下一条记录应使用的 prev_hash。v2 用其 record_hash；v1 用完整记录摘要。"""
    if record.get("schema_version") == 2 and isinstance(record.get("record_hash"), str):
        return record["record_hash"]
    return compute_record_hash(record)


def utc_shard_path(base, when) -> Path:
    """逻辑基准路径 + UTC 日 → 分片路径。无后缀基准名补 `.jsonl`。"""
    base = Path(base)
    if isinstance(when, datetime):
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        day = when.astimezone(timezone.utc).date()
    elif isinstance(when, date):
        day = when
    else:
        raise TypeError("when must be datetime or date")
    token = day.isoformat()
    if base.suffix:
        return base.with_name(f"{base.stem}-{token}{base.suffix}")
    return base.with_name(f"{base.name}-{token}.jsonl")


def _shard_name_re(base: Path) -> re.Pattern[str]:
    if base.suffix:
        return re.compile(
            rf"^{re.escape(base.stem)}-(\d{{4}}-\d{{2}}-\d{{2}}){re.escape(base.suffix)}$"
        )
    return re.compile(rf"^{re.escape(base.name)}-(\d{{4}}-\d{{2}}-\d{{2}})\.jsonl$")


def _parse_utc_date(token: str) -> date | None:
    m = _DATE_TOKEN.fullmatch(token)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _is_symlink(path: Path) -> bool:
    try:
        return stat.S_ISLNK(os.lstat(path).st_mode)
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _is_regular_file(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode)


def _reject_symlink(path: Path, kind: str) -> None:
    if _is_symlink(path):
        raise AuditError(f"error: {kind} is a symbolic link")


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def _state_path(path: Path) -> Path:
    return path.with_name(path.name + ".state.json")


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _list_shards(base: Path) -> list[tuple[date, Path]]:
    """严格按基准名匹配合法 UTC 日期分片。不用宽泛 glob。忽略符号链接。"""
    parent = base.parent
    pattern = _shard_name_re(base)
    out: list[tuple[date, Path]] = []
    try:
        names = os.listdir(parent)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise AuditError(f"审计目录不可读：{type(exc).__name__}") from exc
    for name in names:
        matched = pattern.fullmatch(name)
        if matched is None:
            continue
        day = _parse_utc_date(matched.group(1))
        if day is None:
            continue
        candidate = parent / name
        if not _is_regular_file(candidate):
            continue
        out.append((day, candidate))
    out.sort(key=lambda item: item[0])
    return out


def collection_exists(base) -> bool:
    """True when a legacy regular file, matching shard, or durable write state exists."""
    base = Path(base)
    if _is_regular_file(base):
        return True
    if _list_shards(base):
        return True
    return _is_regular_file(_state_path(base))


def _retention_cutoff_date(now: datetime) -> date:
    today = now.astimezone(timezone.utc).date()
    return today - timedelta(days=RETENTION_DAYS - 1)


def _in_retention_window(day: date, now: datetime) -> bool:
    today = now.astimezone(timezone.utc).date()
    cutoff = _retention_cutoff_date(now)
    return cutoff <= day <= today


def _shards_in_retention(base: Path, now: datetime) -> list[Path]:
    """Shards in the visible collection: in-window plus future (day >= cutoff).

    Expired shards may exist unpruned; they are not collection members for
    append predecessor, read/report, verify, or repair chain checks.
    """
    cutoff = _retention_cutoff_date(now)
    return [shard for day, shard in _list_shards(base) if day >= cutoff]


# ---------------------------------------------------------------- 锁

def _locked(path: Path, fn, *, create_parent: bool = True):
    """跨进程文件锁内执行 fn（独立锁文件，避免 replace 换 inode 的竞态）。"""
    lock_file = _lock_path(path)
    if create_parent:
        lock_file.parent.mkdir(parents=True, exist_ok=True)
    if _is_symlink(lock_file):
        raise AuditError("error: audit lock is a symbolic link")
    if fcntl is not None:
        with open(lock_file, "a+", encoding="utf-8") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                if _is_symlink(lock_file):
                    raise AuditError("error: audit lock is a symbolic link")
                return fn()
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    return fn()


def _locked_maintenance(path: Path, fn):
    try:
        return _locked(path, fn, create_parent=False)
    except AuditMaintenanceError:
        raise
    except AuditError as exc:
        if exc.code:
            _raise_maintenance_from_state(exc)
        msg = str(exc)
        if "symbolic link" in msg:
            raise AuditMaintenanceError(4, "error: audit lock is a symbolic link") from exc
        raise AuditMaintenanceError(5, "error: audit lock failed") from exc
    except OSError as exc:
        raise AuditMaintenanceError(5, f"error: audit lock failed ({type(exc).__name__})") from exc


def _state_error(code: str, message: str, *, exit_code: int = 5) -> AuditError:
    return AuditError(message, code=code, exit_code=exit_code)


def _reject_existing_wrong_type(path: Path, *, kind: str) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _state_error(AUDIT_STATE_IO_ERROR, f"error: audit {kind} I/O failed") from exc
    if stat.S_ISLNK(info.st_mode):
        raise _state_error(AUDIT_STATE_INVALID, f"error: audit {kind} is a symbolic link")
    if not stat.S_ISREG(info.st_mode):
        raise _state_error(AUDIT_STATE_INVALID, f"error: audit {kind} is not a regular file")


def _open_nofollow(path: Path, flags: int, mode: int = 0o600) -> int:
    open_flags = flags
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        open_flags |= nofollow
    else:
        _reject_existing_wrong_type(path, kind="write state")
    try:
        fd = os.open(str(path), open_flags, mode)
    except OSError as exc:
        raise _state_error(AUDIT_STATE_IO_ERROR, "error: audit write state I/O failed") from exc
    try:
        info = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise _state_error(AUDIT_STATE_IO_ERROR, "error: audit write state I/O failed") from exc
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise _state_error(AUDIT_STATE_INVALID, "error: audit write state is not a regular file")
    return fd


def _object_pairs_no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _encode_state(base_name: str, reserved: date | None) -> bytes:
    payload = {
        "protocol_version": STATE_PROTOCOL_VERSION,
        "base_name": base_name,
        "reserved_through": None if reserved is None else reserved.isoformat(),
    }
    errors = sorted(_WRITE_STATE_VALIDATOR.iter_errors(payload), key=lambda e: list(e.path))
    if errors:
        raise _state_error(AUDIT_STATE_INVALID, "error: audit write state is invalid")
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    data = (blob + "\n").encode("utf-8")
    if len(data) > MAX_STATE_BYTES:
        raise _state_error(AUDIT_STATE_INVALID, "error: audit write state is invalid")
    return data


def _decode_state(raw: bytes, base: Path) -> date | None:
    if len(raw) > MAX_STATE_BYTES:
        raise _state_error(AUDIT_STATE_INVALID, "error: audit write state is invalid")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _state_error(AUDIT_STATE_INVALID, "error: audit write state is invalid") from exc
    try:
        payload = json.loads(text, object_pairs_hook=_object_pairs_no_duplicates)
    except Exception as exc:
        raise _state_error(AUDIT_STATE_INVALID, "error: audit write state is invalid") from exc
    errors = sorted(_WRITE_STATE_VALIDATOR.iter_errors(payload), key=lambda e: list(e.path))
    if errors:
        raise _state_error(AUDIT_STATE_INVALID, "error: audit write state is invalid")
    if payload.get("protocol_version") != STATE_PROTOCOL_VERSION:
        raise _state_error(AUDIT_STATE_INVALID, "error: audit write state is invalid")
    if payload.get("base_name") != base.name:
        raise _state_error(AUDIT_STATE_INVALID, "error: audit write state is invalid")
    token = payload.get("reserved_through")
    if token is None:
        return None
    day = _parse_utc_date(token)
    if day is None:
        raise _state_error(AUDIT_STATE_INVALID, "error: audit write state is invalid")
    return day


def _read_state_bytes(state_path: Path) -> bytes:
    fd = _open_nofollow(state_path, os.O_RDONLY)
    try:
        buf = b""
        while len(buf) < STATE_READ_LIMIT:
            chunk = os.read(fd, STATE_READ_LIMIT - len(buf))
            if not chunk:
                break
            buf += chunk
    except OSError as exc:
        raise _state_error(AUDIT_STATE_IO_ERROR, "error: audit write state I/O failed") from exc
    finally:
        os.close(fd)
    if len(buf) > MAX_STATE_BYTES:
        raise _state_error(AUDIT_STATE_INVALID, "error: audit write state is invalid")
    return buf


def _load_state(base: Path) -> date | None:
    state_path = _state_path(base)
    _reject_existing_wrong_type(state_path, kind="write state")
    if not _is_regular_file(state_path):
        raise _state_error(AUDIT_STATE_MISSING, "error: audit write state is missing")
    return _decode_state(_read_state_bytes(state_path), base)


def _load_state_if_present(base: Path) -> tuple[bool, date | None]:
    state_path = _state_path(base)
    try:
        info = os.lstat(state_path)
    except FileNotFoundError:
        return False, None
    except OSError as exc:
        raise _state_error(AUDIT_STATE_IO_ERROR, "error: audit write state I/O failed") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise _state_error(AUDIT_STATE_INVALID, "error: audit write state is invalid")
    return True, _decode_state(_read_state_bytes(state_path), base)


def _fsync_existing_state(base: Path) -> None:
    state_path = _state_path(base)
    fd = _open_nofollow(state_path, os.O_RDWR)
    try:
        os.fsync(fd)
    except OSError as exc:
        raise _state_error(AUDIT_STATE_IO_ERROR, "error: audit write state I/O failed") from exc
    finally:
        os.close(fd)
    try:
        _fsync_dir(state_path.parent)
    except OSError as exc:
        raise _state_error(AUDIT_STATE_IO_ERROR, "error: audit write state I/O failed") from exc


def _publish_state(base: Path, reserved: date | None, *, overwrite: bool) -> None:
    state_path = _state_path(base)
    parent = state_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    _reject_existing_wrong_type(state_path, kind="write state")
    if not overwrite and _is_regular_file(state_path):
        raise _state_error(AUDIT_STATE_INVALID, "error: audit write state already exists")
    data = _encode_state(base.name, reserved)
    tmp = parent / f".{secrets.token_hex(16)}.state.tmp"
    fd = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = _open_nofollow(tmp, flags, 0o600)
        if hasattr(os, "fchmod"):
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
        written = 0
        while written < len(data):
            n = os.write(fd, data[written:])
            if n <= 0:
                raise _state_error(AUDIT_STATE_IO_ERROR, "error: audit write state I/O failed")
            written += n
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(str(tmp), str(state_path))
        tmp = None  # type: ignore[assignment]
        _fsync_dir(parent)
    except AuditError:
        raise
    except OSError as exc:
        raise _state_error(AUDIT_STATE_IO_ERROR, "error: audit write state I/O failed") from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp is not None:
            try:
                if Path(tmp).exists():
                    Path(tmp).unlink()
            except OSError:
                pass


def _inspect_matching_names(base: Path) -> tuple[date | None, bool, bool]:
    """Return (max shard date, has_legacy, has_matching_name).

    Matching names that exist as symlink/dir/other non-regular files are errors.
    """
    parent = base.parent
    pattern = _shard_name_re(base)
    max_day: date | None = None
    has_match = False
    try:
        names = os.listdir(parent)
    except FileNotFoundError:
        names = []
    except OSError as exc:
        raise _state_error(AUDIT_STATE_IO_ERROR, "error: audit write state I/O failed") from exc
    for name in names:
        matched = pattern.fullmatch(name)
        if matched is None:
            continue
        has_match = True
        day = _parse_utc_date(matched.group(1))
        if day is None:
            raise _state_error(AUDIT_STATE_INVALID, "error: audit write state is invalid")
        candidate = parent / name
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _state_error(AUDIT_STATE_IO_ERROR, "error: audit write state I/O failed") from exc
        if not stat.S_ISREG(info.st_mode):
            raise _state_error(
                AUDIT_STATE_INVALID,
                "error: audit shard name is not a regular file",
            )
        if max_day is None or day > max_day:
            max_day = day
    try:
        info = os.lstat(base)
    except FileNotFoundError:
        return max_day, False, has_match
    except OSError as exc:
        raise _state_error(AUDIT_STATE_IO_ERROR, "error: audit write state I/O failed") from exc
    if stat.S_ISLNK(info.st_mode):
        raise _state_error(AUDIT_STATE_INVALID, "error: audit target is a symbolic link")
    if stat.S_ISDIR(info.st_mode):
        raise _state_error(AUDIT_STATE_INVALID, "error: audit target is a directory", exit_code=4)
    if not stat.S_ISREG(info.st_mode):
        raise _state_error(AUDIT_STATE_INVALID, "error: audit target is not a regular file")
    return max_day, True, has_match


def _assert_watermark_covers(reserved: date | None, max_day: date | None) -> None:
    if max_day is None:
        return
    if reserved is None or reserved < max_day:
        raise _state_error(
            AUDIT_STATE_INCONSISTENT,
            "error: audit write state is inconsistent",
        )


def _assert_state_consistent_if_present(base: Path) -> date | None:
    present, reserved = _load_state_if_present(base)
    if not present:
        return None
    max_day = None
    for day, _shard in _list_shards(base):
        if max_day is None or day > max_day:
            max_day = day
    _assert_watermark_covers(reserved, max_day)
    return reserved


def _state_result(status: str, reserved: date | None, error_code: str | None) -> dict:
    payload = {
        "schema_version": 1,
        "operation": "init",
        "status": status,
        "reserved_through": None if reserved is None else reserved.isoformat(),
        "error_code": error_code,
    }
    errors = sorted(_STATE_RESULT_VALIDATOR.iter_errors(payload), key=lambda e: list(e.path))
    if errors:
        raise _state_error(AUDIT_STATE_INVALID, "error: audit write state is invalid")
    return payload


def initialize_audit_state(path, *, adopt_existing: bool = False) -> dict:
    """One-time maintenance: publish a conservative write-date watermark.

    Not part of the append hot path. Directory enumeration is allowed here.
    """
    path = Path(path)
    _reject_symlink(_lock_path(path), "audit lock")

    def _tx() -> dict:
        _reject_symlink(path, "audit target")
        _reject_symlink(_lock_path(path), "audit lock")
        state_path = _state_path(path)
        present, reserved = False, None
        try:
            present, reserved = _load_state_if_present(path)
        except AuditError as exc:
            if exc.code in (AUDIT_STATE_INVALID, AUDIT_STATE_IO_ERROR):
                raise
            raise
        max_day, has_legacy, has_match = _inspect_matching_names(path)
        existing_logs = has_legacy or has_match
        if present:
            _assert_watermark_covers(reserved, max_day)
            return _state_result("unchanged", reserved, None)
        if existing_logs and not adopt_existing:
            raise _state_error(
                AUDIT_ADOPT_REQUIRED,
                "error: existing audit collection requires --adopt-existing",
                exit_code=4,
            )
        new_h = max_day if has_match else None
        _publish_state(path, new_h, overwrite=False)
        return _state_result("initialized", new_h, None)

    try:
        return _locked(path, _tx)
    except AuditError:
        raise
    except OSError as exc:
        raise _state_error(AUDIT_STATE_IO_ERROR, "error: audit write state I/O failed") from exc


def _raise_maintenance_from_state(exc: AuditError) -> None:
    code = exc.code or AUDIT_STATE_IO_ERROR
    message = str(exc)
    exit_code = 5 if exc.exit_code == 5 else exc.exit_code
    if code == AUDIT_ADOPT_REQUIRED:
        exit_code = 4
    raise AuditMaintenanceError(exit_code, message) from exc


# ---------------------------------------------------------------- 有界尾读

def _read_last_complete_line(path: Path) -> bytes | None:
    """读取最后一个完整 JSONL 行（不含换行）。截断或超长 → AuditError。空文件 → None。

    一条记录只允许单一终止换行（LF 或 CRLF）。尾部多余空行（LF / CRLF /
    连续空行）出现在已有完整记录之后时视为未知尾部：拒绝读取，不得跳过空行
    继续追加。仅含空白的空 JSONL（例如单独一个换行）视为空集合。有界窗口
    必须能看到最长合法行的前导分隔符（或文件起点）；窗口截断与超长同样
    fail-closed。
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise AuditError("error: audit target is a symbolic link")
    if not stat.S_ISREG(info.st_mode):
        raise AuditError("error: audit target is not a regular file")
    size = info.st_size
    if size == 0:
        return None
    window = TAIL_READ_BYTES
    start = max(0, size - window)
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.lseek(fd, start, os.SEEK_SET)
        buf = b""
        remaining = size - start
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            buf += chunk
            remaining -= len(chunk)
    finally:
        os.close(fd)
    if not buf.endswith(b"\n"):
        raise AuditError("审计分片尾部截断，拒绝继续链接")
    parts = buf.split(b"\n")
    last_idx = len(parts) - 2
    if last_idx < 0:
        return None
    i = last_idx
    while i >= 0 and not parts[i].rstrip(b"\r").strip():
        i -= 1
    if i < 0:
        if start > 0:
            raise AuditError("审计分片尾部无法确认链头，拒绝继续链接")
        return None
    if i != last_idx:
        raise AuditError("审计分片尾部存在多余空行，拒绝继续链接")
    last_raw = parts[i]
    last = last_raw.rstrip(b"\r")
    if i == 0 and start > 0:
        raise AuditError("审计记录超过单条大小上限，拒绝读取尾部")
    if len(last_raw) + 1 > MAX_RECORD_BYTES:
        raise AuditError(
            f"审计记录超过单条大小上限 {MAX_RECORD_BYTES} 字节，拒绝写入"
        )
    return last


def _parse_tail_record(raw: bytes, *, from_shard: bool) -> dict:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AuditError("审计分片尾部不是合法 UTF-8，拒绝继续链接") from exc
    try:
        rec = json.loads(text)
    except Exception as exc:
        raise AuditError("审计分片尾部 JSON 损坏，拒绝继续链接") from exc
    errors = sorted(_AUDIT_VALIDATOR.iter_errors(rec), key=lambda e: list(e.path))
    if errors:
        raise AuditError(
            f"审计分片尾部未通过 audit-record.schema，拒绝继续链接: {errors[0].message}"
        )
    if from_shard and rec.get("schema_version") != 2:
        raise AuditError("审计分片尾部非法降级 schema_version，拒绝继续链接")
    if rec.get("schema_version") == 2:
        expected = compute_record_hash(rec)
        if rec.get("record_hash") != expected:
            raise AuditError("审计分片尾部 record_hash 不匹配，拒绝继续链接")
    return rec


def _probe_previous_record(base: Path, now: datetime) -> dict | None:
    """O(1) 链头探测：当天分片尾 → 固定 29 个既往日期候选 → legacy 尾。无目录扫描。"""
    today = now.astimezone(timezone.utc).date()
    for offset in range(RETENTION_DAYS):
        day = today - timedelta(days=offset)
        shard = utc_shard_path(base, day)
        if not _is_regular_file(shard):
            if _is_symlink(shard):
                raise AuditError("error: audit target is a symbolic link")
            continue
        raw = _read_last_complete_line(shard)
        if raw is None:
            continue
        return _parse_tail_record(raw, from_shard=True)
    if _is_symlink(base):
        raise AuditError("error: audit target is a symbolic link")
    if _is_regular_file(base):
        raw = _read_last_complete_line(base)
        if raw is not None:
            return _parse_tail_record(raw, from_shard=False)
    return None


# ---------------------------------------------------------------- 记录构造

def build_audit_record(order, portfolio, policy, result) -> dict:
    """构造 v2 业务字段；prev_hash 暂为 null，record_hash 按该值计算。

    append_audit 持锁后覆盖链头。调用方自报的 hash 不得被视为可信。
    """
    sensitive = collect_sensitive_values(order, portfolio)
    hits = []
    for v in result.violations:
        hits.append({"rule_id": v.get("rule_id", ""), "severity": "BLOCK", "detail": v.get("detail", "")})
    for w in result.warnings:
        hits.append({"rule_id": w.get("rule_id", ""), "severity": "WARN", "detail": w.get("detail", "")})
    record = {
        "schema_version": 2,
        "record_id": uuid.uuid4().hex,
        "evaluated_at": result.evaluated_at,
        "input_hash": (result.evidence or {}).get("input_hash", ""),
        "decision": result.decision,
        "shadow_mode": bool(result.shadow_mode),
        "shadow_verdict": result.shadow_verdict,
        "exit_code": result.exit_code,
        "policy_version": sanitize_text(str(policy.version), sensitive),
        "rule_hits": sanitize_hits(hits, sensitive),
        "prev_hash": None,
    }
    record["record_hash"] = compute_record_hash(record)
    errors = sorted(_AUDIT_VALIDATOR.iter_errors(record), key=lambda e: list(e.path))
    if errors:
        raise AuditError(f"审计记录未通过 audit-record.schema：{errors[0].message}")
    return record


def _incoming_business_record(record: dict) -> dict:
    """去掉调用方自报链头，强制升级为 v2 业务字段。"""
    if not isinstance(record, dict):
        raise AuditError("审计记录未通过 audit-record.schema，事务未写盘: not an object")
    business = {k: v for k, v in record.items() if k not in ("prev_hash", "record_hash")}
    business["schema_version"] = 2
    return business


def _finalize_v2(business: dict, prev_hash: str | None) -> dict:
    rec = dict(business)
    rec["schema_version"] = 2
    rec["prev_hash"] = prev_hash
    rec["record_hash"] = compute_record_hash(rec)
    errors = sorted(_AUDIT_VALIDATOR.iter_errors(rec), key=lambda e: list(e.path))
    if errors:
        raise AuditError(
            f"审计记录未通过 audit-record.schema，事务未写盘: {errors[0].message}"
        )
    return rec


def _append_line_fsync(path: Path, line: bytes) -> None:
    _reject_symlink(path, "audit target")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    try:
        fd = os.open(str(path), flags, 0o600)
    except OSError as exc:
        raise AuditError(f"审计打开失败（{type(exc).__name__}）") from exc
    try:
        if hasattr(os, "fchmod"):
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
        try:
            written = 0
            while written < len(line):
                n = os.write(fd, line[written:])
                if n <= 0:
                    raise AuditError("审计写入发生 short write，拒绝报告成功")
                written += n
            os.fsync(fd)
        except AuditError:
            raise
        except OSError as exc:
            raise AuditError(f"审计写入失败（{type(exc).__name__}）") from exc
    finally:
        os.close(fd)


def append_audit(path, record: dict, now: datetime | None = None) -> dict:
    """有界 O(1) 追加一条 v2 记录到当天 UTC 分片。不清理、不重写历史。

    UTC 分片日期在集合锁内决定：未传 now 时持锁后采样默认时钟；链头探测与
    分片选择使用同一次时间决策。不改写记录的 evaluated_at。写入日若早于已
    耐久预留的水位，拒绝写入且日志与状态字节不变。状态缺失不得扫描目录重建。

    返回 {"removed": 0, "future": 0}（保留字段兼容旧调用方；append 不再 prune）。
    """
    path = Path(path)
    requested_now = now

    def _tx() -> dict:
        write_now = requested_now if requested_now is not None else datetime.now(timezone.utc)
        if write_now.tzinfo is None:
            write_now = write_now.replace(tzinfo=timezone.utc)
        _reject_symlink(path, "audit target")
        _reject_symlink(_lock_path(path), "audit lock")
        write_day = write_now.astimezone(timezone.utc).date()
        reserved = _load_state(path)
        if reserved is not None and write_day < reserved:
            raise _state_error(
                AUDIT_DATE_REGRESSION,
                "error: audit write date is earlier than reserved watermark",
            )
        business = _incoming_business_record(record)
        previous = _probe_previous_record(path, write_now)
        prev_hash = None if previous is None else chain_head_of(previous)
        final = _finalize_v2(business, prev_hash)
        payload = json.dumps(final, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        line = (payload + "\n").encode("utf-8")
        if len(line) > MAX_RECORD_BYTES:
            raise AuditError(
                f"审计记录超过单条大小上限 {MAX_RECORD_BYTES} 字节，拒绝写入"
            )
        shard = utc_shard_path(path, write_now)
        _reject_symlink(shard, "audit target")
        path.parent.mkdir(parents=True, exist_ok=True)
        current = 0
        shard_existed = _is_regular_file(shard)
        if shard_existed:
            current = os.lstat(shard).st_size
            if current > MAX_AUDIT_FILE_BYTES:
                raise AuditError(
                    f"审计文件超过大小上限 {MAX_AUDIT_FILE_BYTES} 字节（实际 {current} 字节），拒绝读取"
                )
        final_size = current + len(line)
        if final_size > MAX_AUDIT_FILE_BYTES:
            raise AuditError(
                f"审计文件追加后大小 {final_size} 字节超过上限 {MAX_AUDIT_FILE_BYTES}，"
                f"拒绝写入（原文件未改动）"
            )
        if reserved is None or write_day > reserved:
            _publish_state(path, write_day, overwrite=True)
        else:
            _fsync_existing_state(path)
        try:
            _append_line_fsync(shard, line)
        except AuditError:
            raise
        except OSError as exc:
            raise AuditError(f"审计写入失败（{type(exc).__name__}）") from exc
        if not shard_existed:
            try:
                _fsync_dir(path.parent)
            except OSError as exc:
                raise AuditError(f"目录 fsync 失败（{type(exc).__name__}）") from exc
        return {"removed": 0, "future": 0}

    return _locked(path, _tx)


def _prune_result(status: str, removed: int, kept: int, future: int) -> dict:
    payload = {
        "schema_version": 1,
        "operation": "prune",
        "status": status,
        "removed_segments": removed,
        "kept_segments": kept,
        "future_segments": future,
    }
    errors = sorted(_PRUNE_RESULT_VALIDATOR.iter_errors(payload), key=lambda e: list(e.path))
    if errors:
        raise AuditError(f"prune 结果未通过 audit-prune-result.schema：{errors[0].message}")
    return payload


def prune_audit(path, now: datetime | None = None, retention_days: int = RETENTION_DAYS) -> dict:
    """删除保留窗口之外的完整 UTC 分片。不读分片内容，不删除 legacy 或他人状态 tmp。"""
    del retention_days  # 窗口固定为 RETENTION_DAYS 个日历日；保留参数以免旧调用方崩溃
    path = Path(path)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if not path.parent.exists():
        return _prune_result("clean", 0, 0, 0)

    def _tx() -> dict:
        _reject_symlink(path, "audit target")
        try:
            _assert_state_consistent_if_present(path)
        except AuditError as exc:
            if exc.code:
                raise
            raise
        today = now.astimezone(timezone.utc).date()
        cutoff = _retention_cutoff_date(now)
        shards = _list_shards(path)
        removed = 0
        kept = 0
        future = 0
        to_delete: list[Path] = []
        for day, shard in shards:
            _reject_symlink(shard, "audit target")
            if day > today:
                future += 1
                kept += 1
            elif day < cutoff:
                to_delete.append(shard)
            else:
                kept += 1
        for shard in to_delete:
            try:
                os.unlink(shard)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise AuditError(f"分片删除失败（{type(exc).__name__}）") from exc
            removed += 1
        if to_delete:
            try:
                _fsync_dir(path.parent)
            except OSError as exc:
                raise AuditError(f"目录 fsync 失败（{type(exc).__name__}）") from exc
        status = "pruned" if removed else "clean"
        return _prune_result(status, removed, kept, future)

    return _locked(path, _tx)


# ---------------------------------------------------------------- 读取（供报告）

def _iter_read_segments(base: Path, now: datetime) -> list[Path]:
    segments: list[Path] = []
    if _is_symlink(base):
        raise AuditError("error: audit target is a symbolic link")
    if _is_regular_file(base):
        segments.append(base)
    for shard in _shards_in_retention(base, now):
        _reject_symlink(shard, "audit target")
        segments.append(shard)
    return segments


def read_audit_records(path, now: datetime | None = None) -> list[dict]:
    """读取 legacy + 保留窗口内（含未来）分片。

    同一把锁、同一快照内校验 Schema、摘要、链链接、重复 ID 与版本位置后才返回。
    malformed / schema 非法 / 链完整性失败 → AuditError，不返回成功历史数据。
    """
    path = Path(path)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if not collection_exists(path):
        raise AuditError("审计文件不可读：FileNotFoundError")

    def _tx() -> list[dict]:
        _assert_state_consistent_if_present(path)
        records: list[dict] = []
        chain = _ChainState()
        segments = _iter_read_segments(path, now)
        if not segments:
            return records
        for segment in segments:
            _check_audit_size(segment)
            try:
                data = Path(segment).read_bytes()
            except OSError as exc:
                raise AuditError(f"审计文件不可读：{type(exc).__name__}") from exc
            if not data:
                continue
            in_legacy_file = segment == path
            lines = data.splitlines(keepends=True)
            for lineno, raw in enumerate(lines, 1):
                body = raw[:-1] if raw.endswith(b"\n") else raw
                if raw.endswith(b"\r\n"):
                    body = raw[:-2]
                if not body.strip():
                    continue
                try:
                    text = body.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise AuditError(
                        f"审计文件第 {lineno} 行 JSON 损坏（fail-closed，不静默跳过）: UnicodeDecodeError"
                    ) from exc
                try:
                    rec = json.loads(text)
                except Exception as exc:
                    raise AuditError(
                        f"审计文件第 {lineno} 行 JSON 损坏（fail-closed，不静默跳过）: {type(exc).__name__}"
                    ) from exc
                errors = sorted(_AUDIT_VALIDATOR.iter_errors(rec), key=lambda e: list(e.path))
                if errors:
                    raise AuditError(
                        f"审计文件第 {lineno} 行未通过 audit-record.schema（fail-closed）: {errors[0].message}"
                    )
                reasons = chain.consume(rec, in_legacy_file=in_legacy_file)
                if reasons:
                    raise AuditError(
                        f"审计集合哈希链校验失败（{reasons[0]}，fail-closed），拒绝返回记录"
                    )
                records.append(rec)
        return records

    return _locked(path, _tx)


def collection_notes(path, now: datetime | None = None) -> dict:
    """报告附注用：legacy 未加链条数、链覆盖条数、未来分片数。"""
    path = Path(path)
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(timezone.utc).date()
    future_segments = sum(1 for day, _ in _list_shards(path) if day > today)
    return {"future_segments": future_segments}


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


def _scan_audit_bytes(data: bytes, segment_name: str | None = None) -> tuple[int, list[dict], list[bytes], list[dict]]:
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
        item = {"line_number": lineno, "reason_code": reason}
        if segment_name:
            item["segment_name"] = segment_name
        issues.append(item)
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


def _read_segment_bytes(path: Path) -> bytes:
    try:
        _check_audit_size(path)
    except AuditError:
        raise AuditMaintenanceError(5, "error: audit file exceeds size limit") from None
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise AuditMaintenanceError(5, f"error: audit read failed ({type(exc).__name__})") from exc


def _verify_segments(base: Path) -> tuple[list[Path], bool]:
    """Return (ordered segments, has_legacy). Missing collection → error 4. No mkdir."""
    legacy = False
    try:
        info = os.lstat(base)
    except FileNotFoundError:
        info = None
    except OSError as exc:
        raise AuditMaintenanceError(5, f"error: audit stat failed ({type(exc).__name__})") from exc
    if info is not None:
        if stat.S_ISLNK(info.st_mode):
            raise AuditMaintenanceError(4, "error: audit target is a symbolic link")
        if stat.S_ISDIR(info.st_mode):
            raise AuditMaintenanceError(4, "error: audit target is a directory")
        if not stat.S_ISREG(info.st_mode):
            raise AuditMaintenanceError(4, "error: audit target is not a regular file")
        legacy = True
    shards = _list_shards(base)
    for _day, shard in shards:
        if _is_symlink(shard):
            raise AuditMaintenanceError(4, "error: audit target is a symbolic link")
    if not legacy and not shards:
        state_path = _state_path(base)
        try:
            info = os.lstat(state_path)
        except FileNotFoundError:
            raise AuditMaintenanceError(4, "error: audit file not found") from None
        except OSError as exc:
            raise AuditMaintenanceError(5, f"error: audit stat failed ({type(exc).__name__})") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise AuditMaintenanceError(5, "error: audit write state is invalid")
        return [], False
    segments: list[Path] = []
    if legacy:
        segments.append(base)
    segments.extend(shard for _day, shard in shards)
    return segments, legacy


class _ChainState:
    """Shared hash-chain / version-location state for verify and trusted reads.

    v1 is legal only in the legacy baseline file and only before any v2 record.
    Daily shards are v2-only; a later shard or write must not start at a lower
    schema version than prior in-window records. Incoming append still upgrades
    business v1 to v2; that upgrade is not a downgrade.
    """

    def __init__(self) -> None:
        self.seen_ids: set[str] = set()
        self.expected_prev: str | None = None
        self.have_visible = False
        self.seen_v2 = False
        self.legacy_records = 0
        self.chained_records = 0
        self.first_hash: str | None = None
        self.last_hash: str | None = None

    def consume(self, rec: dict, *, in_legacy_file: bool) -> list[str]:
        reasons: list[str] = []
        rid = rec.get("record_id")
        if isinstance(rid, str):
            if rid in self.seen_ids:
                reasons.append("duplicate_record_id")
            else:
                self.seen_ids.add(rid)
        version = rec.get("schema_version")
        if version == 1:
            if self.seen_v2 or not in_legacy_file:
                reasons.append("schema_version_downgrade")
                self.have_visible = True
                return reasons
            self.legacy_records += 1
            self.expected_prev = compute_record_hash(rec)
            self.have_visible = True
            return reasons
        self.seen_v2 = True
        actual_hash = rec.get("record_hash")
        computed = compute_record_hash(rec)
        if (
            actual_hash != computed
            or not isinstance(actual_hash, str)
            or not _HEX64.fullmatch(actual_hash)
        ):
            reasons.append("record_hash_mismatch")
            self.have_visible = True
            self.expected_prev = actual_hash if isinstance(actual_hash, str) else computed
            return reasons
        prev = rec.get("prev_hash")
        if self.have_visible:
            if prev != self.expected_prev:
                reasons.append("prev_hash_mismatch")
        else:
            if prev is not None and not (isinstance(prev, str) and _HEX64.fullmatch(prev)):
                reasons.append("prev_hash_mismatch")
        self.chained_records += 1
        if self.first_hash is None:
            self.first_hash = actual_hash
        self.last_hash = actual_hash
        self.expected_prev = actual_hash
        self.have_visible = True
        return reasons


def _scan_collection(base: Path, now: datetime) -> dict:
    _legacy_segments, legacy = _verify_segments(base)
    del _legacy_segments
    segments: list[Path] = []
    if legacy:
        segments.append(base)
    segments.extend(_shards_in_retention(base, now))
    issues: list[dict] = []
    valid = 0
    chain = _ChainState()
    source_parts: list[bytes] = []
    scanned_rows: list[tuple[Path, int, bytes, str | None, dict | None]] = []

    for segment in segments:
        data = _read_segment_bytes(segment)
        source_parts.append(data)
        if not data:
            continue
        in_legacy_file = segment == base
        lines = data.splitlines(keepends=True)
        for lineno, raw in enumerate(lines, 1):
            reason, rec = _classify_audit_line(raw)
            scanned_rows.append((segment, lineno, raw, reason, rec))
            if reason is None and rec is None:
                continue
            if reason is not None:
                issues.append({
                    "line_number": lineno,
                    "reason_code": reason,
                    "segment_name": segment.name,
                })
                continue
            assert rec is not None
            valid += 1
            for code in chain.consume(rec, in_legacy_file=in_legacy_file):
                issues.append({
                    "line_number": lineno,
                    "reason_code": code,
                    "segment_name": segment.name,
                })

    return {
        "segments": segments,
        "valid": valid,
        "issues": issues,
        "legacy_records": chain.legacy_records,
        "chained_records": chain.chained_records,
        "chain_start": chain.first_hash,
        "chain_head": chain.last_hash,
        "source_sha256": hashlib.sha256(b"".join(source_parts)).hexdigest() if len(segments) == 1 else hashlib.sha256(source_parts[0] if source_parts else b"").hexdigest(),
        "rows": scanned_rows,
        "segment_bytes": {seg: data for seg, data in zip(segments, source_parts)},
    }


def verify_audit(path, now: datetime | None = None) -> dict:
    """Scan the visible audit collection without modifying it or writing quarantine."""
    path = Path(path)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    _verify_segments(path)

    def _tx() -> dict:
        try:
            _assert_state_consistent_if_present(path)
        except AuditError as exc:
            _raise_maintenance_from_state(exc)
        snap = _scan_collection(path, now)
        status = "invalid" if snap["issues"] else "clean"
        extra = {
            "source_sha256": snap["source_sha256"],
            "segments_scanned": len(snap["segments"]),
            "legacy_records": snap["legacy_records"],
            "chained_records": snap["chained_records"],
            "chain_start": snap["chain_start"],
            "chain_head": snap["chain_head"],
        }
        return _maintenance_result(
            "verify",
            status,
            snap["valid"],
            len(snap["issues"]),
            snap["issues"],
            **extra,
        )

    return _locked_maintenance(path, _tx)


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
    prev_hash: str | None,
) -> dict:
    record = {
        "schema_version": 2,
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
        "prev_hash": prev_hash,
    }
    record["record_hash"] = compute_record_hash(record)
    errors = sorted(_AUDIT_VALIDATOR.iter_errors(record), key=lambda e: list(e.path))
    if errors:
        raise AuditMaintenanceError(5, "internal error: maintenance record invalid")
    return record


def _last_trusted_hash(rows: list[tuple[Path, int, bytes, str | None, dict | None]]) -> str | None:
    last = None
    for _seg, _lineno, _raw, reason, rec in rows:
        if reason is None and rec is not None:
            last = chain_head_of(rec)
    return last


def repair_audit(path, *, now: datetime | None = None) -> dict:
    """Repair structural damage only. Chain integrity problems refuse relink."""
    path = Path(path)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    _verify_segments(path)

    def _tx() -> dict:
        try:
            present, reserved = _load_state_if_present(path)
            if present:
                max_day, _legacy, _has_match = _inspect_matching_names(path)
                _assert_watermark_covers(reserved, max_day)
        except AuditError as exc:
            _raise_maintenance_from_state(exc)
        snap = _scan_collection(path, now)
        issues = snap["issues"]
        extra = {
            "source_sha256": snap["source_sha256"],
            "segments_scanned": len(snap["segments"]),
            "legacy_records": snap["legacy_records"],
            "chained_records": snap["chained_records"],
            "chain_start": snap["chain_start"],
            "chain_head": snap["chain_head"],
        }
        if not issues:
            return _maintenance_result("repair", "clean", snap["valid"], 0, [], **extra)
        if any(item["reason_code"] in CHAIN_INTEGRITY_CODES for item in issues):
            return _maintenance_result(
                "repair", "invalid", snap["valid"], len(issues), issues, **extra
            )

        segments = snap["segments"]
        if not segments:
            return _maintenance_result("repair", "clean", snap["valid"], 0, [], **extra)
        last_segment = segments[-1]
        rows = snap["rows"]
        structural = [item for item in issues if item["reason_code"] in STRUCTURAL_CODES]
        shard_names = {p.name for p in segments if p != path}
        legacy_only = len(segments) == 1 and segments[0] == path
        if not legacy_only and not present:
            raise AuditMaintenanceError(5, "error: audit write state is missing")
        if not legacy_only and present:
            matched = _shard_name_re(path).fullmatch(last_segment.name)
            last_day = _parse_utc_date(matched.group(1)) if matched else None
            if last_day is not None and (reserved is None or reserved < last_day):
                raise AuditMaintenanceError(5, "error: audit write state is inconsistent")

        if not legacy_only:
            non_tail = []
            last_line_no = None
            for seg, lineno, _raw, reason, rec in rows:
                if seg == last_segment:
                    last_line_no = lineno
            for item in structural:
                if item.get("segment_name") != last_segment.name:
                    non_tail.append(item)
                    continue
                if last_line_no is None or item["line_number"] != last_line_no:
                    non_tail.append(item)
            if non_tail:
                return _maintenance_result(
                    "repair", "invalid", snap["valid"], len(issues), issues, **extra
                )

        created_at = now.astimezone(timezone.utc).replace(microsecond=0)
        created_at_text = created_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        if legacy_only:
            data = snap["segment_bytes"][path]
            valid, file_issues, kept, qrows = _scan_audit_bytes(data, path.name)
            source_sha256 = hashlib.sha256(data).hexdigest()
            quarantine_doc = {
                "format_version": 1,
                "created_at": created_at_text,
                "source_sha256": source_sha256,
                "issues": qrows,
            }
            quarantine_bytes = (
                json.dumps(quarantine_doc, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False) + "\n"
            ).encode("utf-8")
            qpath = _publish_quarantine_file(path, created_at, quarantine_bytes)
            quarantine_sha256 = hashlib.sha256(quarantine_bytes).hexdigest()
            prev_hash = None
            for raw in kept:
                reason, rec = _classify_audit_line(raw)
                if reason is None and rec is not None:
                    prev_hash = chain_head_of(rec)
            marker = _build_maintenance_record(
                evaluated_at=created_at_text,
                source_sha256=source_sha256,
                quarantined_lines=len(file_issues),
                quarantine_sha256=quarantine_sha256,
                prev_hash=prev_hash,
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
                len(file_issues),
                file_issues,
                quarantine_name=qpath.name,
                quarantine_sha256=quarantine_sha256,
                source_sha256=source_sha256,
                segments_scanned=1,
                legacy_records=sum(1 for r in [_classify_audit_line(x)[1] for x in kept] if r and r.get("schema_version") == 1),
                chained_records=1 + sum(1 for r in [_classify_audit_line(x)[1] for x in kept] if r and r.get("schema_version") == 2),
                chain_start=marker["record_hash"] if prev_hash is None and not kept else extra.get("chain_start") or marker["record_hash"],
                chain_head=marker["record_hash"],
            )

        # v2: isolate only the last line of the last shard
        data = snap["segment_bytes"][last_segment]
        source_sha256 = hashlib.sha256(data).hexdigest()
        lines = data.splitlines(keepends=True) if data else []
        if not lines:
            return _maintenance_result("repair", "clean", snap["valid"], 0, [], **extra)
        last_raw = lines[-1]
        reason, rec = _classify_audit_line(last_raw)
        if reason is None:
            return _maintenance_result("repair", "clean", snap["valid"], 0, [], **extra)
        try:
            _fsync_existing_state(path)
        except AuditError as exc:
            _raise_maintenance_from_state(exc)
        kept = lines[:-1]
        qrows = [{
            "line_number": len(lines),
            "reason_code": reason,
            "raw_base64": base64.b64encode(last_raw).decode("ascii"),
        }]
        quarantine_doc = {
            "format_version": 1,
            "created_at": created_at_text,
            "source_sha256": source_sha256,
            "issues": qrows,
        }
        quarantine_bytes = (
            json.dumps(quarantine_doc, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False) + "\n"
        ).encode("utf-8")
        qpath = _publish_quarantine_file(last_segment, created_at, quarantine_bytes)
        quarantine_sha256 = hashlib.sha256(quarantine_bytes).hexdigest()
        prev_hash = None
        for raw in kept:
            cr, rec2 = _classify_audit_line(raw)
            if cr is None and rec2 is not None:
                prev_hash = chain_head_of(rec2)
        if prev_hash is None:
            # last trusted may live in an earlier segment
            prev_hash = _last_trusted_hash(rows[:-1] if rows else [])
        marker = _build_maintenance_record(
            evaluated_at=created_at_text,
            source_sha256=source_sha256,
            quarantined_lines=1,
            quarantine_sha256=quarantine_sha256,
            prev_hash=prev_hash,
        )
        marker_line = json.dumps(
            marker, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ) + "\n"
        rewritten = _join_kept_with_marker(kept, marker_line)
        try:
            _atomic_replace_bytes(last_segment, rewritten)
        except OSError as exc:
            raise AuditMaintenanceError(
                5, f"error: audit replace failed ({type(exc).__name__})"
            ) from exc
        last_issue = {
            "line_number": len(lines),
            "reason_code": reason,
            "segment_name": last_segment.name,
        }
        return _maintenance_result(
            "repair",
            "repaired",
            snap["valid"],
            1,
            [last_issue],
            quarantine_name=qpath.name,
            quarantine_sha256=quarantine_sha256,
            source_sha256=source_sha256,
            segments_scanned=len(segments),
            legacy_records=snap["legacy_records"],
            chained_records=snap["chained_records"] + 1,
            chain_start=snap["chain_start"] or marker["record_hash"],
            chain_head=marker["record_hash"],
        )

    try:
        return _locked_maintenance(path, _tx)
    except AuditError as exc:
        if "symbolic link" in str(exc):
            raise AuditMaintenanceError(4, "error: audit target is a symbolic link") from exc
        raise AuditMaintenanceError(5, "error: audit lock failed") from exc
