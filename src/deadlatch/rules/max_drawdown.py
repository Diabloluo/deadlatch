"""R9 — max_drawdown（账户回撤熔断）。

触发：drawdown_ratio >= limits.max_drawdown_ratio → BLOCK（达到即触发）。
drawdown_ratio 自洽校验（与 equity/peak_equity 偏差 > 0.5%）→ R12（防伪造快照）。
peak_equity <= 0 → BLOCK（数据非法）。数据缺失 → R12。平仓豁免（§0.2）。
"""

from ..direction import is_close_order
from ..exposure import business_number
from .base import Rule, RuleContext, RuleOutcome
from .registry import is_rule_enabled


class MaxDrawdownRule(Rule):
    rule_id = "max_drawdown"

    def evaluate(self, ctx: RuleContext) -> RuleOutcome:
        out = RuleOutcome(rule_id=self.rule_id)
        if not is_rule_enabled(self.rule_id, ctx.policy):
            return out
        if is_close_order(ctx.order, ctx.portfolio, ctx.policy, ctx.now):
            out.evidence.append({"name": "exempted", "value": "close"})
            return out
        limit = business_number(ctx.policy.limits.get("max_drawdown_ratio"))
        ddr = business_number(ctx.portfolio.data.get("drawdown_ratio"))
        peak = business_number(ctx.portfolio.data.get("peak_equity"))
        if limit is None or ddr is None or peak is None:
            return out  # 数据缺失 → R12
        if peak <= 0:
            out.violations.append(
                {
                    "rule_id": self.rule_id,
                    "severity": "BLOCK",
                    "detail": f"历史峰值权益 {peak} <= 0（数据非法）",
                }
            )
            return out
        out.evidence.append({"name": "drawdown_ratio", "value": str(ddr)})
        out.evidence.append({"name": "peak_equity", "value": str(peak)})
        out.evidence.append({"name": "limit", "value": str(limit)})
        if ddr >= limit:
            out.violations.append(
                {
                    "rule_id": self.rule_id,
                    "severity": "BLOCK",
                    "detail": f"账户回撤 {ddr} >= 上限 {limit}（达到即触发）",
                }
            )
        return out
