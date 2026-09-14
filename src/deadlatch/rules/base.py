"""规则接口（抽象基类）。全部 12 条规则（及本批次测试桩）实现本接口。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RuleContext:
    """规则求值上下文：一次 check 的不可变输入。"""

    order: Any
    portfolio: Any
    policy: Any
    now: Any = None  # aware datetime（R10/R11 需要；确定性由调用方固定时钟）


@dataclass
class RuleOutcome:
    """单条规则的求值结果。violations/warnings 的结构符合 result.schema.json。"""

    rule_id: str
    violations: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    evidence: list = field(default_factory=list)


class Rule(ABC):
    """规则抽象基类。rule_id 为稳定字符串（docs/rules-spec.md 契约）。"""

    rule_id: str = ""

    @abstractmethod
    def evaluate(self, ctx: RuleContext) -> RuleOutcome:
        """求值一条规则。

        禁止在本方法内抛出除实现缺陷外的异常；任何异常由引擎 S-2 捕获，
        记录后继续执行其余规则，最终由 S-1 合成为 exit 5。
        """
        raise NotImplementedError
