"""数据模型（M-3 库 API 形态）。

Order / Portfolio / Policy 同时支持两种构造：
- Order.from_dict({...}) / Order(data={...})：引擎内部与测试；
- Order(symbol=..., side=..., ...) / Policy(limits=...)：用户库 API。
Result 保持 dataclass，to_dict() 符合 result.schema.json v2。
"""

from dataclasses import dataclass


class Order:
    def __init__(self, data: dict | None = None, **fields):
        if data is not None:
            self.data = dict(data)
        else:
            self.data = fields
            self.data.setdefault("schema_version", 2)  # 关键字构造自动填契约版本

    @classmethod
    def from_dict(cls, data: dict) -> "Order":
        return cls(data=data)

    def to_dict(self) -> dict:
        return dict(self.data)

    @property
    def symbol(self) -> str:
        return str(self.data.get("symbol", ""))

    @property
    def instrument_type(self) -> str:
        return str(self.data.get("instrument_type", ""))

    @property
    def side(self) -> str:
        return str(self.data.get("side", ""))

    @property
    def quantity(self):
        return self.data.get("quantity")

    @property
    def price(self):
        return self.data.get("price")

    @property
    def currency(self) -> str:
        return str(self.data.get("currency", ""))

    @property
    def option(self) -> dict:
        return self.data.get("option") or {}


class Portfolio:
    def __init__(self, data: dict | None = None, **fields):
        if data is not None:
            self.data = dict(data)
        else:
            self.data = fields
            self.data.setdefault("schema_version", 3)  # 关键字构造自动填契约版本

    @classmethod
    def from_dict(cls, data: dict) -> "Portfolio":
        return cls(data=data)

    def to_dict(self) -> dict:
        return dict(self.data)

    @property
    def equity(self):
        return self.data.get("equity")

    @property
    def positions(self) -> list:
        raw = self.data.get("positions")
        return raw if isinstance(raw, list) else []  # 缺失/null/非数组 → []（R12 依原始值判 exit 3）


class Policy:
    def __init__(self, data: dict | None = None, **fields):
        if data is not None:
            self.data = dict(data)
        else:
            self.data = fields
            self.data.setdefault("schema_version", 2)  # 关键字构造自动填契约版本

    @classmethod
    def from_dict(cls, data: dict) -> "Policy":
        return cls(data=data)

    def to_dict(self) -> dict:
        return dict(self.data)

    @property
    def mode(self) -> str:
        return str(self.data.get("mode", "enforce"))

    @property
    def base_currency(self) -> str:
        return str(self.data.get("base_currency", "USD"))

    @property
    def kill_switch(self) -> str:
        return str(self.data.get("kill_switch", "off"))

    @property
    def version(self) -> str:
        return str(self.data.get("version", ""))

    @property
    def limits(self) -> dict:
        return self.data.get("limits") or {}

    @property
    def acknowledged_disabled(self) -> list:
        return self.data.get("acknowledged_disabled") or []


@dataclass
class Result:
    """决策结果（结构符合 result.schema.json v2）。"""

    decision: str
    shadow_mode: bool
    shadow_verdict: str | None
    exit_code: int
    evaluated_at: str
    violations: list
    warnings: list
    evidence: dict
    request_id: str | None = None
    schema_version: int = 2

    def to_dict(self) -> dict:
        out = {
            "schema_version": self.schema_version,
            "decision": self.decision,
            "shadow_mode": self.shadow_mode,
            "shadow_verdict": self.shadow_verdict,
            "exit_code": self.exit_code,
            "evaluated_at": self.evaluated_at,
            "violations": self.violations,
            "warnings": self.warnings,
            "evidence": self.evidence,
        }
        if self.request_id is not None:
            out["request_id"] = self.request_id  # 可选字段：None 时省略（符合 result.schema）
        return out

    def explain(self) -> str:
        """人类可读多行说明；明确区分输入/配置错误与风控拦截（-A §2.3）。"""
        lines = ["Deadlatch — check result"]
        lines.append(f"decision: {self.decision}   exit_code: {self.exit_code}")
        if self.exit_code == 4:
            lines.append("类型：输入/配置错误（非风控拦截）——请检查订单/快照/配置，不是触发了风控规则")
        elif self.exit_code == 5:
            lines.append("类型：引擎内部异常（按 BLOCK 处理）——规则执行失败，详见 evidence.rule_evidence")
        elif self.exit_code == 3:
            lines.append("类型：风控拦截")
        elif self.exit_code == 2:
            lines.append("类型：风控警告（可继续，但请留意）")
        else:
            lines.append("类型：通过")
        for v in self.violations:
            lines.append(f"  - [{v['rule_id']}] {v['detail']}")
        for w in self.warnings:
            lines.append(f"  - [{w['rule_id']}] {w['detail']}")
        if self.shadow_mode:
            lines.append("shadow_mode: true（对外投影由  实现）")
        return "\n".join(lines)
