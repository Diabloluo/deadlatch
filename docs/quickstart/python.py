#!/usr/bin/env python3
"""Deadlatch — Python API Quick Start（60 秒， §6.1）。

- 使用当前真实构造器（Guard.from_policy / Order / Portfolio / Guard.check）；
- 全部虚构数据 + 临时目录（审计路径也在临时目录）；
- BLOCK 时示例调用方自行停止——本脚本绝不调用任何券商下单工具；
- 时间戳动态生成，永不因静态时间自然变红。

运行：python docs/quickstart/python.py
"""

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from deadlatch import Guard, Order, Portfolio

POLICY_YAML = """\
schema_version: 2
version: '1.0.0'
mode: enforce
base_currency: USD
kill_switch: "off"
acknowledged_disabled: []
limits:
  max_order_quantity: 500
  max_order_value: 5000.0
  max_symbol_exposure_ratio: 0.10
  max_total_exposure_ratio: 0.60
  min_cash: 0.0
  max_options_margin_ratio: 0.35
  max_daily_loss_ratio: 0.03
  max_drawdown_ratio: 0.15
  max_order_age_seconds: 300
  max_snapshot_age_seconds: 300
"""


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _portfolio(now: datetime) -> dict:
    return {
        "schema_version": 3,
        "equity": 123456.78,
        "cash": 30000.0,
        "day_start_equity": 125000.0,
        "peak_equity": 128000.0,
        "daily_pnl": -1200.5,
        "drawdown_ratio": 0.0355,
        "snapshot_at": _ts(now - timedelta(seconds=61)),
        "base_currency": "USD",
        "positions": [],
    }


def _order(now: datetime, **overrides) -> dict:
    doc = {
        "schema_version": 2,
        "symbol": "AAA",
        "instrument_type": "stock",
        "side": "buy",
        "quantity": 20,
        "price": 190.0,
        "order_type": "limit",
        "currency": "USD",
        "created_at": _ts(now - timedelta(seconds=1)),
    }
    doc.update(overrides)
    return doc


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        policy_path = tmp / "policy.yaml"
        policy_path.write_text(POLICY_YAML, encoding="utf-8")
        guard = Guard.from_policy(str(policy_path), audit_path=str(tmp / "audit.jsonl"))
        now = datetime.now(timezone.utc).replace(microsecond=0)
        portfolio = Portfolio.from_dict(_portfolio(now))

        # 1) 合法订单 → PASS / 0
        result = guard.check(Order.from_dict(_order(now)), portfolio, now=now)
        print(f"[1] decision={result.decision} exit_code={result.exit_code}")
        assert result.decision == "PASS" and result.exit_code == 0

        # 2) 超量订单 → BLOCK / 3（命中 max_order_quantity）
        result = guard.check(
            Order.from_dict(_order(now, quantity=1000)), portfolio, now=now
        )
        print(f"[2] decision={result.decision} exit_code={result.exit_code}")
        for v in result.violations:
            print(f"    - [{v['rule_id']}] {v['detail']}")
        assert result.decision == "BLOCK" and result.exit_code == 3

        # 3) BLOCK 时调用方自行停止（Guard 永不下单）
        if result.decision == "BLOCK":
            print("[3] Order NOT submitted (decision=BLOCK).")
        else:
            raise AssertionError("BLOCK 订单必须被拦截")

        # 本地审计记录（每次 check 一条，30 天保留）
        print(f"    audit: {guard.audit_path}")

        # 审计 JSONL 与输入摘要（可用 shadow report 聚合）
        from deadlatch.audit import read_audit_records

        records = read_audit_records(guard.audit_path)
        print(f"    audit records: {len(records)}")
        assert len(records) == 2


if __name__ == "__main__":
    main()
