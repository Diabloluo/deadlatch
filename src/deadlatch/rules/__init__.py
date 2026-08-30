"""规则包。产品规则（R1...）与测试桩的注册入口。"""

from .base import Rule, RuleContext, RuleOutcome
from .kill_switch import KillSwitchRule

__all__ = ["Rule", "RuleContext", "RuleOutcome", "KillSwitchRule"]
