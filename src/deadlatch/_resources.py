"""包内资源统一读取。

wheel 安装后运行期 Schema 的唯一入口：使用 importlib.resources 读取包数据
（支持普通目录安装与 zip 安装语义），禁止各模块自行拼接
``Path(__file__).resolve().parents[...] / "schemas"``。

仓库根 ``schemas/`` 仅作可读设计/分发副本（tools/validate_schemas.py 校验用）；
两份内容一致性由 tests/test_resources.py 逐文件字节断言保证。
"""

import json
from importlib import resources

_PACKAGE = "deadlatch"  # 包名固定（importlib.resources 要求非 None）
_SCHEMA_DIR = "schemas"

SCHEMA_NAMES = (
    "order",
    "portfolio",
    "policy",
    "result",
    "audit-record",
    "shadow-report",
    "audit-maintenance-result",
)


def _schema_ref(name: str):
    if name not in SCHEMA_NAMES:
        raise ValueError(f"未知 Schema: {name!r}（可用: {SCHEMA_NAMES}）")
    return resources.files(_PACKAGE).joinpath(_SCHEMA_DIR, f"{name}.schema.json")


def schema_text(name: str) -> str:
    """返回 {name}.schema.json 的 UTF-8 文本（目录/zip 安装均可用）。"""
    return _schema_ref(name).read_text(encoding="utf-8")


def schema_dict(name: str) -> dict:
    """解析为 dict（运行时契约对象）。"""
    return json.loads(schema_text(name))
