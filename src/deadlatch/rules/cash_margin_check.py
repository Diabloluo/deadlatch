"""R7 — cash_margin_check（现金与保证金检查；R7a + R7b 同属唯一 rule_id）。

R7a 现金充足性：post_trade_cash < limits.min_cash → BLOCK（恰好相等 → PASS）。
    outflow 口径（rules-spec §0.1 / R7）：
    - 股票 buy（推断=开仓）：price × qty
    - 期权 buy_to_open，或无法由快照证实的 buy_to_close：price × M × qty
    - 股票 sell（推断=开仓，卖空）：0 —— **已知简化**（卖空所得与保证金要求相抵，
      会低估实际保证金占用；完整卖空保证金模型为 v0.2 候选），简化事实写入 evidence
    - 期权 sell_to_open，或无法由快照证实的 sell_to_close：
      max(strike × M × qty − price × M × qty, 0)
R7b 卖出期权保证金占用：(existing_short_margin + new_short_margin) / equity
    > limits.max_options_margin_ratio → BLOCK（恰好相等 → PASS）。
    existing = Σ 快照 short 期权持仓 max(strike×M×qty − avg_cost×M×qty, 0)
              （avg_cost 缺失 → strike×M×qty）
平仓豁免（§0.2）。
配置键为 min_cash 与 max_options_margin_ratio 两个；任一缺失须整体承认禁用（loader 校验）。
max_options_margin_ratio = 0 → 非法配置 exit 4（Schema exclusiveMinimum 拒绝；
rules-spec R7 §7 中"0=禁止卖权合法配置"为未同步残句，不据此实现——按工单裁定）。
"""

from decimal import Decimal

from ..direction import infer_order_direction, is_close_order
from ..exposure import business_int, business_number, option_multiplier, short_option_margin
from .base import Rule, RuleContext, RuleOutcome
from .registry import is_rule_enabled


class CashMarginCheckRule(Rule):
    rule_id = "cash_margin_check"

    def evaluate(self, ctx: RuleContext) -> RuleOutcome:
        out = RuleOutcome(rule_id=self.rule_id)
        if not is_rule_enabled(self.rule_id, ctx.policy):
            return out
        if is_close_order(ctx.order, ctx.portfolio, ctx.policy, ctx.now):
            out.evidence.append({"name": "exempted", "value": "close"})
            return out

        self._check_cash(ctx, out)
        self._check_margin(ctx, out)
        return out

    # ---- R7a ----
    def _check_cash(self, ctx, out) -> None:
        min_cash = business_number(ctx.policy.limits.get("min_cash"))
        cash = business_number(ctx.portfolio.data.get("cash"))
        if min_cash is None or cash is None:
            return  # 配置非法/数据缺失由 loader/R12 处理
        if (
            ctx.order.instrument_type == "stock"
            and ctx.order.side == "sell"
            and infer_order_direction(ctx.order, ctx.portfolio, ctx.policy, ctx.now) == "open"
        ):
            out.evidence.append(
                {
                    "name": "short_sell_outflow_simplification",
                    "value": "outflow=0（卖空所得与保证金相抵，低估保证金占用；完整模型 v0.2）",
                }
            )
        outflow = self._outflow(ctx)
        if outflow is None:
            return
        post_trade_cash = cash - outflow
        out.evidence.append({"name": "cash", "value": str(cash)})
        out.evidence.append({"name": "outflow", "value": str(outflow)})
        out.evidence.append({"name": "post_trade_cash", "value": str(post_trade_cash)})
        out.evidence.append({"name": "min_cash", "value": str(min_cash)})
        if post_trade_cash < min_cash:
            out.violations.append(
                {
                    "rule_id": self.rule_id,
                    "severity": "BLOCK",
                    "detail": f"成交后现金 {post_trade_cash} < 下限 {min_cash}",
                }
            )

    def _outflow(self, ctx) -> Decimal | None:
        price = business_number(ctx.order.price)
        qty = business_int(ctx.order.quantity)
        if price is None or qty is None:
            return None
        if ctx.order.instrument_type == "option":
            side = ctx.order.side
            mult = business_int((ctx.order.option or {}).get("multiplier"))
            if mult is None:
                return None
            direction = infer_order_direction(ctx.order, ctx.portfolio, ctx.policy, ctx.now)
            if direction == "close":
                return None  # 已豁免；防御性返回
            if side in ("buy_to_open", "buy_to_close"):
                return price * mult * qty
            if side in ("sell_to_open", "sell_to_close"):
                strike = business_number((ctx.order.option or {}).get("strike"))
                if strike is None:
                    return None
                gross = strike * mult * qty - price * mult * qty
                return gross if gross > 0 else Decimal("0")
            return None
        # 正股
        direction = infer_order_direction(ctx.order, ctx.portfolio, ctx.policy, ctx.now)
        if direction == "close":
            return None  # 已豁免
        if ctx.order.side == "sell":
            # 已知简化：卖空 outflow = 0（卖空所得与保证金相抵，低估保证金占用）
            return Decimal("0")
        return price * qty

    # ---- R7b ----
    def _check_margin(self, ctx, out) -> None:
        limit = business_number(ctx.policy.limits.get("max_options_margin_ratio"))
        equity = business_number(ctx.portfolio.equity)
        if limit is None or equity is None:
            return
        if equity <= 0:
            out.violations.append(
                {
                    "rule_id": self.rule_id,
                    "severity": "BLOCK",
                    "detail": f"账户权益 {equity} <= 0，无法计算保证金占用（fail-closed）",
                }
            )
            return
        existing = Decimal("0")
        for pos in ctx.portfolio.positions:
            if not isinstance(pos, dict):
                continue  # 元素非对象 → R12 判数据缺失（本规则不抛异常）
            if pos.get("instrument_type") != "option" or pos.get("side") != "short":
                continue
            opt = pos.get("option") or {}
            strike = business_number(opt.get("strike"))
            mult = business_int(opt.get("multiplier"))
            qty = business_int(pos.get("quantity"))
            if strike is None or mult is None or qty is None:
                continue  # R12 判为数据缺失
            premium = business_number(pos.get("avg_cost"))
            existing += short_option_margin(strike, mult, qty, premium)
        new_margin = Decimal("0")
        direction = infer_order_direction(ctx.order, ctx.portfolio, ctx.policy, ctx.now)
        if (
            ctx.order.instrument_type == "option"
            and direction == "open"
            and ctx.order.side in ("sell_to_open", "sell_to_close")
        ):
            strike = business_number((ctx.order.option or {}).get("strike"))
            mult = business_int((ctx.order.option or {}).get("multiplier"))
            qty = business_int(ctx.order.quantity)
            price = business_number(ctx.order.price)
            if strike is not None and mult is not None and qty is not None:
                new_margin = short_option_margin(strike, mult, qty, price)
        total = existing + new_margin
        ratio = total / equity
        out.evidence.append({"name": "existing_short_margin", "value": str(existing)})
        out.evidence.append({"name": "new_short_margin", "value": str(new_margin)})
        out.evidence.append({"name": "margin_ratio", "value": str(ratio)})
        out.evidence.append({"name": "equity", "value": str(equity)})
        out.evidence.append({"name": "limit", "value": str(limit)})
        if ctx.order.instrument_type == "option":
            mult = option_multiplier(ctx.order)
            if mult is not None:
                out.evidence.append({"name": "used_multiplier", "value": mult})
        if ratio > limit:
            out.violations.append(
                {
                    "rule_id": self.rule_id,
                    "severity": "BLOCK",
                    "detail": f"卖出期权保证金占用 {ratio} > 上限 {limit}",
                }
            )
