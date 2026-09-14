"""R10 — order_time_validity（订单时间有效性）。

触发：(now − created_at) > limits.max_order_age_seconds → BLOCK（恰好相等 → PASS）。
未来时间防护：created_at − now > 300（时钟偏差容忍）→ BLOCK；偏差 ≤ 300s 按 age=0。
不豁免（陈旧订单任何方向都不可执行）。时间比较用 UTC 纪元秒（整数）。
"""

from .._timeutil import parse_rfc3339, to_epoch_seconds
from .base import Rule, RuleContext, RuleOutcome
from .registry import is_rule_enabled

_FUTURE_SKEW_ALLOWANCE_SECONDS = 300


class OrderTimeValidityRule(Rule):
    rule_id = "order_time_validity"

    def evaluate(self, ctx: RuleContext) -> RuleOutcome:
        out = RuleOutcome(rule_id=self.rule_id)
        if not is_rule_enabled(self.rule_id, ctx.policy):
            return out
        limit = ctx.policy.limits.get("max_order_age_seconds")
        created = parse_rfc3339(ctx.order.data.get("created_at", ""))
        if limit is None or ctx.now is None:
            return out  # 配置非法由 loader 拦截；now 缺失时防御性跳过
        if created is None:
            # created_at 无法解析（极端时区/非法日期）→ 时间有效性
            # 不可判定 → fail-closed BLOCK（不得静默跳过造成错误 PASS）
            out.violations.append(
                {
                    "rule_id": self.rule_id,
                    "severity": "BLOCK",
                    "detail": "订单 created_at 无法解析（时间有效性不可判定，fail-closed）",
                }
            )
            return out
        now_epoch = to_epoch_seconds(ctx.now)
        created_epoch = to_epoch_seconds(created)
        age = now_epoch - created_epoch
        out.evidence.append({"name": "created_at", "value": created.isoformat()})
        out.evidence.append({"name": "now", "value": str(now_epoch)})
        out.evidence.append({"name": "age_seconds", "value": age})
        out.evidence.append({"name": "limit", "value": limit})
        if created_epoch - now_epoch > _FUTURE_SKEW_ALLOWANCE_SECONDS:
            out.evidence.append({"name": "future_skew_seconds", "value": created_epoch - now_epoch})
            out.violations.append(
                {
                    "rule_id": self.rule_id,
                    "severity": "BLOCK",
                    "detail": f"订单创建时间在未来（超 {_FUTURE_SKEW_ALLOWANCE_SECONDS}s 容忍）",
                }
            )
            return out
        if age > limit:
            out.violations.append(
                {
                    "rule_id": self.rule_id,
                    "severity": "BLOCK",
                    "detail": f"订单已过期 {age}s > 上限 {limit}s",
                }
            )
        return out
