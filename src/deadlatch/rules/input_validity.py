"""R2 — input_validity（必填字段与数值合法性）。

规则语义（docs/rules-spec.md R2）：订单违反 order.schema.json 任何约束
（缺必填字段/非法枚举/数量价格非法/币种不匹配/版本不符/额外字段）→
INPUT_ERROR（exit 4），不是 BLOCK——调用方用错了工具，不是风控事件。

引擎在优先级 2（validate_inputs）先于规则循环执行 R2 域校验并直接
返回 exit 4；本规则在规则循环内作纵深防御（同一校验器），正常路径
下输入已通过输入门，规则恒 PASS。恒启用，不可关闭。
"""

from .base import Rule, RuleContext, RuleOutcome
from .registry import ALWAYS_ON_RULE_IDS


class InputValidityRule(Rule):
    rule_id = "input_validity"

    def evaluate(self, ctx: RuleContext) -> RuleOutcome:
        out = RuleOutcome(rule_id=self.rule_id)
        details = _order_violations(ctx.order, ctx.policy)
        for d in details:
            out.violations.append(
                {"rule_id": self.rule_id, "severity": "BLOCK", "detail": d}
            )
            out.evidence.append({"name": "input_error", "value": d})
        return out


def _order_violations(order, policy) -> list[str]:
    """R2 域校验：order Schema + 币种一致性 + 金额有限性。失败列表（空=合法）。"""
    from .._validation import _validator, safe_field_error, schema_error_text

    details: list[str] = []
    errs = sorted(_validator("order").iter_errors(order.to_dict()), key=lambda e: list(e.path))
    for err in errs:
        # 不回显 jsonschema message 内嵌的实例值（explain/审计共用）
        details.append(schema_error_text("order", err))
    if order.currency and order.currency != policy.base_currency:
        # 与入口校验同一安全构造（不复制会漂移的拼接模板），不回显币种值
        details.append(
            safe_field_error("order", "currency",
                             "currency mismatch（与 policy.base_currency 不一致）")
        )
    from .._decimal import DecimalInputError, as_decimal

    for name, value in (("order.price", order.price), ("order.option.strike", (order.option or {}).get("strike"))):
        if value is None:
            continue
        try:
            as_decimal(value)
        except DecimalInputError:
            details.append(f"{name}: 非有限数值（NaN/Infinity）")
    return details


assert "input_validity" in ALWAYS_ON_RULE_IDS  # 恒启用契约
