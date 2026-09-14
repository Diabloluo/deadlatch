"""Schema 显式迁移模块。

- 单一迁移模块与注册表：order v1→v2、policy v1→v2、portfolio v1→v2→v3；
  当前版本以仓内 Schema 为唯一来源（order=2、policy=2、portfolio=3）；
- 禁止在多个 loader/CLI/MCP 中复制迁移逻辑；Guard/CLI/MCP 正常评估入口仍
  严格拒绝旧版本（版本门 exit 4），本模块仅供显式、离线调用；
- 不在迁移链中的旧版、未来版、缺版本、跳步或语义不明输入全部拒绝
  （MigrationError），不猜测、不静默补业务字段；
- 迁移结果必须通过当前 Schema 完整校验；同一文档重复迁移为 no-op；
  迁移错误信息不含路径/secret/traceback。
"""

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from ._resources import schema_dict, schema_text
from .rules.registry import rule_config_keys  # noqa: F401  (保留引用，测试可探针)

# 当前版本：以仓内 Schema 为唯一来源（properties.schema_version.const），
# 禁止手写数值常量。


def load_current_versions(schema_dir: Path | None = None) -> dict[str, int]:
    """从三个 Schema 的 properties.schema_version.const 读取当前版本（唯一版本源）。

    schema_dir 缺省时读包内 Schema（wheel 安装后无源码仓）；
    传 schema_dir 时读指定目录（供测试探针改动 const 验证非硬编码）。
    """
    versions: dict[str, int] = {}
    for kind in ("order", "policy", "portfolio"):
        if schema_dir is not None:
            doc = json.loads((Path(schema_dir) / f"{kind}.schema.json").read_text(encoding="utf-8"))
        else:
            doc = json.loads(schema_text(kind))
        const = doc["properties"]["schema_version"]["const"]
        if not isinstance(const, int):
            raise MigrationError(f"{kind}.schema.json 的 schema_version.const 不是整数")
        versions[kind] = const
    return versions


CURRENT_VERSIONS = load_current_versions()

# 迁移链：{kind: {from_version: to_version}}
MIGRATION_CHAIN = {
    "order": {1: 2},
    "policy": {1: 2},
    "portfolio": {1: 2, 2: 3},
}

# 包内 Schema 是运行时唯一来源
_VALIDATORS = {
    "order": Draft202012Validator(schema_dict("order")),
    "policy": Draft202012Validator(schema_dict("policy")),
    "portfolio": Draft202012Validator(schema_dict("portfolio")),
}


class MigrationError(Exception):
    """迁移失败（调用方转 exit 4；消息不含路径/secret/traceback）。"""


def _validate_current(kind: str, document: dict) -> None:
    errors = sorted(_VALIDATORS[kind].iter_errors(document), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        path = "/".join(str(p) for p in first.path) or "$"
        raise MigrationError(
            f"{kind} 迁移后未通过当前 Schema（字段 {path}）"
        )


# ---------------------------------------------------------------- 各步迁移

def _migrate_order_v1(document: dict) -> dict:
    """order v1→v2：股票四值 side 转券商原生 buy/sell；期权四值保持（带 option 即期权）。"""
    side = document.get("side")
    is_option = isinstance(document.get("option"), dict)
    if is_option:
        if side not in ("buy_to_open", "sell_to_open", "buy_to_close", "sell_to_close"):
            raise MigrationError("期权订单 side 必须是 buy_to_open/sell_to_open/buy_to_close/sell_to_close")
        return {**document, "schema_version": 2, "instrument_type": "option"}
    mapping = {
        "buy_to_open": "buy",
        "buy_to_close": "buy",
        "sell_to_open": "sell",
        "sell_to_close": "sell",
    }
    if side not in mapping:
        raise MigrationError("正股订单 side 必须是 v1 四值之一（buy_to_open/buy_to_close/sell_to_open/sell_to_close）")
    return {**document, "schema_version": 2, "instrument_type": "stock", "side": mapping[side]}


def _migrate_policy_v1(document: dict) -> dict:
    """policy v1→v2：只执行已裁定转换——布尔 kill_switch false→off / true→full。

    其他字段原样保留，不 setdefault mode、不根据缺失 limit 自动生成
    acknowledged_disabled（不得猜测或静默补业务字段）。v1 缺少当前 Schema 所需
    字段、存在启用/禁用矛盾时，迁移后的完整 Schema 校验失败并抛 MigrationError，
    不生成"看起来合法"的新策略。
    """
    ks = document.get("kill_switch")
    if not isinstance(ks, bool):
        raise MigrationError("policy v1 kill_switch 必须是布尔（false→off / true→full）")
    return {**document, "schema_version": 2, "kill_switch": "off" if ks is False else "full"}


def _migrate_portfolio_v1(document: dict) -> dict:
    """portfolio v1→v2：drawdown_pct 改名 drawdown_ratio（数值不缩放）；新旧并存 → 拒绝。"""
    has_pct = "drawdown_pct" in document
    has_ratio = "drawdown_ratio" in document
    if has_pct and has_ratio:
        raise MigrationError("drawdown_pct 与 drawdown_ratio 同时存在，语义不明，拒绝迁移")
    if not has_pct:
        raise MigrationError("portfolio v1 缺少 drawdown_pct（无法确定回撤字段）")
    new = {k: v for k, v in document.items() if k != "drawdown_pct"}
    new["drawdown_ratio"] = document["drawdown_pct"]
    new["schema_version"] = 2
    return new


def _migrate_portfolio_v2(document: dict) -> dict:
    """portfolio v2→v3：版本升级 + 现行完整 Schema 校验（不伪造 equity/day_start/peak 等缺失值）。"""
    return {**document, "schema_version": 3}


_STEP_FNS = {
    "order": {1: _migrate_order_v1},
    "policy": {1: _migrate_policy_v1},
    "portfolio": {1: _migrate_portfolio_v1, 2: _migrate_portfolio_v2},
}

_STEP_DESCRIPTIONS = {
    "order": {1: "股票四值 side 转券商原生 buy/sell；期权四值保持；补充 instrument_type"},
    "policy": {1: "布尔 kill_switch 映射 false→off / true→full（其他字段原样保留；缺 mode/acknowledged_disabled 由现行 Schema 校验拒绝，不猜测补业务字段）"},
    "portfolio": {
        1: "drawdown_pct 改名 drawdown_ratio（数值不缩放）",
        2: "版本号升级至 v3（现行完整 Schema 校验，不伪造缺失值）",
    },
}


# ---------------------------------------------------------------- 主入口

def migrate(kind: str, document: dict) -> dict:
    """显式迁移：返回 {"kind","from_version","to_version","steps","document"}。

    缺版本/未来版/跳步/链外版本 → MigrationError；迁移后过当前 Schema；
    当前版本 → no-op 校验结果（steps 标注已是最新）。
    """
    if kind not in CURRENT_VERSIONS:
        raise MigrationError(f"未知 kind: {kind}（仅支持 order/policy/portfolio）")
    if not isinstance(document, dict):
        raise MigrationError(f"{kind} 输入必须是 JSON 对象")
    version = document.get("schema_version")
    if not isinstance(version, int):
        raise MigrationError(f"{kind} 缺少整数 schema_version，拒绝迁移（不猜测版本）")
    current = CURRENT_VERSIONS[kind]
    steps: list[str] = []
    doc = dict(document)
    if version == current:
        _validate_current(kind, doc)
        return {
            "kind": kind, "from_version": version, "to_version": current,
            "steps": [f"{kind} 已是最新版本（v{current}），no-op 校验通过"],
            "document": doc,
        }
    if version < 1 or version > current:
        raise MigrationError(f"{kind} 版本 v{version} 不在迁移链内（当前 v{current}），拒绝迁移")
    # 逐级迁移（不允许跳步：链必须连续）
    v = version
    while v < current:
        chain = MIGRATION_CHAIN[kind]
        if v not in chain or chain[v] != v + 1:
            raise MigrationError(f"{kind} v{v}→v{v + 1} 无迁移链，拒绝跳步迁移")
        fn = _STEP_FNS[kind][v]
        doc = fn(doc)
        desc = _STEP_DESCRIPTIONS[kind][v]
        steps.append(f"{kind} v{v}→v{v + 1}: {desc}")
        v += 1
    _validate_current(kind, doc)
    return {
        "kind": kind, "from_version": version, "to_version": current,
        "steps": steps, "document": doc,
    }


def migrate_json(kind: str, text: str) -> dict:
    """从 JSON 文本迁移（解析失败 → MigrationError，消息不含路径）。"""
    try:
        document = json.loads(text)
    except Exception as exc:
        raise MigrationError(f"{kind} 输入 JSON 解析失败（{type(exc).__name__}）") from exc
    return migrate(kind, document)
