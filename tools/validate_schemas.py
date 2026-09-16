#!/usr/bin/env python3
""" D4 校验（第 2 轮增强版）：meta-schema 自校验 + 正例 + 反例。

反例部分断言"拒绝原因"（期望错误信息子串必须命中），而非仅断言"产生了错误"——
防止"捕获异常即打印 OK"式的假全绿。
用法: python validate_schemas.py   （需要 jsonschema>=4；退出码 0=全过 1=失败）
"""
import json, sys
from pathlib import Path
from jsonschema import Draft202012Validator

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
FILES = [
    "order.schema.json",
    "portfolio.schema.json",
    "policy.schema.json",
    "result.schema.json",
    "audit-record.schema.json",
    "shadow-report.schema.json",
    "audit-maintenance-result.schema.json",
    "audit-prune-result.schema.json",
    "audit-write-state.schema.json",
    "audit-state-result.schema.json",
]

def main() -> int:
    ok = True

    # ---- 1) meta-schema 自校验（Draft 2020-12）----
    print("== 1) meta-schema 自校验 ==")
    metas = Draft202012Validator(Draft202012Validator.META_SCHEMA)
    for f in FILES:
        doc = json.loads((SCHEMA_DIR / f).read_text(encoding="utf-8"))
        errs = sorted(metas.iter_errors(doc), key=lambda e: list(e.path))
        if errs:
            ok = False
            print(f"[FAIL] {f}: {len(errs)} meta 错误")
            for e in errs[:5]:
                print(f"   - {list(e.path)}: {e.message}")
        else:
            print(f"[OK]   {f} 通过 meta-schema 校验（0 错误）")

    # ---- 2) 各 Schema 正例实例 ----
    print("\n== 2) 正例实例校验 ==")
    now = "2026-08-29T10:00:01Z"
    order_ok = {
        "schema_version": 2, "request_id": "req-1",
        "symbol": "AAA", "instrument_type": "stock",
        "side": "buy", "quantity": 20, "price": 190.0,
        "order_type": "limit", "currency": "USD", "created_at": now,
    }
    order_opt = {
        "schema_version": 2, "request_id": "req-2",
        "symbol": "AAA 260918C00190000", "instrument_type": "option",
        "side": "sell_to_open", "quantity": 10, "price": 2.5,
        "order_type": "limit", "currency": "USD", "created_at": now,
        "option": {"underlying": "AAA", "expiry": "2026-09-18", "strike": 190.0, "right": "put", "multiplier": 100},
    }
    portfolio_ok = {
        "schema_version": 3, "equity": 123456.78, "cash": 30000.0,
        "day_start_equity": 125000.0, "peak_equity": 128000.0,
        "daily_pnl": -1200.5, "drawdown_ratio": 0.0355,
        "snapshot_at": "2026-08-29T09:59:00+08:00", "base_currency": "USD",
        "positions": [
            {"symbol": "AAA", "instrument_type": "stock", "side": "long",
             "quantity": 100, "market_value": 19000.0, "avg_cost": 185.0,
             "currency": "USD", "unrealized_pnl": 500.0},
            {"symbol": "AAA 260918P00190000", "instrument_type": "option", "side": "short",
             "quantity": 2, "market_value": 500.0, "avg_cost": 2.5, "currency": "USD",
             "option": {"underlying": "AAA", "expiry": "2026-09-18", "strike": 190.0, "right": "put", "multiplier": 100}},
        ],
    }
    portfolio_zero_equity = {**portfolio_ok, "equity": 0}
    portfolio_neg_equity = {**portfolio_ok, "equity": -5000.0}
    policy_ok = {
        "schema_version": 2, "version": "1.0.0", "mode": "enforce",
        "base_currency": "USD", "kill_switch": "off", "acknowledged_disabled": [],
        "limits": {
            "max_order_quantity": 500, "max_order_value": 5000.0,
            "max_symbol_exposure_ratio": 0.10, "max_total_exposure_ratio": 0.60,
            "min_cash": 0.0, "max_options_margin_ratio": 0.35,
            "max_daily_loss_ratio": 0.03, "max_drawdown_ratio": 0.15,
            "max_order_age_seconds": 300, "max_snapshot_age_seconds": 300,
        },
    }
    result_ok = {
        "schema_version": 2, "request_id": "req-1",
        "decision": "PASS", "shadow_mode": False, "shadow_verdict": None,
        "exit_code": 0, "evaluated_at": now, "violations": [], "warnings": [],
        "evidence": {
            "input_hash": "a" * 64,
            "order_summary": {"symbol": "AAA", "instrument_type": "stock", "side": "buy_to_open",
                              "quantity": 20, "price": 190.0, "currency": "USD", "created_at": now},
            "rule_evidence": {"data_freshness": [{"name": "age_seconds", "value": 42}]},
            "inactive_rules": [],
        },
    }
    audit_ok = {
        "schema_version": 1, "record_id": "rec-0001", "evaluated_at": now,
        "input_hash": "a" * 64, "decision": "BLOCK", "shadow_mode": False,
        "shadow_verdict": None, "exit_code": 3, "policy_version": "1.0.0",
        "rule_hits": [{"rule_id": "kill_switch", "severity": "BLOCK", "detail": "kill switch engaged"}],
    }
    report_ok = {
        "schema_version": 1,
        "window_start": "2026-08-01T00:00:00Z", "window_end": "2026-08-29T23:59:59Z",
        "generated_at": now,
        "totals": {"orders_evaluated": 412, "would_block": 7, "would_warn": 23,
                   "block_rate": 0.017, "warn_rate": 0.056},
        "by_rule": [{"rule_id": "max_symbol_exposure", "block_count": 4, "warn_count": 0,
                     "worst_case": {"detail": "38.1% vs limit 10.0%", "triggered_value": 0.381, "limit": 0.10}}],
        "notes": ["fictional demo data"],
    }
    maintain_ok = {
        "schema_version": 1, "operation": "verify", "status": "clean",
        "valid_lines": 2, "invalid_lines": 0, "issues": [],
        "issues_truncated": False,
    }
    audit_v2_ok = {
        **audit_ok,
        "schema_version": 2,
        "prev_hash": None,
        "record_hash": "b" * 64,
    }
    prune_ok = {
        "schema_version": 1, "operation": "prune", "status": "clean",
        "removed_segments": 0, "kept_segments": 2, "future_segments": 0,
    }
    write_state_ok = {
        "protocol_version": 1, "base_name": "audit.jsonl",
        "reserved_through": "2026-09-16",
    }
    write_state_null = {
        "protocol_version": 1, "base_name": "audit.jsonl",
        "reserved_through": None,
    }
    state_result_ok = {
        "schema_version": 1, "operation": "init", "status": "initialized",
        "reserved_through": None, "error_code": None,
    }
    cases = [
        ("order.schema.json", order_ok), ("order.schema.json#option", order_opt),
        ("portfolio.schema.json", portfolio_ok),
        ("portfolio.schema.json#equity=0（FIN-4 正例：应通过 Schema，规则层 BLOCK）", portfolio_zero_equity),
        ("portfolio.schema.json#equity<0（FIN-4 正例：应通过 Schema，规则层 BLOCK）", portfolio_neg_equity),
        ("policy.schema.json", policy_ok),
        ("result.schema.json", result_ok), ("audit-record.schema.json", audit_ok),
        ("audit-record.schema.json#v2", audit_v2_ok),
        ("shadow-report.schema.json", report_ok),
        ("audit-maintenance-result.schema.json", maintain_ok),
        ("audit-prune-result.schema.json", prune_ok),
        ("audit-write-state.schema.json", write_state_ok),
        ("audit-write-state.schema.json#null", write_state_null),
        ("audit-state-result.schema.json", state_result_ok),
    ]
    for name, inst in cases:
        f = name.split("#")[0]
        v = Draft202012Validator(json.loads((SCHEMA_DIR / f).read_text(encoding="utf-8")))
        errs = sorted(v.iter_errors(inst), key=lambda e: list(e.path))
        if errs:
            ok = False
            print(f"[FAIL] {name}: {len(errs)} 错误")
            for e in errs[:5]:
                print(f"   - {list(e.path)}: {e.message}")
        else:
            print(f"[OK]   {name} 正例通过（0 错误）")

    # ---- 3) 反例（应失败，且拒绝原因必须命中期望子串）----
    print("\n== 3) 反例校验（断言拒绝原因，应全部命中期望子串）==")
    negs = [
        ("order.schema.json", "schema_version 高于当前（v3，NEW-13）", {**order_ok, "schema_version": 3},
         ["was expected"]),
        ("order.schema.json", "schema_version 低于当前（v1，NEW-13）", {**order_ok, "schema_version": 1},
         ["was expected"]),
        ("order.schema.json", "schema_version 缺失（NEW-13）", {k: v for k, v in order_ok.items() if k != "schema_version"},
         ["'schema_version' is a required property"]),
        ("order.schema.json", "未知字段 hack", {**order_ok, "hack": 1},
         ["Additional properties are not allowed"]),
        ("order.schema.json", "缺必填 currency", {k: v for k, v in order_ok.items() if k != "currency"},
         ["'currency' is a required property"]),
        ("order.schema.json", "正股 side=buy_to_open 非法（NEW-10）", {**order_ok, "side": "buy_to_open"},
         ["'buy_to_open' is not one of ['buy', 'sell']"]),
        ("order.schema.json", "期权 side=buy 非法（NEW-10）", {**order_opt, "side": "buy"},
         ["'buy' is not one of ['buy_to_open', 'sell_to_open', 'buy_to_close', 'sell_to_close']"]),
        ("order.schema.json", "期权缺 option 对象", {k: v for k, v in order_opt.items() if k != "option"},
         ["'option' is a required property"]),
        ("order.schema.json", "quantity=0", {**order_ok, "quantity": 0},
         ["less than or equal to the minimum of 0"]),
        ("order.schema.json", "created_at 无时区", {**order_ok, "created_at": "2026-08-29T10:00:01"},
         ["does not match"]),
        ("portfolio.schema.json", "position 缺 currency", {**portfolio_ok, "positions": [{k: v for k, v in portfolio_ok["positions"][0].items() if k != "currency"}]},
         ["'currency' is a required property"]),
        ("policy.schema.json", "base_currency=HKD（const 拒绝）", {**policy_ok, "base_currency": "HKD"},
         ["'USD' was expected"]),
        ("policy.schema.json", "mode 非法 dry", {**policy_ok, "mode": "dry"},
         ["'dry' is not one of"]),
        ("policy.schema.json", "max_order_quantity 非整数 500.5（MIN-6）", {**policy_ok, "limits": {**policy_ok["limits"], "max_order_quantity": 500.5}},
         ["is not of type 'integer'"]),
        ("policy.schema.json", "kill_switch 非法值 enable（OPEN-1 三态枚举）", {**policy_ok, "kill_switch": "enable"},
         ["'enable' is not one of ['off', 'full', 'reduce_only']"]),
        ("policy.schema.json", "acknowledged_disabled 拼写错误 typo_rule", {**policy_ok, "acknowledged_disabled": ["typo_rule"]},
         ["'typo_rule' is not one of"]),
        ("policy.schema.json", "矛盾：max_order_quantity 启用中却被承认禁用", {**policy_ok, "acknowledged_disabled": ["max_order_quantity"]},
         ["should not be valid under"]),
        ("policy.schema.json", "主方向：max_symbol_exposure_ratio 缺失且未承认禁用", {**{k: v for k, v in policy_ok.items() if k != "acknowledged_disabled"}, "acknowledged_disabled": [], "limits": {k: v for k, v in policy_ok["limits"].items() if k != "max_symbol_exposure_ratio"}},
         ["does not contain"]),
        ("policy.schema.json", "主方向：强制规则 max_daily_loss_ratio 缺失", {**policy_ok, "limits": {k: v for k, v in policy_ok["limits"].items() if k != "max_daily_loss_ratio"}},
         ["'max_daily_loss_ratio' is a required property"]),
        ("result.schema.json", "exit_code=1 非法", {**result_ok, "exit_code": 1},
         ["is not one of [0, 2, 3, 4, 5]"]),
        ("audit-record.schema.json", "缺 input_hash", {k: v for k, v in audit_ok.items() if k != "input_hash"},
         ["is not valid under any of the given schemas"]),
        ("shadow-report.schema.json", "totals 缺 would_block", {**report_ok, "totals": {k: v for k, v in report_ok["totals"].items() if k != "would_block"}},
         ["'would_block' is a required property"]),
        ("audit-maintenance-result.schema.json", "缺 status", {k: v for k, v in maintain_ok.items() if k != "status"},
         ["'status' is a required property"]),
    ]
    for f, label, inst, expect in negs:
        v = Draft202012Validator(json.loads((SCHEMA_DIR / f).read_text(encoding="utf-8")))
        errs = list(v.iter_errors(inst))
        if not errs:
            ok = False
            print(f"[FAIL] {f} 反例({label}) 未被拒绝！")
            continue
        joined = " || ".join(e.message for e in errs)
        hit = [sub for sub in expect if sub in joined]
        if hit:
            print(f"[OK]   {f} 反例({label}) 拒绝原因命中: {hit[0]!r}")
        else:
            ok = False
            print(f"[FAIL] {f} 反例({label}) 被拒绝但原因不符，期望命中 {expect}")
            for e in errs[:5]:
                print(f"   - 实际错误: {e.message}")

    print("\n== 总结果 ==")
    print("ALL SCHEMA CHECKS PASSED" if ok else "SCHEMA CHECKS FAILED")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
