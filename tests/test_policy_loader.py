"""Policy loader 测试：YAML/JSON 加载、版本门、acknowledged_disabled 矛盾、阈值合法性。

所有非法配置 → InputValidationError（CLI 映射 exit 4）。
"""

import json

import pytest
import yaml

from deadlatch import load_policy_file
from deadlatch._validation import InputValidationError


def _policy_dict(**overrides) -> dict:
    d = {
        "schema_version": 2,
        "version": "1.0.0",
        "mode": "enforce",
        "base_currency": "USD",
        "kill_switch": "off",
        "acknowledged_disabled": [
            "max_order_quantity",
            "max_order_value",
            "max_symbol_exposure",
            "max_total_exposure",
            "cash_margin_check",
        ],
        "limits": {
            "max_daily_loss_ratio": 0.03,
            "max_drawdown_ratio": 0.15,
            "max_order_age_seconds": 300,
            "max_snapshot_age_seconds": 300,
        },
    }
    d.update(overrides)
    return d


@pytest.fixture
def tmp_policy(tmp_path):
    def write(data: dict, suffix: str = ".yaml") -> str:
        p = tmp_path / f"policy{suffix}"
        if suffix == ".json":
            p.write_text(json.dumps(data), encoding="utf-8")
        else:
            p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return str(p)

    return write


def test_load_yaml(tmp_policy):
    path = tmp_policy(_policy_dict())
    pol = load_policy_file(path)
    assert pol.mode == "enforce"
    assert pol.kill_switch == "off"


def test_load_json(tmp_policy):
    path = tmp_policy(_policy_dict(), suffix=".json")
    pol = load_policy_file(path)
    assert pol.base_currency == "USD"


def test_missing_file_raises(tmp_path):
    with pytest.raises(InputValidationError):
        load_policy_file(str(tmp_path / "nope.yaml"))


def test_parse_error_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("{{{{ not yaml", encoding="utf-8")
    with pytest.raises(InputValidationError):
        load_policy_file(str(p))


def test_invalid_kill_switch_raises(tmp_policy):
    path = tmp_policy(_policy_dict(kill_switch="enable"))
    with pytest.raises(InputValidationError):
        load_policy_file(path)


def test_version_gate_raises(tmp_policy):
    path = tmp_policy(_policy_dict(schema_version=1))
    with pytest.raises(InputValidationError):
        load_policy_file(path)


def test_mandatory_limit_missing_raises(tmp_policy):
    d = _policy_dict()
    del d["limits"]["max_daily_loss_ratio"]
    with pytest.raises(InputValidationError):
        load_policy_file(tmp_policy(d))


def test_optional_missing_unacknowledged_raises(tmp_policy):
    # 可选规则键缺失且未承认 → 非法配置（NEW-4 主方向）
    d = _policy_dict()
    d["limits"]["max_order_quantity"] = 500  # 配置了 → 反而合法
    with pytest.raises(InputValidationError):
        load_policy_file(tmp_policy(d))


def test_optional_missing_acknowledged_ok(tmp_policy):
    # 默认 fixture：5 条可选规则全部承认禁用（键缺失 + 已承认）→ 合法
    pol = load_policy_file(tmp_policy(_policy_dict()))
    assert set(pol.acknowledged_disabled) == {
        "max_order_quantity", "max_order_value", "max_symbol_exposure",
        "max_total_exposure", "cash_margin_check",
    }


def test_contradiction_key_present_and_acknowledged_raises(tmp_policy):
    d = _policy_dict(acknowledged_disabled=["max_order_quantity"])
    d["limits"]["max_order_quantity"] = 500  # 键存在 + 承认禁用 = 矛盾 → exit 4
    with pytest.raises(InputValidationError):
        load_policy_file(tmp_policy(d))


def test_threshold_zero_raises(tmp_policy):
    d = _policy_dict()
    d["limits"]["max_daily_loss_ratio"] = 0  # NEW-1：0 为非法配置
    with pytest.raises(InputValidationError):
        load_policy_file(tmp_policy(d))
