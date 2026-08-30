"""输入/配置校验（优先级 2 输入门 + 分诊）。

分诊（-B §2.3，四类不串码）：
- order / policy：全部 Schema 错误（含版本门、币种、订单金额有限性）→ exit 4；
- portfolio：结构类错误（额外字段 / 枚举 / 格式 pattern / const）→ exit 4；
  portfolio 业务数据缺失 / null / 类型非法 / 数值范围 / 非有限 → **不在此抛错**，
  由 R12 missing_data_fail_closed 判为 exit 3（账户数据不可用，风控语义）；
- 引擎/规则异常 → exit 5（DEF-A3 兜底）。

任何 exit-4 失败抛 InputValidationError。
"""

import json
from jsonschema import Draft202012Validator

from ._decimal import DecimalInputError, as_decimal
from ._resources import schema_dict

_validators: dict[str, Draft202012Validator] = {}

# 各 Schema 当前版本（NEW-12f 独立版本号；版本门 exit 4，NEW-13）
_EXPECTED_SCHEMA_VERSION = {"order": 2, "portfolio": 3, "policy": 2}

# portfolio 结构类错误关键词（其余 → R12 exit 3）
_PORTFOLIO_EXIT4_KEYWORDS = ("additionalProperties", "enum", "pattern", "const")


def _validator(name: str) -> Draft202012Validator:
    if name not in _validators:
        # ：包内 Schema 是运行时唯一来源（wheel 安装后无源码仓）
        _validators[name] = Draft202012Validator(schema_dict(name))
    return _validators[name]


class InputValidationError(Exception):
    def __init__(self, details: list[str]):
        super().__init__("; ".join(details))
        self.details = details


def _schema_errors(name: str, instance: dict) -> list:
    return sorted(_validator(name).iter_errors(instance), key=lambda e: list(e.path))


def _path_of(err) -> str:
    return "/".join(str(p) for p in err.path) or "$"


# FIX-005-5：jsonschema 的 err.message 内嵌实例值（如 "'<注入值>' is not one of [...]"），
# 直接进入 details/evidence 会被 CLI explain()/审计记录回显。统一改为
# "字段路径 + 通用类别"；enum/required 附 Schema 已知静态内容（合法值/必填字段名），
# 绝不回显用户输入本身。
_SCHEMA_ERROR_CATEGORY = {
    "required": "缺少必填字段",
    "type": "类型非法",
    "enum": "枚举值非法",
    "const": "值非法",
    "pattern": "格式非法",
    "minimum": "数值低于下限",
    "maximum": "数值高于上限",
    "exclusiveMinimum": "数值不满足下限",
    "exclusiveMaximum": "数值不满足上限",
    "minLength": "长度不足",
    "maxLength": "长度超限",
    "additionalProperties": "未知字段",
    "items": "数组元素非法",
    "format": "格式非法",
    "anyOf": "条件约束非法",
    "oneOf": "条件约束非法",
    "if": "条件约束非法",
}


def schema_error_text(name: str, err) -> str:
    """jsonschema 错误的脱敏文本：{文档}.{字段路径}: {通用类别}，不回显实例值。"""
    path = _path_of(err)
    category = _SCHEMA_ERROR_CATEGORY.get(err.validator, "校验失败")
    text = f"{name}.{path}: {category}"
    if err.validator == "enum":
        allowed = err.validator_value
        if isinstance(allowed, list):
            text += f"（允许 {allowed}）"
    elif err.validator == "required":
        missing = err.validator_value
        if isinstance(missing, list):
            text += f"（{missing}）"
    return text


def safe_field_error(doc: str, path: str, category: str) -> str:
    """共享安全错误文本（FIX-005-6/7）：{文档}.{字段路径}: {固定类别}。

    只接受文档名、字段路径与固定类别字符串；调用方原始值（版本号/币种/任意
    文本）不得作为参数传入，本函数也不格式化任何值——错误文本绝不回显
    调用方输入。CLI/MCP/审计层不得再替换或追加原始值。
    """
    return f"{doc}.{path}: {category}"


def validate_inputs(order, portfolio, policy) -> None:
    """校验 order / portfolio / policy；exit-4 类失败抛 InputValidationError。"""
    details_exit4: list[str] = []

    # 版本门（NEW-13）：缺 / 高 / 低于当前版本 → exit 4（先于分类，避免被 R12 吞掉）
    # FIX-005-6：不回显实际值/类型 repr/容器内容（恶意版本值不得进入任何输出）
    for name, inst in (
        ("order", order.to_dict()),
        ("portfolio", portfolio.to_dict()),
        ("policy", policy.to_dict()),
    ):
        if inst.get("schema_version") != _EXPECTED_SCHEMA_VERSION[name]:
            details_exit4.append(
                safe_field_error(
                    name, "schema_version",
                    f"版本缺失或不符（期望 {_EXPECTED_SCHEMA_VERSION[name]}）",
                )
            )

    # order：全量 Schema → exit 4
    for err in _schema_errors("order", order.to_dict()):
        if err.path and err.path[0] == "schema_version":
            continue  # 版本门已单独处理
        details_exit4.append(schema_error_text("order", err))

    # policy：全量 Schema → exit 4
    for err in _schema_errors("policy", policy.to_dict()):
        if err.path and err.path[0] == "schema_version":
            continue
        details_exit4.append(schema_error_text("policy", err))

    # portfolio：结构类 → exit 4；业务数据缺失/null/类型/范围 → 留给 R12（exit 3）
    for err in _schema_errors("portfolio", portfolio.to_dict()):
        if err.path and err.path[0] == "schema_version":
            continue
        if err.validator in _PORTFOLIO_EXIT4_KEYWORDS:
            details_exit4.append(schema_error_text("portfolio", err))

    # 币种一致性（运行时规则，exit 4 输入错误语义）
    # FIX-005-7：不回显任一实际币种值（固定类别 + 安全字段路径）
    if order.currency and order.currency != policy.base_currency:
        details_exit4.append(
            safe_field_error("order", "currency",
                             "currency mismatch（与 policy.base_currency 不一致）")
        )
    pf_currency = portfolio.data.get("base_currency")
    if pf_currency and pf_currency != policy.base_currency:
        details_exit4.append(
            safe_field_error("portfolio", "base_currency",
                             "currency mismatch（与 policy.base_currency 不一致）")
        )
    positions_raw = portfolio.data.get("positions")
    if isinstance(positions_raw, list):
        for i, pos in enumerate(positions_raw):
            if isinstance(pos, dict) and pos.get("currency") and pos["currency"] != policy.base_currency:
                details_exit4.append(
                    safe_field_error("portfolio", f"positions[{i}].currency",
                                     "currency mismatch（与 policy.base_currency 不一致）")
                )

    # 订单金额有限性（order 侧 → exit 4；portfolio 侧非有限由 R12/business_number 处理）
    _check_finite(order.price, "order.price", details_exit4)
    opt = order.option or {}
    _check_finite(opt.get("strike"), "order.option.strike", details_exit4)

    if details_exit4:
        raise InputValidationError(details_exit4)


def _check_finite(value, name: str, details: list[str]) -> None:
    if value is None:
        return
    try:
        as_decimal(value)
    except DecimalInputError:
        # FIX-005-5：不回显输入值（只给类别；schema 已保证为 number，此处仅有限性）
        details.append(f"{name}: 非有限数值（NaN/Infinity）")
