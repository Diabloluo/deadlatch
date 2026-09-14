"""R8 — max_daily_loss（每日亏损熔断）。

daily_loss_ratio = daily_pnl / day_start_equity（即时 Decimal 计算，不缓存）。
触发：daily_loss_ratio <= −limits.max_daily_loss_ratio → BLOCK（达到即触发）。
day_start_equity <= 0 → 分母非法，由 R12 判为 BLOCK（exit 3）。
数据缺失 → R12。平仓豁免（§0.2）。
"""

from decimal import Decimal

from ..direction import is_close_order
from ..exposure import business_number
from .base import Rule, RuleContext, RuleOutcome
from .registry import is_rule_enabled


class MaxDailyLossRule(Rule):
    rule_id = "max_daily_loss"

    def evaluate(self, ctx: RuleContext) -> RuleOutcome:
        out = RuleOutcome(rule_id=self.rule_id)
        if not is_rule_enabled(self.rule_id, ctx.policy):
            return out
        if is_close_order(ctx.order, ctx.portfolio, ctx.policy, ctx.now):
            out.evidence.append({"name": "exempted", "value": "close"})
            return out
        limit = business_number(ctx.policy.limits.get("max_daily_loss_ratio"))
        daily_pnl = business_number(ctx.portfolio.data.get("daily_pnl"))
        day_start = business_number(ctx.portfolio.data.get("day_start_equity"))
        if limit is None or daily_pnl is None or day_start is None:
            return out  # 数据缺失 → R12
        if day_start <= 0:
            return out  # 分母非法 → R12（docs/rules-spec.md R8 §4）
        ratio = daily_pnl / day_start
        out.evidence.append({"name": "daily_pnl", "value": str(daily_pnl)})
        out.evidence.append({"name": "day_start_equity", "value": str(day_start)})
        out.evidence.append({"name": "daily_loss_ratio", "value": str(ratio)})
        out.evidence.append({"name": "limit", "value": str(limit)})
        if ratio <= -limit:
            out.violations.append(
                {
                    "rule_id": self.rule_id,
                    "severity": "BLOCK",
                    "detail": f"每日亏损比例 {ratio} <= -上限 {-limit}（达到即触发）",
                }
            )
        return out
