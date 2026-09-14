"""deadlatch 核心包。

Decision synthesis, exception capture, Decimal intake, rule interface, R1.
R2–R12, Guard.from_policy, CLI, and the standard rule registry.
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
