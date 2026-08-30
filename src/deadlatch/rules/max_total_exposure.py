"""R6 — max_total_exposure（总敞口比例，毛敞口）。

触发：post_total_gross / portfolio.equity > limits.max_total_exposure_ratio
（恰好相等 → PASS）。
- pre_total_gross = Σ 全部持仓敞口绝对值（多头+空头都计入毛敞口；v0.1 不做多腿合并）；
- 单笔敞口口径同 R5（期权 sell_to_open 用行权价）；
- equity <= 0 → BLOCK（fail-closed）；平仓豁免（§0.2）。
"""

from decimal import Decimal

from ..direction import infer_order_direction
from ..exposure import business_number, order_exposure_delta, position_exposure
from .base import Rule, RuleContext, RuleOutcome
from .registry import is_rule_enabled


class MaxTotalExposureRule(Rule):
    rule_id = "max_total_exposure"

    def evaluate(self, ctx: RuleContext) -> RuleOutcome:
        out = RuleOutcome(rule_id=self.rule_id)
        if not is_rule_enabled(self.rule_id, ctx.policy):
            return out
        if infer_order_direction(ctx.order, ctx.portfolio, ctx.policy, ctx.now) == "close":
            out.evidence.append({"name": "exempted", "value": "close"})
            return out

        limit = business_number(ctx.policy.limits.get("max_total_exposure_ratio"))
        equity = business_number(ctx.portfolio.equity)
        if limit is None or equity is None:
            return out  # 防御：配置非法/数据缺失由 loader/R12 处理
        if equity <= 0:
            out.violations.append(
                {
                    "rule_id": self.rule_id,
                    "severity": "BLOCK",
                    "detail": f"账户权益 {equity} <= 0，无法计算总敞口比例（分母非法，fail-closed）",
                }
            )
            out.evidence.append({"name": "equity", "value": str(equity)})
            out.evidence.append({"name": "limit", "value": str(limit)})
            return out

        pre = Decimal("0")
        for pos in ctx.portfolio.positions:
            exp = position_exposure(pos)
            if exp is not None:
                pre += exp  # 持仓敞口恒为非负（毛敞口口径）

        delta = order_exposure_delta(
            ctx.order, infer_order_direction(ctx.order, ctx.portfolio, ctx.policy, ctx.now)
        )
        if delta is None:
            return out  # 防御：R2/R12 已拦截
        order_delta = abs(delta)
        post = pre + order_delta
        ratio = post / equity

        out.evidence.append({"name": "pre_total_gross", "value": str(pre)})
        out.evidence.append({"name": "order_delta", "value": str(order_delta)})
        out.evidence.append({"name": "post_total_gross", "value": str(post)})
        out.evidence.append({"name": "equity", "value": str(equity)})
        out.evidence.append({"name": "ratio", "value": str(ratio)})
        out.evidence.append({"name": "limit", "value": str(limit)})
        if ctx.order.instrument_type == "option":
            mult = (ctx.order.option or {}).get("multiplier")
            if isinstance(mult, int):
                out.evidence.append({"name": "used_multiplier", "value": mult})

        if ratio > limit:
            out.violations.append(
                {
                    "rule_id": self.rule_id,
                    "severity": "BLOCK",
                    "detail": (
                        f"组合总敞口占比 {ratio} > 上限 {limit}"
                        f"（post_total_gross {post} / equity {equity}）"
                    ),
                }
            )
        return out
