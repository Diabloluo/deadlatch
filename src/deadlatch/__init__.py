"""deadlatch 核心包。

-A：S-1 决策合成、S-2 异常捕获、S-7 数值摄取、规则接口、R1。
-B：R2–R12 全部真实实现、Guard.from_policy、CLI、标准规则注册表。
"""

from ._decimal import DecimalInputError, as_decimal
from .engine import GuardEngine
from .guard import Guard, load_policy_file
from .model import Order, Policy, Portfolio, Result
from .rules.base import Rule, RuleContext, RuleOutcome
from .rules.kill_switch import KillSwitchRule
from .rules.registry import RULE_IDS, MANDATORY_RULE_IDS, standard_rule_registry

__all__ = [
    "DecimalInputError",
    "as_decimal",
    "GuardEngine",
    "Guard",
    "load_policy_file",
    "Order",
    "Policy",
    "Portfolio",
    "Result",
    "Rule",
    "RuleContext",
    "RuleOutcome",
    "KillSwitchRule",
    "RULE_IDS",
    "MANDATORY_RULE_IDS",
    "standard_rule_registry",
]
