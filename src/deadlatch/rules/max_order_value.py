"""R4 — max_order_value（单笔金额上限，现金流口径）。

order_value = price × qty（股票）；price × M × qty（期权，M 从输入读取）。
触发：order_value > limits.max_order_value（恰好相等 → PASS）。
注意：现金流口径，不是风险上限（裸卖期权 = 已收权利金；风险由 R5/R6 兜底）。
平仓豁免（§0.2）。
"""

from decimal import Decimal

from ..direction import is_close_order
from ..exposure import business_number, option_multiplier, order_cash_value
from .base import Rule, RuleContext, RuleOutcome
from .registry import is_rule_enabled


class MaxOrderValueRule(Rule):
    rule_id = "max_order_value"

    def evaluate(self, ctx: RuleContext) -> RuleOutcome:
        out = RuleOutcome(rule_id=self.rule_id)
        if not is_rule_enabled(self.rule_id, ctx.policy):
            return out
        if is_close_order(ctx.order, ctx.portfolio, ctx.policy, ctx.now):
            out.evidence.append({"name": "exempted", "value": "close"})
            return out
        limit = business_number(ctx.policy.limits.get("max_order_value"))
        value = order_cash_value(ctx.order)
        if limit is None or value is None:
            return out  # 防御：配置非法/订单非法由 loader/R2 拦截
        out.evidence.append({"name": "order_value", "value": str(value)})
        out.evidence.append({"name": "limit", "value": str(limit)})
        mult = option_multiplier(ctx.order)
        if mult is not None:
            out.evidence.append({"name": "used_multiplier", "value": mult})
        if value > limit:
            out.violations.append(
                {
                    "rule_id": self.rule_id,
                    "severity": "BLOCK",
                    "detail": f"单笔订单金额 {value} > 上限 {limit}",
                }
            )
        return out
