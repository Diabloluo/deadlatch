#!/usr/bin/env python3
"""离线刷新 examples/ 的时间戳（-B §2.5：CLI 示例可直接执行）。

原理：examples 的 order.json / open_order.json / close_order.json 的
created_at 与 portfolio.json 的 snapshot_at 是生成时刻的静态时间戳，随墙上
时间推移会触发 R10/R11 陈旧 BLOCK（预期行为）。本工具把全部示例时间戳原地
重写为"当前时刻 − 固定偏移"（订单 = now−1s，快照 = now−61s，均在 300s 新鲜
窗口内），完全离线、无网络、无券商依赖。其余字段原样保留。

订单文件统一按 `<场景目录>/*order.json` 枚举（覆盖 order.json / open_order.json /
close_order.json 及任何未来变体）；快照按 portfolio.json 独立检查与刷新。

用法（仓库根目录）：
    python tools/refresh_examples.py [目标目录]  # 默认 examples/；刷新全部场景
    python tools/refresh_examples.py --check     # 只检查是否过期（不写盘）
退出码：0 = 全部新鲜 / 无需刷新；1 = 有文件需要刷新或发生错误。
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"

ORDER_OFFSET = timedelta(seconds=-1)     # created_at = now − 1s
SNAPSHOT_OFFSET = timedelta(seconds=-61)  # snapshot_at = now − 61s
FRESH_WINDOW = timedelta(seconds=300)     # 与 policy limits.max_*_age_seconds 默认一致


def _parse(ts: str) -> datetime | None:
    """RFC3339 解析（容忍 Z / ±HH:MM）。不可解析 → None。"""
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _order_stale(created_raw: str, now: datetime) -> bool:
    """订单 created_at 需刷新：不可解析 / 陈旧（>300s）/ 未来超容忍（>300s）。"""
    created = _parse(created_raw)
    if created is None:
        return True
    age = now - created
    if age < -FRESH_WINDOW:
        return True  # 未来超 R10 容忍 → BLOCK
    return age > FRESH_WINDOW


def _snapshot_stale(snap_raw: str, now: datetime) -> bool:
    """快照 snapshot_at 需刷新：不可解析 / 未来快照 / 陈旧（>300s）。独立判定。"""
    snap = _parse(snap_raw)
    if snap is None:
        return True
    if snap > now:
        return True  # 未来快照 → R11 BLOCK
    return (now - snap) > FRESH_WINDOW


def _scenario_files(scenario: Path) -> tuple[list[Path], Path | None]:
    """场景内订单文件（*order.json，含 open/close 变体）与 portfolio.json。"""
    orders = sorted(scenario.glob("*order.json"))
    snap = scenario / "portfolio.json"
    return orders, snap if snap.exists() else None


def _scan(target: Path, now: datetime) -> tuple[list[Path], list[Path]]:
    """返回 (需刷新的订单文件列表, 需刷新的快照文件列表)。"""
    stale_orders: list[Path] = []
    stale_snapshots: list[Path] = []
    for scenario in sorted(p for p in target.iterdir() if p.is_dir()):
        orders, snap_path = _scenario_files(scenario)
        if not orders and snap_path is None:
            continue  # 空目录，跳过
        for p in orders:
            data = json.loads(p.read_text(encoding="utf-8"))
            if _order_stale(data.get("created_at", ""), now):
                stale_orders.append(p)
        if snap_path is not None:
            data = json.loads(snap_path.read_text(encoding="utf-8"))
            if _snapshot_stale(data.get("snapshot_at", ""), now):
                stale_snapshots.append(snap_path)
    return stale_orders, stale_snapshots


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="refresh_examples", description="离线刷新 examples/ 时间戳"
    )
    parser.add_argument("target", nargs="?", default=None,
                        help="示例目录（默认仓库 examples/；测试用 tmp 副本时指定）")
    parser.add_argument("--check", action="store_true", help="只检查是否过期，不写盘")
    args = parser.parse_args(argv)
    target = Path(args.target).resolve() if args.target else EXAMPLES
    if not target.is_dir():
        print(f"错误：目录不存在 {target}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    stale_orders, stale_snapshots = _scan(target, now)
    needs_refresh = stale_orders + stale_snapshots

    if args.check:
        if needs_refresh:
            print(f"过期/需刷新: {len(needs_refresh)} 个文件")
            for p in stale_orders:
                print(f"  - {p.relative_to(target)}（订单 created_at 陈旧/不可解析/未来）")
            for p in stale_snapshots:
                print(f"  - {p.relative_to(target)}（快照 snapshot_at 陈旧/不可解析/未来）")
            return 1
        print("全部示例时间戳新鲜（无需刷新）")
        return 0

    if not needs_refresh:
        print("全部示例时间戳新鲜（无需刷新）")
        return 0

    order_ts = (now + ORDER_OFFSET).strftime("%Y-%m-%dT%H:%M:%SZ")
    snap_ts = (now + SNAPSHOT_OFFSET).strftime("%Y-%m-%dT%H:%M:%SZ")
    for scenario in sorted(p for p in target.iterdir() if p.is_dir()):
        orders, snap_path = _scenario_files(scenario)
        if not orders and snap_path is None:
            continue
        if snap_path is not None:
            data = json.loads(snap_path.read_text(encoding="utf-8"))
            data["snapshot_at"] = snap_ts
            snap_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        for p in orders:
            data = json.loads(p.read_text(encoding="utf-8"))
            data["created_at"] = order_ts
            p.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        print(f"刷新 {scenario.name}/（{len(orders)} 个订单 created_at={order_ts}，snapshot_at={snap_ts}）")
    print("\n完成。README 中的 CLI 示例现在可直接执行；测试无需刷新（固定时钟派生）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
