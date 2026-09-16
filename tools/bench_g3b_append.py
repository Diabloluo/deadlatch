#!/usr/bin/env python3
"""G3B 10,000-append timing evidence.

Three real Guard.check() rounds of 10,000 with fsync. Does not mock audit I/O.
Failed rounds are kept and printed. Timing thresholds are a regression check,
not a latency SLA.

Usage:
    python tools/bench_g3b_append.py
"""

from __future__ import annotations

import json
import os
import platform
import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from deadlatch import Guard, Order, Portfolio
from deadlatch.audit import initialize_audit_state, utc_shard_path

POLICY_YAML = (
    "schema_version: 2\nversion: '1.0.0'\nmode: enforce\nbase_currency: USD\n"
    'kill_switch: "off"\nacknowledged_disabled: []\nlimits:\n'
    "  max_order_quantity: 500\n  max_order_value: 5000.0\n"
    "  max_symbol_exposure_ratio: 0.10\n  max_total_exposure_ratio: 0.60\n"
    "  min_cash: 0.0\n  max_options_margin_ratio: 0.35\n"
    "  max_daily_loss_ratio: 0.03\n  max_drawdown_ratio: 0.15\n"
    "  max_order_age_seconds: 300\n  max_snapshot_age_seconds: 300\n"
)


def _ms(samples: list[float]) -> dict:
    ordered = sorted(samples)
    return {
        "median_ms": round(statistics.median(ordered) * 1000, 3),
        "p95_ms": round(ordered[int(0.95 * (len(ordered) - 1))] * 1000, 3),
    }


def _round(tmp: Path, now: datetime) -> dict:
    policy = tmp / "policy.yaml"
    policy.write_text(POLICY_YAML, encoding="utf-8")
    audit = tmp / "audit.jsonl"
    t_init = time.perf_counter()
    initialize_audit_state(audit)
    init_s = round(time.perf_counter() - t_init, 6)
    guard = Guard.from_policy(str(policy), audit_path=str(audit))
    pf = Portfolio.from_dict({
        "schema_version": 3, "equity": 123456.78, "cash": 30000.0,
        "day_start_equity": 125000.0, "peak_equity": 128000.0,
        "daily_pnl": -1200.5, "drawdown_ratio": 0.0355,
        "snapshot_at": (now - timedelta(seconds=61)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "base_currency": "USD", "positions": [],
    })
    order = Order.from_dict({
        "schema_version": 2, "symbol": "AAA", "instrument_type": "stock",
        "side": "buy", "quantity": 20, "price": 190.0, "order_type": "limit",
        "currency": "USD",
        "created_at": (now - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    samples: list[float] = []
    t0 = time.perf_counter()
    for _ in range(10000):
        t = time.perf_counter()
        result = guard.check(order, pf, now=now)
        samples.append(time.perf_counter() - t)
        if result.exit_code not in (0, 2, 3):
            raise RuntimeError(f"unexpected exit {result.exit_code}")
    total = time.perf_counter() - t0
    shard = utc_shard_path(audit, now)
    first = _ms(samples[0:500])
    mid = _ms(samples[4750:5250])
    last = _ms(samples[9500:10000])
    delta = last["median_ms"] - first["median_ms"]
    passed = last["median_ms"] <= first["median_ms"] * 3 and delta <= 5
    return {
        "first_1_500": first,
        "mid_4751_5250": mid,
        "last_9501_10000": last,
        "total_s": round(total, 3),
        "shard_bytes": shard.stat().st_size if shard.exists() else 0,
        "init_s": init_s,
        "pass": passed,
    }


def main() -> int:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    rounds = []
    with tempfile.TemporaryDirectory(prefix="deadlatch-g3b-bench-") as raw:
        root = Path(raw)
        for i in range(3):
            rnd = root / f"round-{i + 1}"
            rnd.mkdir()
            summary = _round(rnd, now)
            rounds.append(summary)
            print(f"round {i + 1}: {json.dumps(summary, ensure_ascii=False)}")
    passed = sum(1 for item in rounds if item["pass"])
    env = {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "filesystem": "tempfile",
        "cwd_fs": getattr(os.statvfs("."), "f_fstypename", None) or platform.system(),
    }
    print("environment:", json.dumps(env, ensure_ascii=False))
    print(f"passed_rounds={passed}/3")
    return 0 if passed >= 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
