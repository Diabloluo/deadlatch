"""R5 — max_symbol_exposure（单标的持仓比例）。

触发：post_exposure(symbol_group) / portfolio.equity > limits.max_symbol_exposure_ratio
（恰好相等 → PASS）。
- symbol_group：正股按 symbol，期权按 option.underlying，同 underlying 合并；
- pre_exposure 由引擎按 §0.1 持仓敞口表自算（不信任快照敞口字段）；
- Δ(order) 关键：期权 sell_to_open 用 strike × M × qty（行权价口径，非权利金）；
- equity <= 0 → BLOCK（分母非法，fail-closed）；平仓豁免（§0.2）。
"""

from decimal import Decimal

from ..direction import infer_order_direction
from ..exposure import business_number, order_exposure_delta, position_exposure, position_group_key
from .base import Rule, RuleContext, RuleOutcome
from .registry import is_rule_enabled


class MaxSymbolExposureRule(Rule):
    rule_id = "max_symbol_exposure"

    def evaluate(self, ctx: RuleContext) -> RuleOutcome:
        out = RuleOutcome(rule_id=self.rule_id)
        if not is_rule_enabled(self.rule_id, ctx.policy):
            return out
        if infer_order_direction(ctx.order, ctx.portfolio, ctx.policy, ctx.now) == "close":
            out.evidence.append({"name": "exempted", "value": "close"})
            return out

        limit = business_number(ctx.policy.limits.get("max_symbol_exposure_ratio"))
        equity = business_number(ctx.portfolio.equity)
        if limit is None or equity is None:
            return out  # 防御：配置非法/数据缺失由 loader/R12 处理
        if equity <= 0:
            out.violations.append(
                {
                    "rule_id": self.rule_id,
                    "severity": "BLOCK",
                    "detail": f"账户权益 {equity} <= 0，无法计算敞口比例（分母非法，fail-closed）",
                }
            )
            out.evidence.append({"name": "equity", "value": str(equity)})
            out.evidence.append({"name": "limit", "value": str(limit)})
            return out

        group_key = _order_group_key(ctx.order)
        pre = Decimal("0")
        members: set[str] = set()
        for pos in ctx.portfolio.positions:
            if position_group_key(pos) == group_key:
                exp = position_exposure(pos)
                if exp is not None:
                    pre += exp
                    members.add(str(pos.get("symbol", "")))

        delta = order_exposure_delta(
            ctx.order, infer_order_direction(ctx.order, ctx.portfolio, ctx.policy, ctx.now)
        )
        if delta is None:
            return out  # 防御：R2/R12 已拦截
        post = pre + delta
        ratio = post / equity

        out.evidence.append({"name": "pre_exposure", "value": str(pre)})
        out.evidence.append({"name": "order_delta", "value": str(delta)})
        out.evidence.append({"name": "post_exposure", "value": str(post)})
        out.evidence.append({"name": "equity", "value": str(equity)})
        out.evidence.append({"name": "ratio", "value": str(ratio)})
        out.evidence.append({"name": "limit", "value": str(limit)})
        if ctx.order.instrument_type == "option":
            mult = (ctx.order.option or {}).get("multiplier")
            if isinstance(mult, int):
                out.evidence.append({"name": "used_multiplier", "value": mult})
        out.evidence.append(
            {"name": "group_members", "value": ",".join(sorted(m for m in members if m))}
        )

        if ratio > limit:
            out.violations.append(
                {
                    "rule_id": self.rule_id,
                    "severity": "BLOCK",
                    "detail": (
                        f"单标的 {group_key} 敞口占比 {ratio} > 上限 {limit}"
                        f"（post_exposure {post} / equity {equity}）"
                    ),
                }
            )
        return out


def _order_group_key(order) -> str:
    if order.instrument_type == "option":
        return str((order.option or {}).get("underlying", ""))
    return order.symbol
