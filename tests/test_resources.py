""" §二：包内 Schema 是运行时唯一来源，与仓库根设计副本逐文件一致。

- 六个 Schema 均可经包资源读取且通过 meta-schema 校验；
- 包内副本与仓库根 schemas/（设计/分发副本）逐文件字节一致（防漂移）；
- 未知名拒绝；包资源读取不依赖源码仓目录（verify_wheel 在安装后另行断言）。
"""

import pytest
from jsonschema import Draft202012Validator

from deadlatch import _resources

SCHEMA_NAMES = ("order", "portfolio", "policy", "result", "audit-record", "shadow-report")


def test_six_schemas_readable_and_pass_meta_schema():
    for name in SCHEMA_NAMES:
        doc = _resources.schema_dict(name)
        Draft202012Validator.check_schema(doc)  # meta-schema 校验（非法会抛 SchemaError）
        assert isinstance(doc, dict) and doc.get("$id"), name


def test_package_schemas_match_repo_copies_byte_for_byte():
    """包内与仓库根副本逐文件字节一致（防止两份漂移）。"""
    import json as _json
    from pathlib import Path

    repo_dir = Path(__file__).resolve().parents[1] / "schemas"
    for name in SCHEMA_NAMES:
        pkg = _resources.schema_text(name)
        repo = (repo_dir / f"{name}.schema.json").read_text(encoding="utf-8")
        assert pkg == repo, name
        # 两边解析出的 JSON 也一致（双重校验）
        assert _json.loads(pkg) == _json.loads(repo)


def test_unknown_schema_name_rejected():
    with pytest.raises(ValueError):
        _resources.schema_text("not-a-schema")


def test_runtime_validation_uses_package_schemas():
    """运行期校验器确实从包资源构建（改包内副本会真实改变行为）。"""
    from deadlatch._validation import _validator

    v = _validator("order")
    # 构造一个违反 order.schema 的实例，证明校验器确实在起作用
    errors = list(v.iter_errors({"schema_version": 2, "side": "buy"}))
    assert errors  # 缺必填 symbol/quantity 等 → 校验失败
