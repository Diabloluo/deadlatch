#!/usr/bin/env bash
# Deadlatch — CLI Quick Start (60 seconds).
#
# - 全部虚构数据；输入文件在临时目录生成（动态时间戳，永不因静态时间自然变红）；
# - 展示 PASS(0)、BLOCK(3)、输入错误(4) 与 --json 输出中的 decision/exit_code；
# - 不调用任何不存在的命令（如 kill on / stop / place_order）。
#
# 运行：bash docs/quickstart/cli.sh （需要 deadlatch 在 PATH，或激活安装 venv）
set -euo pipefail

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

python3 - "$TMP" <<'PY'
import json, sys
from datetime import datetime, timedelta, timezone

tmp = sys.argv[1]
now = datetime.now(timezone.utc).replace(microsecond=0)

def ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

policy = {
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
portfolio = {
    "schema_version": 3, "equity": 123456.78, "cash": 30000.0,
    "day_start_equity": 125000.0, "peak_equity": 128000.0,
    "daily_pnl": -1200.5, "drawdown_ratio": 0.0355,
    "snapshot_at": ts(now - timedelta(seconds=61)),
    "base_currency": "USD", "positions": [],
}

def order(**overrides):
    doc = {
        "schema_version": 2, "symbol": "AAA", "instrument_type": "stock",
        "side": "buy", "quantity": 20, "price": 190.0, "order_type": "limit",
        "currency": "USD", "created_at": ts(now - timedelta(seconds=1)),
    }
    doc.update(overrides)
    return doc

with open(f"{tmp}/policy.yaml", "w", encoding="utf-8") as f:
    f.write("schema_version: 2\nversion: '1.0.0'\nmode: enforce\nbase_currency: USD\n"
            'kill_switch: "off"\nacknowledged_disabled: []\nlimits:\n'
            "  max_order_quantity: 500\n  max_order_value: 5000.0\n"
            "  max_symbol_exposure_ratio: 0.10\n  max_total_exposure_ratio: 0.60\n"
            "  min_cash: 0.0\n  max_options_margin_ratio: 0.35\n"
            "  max_daily_loss_ratio: 0.03\n  max_drawdown_ratio: 0.15\n"
            "  max_order_age_seconds: 300\n  max_snapshot_age_seconds: 300\n")
with open(f"{tmp}/portfolio.json", "w", encoding="utf-8") as f:
    json.dump(portfolio, f)
with open(f"{tmp}/order_pass.json", "w", encoding="utf-8") as f:
    json.dump(order(), f)
with open(f"{tmp}/order_block.json", "w", encoding="utf-8") as f:
    json.dump(order(quantity=1000), f)
with open(f"{tmp}/order_bad.json", "w", encoding="utf-8") as f:
    json.dump(order(currency="HKD"), f)
print(f"inputs written to {tmp}")
PY

AUDIT="--audit-path $TMP/audit.jsonl"

echo "== 1) PASS =="
deadlatch check --policy "$TMP/policy.yaml" --order "$TMP/order_pass.json" \
    --portfolio "$TMP/portfolio.json" $AUDIT
echo "exit=$?"

echo "== 2) BLOCK (exit 3) =="
set +e
deadlatch check --policy "$TMP/policy.yaml" --order "$TMP/order_block.json" \
    --portfolio "$TMP/portfolio.json" $AUDIT
echo "exit=$?"
set -e

echo "== 3) input error (exit 4) =="
set +e
deadlatch check --policy "$TMP/policy.yaml" --order "$TMP/order_bad.json" \
    --portfolio "$TMP/portfolio.json" $AUDIT
echo "exit=$?"
set -e

echo "== 4) JSON output =="
deadlatch check --policy "$TMP/policy.yaml" --order "$TMP/order_pass.json" \
    --portfolio "$TMP/portfolio.json" $AUDIT --json | python3 -c \
    "import json,sys; d=json.load(sys.stdin); print('decision:', d['decision'], 'exit_code:', d['exit_code'])"

echo "== 5) shadow report (audit of the checks above) =="
deadlatch shadow report --since 30d $AUDIT --json | python3 -c \
    "import json,sys; d=json.load(sys.stdin); print('orders_evaluated:', d['totals']['orders_evaluated'])"

echo "CLI Quick Start OK"
