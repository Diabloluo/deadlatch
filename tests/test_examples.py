"""示例场景测试：examples/ 全部可离线运行，退出码符合 README 预期。

时间策略（-B §2.5）：评估时钟由示例文件自身时间戳派生
（max(created_at, snapshot_at) + 1s），不随墙上时间自然变红——无论何时
运行，R10/R11 都按文件内相对关系评估；CLI 复现请先执行
`python tools/refresh_examples.py` 刷新时间戳（README 有说明）。
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from deadlatch import Guard, Order, Portfolio
from deadlatch._timeutil import parse_rfc3339

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _derive_now(order: Order, portfolio: Portfolio) -> datetime:
    """固定评估时钟：文件时间戳的最大值 + 1s（文件内相对关系恒定，与墙钟无关）。"""
    stamps = []
    for value in (order.data.get("created_at"), portfolio.data.get("snapshot_at")):
        parsed = parse_rfc3339(value or "")
        if parsed is not None:
            stamps.append(parsed)
    if not stamps:
        return datetime.now(timezone.utc)  # 防御：文件时间戳不可解析（R12 会 exit 3）
    return max(stamps) + timedelta(seconds=1)


def _check(scenario: str, order_name: str = "order.json") -> int:
    base = EXAMPLES / scenario
    guard = Guard.from_policy(str(base / "policy.yaml"))
    order = Order.from_dict(json.loads((base / order_name).read_text(encoding="utf-8")))
    portfolio = Portfolio.from_dict(
        json.loads((base / "portfolio.json").read_text(encoding="utf-8"))
    )
    return guard.check(order, portfolio, now=_derive_now(order, portfolio)).exit_code


def test_example_01_pass():
    assert _check("01_pass") == 0


def test_example_02_symbol_exposure_block():
    assert _check("02_symbol_exposure") == 3


def test_example_03_daily_loss_block():
    assert _check("03_daily_loss") == 3


def test_example_04_kill_full_block():
    assert _check("04_kill_full") == 3


def test_example_05_reduce_only_open_block():
    assert _check("05_kill_reduce_only", "open_order.json") == 3


def test_example_05_reduce_only_close_pass():
    assert _check("05_kill_reduce_only", "close_order.json") == 0


def test_example_06_seller_option_r5_block():
    r = _check("06_seller_option")
    assert r == 3


def test_example_06_evidence_shows_strike_basis():
    base = EXAMPLES / "06_seller_option"
    guard = Guard.from_policy(str(base / "policy.yaml"))
    order = Order.from_dict(json.loads((base / "order.json").read_text(encoding="utf-8")))
    portfolio = Portfolio.from_dict(
        json.loads((base / "portfolio.json").read_text(encoding="utf-8"))
    )
    result = guard.check(order, portfolio, now=_derive_now(order, portfolio))
    ev = result.evidence["rule_evidence"].get("max_symbol_exposure", [])
    assert any(e["name"] == "order_delta" and e["value"] == "190000.0" for e in ev)
