"""R11 — data_freshness（持仓快照陈旧检查）。

触发：(now − snapshot_at) > limits.max_snapshot_age_seconds → BLOCK（恰好相等 → PASS）。
snapshot_at 在未来 → BLOCK（数据可疑，fail-closed）。
不豁免（陈旧快照下任何方向都不可信）。快照缺失/解析失败 → R12（业务数据缺失）。
"""

from .._timeutil import parse_rfc3339, to_epoch_seconds
from .base import Rule, RuleContext, RuleOutcome
from .registry import is_rule_enabled


class DataFreshnessRule(Rule):
    rule_id = "data_freshness"

    def evaluate(self, ctx: RuleContext) -> RuleOutcome:
        out = RuleOutcome(rule_id=self.rule_id)
        if not is_rule_enabled(self.rule_id, ctx.policy):
            return out
        limit = ctx.policy.limits.get("max_snapshot_age_seconds")
        snapshot = parse_rfc3339(ctx.portfolio.data.get("snapshot_at", ""))
        if limit is None or snapshot is None or ctx.now is None:
            return out  # 配置非法由 loader 拦截；快照缺失/非法 → R12
        now_epoch = to_epoch_seconds(ctx.now)
        snap_epoch = to_epoch_seconds(snapshot)
        age = now_epoch - snap_epoch
        out.evidence.append({"name": "snapshot_at", "value": snapshot.isoformat()})
        out.evidence.append({"name": "now", "value": str(now_epoch)})
        out.evidence.append({"name": "age_seconds", "value": age})
        out.evidence.append({"name": "limit", "value": limit})
        if age < 0:
            out.violations.append(
                {
                    "rule_id": self.rule_id,
                    "severity": "BLOCK",
                    "detail": "快照时间在未来（数据可疑，fail-closed）",
                }
            )
            return out
        if age > limit:
            out.violations.append(
                {
                    "rule_id": self.rule_id,
                    "severity": "BLOCK",
                    "detail": f"快照已陈旧 {age}s > 上限 {limit}s",
                }
            )
        return out
