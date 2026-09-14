"""测试桩规则（仅用于驱动 S-1/S-2 全部分支，非产品规则）。

说明：测试桩提供两条路径（恒 PASS / 恒抛异常）；为覆盖 S-1 真值表
优先级 3/4（违规/警告）与"多条同时违规聚合"，补充 WarnRule 与
ViolationRule 两条测试专用桩——kill_switch 仅能产出 BLOCK 违规，
无法单独驱动警告与聚合分支。
"""

from .base import Rule, RuleContext, RuleOutcome


class AlwaysPassRule(Rule):
    rule_id = "stub_always_pass"

    def evaluate(self, ctx: RuleContext) -> RuleOutcome:
        return RuleOutcome(rule_id=self.rule_id)


class BoomRule(Rule):
    """恒抛异常：驱动 S-2 异常捕获与 exit 5 路径。"""

    rule_id = "stub_boom"

    def evaluate(self, ctx: RuleContext) -> RuleOutcome:
        raise RuntimeError("stub boom: intentional rule failure")


class WarnRule(Rule):
    rule_id = "stub_warn"

    def evaluate(self, ctx: RuleContext) -> RuleOutcome:
        return RuleOutcome(
            rule_id=self.rule_id,
            warnings=[{"rule_id": self.rule_id, "severity": "WARN", "detail": "stub warning"}],
        )


class ViolationRule(Rule):
    rule_id = "stub_violation"

    def evaluate(self, ctx: RuleContext) -> RuleOutcome:
        return RuleOutcome(
            rule_id=self.rule_id,
            violations=[
                {"rule_id": self.rule_id, "severity": "BLOCK", "detail": "stub violation"}
            ],
        )
