"""tools/refresh_examples.py 回归测试（-B 最后一项）。

证明：05_kill_reduce_only 的 open_order.json / close_order.json 双订单均被
刷新与检查；快照 snapshot_at 被独立检查（不可解析 / 未来 / 陈旧 → 报需刷新）。
全部在 tmp_path 副本上运行，不改动仓库 examples/。
"""

import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import refresh_examples

TARGET = "05_kill_reduce_only"


def _copy_examples(tmp_path) -> Path:
    target = tmp_path / "examples"
    shutil.copytree(REPO / "examples", target)
    return target


def _read(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path: Path, data: dict) -> None:
    Path(path).write_text(json.dumps(data), encoding="utf-8")


def _ts(offset: timedelta) -> str:
    return (datetime.now(timezone.utc) + offset).strftime("%Y-%m-%dT%H:%M:%SZ")


def _order_paths(scenario: Path) -> list[Path]:
    return sorted(scenario.glob("*order.json"))


def test_refresh_rewrites_both_05_orders(tmp_path, capsys):
    target = _copy_examples(tmp_path)
    scenario = target / TARGET
    stale = _ts(timedelta(hours=-2))
    for name in ("open_order.json", "close_order.json"):
        data = _read(scenario / name)
        data["created_at"] = stale
        _write(scenario / name, data)

    rc = refresh_examples.main([str(target)])
    assert rc == 0
    capsys.readouterr()  # 清空输出

    now = datetime.now(timezone.utc)
    for name in ("open_order.json", "close_order.json"):
        created = _read(scenario / name)["created_at"]
        assert created != stale, f"{name} 未被刷新"
        parsed = datetime.fromisoformat(created.replace("Z", "+00:00"))
        assert (now - parsed) < timedelta(seconds=300), f"{name} 刷新后仍不新鲜"

    snap = _read(scenario / "portfolio.json")["snapshot_at"]
    snap_dt = datetime.fromisoformat(snap.replace("Z", "+00:00"))
    assert snap_dt <= now and (now - snap_dt) < timedelta(seconds=300)


def test_check_flags_stale_05_open_order_only(tmp_path, capsys):
    target = _copy_examples(tmp_path)
    assert refresh_examples.main([str(target)]) == 0  # 基线：副本先刷新到新鲜
    scenario = target / TARGET
    stale = _ts(timedelta(hours=-2))
    data = _read(scenario / "open_order.json")
    data["created_at"] = stale
    _write(scenario / "open_order.json", data)

    rc = refresh_examples.main(["--check", str(target)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "open_order.json" in out  # 陈旧的 open 订单被点名
    assert "close_order.json" not in out  # 新鲜的 close 订单不误报


def test_check_independently_flags_future_snapshot(tmp_path, capsys):
    # 订单全部新鲜，仅快照为未来 → --check 仍必须报需刷新（独立检查）
    target = _copy_examples(tmp_path)
    assert refresh_examples.main([str(target)]) == 0
    scenario = target / TARGET
    snap = _read(scenario / "portfolio.json")
    snap["snapshot_at"] = _ts(timedelta(hours=1))
    _write(scenario / "portfolio.json", snap)

    rc = refresh_examples.main(["--check", str(target)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "portfolio.json" in out
    assert "未来" in out


def test_check_fresh_copy_returns_0(tmp_path, capsys):
    target = _copy_examples(tmp_path)
    assert refresh_examples.main([str(target)]) == 0  # 先刷新到新鲜
    assert refresh_examples.main(["--check", str(target)]) == 0
    out = capsys.readouterr().out
    assert "无需刷新" in out


def test_refresh_leaves_other_fields_intact(tmp_path):
    target = _copy_examples(tmp_path)
    scenario = target / TARGET
    before = _read(scenario / "close_order.json")
    rc = refresh_examples.main([str(target)])
    assert rc == 0
    after = _read(scenario / "close_order.json")
    # 只改 created_at；symbol/quantity/price/side 等其余字段原样
    before.pop("created_at")
    after.pop("created_at")
    assert after == before
