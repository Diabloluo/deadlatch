"""R12 — missing_data_fail_closed（关键数据缺失 fail-closed）。

已启用规则所需的 portfolio 业务数据缺失 / null / 类型非法 / 非有限 /
分母非法 / 快照不可解析 / 持仓敞口不可用 / 回撤自洽校验失败
→ BLOCK（exit 3），detail 标注缺失字段与受影响规则。

与输入门的分工（-B §2.3）：
- order / policy / portfolio 结构类错误 → exit 4（输入门）；
- portfolio 业务数据不可用 → 本规则（exit 3，风控语义）；
- 引擎/规则异常 → exit 5。

positions 字段本身（-B §2.1）：缺失 / null / 非数组 /
数组元素不是合法对象 → 本规则 exit 3（持仓快照是方向推断与 R5/R6/R7
的基础，结构不可用即 fail-closed；空数组 [] 仍为合法快照）。
恒启用，不可关闭。evidence 同时记录具体字段（missing_field）与
受影响规则（affected_rules）。
"""

from decimal import Decimal

from .._timeutil import parse_rfc3339
from ..exposure import business_number, position_exposure
from .base import Rule, RuleContext, RuleOutcome
from .registry import is_rule_enabled

_DRAWDOWN_SELF_CONSISTENCY_TOLERANCE = Decimal("0.005")

# 受影响规则标注（evidence.affected_rules 与 detail 共用）
_EXPOSURE_RULES = "R5/R6/R7（敞口）"
_DIRECTION_RULES = "R1/R3–R9（方向推断）"


class MissingDataFailClosedRule(Rule):
    rule_id = "missing_data_fail_closed"

    def evaluate(self, ctx: RuleContext) -> RuleOutcome:
        out = RuleOutcome(rule_id=self.rule_id)
        problems: list[tuple[str, str, str]] = []  # (字段, 受影响规则, 原因)
        pf = ctx.portfolio.data

        # positions 字段本身：缺失/null/非数组/元素非对象 → exit 3（无条件，恒启用）
        positions_raw = pf.get("positions")
        if not isinstance(positions_raw, list):
            problems.append(
                (
                    "portfolio.positions",
                    f"{_EXPOSURE_RULES}与{_DIRECTION_RULES}",
                    "缺失/null/非数组（持仓快照结构不可用）",
                )
            )
        else:
            for i, pos in enumerate(positions_raw):
                if not isinstance(pos, dict):
                    problems.append(
                        (
                            f"portfolio.positions[{i}]",
                            f"{_EXPOSURE_RULES}与{_DIRECTION_RULES}",
                            "数组元素不是合法对象",
                        )
                    )
                    break  # 一条即可，避免刷屏

        exposure_rules = [
            rid
            for rid in ("max_symbol_exposure", "max_total_exposure", "cash_margin_check")
            if is_rule_enabled(rid, ctx.policy)
        ]
        if exposure_rules:
            if business_number(pf.get("equity")) is None:
                problems.append(("portfolio.equity", _EXPOSURE_RULES, "缺失/null/类型非法/非有限"))
        if is_rule_enabled("cash_margin_check", ctx.policy):
            if business_number(pf.get("cash")) is None:
                problems.append(("portfolio.cash", "R7（R7a 现金）", "缺失/null/类型非法/非有限"))
        if is_rule_enabled("max_daily_loss", ctx.policy):
            if business_number(pf.get("daily_pnl")) is None:
                problems.append(("portfolio.daily_pnl", "R8", "缺失/null/类型非法/非有限"))
            dse = business_number(pf.get("day_start_equity"))
            if dse is None:
                problems.append(("portfolio.day_start_equity", "R8", "缺失/null/类型非法/非有限"))
            elif dse <= 0:
                problems.append(("portfolio.day_start_equity", "R8", "分母非法，fail-closed"))
        if is_rule_enabled("max_drawdown", ctx.policy):
            if business_number(pf.get("drawdown_ratio")) is None:
                problems.append(("portfolio.drawdown_ratio", "R9", "缺失/null/类型非法/非有限"))
            if business_number(pf.get("peak_equity")) is None:
                problems.append(("portfolio.peak_equity", "R9", "缺失/null/类型非法/非有限"))
            self._check_drawdown_consistency(ctx, problems)
        if is_rule_enabled("data_freshness", ctx.policy):
            if parse_rfc3339(pf.get("snapshot_at", "")) is None:
                problems.append(("portfolio.snapshot_at", "R11", "缺失/非法/不可解析"))
        if exposure_rules and isinstance(positions_raw, list):
            for i, pos in enumerate(positions_raw):
                if not isinstance(pos, dict):
                    continue  # 形状问题已在上方申报
                if position_exposure(pos) is None:
                    problems.append(
                        (
                            f"portfolio.positions[{i}]",
                            _EXPOSURE_RULES,
                            "持仓敞口数据不可用",
                        )
                    )
                    break  # 一条即可，避免刷屏

        for field, affected_rules, reason in problems:
            out.violations.append(
                {
                    "rule_id": self.rule_id,
                    "severity": "BLOCK",
                    "detail": f"关键数据缺失/不可用：{field}（{affected_rules}所需：{reason}）",
                }
            )
            out.evidence.append({"name": "missing_field", "value": field})
            out.evidence.append({"name": "affected_rules", "value": affected_rules})
        return out

    def _check_drawdown_consistency(self, ctx, problems: list[tuple[str, str, str]]) -> None:
        """R9 自洽校验：|drawdown_ratio − (peak−equity)/peak| > 0.5% → 疑似伪造快照。"""
        ddr = business_number(ctx.portfolio.data.get("drawdown_ratio"))
        equity = business_number(ctx.portfolio.data.get("equity"))
        peak = business_number(ctx.portfolio.data.get("peak_equity"))
        if ddr is None or equity is None or peak is None or peak <= 0:
            return  # 缺失/分母非法已在 R9/R12 其他分支处理
        implied = (peak - equity) / peak
        if abs(ddr - implied) > _DRAWDOWN_SELF_CONSISTENCY_TOLERANCE:
            problems.append(
                (
                    "portfolio.drawdown_ratio",
                    "R9",
                    f"与 equity/peak_equity 不自洽（{ddr} vs 推算 {implied}，偏差 > 0.5%，疑似伪造快照）",
                )
            )
