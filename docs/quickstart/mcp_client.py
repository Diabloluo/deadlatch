#!/usr/bin/env python3
"""Deadlatch — MCP stdio Quick Start (60 seconds).

- 官方 MCP Python 客户端真实 subprocess stdio 连接 deadlatch-mcp；
- 展示服务器启动参数、五工具清单与 check_order（PASS 与 BLOCK）；
- policy/portfolio/audit 均为服务器进程启动配置——Agent 不能把它们作为
  工具参数替换；本脚本同样没有权限修改它们；
- BLOCK 是调用方必须遵守的约束语义；技术上 Guard 不能强制一个完全绕过
  它的 Agent 调用检查（advisory-only 诚实边界）。

运行：python docs/quickstart/mcp_client.py （需 deadlatch 已安装）
"""

import asyncio
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

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


def _text(result) -> dict:
    content = result.content[0]
    return json.loads(content.text if isinstance(content, TextContent) else "")


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        policy = tmp / "policy.yaml"
        policy.write_text(POLICY_YAML, encoding="utf-8")
        portfolio = tmp / "portfolio.json"
        portfolio.write_text(json.dumps({
            "schema_version": 3, "equity": 123456.78, "cash": 30000.0,
            "day_start_equity": 125000.0, "peak_equity": 128000.0,
            "daily_pnl": -1200.5, "drawdown_ratio": 0.0355,
            "snapshot_at": _ts(now - timedelta(seconds=61)),
            "base_currency": "USD", "positions": [],
        }), encoding="utf-8")
        audit = tmp / "audit.jsonl"

        # 服务器启动配置：policy/portfolio/audit 均为进程参数，不是工具入参
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "deadlatch.mcp_server",
                  "--policy", str(policy), "--portfolio", str(portfolio),
                  "--audit-path", str(audit)],
        )

        def order(**overrides):
            doc = {
                "schema_version": 2, "symbol": "AAA", "instrument_type": "stock",
                "side": "buy", "quantity": 20, "price": 190.0, "order_type": "limit",
                "currency": "USD", "created_at": _ts(now - timedelta(seconds=1)),
            }
            doc.update(overrides)
            return doc

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools = await session.list_tools()
                names = sorted(t.name for t in tools.tools)
                print("[1] tools:", ", ".join(names))
                assert names == ["check_order", "get_account_status", "get_policy",
                                 "kill_switch_status", "recent_decisions"]

                r = await session.call_tool("check_order", {"order": order()})
                body = _text(r)
                print(f"[2] decision={body['decision']} exit_code={body['exit_code']}")
                assert body["decision"] == "PASS" and body["exit_code"] == 0

                r = await session.call_tool("check_order", {"order": order(quantity=1000)})
                body = _text(r)
                print(f"[3] decision={body['decision']} exit_code={body['exit_code']}")
                for h in body.get("violations", []):
                    print(f"    - [{h['rule_id']}] {h['detail']}")
                assert body["decision"] == "BLOCK" and body["exit_code"] == 3

                # BLOCK 是必须遵守的约束；调用方自行停止（本脚本不调用任何券商工具）
                if body["decision"] == "BLOCK":
                    print("[4] Order NOT submitted (decision=BLOCK).")
                else:
                    raise AssertionError("BLOCK 订单必须被拦截")

                # 审计在本地 JSONL（启动配置路径），30 天保留
                from deadlatch.audit import read_audit_records

                records = read_audit_records(audit)
                print(f"    audit records: {len(records)} at {audit}")
                assert len(records) == 2


if __name__ == "__main__":
    asyncio.run(main())
    print("MCP Quick Start OK")
