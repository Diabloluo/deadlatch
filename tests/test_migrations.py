""" §七 Schema 迁移测试：三链正例、反例、幂等、CLI migrate。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from deadlatch.migrations import CURRENT_VERSIONS, MigrationError, migrate, migrate_json
from deadlatch.cli import main

REPO = Path(__file__).resolve().parents[1]


def _validator(name):
    return Draft202012Validator(json.loads((REPO / "schemas" / f"{name}.schema.json").read_text()))


# ---------------- order v1 → v2 ----------------

def _order_v1(side="buy_to_open", **extra):
    d = {"schema_version": 1, "symbol": "AAA", "side": side, "quantity": 20,
         "price": 190.0, "order_type": "limit", "currency": "USD",
         "created_at": "2026-08-29T10:00:00Z"}
    d.update(extra)
    return d


def test_order_v1_stock_four_sides_to_buy_sell():
    for v1_side, v2_side in (("buy_to_open", "buy"), ("buy_to_close", "buy"),
                             ("sell_to_open", "sell"), ("sell_to_close", "sell")):
        r = migrate("order", _order_v1(side=v1_side))
        assert r["from_version"] == 1 and r["to_version"] == 2
        assert r["document"]["side"] == v2_side
        assert r["document"]["instrument_type"] == "stock"
        assert r["document"]["schema_version"] == 2
        _validator("order").validate(r["document"])
        assert r["steps"] and "四值" in r["steps"][0]


def test_order_v1_option_four_sides_kept():
    for side in ("buy_to_open", "sell_to_open", "buy_to_close", "sell_to_close"):
        doc = _order_v1(side=side, symbol="AAA 260918P00190000", option={
            "underlying": "AAA", "expiry": "2026-09-18", "strike": 190.0,
            "right": "put", "multiplier": 100})
        r = migrate("order", doc)
        assert r["document"]["side"] == side  # 期权四值保持
        assert r["document"]["instrument_type"] == "option"
        _validator("order").validate(r["document"])


def test_order_v1_invalid_side_rejected():
    with pytest.raises(MigrationError):
        migrate("order", _order_v1(side="enable"))


# ---------------- policy v1 → v2 ----------------

_FULL_V1_LIMITS = {
    "max_order_quantity": 500, "max_order_value": 5000.0,
    "max_symbol_exposure_ratio": 0.10, "max_total_exposure_ratio": 0.60,
    "min_cash": 0.0, "max_options_margin_ratio": 0.35,
    "max_daily_loss_ratio": 0.03, "max_drawdown_ratio": 0.15,
    "max_order_age_seconds": 300, "max_snapshot_age_seconds": 300,
}


def _policy_v1(kill_switch=True, mode="enforce", **extra):
    d = {"schema_version": 1, "version": "1.0.0", "mode": mode,
         "base_currency": "USD", "kill_switch": kill_switch,
         "acknowledged_disabled": [],
         "limits": dict(_FULL_V1_LIMITS)}
    d.update(extra)
    return d


def test_policy_v1_false_to_off_true_to_full():
    # 带明确 mode/limits/acknowledged_disabled 的 v1：只转换 kill switch，其余原样
    r_off = migrate("policy", _policy_v1(False))
    assert r_off["document"]["kill_switch"] == "off"
    r_full = migrate("policy", _policy_v1(True))
    assert r_full["document"]["kill_switch"] == "full"  # 不得映射 reduce_only
    for r in (r_off, r_full):
        assert r["document"]["schema_version"] == 2
        assert r["document"]["mode"] == "enforce"  # 原样保留，不猜测
        assert r["document"]["acknowledged_disabled"] == []
        assert r["document"]["limits"] == _FULL_V1_LIMITS  # 不自动生成禁用
        _validator("policy").validate(r["document"])


def test_policy_v1_shadow_mode_preserved():
    r = migrate("policy", _policy_v1(True, mode="shadow"))
    assert r["document"]["mode"] == "shadow"  # shadow/enforce 均原样保留
    _validator("policy").validate(r["document"])


def test_policy_v1_missing_mode_rejected():
    doc = _policy_v1(True)
    del doc["mode"]
    with pytest.raises(MigrationError):
        migrate("policy", doc)  # 缺 mode → 迁移后 Schema 校验失败，不猜测补 enforce


def test_policy_v1_missing_acknowledged_rejected():
    # 缺 acknowledged_disabled 且可选规则状态不完整（limits 缺可选键）→ 拒绝
    doc = _policy_v1(True)
    del doc["acknowledged_disabled"]
    doc["limits"] = {k: v for k, v in _FULL_V1_LIMITS.items()
                     if k in ("max_daily_loss_ratio", "max_drawdown_ratio",
                              "max_order_age_seconds", "max_snapshot_age_seconds")}
    with pytest.raises(MigrationError):
        migrate("policy", doc)


def test_policy_v1_non_bool_kill_switch_rejected():
    with pytest.raises(MigrationError):
        migrate("policy", _policy_v1(kill_switch="enable"))


def test_policy_v1_no_automatic_disabled_generation():
    # 即使 limits 完整，迁移也不得自动生成 acknowledged_disabled
    doc = _policy_v1(True, acknowledged_disabled=["max_order_quantity"])
    doc["limits"].pop("max_order_quantity", None)
    r = migrate("policy", doc)
    assert r["document"]["acknowledged_disabled"] == ["max_order_quantity"]  # 原样保留


# ---------------- portfolio v1 → v2 → v3 ----------------

def _portfolio_v1(**extra):
    d = {"schema_version": 1, "equity": 123456.78, "cash": 30000.0,
         "day_start_equity": 125000.0, "peak_equity": 128000.0,
         "daily_pnl": -1200.5, "drawdown_pct": 0.0355,  # v1 命名带 pct，值为分数口径（NEW-5 不缩放）
         "snapshot_at": "2026-08-29T10:00:00Z", "base_currency": "USD", "positions": []}
    d.update(extra)
    return d


def test_portfolio_v1_to_v3_chain():
    r1 = migrate("portfolio", _portfolio_v1())
    assert r1["from_version"] == 1 and r1["to_version"] == 3
    doc = r1["document"]
    assert doc["schema_version"] == 3
    assert "drawdown_pct" not in doc
    assert doc["drawdown_ratio"] == 0.0355  # 改名不缩放
    assert len(r1["steps"]) == 2  # v1→v2、v2→v3 两步
    _validator("portfolio").validate(doc)


def test_portfolio_v2_to_v3_upgrade():
    v2 = {k: v for k, v in _portfolio_v1().items() if k != "drawdown_pct"}
    v2["drawdown_ratio"] = 0.0355
    v2["schema_version"] = 2
    r = migrate("portfolio", v2)
    assert r["from_version"] == 2 and r["to_version"] == 3
    assert r["document"]["schema_version"] == 3
    _validator("portfolio").validate(r["document"])


def test_portfolio_v1_ambiguous_both_fields_rejected():
    doc = _portfolio_v1(drawdown_ratio=0.0355)  # 新旧并存
    with pytest.raises(MigrationError):
        migrate("portfolio", doc)


def test_portfolio_v1_missing_drawdown_rejected():
    doc = {k: v for k, v in _portfolio_v1().items() if k != "drawdown_pct"}
    with pytest.raises(MigrationError):
        migrate("portfolio", doc)


# ---------------- 反例：缺版本/未来版/跳步/未知 kind ----------------

def test_missing_version_rejected():
    for kind in ("order", "policy", "portfolio"):
        with pytest.raises(MigrationError):
            migrate(kind, {"symbol": "AAA"})


def test_future_version_rejected():
    with pytest.raises(MigrationError):
        migrate("order", {"schema_version": 99})
    with pytest.raises(MigrationError):
        migrate("portfolio", {"schema_version": 5})


def test_unknown_kind_rejected():
    with pytest.raises(MigrationError):
        migrate("license", {"schema_version": 1})


def test_non_dict_rejected():
    with pytest.raises(MigrationError):
        migrate("order", ["not", "a", "dict"])


# ---------------- 幂等与 no-op ----------------

def test_current_version_noop():
    order_v2 = {"schema_version": 2, "symbol": "AAA", "instrument_type": "stock",
                "side": "buy", "quantity": 20, "price": 190.0, "order_type": "limit",
                "currency": "USD", "created_at": "2026-08-29T10:00:00Z"}
    r = migrate("order", order_v2)
    assert r["from_version"] == 2 and r["to_version"] == 2
    assert r["document"] == order_v2  # 内容不变
    assert "no-op" in r["steps"][0]


def test_repeat_migration_idempotent():
    doc = _order_v1()
    once = migrate("order", doc)["document"]
    twice = migrate("order", once)["document"]
    assert twice == once  # 迁移后再次迁移不改变内容


# ---------------- CLI migrate ----------------

def _cli_migrate(kind, input_path, *extra, cwd=None):
    return subprocess.run(
        [sys.executable, "-m", "deadlatch.cli", "migrate", "--kind", kind,
         "--input", str(input_path), *extra],
        capture_output=True, text=True, cwd=str(cwd or REPO),
        env={"PYTHONPATH": str(REPO / "src")}, timeout=60,
    )


def test_cli_migrate_stdout(tmp_path):
    inp = tmp_path / "order_v1.json"
    inp.write_text(json.dumps(_order_v1()), encoding="utf-8")
    r = _cli_migrate("order", inp)
    assert r.returncode == 0
    assert "order v1 → v2" in r.stdout
    assert '"side": "buy"' in r.stdout  # 迁移后文档输出
    assert r.stderr == ""


def test_cli_migrate_json_stdout_only_json(tmp_path):
    inp = tmp_path / "policy_v1.json"
    inp.write_text(json.dumps(_policy_v1(True)), encoding="utf-8")
    r = _cli_migrate("policy", inp, "--json")
    assert r.returncode == 0
    doc = json.loads(r.stdout)
    assert doc["kind"] == "policy" and doc["from_version"] == 1 and doc["to_version"] == 2
    assert doc["document"]["kill_switch"] == "full"


def test_cli_migrate_output_atomic_write(tmp_path):
    inp = tmp_path / "portfolio_v1.json"
    out = tmp_path / "migrated.json"
    inp.write_text(json.dumps(_portfolio_v1()), encoding="utf-8")
    r = _cli_migrate("portfolio", inp, "--output", str(out))
    assert r.returncode == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["schema_version"] == 3 and doc["drawdown_ratio"] == 0.0355
    assert not (tmp_path / "migrated.json.tmp").exists()  # 临时文件已替换


def test_cli_migrate_rejects_overwriting_input(tmp_path):
    inp = tmp_path / "order_v1.json"
    inp.write_text(json.dumps(_order_v1()), encoding="utf-8")
    before = inp.read_bytes()
    r = _cli_migrate("order", inp, "--output", str(inp))
    assert r.returncode == 4
    assert "覆盖输入" in r.stderr
    assert inp.read_bytes() == before  # 原文件不变


def test_cli_migrate_errors_exit4_no_traceback(tmp_path):
    inp = tmp_path / "bad.json"
    inp.write_text('{"schema_version": 99}', encoding="utf-8")
    r = _cli_migrate("order", inp)
    assert r.returncode == 4
    assert "error:" in r.stderr
    assert "Traceback" not in r.stdout and "Traceback" not in r.stderr


def test_cli_migrate_missing_input_exit4(tmp_path):
    r = _cli_migrate("order", tmp_path / "nope.json")
    assert r.returncode == 4
    assert "error:" in r.stderr


def test_migrate_module_current_versions():
    assert CURRENT_VERSIONS == {"order": 2, "policy": 2, "portfolio": 3}


def test_current_versions_read_from_schema_const(tmp_path):
    # FIX-005-2 探针：版本必须从 Schema 的 properties.schema_version.const 读取
    from deadlatch.migrations import load_current_versions

    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()
    for kind in ("order", "policy", "portfolio"):
        doc = json.loads((REPO / "schemas" / f"{kind}.schema.json").read_text())
        doc["properties"]["schema_version"]["const"] = 7  # 改动 const 探针
        (schema_dir / f"{kind}.schema.json").write_text(json.dumps(doc), encoding="utf-8")
    versions = load_current_versions(schema_dir)
    assert versions == {"order": 7, "policy": 7, "portfolio": 7}  # 随 const 变化，非硬编码


# ----------------  复核：正常评估入口拒绝旧版本 ----------------

def test_normal_entry_rejects_old_versions(tmp_path):
    # Guard/CLI/MCP 正常评估入口严格拒绝旧版本（禁止隐式迁移后直接裁决）
    from deadlatch import Guard, Portfolio, Order

    policy = Path(REPO) / "examples" / "01_pass" / "policy.yaml"
    guard = Guard.from_policy(str(policy), audit_path=str(tmp_path / "audit.jsonl"))
    old_order = Order.from_dict({**_order_v1(), "schema_version": 1})
    r = guard.check(old_order, Portfolio.from_dict(
        {"schema_version": 3, "equity": 1.0, "cash": 1.0, "day_start_equity": 1.0,
         "peak_equity": 1.0, "daily_pnl": 0.0, "drawdown_ratio": 0.0,
         "snapshot_at": "2026-08-29T10:00:00Z", "base_currency": "USD", "positions": []}))
    assert r.exit_code == 4  # 版本门：v1 拒绝，不隐式迁移


def test_cli_check_rejects_old_order_version(tmp_path):
    inp = tmp_path / "order_v1.json"
    inp.write_text(json.dumps(_order_v1()), encoding="utf-8")
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "schema_version: 2\nversion: '1.0.0'\nmode: enforce\nbase_currency: USD\n"
        'kill_switch: "off"\nacknowledged_disabled: []\nlimits:\n'
        "  max_order_quantity: 500\n  max_order_value: 5000.0\n"
        "  max_symbol_exposure_ratio: 0.10\n  max_total_exposure_ratio: 0.60\n"
        "  min_cash: 0.0\n  max_options_margin_ratio: 0.35\n"
        "  max_daily_loss_ratio: 0.03\n  max_drawdown_ratio: 0.15\n"
        "  max_order_age_seconds: 300\n  max_snapshot_age_seconds: 300\n",
        encoding="utf-8")
    portfolio = tmp_path / "portfolio.json"
    portfolio.write_text(json.dumps({
        "schema_version": 3, "equity": 123456.78, "cash": 30000.0,
        "day_start_equity": 125000.0, "peak_equity": 128000.0,
        "daily_pnl": -1200.5, "drawdown_ratio": 0.0355,
        "snapshot_at": "2026-08-29T09:59:00Z", "base_currency": "USD", "positions": []}),
        encoding="utf-8")
    r = _cli_migrate("order", inp)  # migrate 命令正常
    assert r.returncode == 0
    # CLI check 拒绝旧版本
    chk = subprocess.run(
        [sys.executable, "-m", "deadlatch.cli", "check",
         "--policy", str(policy), "--order", str(inp), "--portfolio", str(portfolio)],
        capture_output=True, text=True, cwd=str(REPO),
        env={"PYTHONPATH": str(REPO / "src")}, timeout=60)
    assert chk.returncode == 4  # 版本不符 → exit 4（不隐式迁移）


def test_migrate_output_write_failure_keeps_existing_file(tmp_path):
    # --output 指向已有文件但写入失败（只读目录）→ exit 4，原文件不变
    inp = tmp_path / "order_v1.json"
    inp.write_text(json.dumps(_order_v1()), encoding="utf-8")
    out_dir = tmp_path / "ro"
    out_dir.mkdir()
    out = out_dir / "migrated.json"
    out.write_text("ORIGINAL", encoding="utf-8")
    out_dir.chmod(0o555)
    try:
        r = _cli_migrate("order", inp, "--output", str(out))
        assert r.returncode == 4
        assert "error:" in r.stderr
        assert out.read_text(encoding="utf-8") == "ORIGINAL"  # 原文件不变
    finally:
        out_dir.chmod(0o755)
