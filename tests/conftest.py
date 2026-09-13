"""pytest 共享夹具（-B）。全部标的使用虚构代码（AAA/BETA）。

时间策略（-B §2.5：测试不得随墙上时间自然变红）：
- NOW 在 conftest 导入时取真实 UTC 时刻，全模块共享（一次运行内恒定）；
- 所有 fixture 时间戳由 NOW 派生（订单 = NOW−1s，快照 = NOW−61s），
  因此无论何时运行，未显式传 now 的测试（引擎默认取墙钟 ≈ NOW）与
  显式传 now=NOW 的测试都保持新鲜，不会因日期推移触发 R10/R11；
- 需要精确时长的测试用 timedelta 显式覆盖（见 test_rules_time.py）。
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from deadlatch import GuardEngine, Order, Policy, Portfolio
from deadlatch.rules.base import Rule
from deadlatch.rules.data_freshness import DataFreshnessRule
from deadlatch.rules.input_validity import InputValidityRule
from deadlatch.rules.kill_switch import KillSwitchRule
from deadlatch.rules.max_daily_loss import MaxDailyLossRule
from deadlatch.rules.max_drawdown import MaxDrawdownRule
from deadlatch.rules.missing_data_fail_closed import MissingDataFailClosedRule
from deadlatch.rules.order_time_validity import OrderTimeValidityRule
from deadlatch.rules.registry import MANDATORY_RULE_IDS, standard_rule_registry

# 固定评估时刻（本运行内恒定）：conftest 导入时刻的 UTC 整秒
NOW = datetime.now(timezone.utc).replace(microsecond=0)

# 强制规则 → 类映射（make_engine 自动补齐用；registry.MANDATORY_RULE_IDS 为唯一来源）
_MANDATORY_RULE_CLASSES = {
    "kill_switch": KillSwitchRule,
    "input_validity": InputValidityRule,
    "max_daily_loss": MaxDailyLossRule,
    "max_drawdown": MaxDrawdownRule,
    "order_time_validity": OrderTimeValidityRule,
    "data_freshness": DataFreshnessRule,
    "missing_data_fail_closed": MissingDataFailClosedRule,
}


def _ts(**delta) -> str:
    """NOW 偏移 → RFC3339 UTC 字符串（无小数秒）。"""
    return (NOW + timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _policy(**overrides) -> dict:
    data = {
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
    data.update(overrides)
    return data


@pytest.fixture
def policy() -> Policy:
    return Policy.from_dict(_policy())


@pytest.fixture
def policy_off() -> Policy:
    return Policy.from_dict(_policy())


@pytest.fixture
def policy_full() -> Policy:
    return Policy.from_dict(_policy(kill_switch="full"))


@pytest.fixture
def policy_reduce_only() -> Policy:
    return Policy.from_dict(_policy(kill_switch="reduce_only"))


@pytest.fixture
def policy_shadow_full() -> Policy:
    return Policy.from_dict(_policy(mode="shadow", kill_switch="full"))


def stock_order(**overrides) -> Order:
    data = {
        "schema_version": 2,
        "symbol": "AAA",
        "instrument_type": "stock",
        "side": "buy",
        "quantity": 20,
        "price": 190.0,
        "order_type": "limit",
        "currency": "USD",
        "created_at": _ts(seconds=-1),
    }
    data.update(overrides)
    return Order.from_dict(data)


def option_order(**overrides) -> Order:
    data = {
        "schema_version": 2,
        "symbol": "AAA 260918P00190000",
        "instrument_type": "option",
        "side": "sell_to_open",
        "quantity": 10,
        "price": 2.5,
        "order_type": "limit",
        "currency": "USD",
        "created_at": _ts(seconds=-1),
        "option": {
            "underlying": "AAA",
            "expiry": "2026-09-18",
            "strike": 190.0,
            "right": "put",
            "multiplier": 100,
        },
    }
    data.update(overrides)
    return Order.from_dict(data)


def portfolio(**overrides) -> Portfolio:
    data = {
        "schema_version": 3,
        "equity": 123456.78,
        "cash": 30000.0,
        "day_start_equity": 125000.0,
        "peak_equity": 128000.0,
        "daily_pnl": -1200.5,
        "drawdown_ratio": 0.0355,
        "snapshot_at": _ts(seconds=-61),
        "base_currency": "USD",
        "positions": [],
    }
    data.update(overrides)
    return Portfolio.from_dict(data)


def make_engine(policy: Policy, rules: list[Rule]) -> GuardEngine:
    """注入规则并自动补齐缺失的强制规则（registry.MANDATORY_RULE_IDS 唯一来源）。"""
    ids = {r.rule_id for r in rules}
    full = list(rules)
    for rid in MANDATORY_RULE_IDS:
        if rid not in ids:
            full.append(_MANDATORY_RULE_CLASSES[rid]())
    return GuardEngine(policy, rules=full)


def make_standard(policy: Policy) -> GuardEngine:
    """完整标准 12 条规则集（Guard.from_policy 同款装载）。"""
    return GuardEngine(policy, rules=standard_rule_registry())


def full_policy(**overrides) -> Policy:
    """全部规则启用的 policy（acknowledged_disabled 为空，所有可选键配置齐全）。

    limits= 覆盖时按键合并（其余键保留）；其他顶层键直接覆盖。
    """
    data = {
        "schema_version": 2,
        "version": "1.0.0",
        "mode": "enforce",
        "base_currency": "USD",
        "kill_switch": "off",
        "acknowledged_disabled": [],
        "limits": {
            "max_order_quantity": 500,
            "max_order_value": 5000.0,
            "max_symbol_exposure_ratio": 0.10,
            "max_total_exposure_ratio": 0.60,
            "min_cash": 0.0,
            "max_options_margin_ratio": 0.35,
            "max_daily_loss_ratio": 0.03,
            "max_drawdown_ratio": 0.15,
            "max_order_age_seconds": 300,
            "max_snapshot_age_seconds": 300,
        },
    }
    lim = overrides.pop("limits", None)
    if lim:
        data["limits"].update(lim)
    data.update(overrides)
    return Policy.from_dict(data)


def fresh_order(**overrides) -> Order:
    """与 NOW 匹配的新鲜订单（created_at = NOW − 1s）。"""
    data = {
        "schema_version": 2,
        "symbol": "AAA",
        "instrument_type": "stock",
        "side": "buy",
        "quantity": 20,
        "price": 190.0,
        "order_type": "limit",
        "currency": "USD",
        "created_at": _ts(seconds=-1),
    }
    data.update(overrides)
    return Order.from_dict(data)


def fresh_portfolio(**overrides) -> Portfolio:
    """与 NOW 匹配的新鲜快照（snapshot_at = NOW − 61s）。"""
    data = {
        "schema_version": 3,
        "equity": 123456.78,
        "cash": 30000.0,
        "day_start_equity": 125000.0,
        "peak_equity": 128000.0,
        "daily_pnl": -1200.5,
        "drawdown_ratio": 0.0355,
        "snapshot_at": _ts(seconds=-61),
        "base_currency": "USD",
        "positions": [],
    }
    data.update(overrides)
    return Portfolio.from_dict(data)


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture(autouse=True)
def _audit_path_tmp(tmp_path, monkeypatch):
    """审计写入隔离（ §3.3）：Guard 审计 JSONL 落在 tmp_path，绝不写用户真实目录。"""
    monkeypatch.setenv("DEADLATCH_AUDIT_PATH", str(tmp_path / "audit.jsonl"))


# ----  T10：确定性 Hypothesis profile（显式注册并加载）----
# derandomize=True 固定种子（不依赖随机偶然性）；database=None 禁用示例数据库；
# deadline=None 避免慢机器上假失败。pyproject 的 [tool.hypothesis] 对 derandomize
# 不生效，因此在此注册为唯一事实来源。
from hypothesis import Phase, settings  # noqa: E402

settings.register_profile(
    "deadlatch-deterministic",
    derandomize=True,
    database=None,
    deadline=None,
    max_examples=100,
    phases=(Phase.generate, Phase.shrink),
)
settings.load_profile("deadlatch-deterministic")


@pytest.fixture
def order_buy() -> Order:
    return stock_order()


@pytest.fixture
def order_put_sell_open() -> Order:
    return option_order()
