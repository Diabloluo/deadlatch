"""RFC3339 时间解析工具（R10/R11/R12 共用）。

Schema 强制 RFC3339 且带显式时区（pattern 约束）；本模块只负责解析成
aware datetime，失败返回 None（由 R12 判为业务数据缺失，R2 已先拦截
订单侧格式错误）。
"""

from datetime import datetime, timezone


def parse_rfc3339(value) -> datetime | None:
    """解析 RFC3339 字符串为 aware datetime。非法输入 → None。"""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def to_epoch_seconds(dt: datetime) -> int:
    """aware datetime → UTC 纪元秒（整数）。naive 视为 UTC。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())
