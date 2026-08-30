"""引擎主循环（-A + 第 2 轮修复）。

S-1 决策合成：先收集、后判定——跑完全部已启用规则，在单一出口按真值表合成。
S-2 异常捕获：except Exception（禁止 BaseException），记录后继续，最终 exit 5。
DEF-A2：空规则集 / 缺强制规则（registry.MANDATORY_RULE_IDS：R1/R2/R8–R12）
        → 拒绝构造（空规则集 = 闸门对每笔订单放行，定义上非法）。
DEF-A3：check() 最外层异常兜底 → exit 5 Result，不向上抛；evidence 标注发生阶段。
MIN-A2：rule_id 唯一性校验；evidence 累加（setdefault+extend，与异常路径一致）。
"""

import hashlib
import json
from datetime import datetime, timezone

from ._validation import InputValidationError, validate_inputs
from .model import Order, Policy, Portfolio, Result
from .rules.base import Rule, RuleContext
from .rules.registry import MANDATORY_RULE_IDS, RULE_IDS, is_rule_enabled


def _canonical_hash(order: Order, portfolio: Portfolio, policy: Policy) -> str:
    """规范化输入摘要（审计对账用，符合 audit-record.schema input_hash）。"""
    payload = json.dumps(
        {
            "order": order.to_dict(),
            "portfolio": portfolio.to_dict(),
            "policy_version": policy.version,
            "policy": policy.to_dict(),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _order_summary(order: Order) -> dict:
    # FIX-005-6/7 纵深：order_summary 是 Result.evidence 的一部分（契约字段），
    # 恶意注入值（Token/Cookie/绝对路径形态）不得原样进入 Result——字符串值
    # 统一过通用敏感形态过滤（正常业务值不受影响）；不依赖 CLI/审计层二次脱敏
    from .audit import sanitize_text

    opt = order.option or {}
    raw = {
        "symbol": order.symbol,
        "instrument_type": order.instrument_type,
        "side": order.side,
        "quantity": order.quantity,
        "price": order.price,
        "currency": order.currency,
        "created_at": order.data.get("created_at", ""),
        "underlying": opt.get("underlying"),
        "strike": opt.get("strike"),
        "right": opt.get("right"),
        "multiplier": opt.get("multiplier"),
    }
    return {
        k: (sanitize_text(v, []) if isinstance(v, str) else v)
        for k, v in raw.items()
    }


def _project_outcome(shadow: bool, decision: str, exit_code: int, violations: list) -> tuple[str, int, str | None]:
    """shadow 对外投影（唯一入口；库 API 与 CLI 共用，禁止各自实现）。

    矩阵（工单  §2.1）：
    - enforce：原样（shadow_verdict=None）， 行为完全不变；
    - shadow 普通 PASS/WARN/BLOCK → 对外 PASS/0，内部裁决进 shadow_verdict；
    - shadow × kill switch（full / reduce_only 判定 open）→ 保持 BLOCK/3；
    - shadow × 输入/配置错误（4）/ 内部/规则异常（5）→ 保持真实 BLOCK；
    - 规则必须先完整执行并得到内部裁决，再做一次对外投影（调用方保证）。
    """
    if not shadow:
        return decision, exit_code, None
    kill_switch_hit = any(v.get("rule_id") == "kill_switch" for v in violations)
    if exit_code in (4, 5) or kill_switch_hit:
        return decision, exit_code, decision  # shadow 不得吞掉 kill switch / 调用方错误 / 引擎异常
    return "PASS", 0, decision


class GuardEngine:
    """决策引擎。policy 与规则集均在构造期校验（fail-fast，拒绝进入不合法状态）。"""

    def __init__(self, policy: Policy, rules: list[Rule] | None = None):
        # 配置非法 → 构造期拒绝（fail-fast；rules-spec §0.5：policy 非法 = exit 4）
        validate_inputs_policy(policy)
        rule_list = list(rules) if rules is not None else []
        ids = [r.rule_id for r in rule_list]
        id_set = set(ids)
        # -B §2.2：强制规则集合唯一来源 = registry.MANDATORY_RULE_IDS
        # （不含任何只覆盖 R1/R2/R12 的漂移常量；缺任一强制规则 = 闸门漏检，定义上非法）
        missing = [rid for rid in MANDATORY_RULE_IDS if rid not in id_set]
        if missing:
            raise InputValidationError(
                [f"规则集不合法：缺少强制规则 {missing}（强制规则恒启用，不可缺省）"]
            )
        # MIN-A2：rule_id 唯一性（重复会导致 evidence 覆盖 / 异常丢失）
        if len(ids) != len(id_set):
            dups = sorted({rid for rid in ids if ids.count(rid) > 1})
            raise InputValidationError([f"规则集不合法：rule_id 重复 {dups}"])
        self.policy = policy
        self._rules = rule_list

    def check(self, order: Order, portfolio: Portfolio, now: datetime | None = None) -> Result:
        """最外层兜底（DEF-A3）：任何非规则层异常 → exit 5 Result，不向上抛。

        结构边界说明：若 Result 构造器本身抛错（引擎输出契约被破坏），本层无法
        返回任何 Result——该场景由调用层（CLI/MCP fail_closed 语义）兜底。
        """
        stage = ["start"]
        try:
            return self._check(order, portfolio, now, stage)
        except Exception as exc:  # 禁止 except BaseException / except: pass
            return self._fallback_result(order, portfolio, exc, stage[0], now)

    def _check(self, order: Order, portfolio: Portfolio, now, stage) -> Result:
        now = now or datetime.now(timezone.utc)
        evaluated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        shadow = self.policy.mode == "shadow"

        # ---- 优先级 2：输入校验（失败 → BLOCK/4，不执行规则）----
        stage[0] = "validation"
        inactive_rules = [rid for rid in RULE_IDS if not is_rule_enabled(rid, self.policy)]
        try:
            validate_inputs(order, portfolio, self.policy)
        except InputValidationError as exc:
            violations = [
                {"rule_id": "input_validity", "severity": "BLOCK", "detail": d}
                for d in exc.details
            ]
            evidence = {
                "input_hash": _canonical_hash(order, portfolio, self.policy),
                "order_summary": _order_summary(order),
                "inactive_rules": inactive_rules,
                "rule_evidence": {
                    "input_validity": [{"name": "input_error", "value": d} for d in exc.details]
                },
            }
            decision, exit_code, shadow_verdict = _project_outcome(shadow, "BLOCK", 4, violations)
            return Result(
                decision=decision,
                shadow_mode=shadow,
                shadow_verdict=shadow_verdict,
                exit_code=exit_code,
                evaluated_at=evaluated_at,
                violations=violations,
                warnings=[],
                evidence=evidence,
            )

        # ---- 规则循环（S-2：异常捕获后继续，不中断其余规则）----
        stage[0] = "rule_loop"
        ctx = RuleContext(order=order, portfolio=portfolio, policy=self.policy, now=now)
        violations: list = []
        warnings: list = []
        rule_evidence: dict = {}
        had_exception = False

        for rule in self._rules:
            try:
                outcome = rule.evaluate(ctx)
            except Exception as exc:  # 禁止 except BaseException（吞 KeyboardInterrupt/SystemExit）
                had_exception = True
                rule_evidence.setdefault(rule.rule_id, []).append(
                    {"name": "exception_type", "value": type(exc).__name__}
                )
                rule_evidence.setdefault(rule.rule_id, []).append(
                    {"name": "exception_detail", "value": str(exc)[:200]}
                )
                continue  # 异常不中断其余规则的执行
            violations.extend(outcome.violations)
            warnings.extend(outcome.warnings)
            # MIN-A2：累加语义（与异常路径一致；构造期已禁止同 rule_id 重复，此处为纵深防御）
            rule_evidence.setdefault(rule.rule_id, []).extend(outcome.evidence)

        # ---- S-1 决策合成（真值表，单一出口，命中即停）----
        stage[0] = "synthesis"
        if had_exception:
            internal_decision, internal_exit = "BLOCK", 5   # 优先级 1
        elif violations:
            internal_decision, internal_exit = "BLOCK", 3   # 优先级 3（输入校验已先于规则执行）
        elif warnings:
            internal_decision, internal_exit = "WARN", 2    # 优先级 4
        else:
            internal_decision, internal_exit = "PASS", 0    # 优先级 5：唯一 PASS 路径
        # shadow 对外投影（唯一入口）：规则已完整执行并得到内部裁决，再做投影
        decision, exit_code, shadow_verdict = _project_outcome(
            shadow, internal_decision, internal_exit, violations
        )

        stage[0] = "post_rules"
        evidence = {
            "input_hash": _canonical_hash(order, portfolio, self.policy),
            "order_summary": _order_summary(order),
            "inactive_rules": inactive_rules,
            "rule_evidence": rule_evidence,
        }
        return Result(
            decision=decision,
            shadow_mode=shadow,
            shadow_verdict=shadow_verdict,
            exit_code=exit_code,
            evaluated_at=evaluated_at,
            violations=violations,
            warnings=warnings,
            evidence=evidence,
        )

    def _fallback_result(self, order, portfolio, exc, stage: str, now) -> Result:
        """DEF-A3 兜底：exit 5，异常类型与发生阶段写入 evidence；尽力构造输入摘要。"""
        now = now or datetime.now(timezone.utc)
        try:
            input_hash = _canonical_hash(order, portfolio, self.policy)
        except Exception:
            input_hash = "0" * 64  # 摘要不可用时占位（64 个 0，审计可见为"不可用"）
        try:
            summary = _order_summary(order)
        except Exception:
            summary = {}
        shadow = self.policy.mode == "shadow"
        #  §四：兜底必须直接、无依赖地构造 BLOCK/exit 5——不得再次调用
        # 可能已经损坏的 _project_outcome（否则兜底自身抛异常会逃逸出 check()）。
        # shadow 下保留内部裁决 shadow_verdict=BLOCK（不得投影为 PASS）。
        decision, exit_code = "BLOCK", 5
        shadow_verdict = "BLOCK" if shadow else None
        return Result(
            decision=decision,
            shadow_mode=shadow,
            shadow_verdict=shadow_verdict,
            exit_code=exit_code,
            evaluated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            violations=[],
            warnings=[],
            evidence={
                "input_hash": input_hash,
                "order_summary": summary,
                "inactive_rules": [rid for rid in RULE_IDS if not is_rule_enabled(rid, self.policy)],
                "rule_evidence": {
                    "engine": [
                        {"name": "exception_type", "value": type(exc).__name__},
                        {"name": "exception_detail", "value": str(exc)[:200]},
                        {"name": "stage", "value": stage},
                    ]
                },
            },
        )


def validate_inputs_policy(policy: Policy) -> None:
    """policy 单独校验（构造期）。失败抛 InputValidationError。"""
    from ._validation import _validator, schema_error_text

    errs = sorted(_validator("policy").iter_errors(policy.to_dict()), key=lambda e: list(e.path))
    if errs:
        details = [schema_error_text("policy", e) for e in errs]
        raise InputValidationError(details)
