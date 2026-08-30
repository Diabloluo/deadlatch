"""规则注册表（-B §2.1：固定、确定的 12 条 v0.1 规则注册表）。

- 规则次序固定（R1→R12），同一输入同一时钟 → Result.to_dict() 字节级一致；
- 启用/禁用映射表（rules-spec.md §0.4 NEW-4/NEW-6）：可选规则配置键缺失时
  必须列入 policy.acknowledged_disabled（policy loader 校验，exit 4）；
  强制/恒启用规则不可禁用；
- Guard.from_policy(...) 通过 standard_rule_registry() 装载完整规则集，
  不允许调用方漏装规则；引擎构造期的强制规则与唯一性校验不可绕过。
"""

from typing import Any

# 12 条规则 ID 的固定集合（policy.schema.json acknowledged_disabled enum 同源）
RULE_IDS = (
    "kill_switch",
    "input_validity",
    "max_order_quantity",
    "max_order_value",
    "max_symbol_exposure",
    "max_total_exposure",
    "cash_margin_check",
    "max_daily_loss",
    "max_drawdown",
    "order_time_validity",
    "data_freshness",
    "missing_data_fail_closed",
)

# 强制 / 恒启用规则：缺失即非法配置（exit 4），永不可列入 acknowledged_disabled
MANDATORY_RULE_IDS = (
    "kill_switch",
    "input_validity",
    "max_daily_loss",
    "max_drawdown",
    "order_time_validity",
    "data_freshness",
    "missing_data_fail_closed",
)

# 恒启用规则（无配置键）：R2 / R12 恒启用（规则本体即校验器）
ALWAYS_ON_RULE_IDS = ("input_validity", "missing_data_fail_closed")

# 可选规则 → 配置键映射表（§0.4：判定“缺失”与“矛盾”的唯一依据）
# cash_margin_check 需要两个键同时存在才启用（任一缺失须整体承认禁用）
_OPTIONAL_KEYS = {
    "max_order_quantity": ("limits.max_order_quantity",),
    "max_order_value": ("limits.max_order_value",),
    "max_symbol_exposure": ("limits.max_symbol_exposure_ratio",),
    "max_total_exposure": ("limits.max_total_exposure_ratio",),
    "cash_margin_check": ("limits.min_cash", "limits.max_options_margin_ratio"),
}


def rule_config_keys(rule_id: str) -> tuple[str, ...]:
    """规则 ID → 配置键路径（空元组 = 恒启用/无配置键）。"""
    return _OPTIONAL_KEYS.get(rule_id, ())


def _has_key(policy: Any, dotted: str) -> bool:
    obj: Any = policy
    if hasattr(policy, "data") and isinstance(policy.data, dict):
        obj = policy.data
    for part in dotted.split("."):
        if not isinstance(obj, dict) or part not in obj:
            return False
        obj = obj[part]
    return True


def is_rule_enabled(rule_id: str, policy: Any) -> bool:
    """启用判定（§0.4 映射表）：配置键存在（且被 loader 校验未与禁用声明矛盾）→ 启用。"""
    if rule_id in ALWAYS_ON_RULE_IDS:
        return True
    keys = rule_config_keys(rule_id)
    if not keys:
        # kill_switch / max_daily_loss / max_drawdown / order_time_validity /
        # data_freshness 为强制规则：配置键必填（policy 校验保证），视为恒启用
        return True
    return all(_has_key(policy, k) for k in keys)


def standard_rule_registry() -> list:
    """固定次序的 12 条 v0.1 标准规则注册表（Guard.from_policy 装载的完整规则集）。"""
    # 函数级导入避免 registry ↔ 规则类循环依赖
    from .cash_margin_check import CashMarginCheckRule
    from .data_freshness import DataFreshnessRule
    from .input_validity import InputValidityRule
    from .kill_switch import KillSwitchRule
    from .max_daily_loss import MaxDailyLossRule
    from .max_drawdown import MaxDrawdownRule
    from .max_order_quantity import MaxOrderQuantityRule
    from .max_order_value import MaxOrderValueRule
    from .max_symbol_exposure import MaxSymbolExposureRule
    from .max_total_exposure import MaxTotalExposureRule
    from .missing_data_fail_closed import MissingDataFailClosedRule
    from .order_time_validity import OrderTimeValidityRule

    return [
        KillSwitchRule(),            # R1
        InputValidityRule(),         # R2
        MaxOrderQuantityRule(),      # R3
        MaxOrderValueRule(),         # R4
        MaxSymbolExposureRule(),     # R5
        MaxTotalExposureRule(),      # R6
        CashMarginCheckRule(),       # R7
        MaxDailyLossRule(),          # R8
        MaxDrawdownRule(),           # R9
        OrderTimeValidityRule(),     # R10
        DataFreshnessRule(),         # R11
        MissingDataFailClosedRule(), # R12
    ]
