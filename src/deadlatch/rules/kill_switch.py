"""R1 — kill_switch（全局 Kill Switch，三态：off / full / reduce_only）。

实现 docs/rules-spec.md R1：
- 三态枚举（非法值由 policy Schema 校验拒绝 → exit 4，本规则不重复处理）；
- full 拦截全部订单（含平仓）；
- reduce_only 仅放行推断为 close 的订单——方向推断唯一实现为
  direction.infer_order_direction（本规则不再自带推断逻辑），
  期权四值与正股六组合共用；快照不可信（缺失/不可解析/陈旧/未来）→ open，不放行；
- 放行 ≠ 免检：本规则只裁决 R1 自身，其余规则由引擎无条件继续执行
  （引擎主循环无任何"R1 通过即跳过其余"的路径）；
- shadow 位由引擎统一处理（policy.mode == "shadow" 时规则照常求值）。
"""

from ..direction import infer_order_direction
from .base import Rule, RuleContext, RuleOutcome


class KillSwitchRule(Rule):
    rule_id = "kill_switch"

    def evaluate(self, ctx: RuleContext) -> RuleOutcome:
        ks = ctx.policy.kill_switch
        out = RuleOutcome(rule_id=self.rule_id, evidence=[{"name": "kill_switch", "value": ks}])

        if ks == "off":
            return out

        if ks == "full":
            out.violations.append(
                {
                    "rule_id": self.rule_id,
                    "severity": "BLOCK",
                    "detail": "全局 kill switch = full：拦截所有订单（含平仓）",
                }
            )
            return out

        # reduce_only：仅放行引擎推断为 close 的订单（方向唯一实现见 direction.py）
        is_close = infer_order_direction(ctx.order, ctx.portfolio, ctx.policy, ctx.now) == "close"
        out.evidence.append(
            {"name": "order_direction", "value": "close" if is_close else "open"}
        )
        if not is_close:
            out.violations.append(
                {
                    "rule_id": self.rule_id,
                    "severity": "BLOCK",
                    "detail": "全局 kill switch = reduce_only：仅放行平仓订单，本单推断为开仓",
                }
            )
        return out
