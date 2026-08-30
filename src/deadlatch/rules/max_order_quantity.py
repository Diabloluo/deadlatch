"""R3 — max_order_quantity（单笔数量上限）。

触发：order.quantity > limits.max_order_quantity（恰好相等 → PASS）。
数量口径统一（股/张），期权不需要乘数。平仓豁免（§0.2）。
"""

from ..direction import is_close_order
from ..exposure import business_int
from .base import Rule, RuleContext, RuleOutcome
from .registry import is_rule_enabled


class MaxOrderQuantityRule(Rule):
    rule_id = "max_order_quantity"

    def evaluate(self, ctx: RuleContext) -> RuleOutcome:
        out = RuleOutcome(rule_id=self.rule_id)
        if not is_rule_enabled(self.rule_id, ctx.policy):
            return out
        if is_close_order(ctx.order, ctx.portfolio, ctx.policy, ctx.now):
            out.evidence.append({"name": "exempted", "value": "close"})
            return out
        limit = ctx.policy.limits.get("max_order_quantity")
        qty = business_int(ctx.order.quantity)
        if limit is None or qty is None:
            return out  # 防御：配置非法/订单非法由 loader/R2 拦截
        out.evidence.append({"name": "triggered_value", "value": qty})
        out.evidence.append({"name": "limit", "value": limit})
        if qty > limit:
            out.violations.append(
                {
                    "rule_id": self.rule_id,
                    "severity": "BLOCK",
                    "detail": f"单笔数量 {qty} > 上限 {limit}",
                }
            )
        return out
