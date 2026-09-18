"""CLI 进程内测试（coverage 追踪 cli.py 全分支；subprocess 级验证仍在 test_cli.py）。

直接调 cli.main(argv)，capsys 捕获输出；审计路径注入 tmp_path。
"""

import json
try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.10：使用 tomli 兼容包
    import tomli as tomllib  # type: ignore[no-redef]
from pathlib import Path

from deadlatch.cli import build_parser, main
from tests.conftest import NOW, _ts, assert_no_audit_artifacts, audit_writes_ok

REPO = Path(__file__).resolve().parents[1]


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


POLICY_YAML = """schema_version: 2
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


def _order(order_ts=None, **kw):
    d = {
        "schema_version": 2, "symbol": "AAA", "instrument_type": "stock",
        "side": "buy", "quantity": 20, "price": 190.0, "order_type": "limit",
        "currency": "USD", "created_at": order_ts or _ts(seconds=-1),
    }
    d.update(kw)
    return json.dumps(d)


def _portfolio(snap_ts=None, **kw):
    d = {
        "schema_version": 3, "equity": 123456.78, "cash": 30000.0,
        "day_start_equity": 125000.0, "peak_equity": 128000.0,
        "daily_pnl": -1200.5, "drawdown_ratio": 0.0355,
        "snapshot_at": snap_ts or _ts(seconds=-61), "base_currency": "USD",
        "positions": [],
    }
    d.update(kw)
    return json.dumps(d)


def _cli_files(tmp_path):
    policy = _write(tmp_path, "policy.yaml", POLICY_YAML)
    order = _write(tmp_path, "order.json", _order())
    order_big = _write(tmp_path, "order_big.json", _order(quantity=1000))
    portfolio = _write(tmp_path, "portfolio.json", _portfolio())
    audit = str(tmp_path / "audit.jsonl")
    return policy, order, order_big, portfolio, audit


def test_cli_inprocess_pass_exit0(tmp_path, capsys):
    policy, order, _, portfolio, audit = _cli_files(tmp_path)
    rc = main(["check", "--policy", policy, "--order", order,
               "--portfolio", portfolio, "--audit-path", audit])
    out = capsys.readouterr().out
    if audit_writes_ok():
        assert rc == 0
        assert "exit_code: 0" in out
    else:
        assert rc == 2
        assert "exit_code: 2" in out
        assert "audit_write_failed" in out or "平台不支持" in out
        assert_no_audit_artifacts(audit)


def test_cli_inprocess_block_exit3(tmp_path, capsys):
    policy, _, order_big, portfolio, audit = _cli_files(tmp_path)
    rc = main(["check", "--policy", policy, "--order", order_big,
               "--portfolio", portfolio, "--audit-path", audit])
    assert rc == 3
    assert "风控拦截" in capsys.readouterr().out
    if not audit_writes_ok():
        assert_no_audit_artifacts(audit)


def test_cli_inprocess_json_pass(tmp_path, capsys):
    policy, order, _, portfolio, audit = _cli_files(tmp_path)
    rc = main(["check", "--policy", policy, "--order", order,
               "--portfolio", portfolio, "--json", "--audit-path", audit])
    doc = json.loads(capsys.readouterr().out)
    if audit_writes_ok():
        assert rc == 0
        assert doc["decision"] == "PASS" and doc["exit_code"] == 0
    else:
        assert rc == 2
        assert doc["decision"] == "WARN" and doc["exit_code"] == 2
        assert any(w["rule_id"] == "audit_write_failed" for w in doc["warnings"])
        assert_no_audit_artifacts(audit)


def test_cli_inprocess_exit4_stdout(tmp_path, capsys):
    policy, _, _, portfolio, audit = _cli_files(tmp_path)
    bad = _write(tmp_path, "bad.json", _order(currency="HKD"))
    rc = main(["check", "--policy", policy, "--order", bad,
               "--portfolio", portfolio, "--audit-path", audit])
    assert rc == 4
    captured = capsys.readouterr()
    # 输入错误经引擎 → Result exit 4（人类输出到 stdout，标注非风控拦截）
    assert "非风控拦截" in captured.out
    assert captured.err == ""


def test_cli_inprocess_missing_file_exit4(tmp_path, capsys):
    policy, _, _, portfolio, audit = _cli_files(tmp_path)
    rc = main(["check", "--policy", policy, "--order", str(tmp_path / "nope.json"),
               "--portfolio", portfolio, "--audit-path", audit])
    assert rc == 4
    assert "error:" in capsys.readouterr().err


def test_cli_inprocess_report_human(tmp_path, capsys):
    policy, order, _, portfolio, audit = _cli_files(tmp_path)
    rc_check = main(["check", "--policy", policy, "--order", order,
                     "--portfolio", portfolio, "--audit-path", audit])
    capsys.readouterr()
    rc = main(["shadow", "report", "--since", "30d", "--audit-path", audit])
    captured = capsys.readouterr()
    if audit_writes_ok():
        assert rc_check == 0
        assert rc == 0
        assert "orders_evaluated: 1" in captured.out
        assert "window" in captured.out or "窗口" in captured.out
    else:
        assert rc_check == 2
        assert rc == 5
        assert "error:" in captured.err
        assert "audit_platform_unsupported" in captured.err
        assert_no_audit_artifacts(audit)


def test_cli_inprocess_report_json(tmp_path, capsys):
    policy, order, _, portfolio, audit = _cli_files(tmp_path)
    main(["check", "--policy", policy, "--order", order,
          "--portfolio", portfolio, "--audit-path", audit])
    capsys.readouterr()  # 清掉 check 的人类输出
    rc = main(["shadow", "report", "--since", "30d", "--json", "--audit-path", audit])
    captured = capsys.readouterr()
    if audit_writes_ok():
        assert rc == 0
        doc = json.loads(captured.out)
        assert doc["totals"]["orders_evaluated"] == 1
    else:
        assert rc == 5
        assert "audit_platform_unsupported" in captured.err
        assert_no_audit_artifacts(audit)


def test_cli_inprocess_report_corrupt_exit5(tmp_path, capsys):
    audit = _write(tmp_path, "audit.jsonl", "garbage\n")
    before = (tmp_path / "audit.jsonl").read_bytes()
    rc = main(["shadow", "report", "--since", "30d", "--audit-path", audit])
    assert rc == 5
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "Traceback" not in captured.out
    if not audit_writes_ok():
        assert "audit_platform_unsupported" in captured.err
        assert (tmp_path / "audit.jsonl").read_bytes() == before


def test_cli_inprocess_report_bad_since_exit5(tmp_path, capsys):
    audit = _write(tmp_path, "audit.jsonl", "")
    rc = main(["shadow", "report", "--since", "30x", "--audit-path", audit])
    assert rc == 5
    assert "error:" in capsys.readouterr().err


def test_cli_inprocess_unknown_command_system_exit2(capsys):
    import pytest

    with pytest.raises(SystemExit) as ei:
        main(["frobnicate"])  # argparse 拒绝非法子命令
    assert ei.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


# ---------------- FIX-003-3：不得声称 Agent 绝对绕不过 Guard ----------------

def test_package_and_cli_no_cannot_bypass_claim():
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    desc = pyproject["project"]["description"].lower()
    assert "cannot bypass" not in desc
    assert "advisory" in desc  # 准确的 advisory-only 表述
    assert "pre-trade risk evaluation" in desc

    help_text = build_parser().format_help().lower()
    assert "cannot bypass" not in help_text
    assert "advisory" in help_text


# ---------------- ：migrate 子命令与错误分支进程内覆盖 ----------------

def _order_v1(side="buy_to_open") -> str:
    return json.dumps({
        "schema_version": 1, "symbol": "AAA", "side": side, "quantity": 20,
        "price": 190.0, "order_type": "limit", "currency": "USD",
        "created_at": "2026-08-29T10:00:00Z",
    })


def test_cli_inprocess_migrate_stdout(tmp_path, capsys):
    inp = _write(tmp_path, "order_v1.json", _order_v1())
    rc = main(["migrate", "--kind", "order", "--input", inp])
    assert rc == 0
    out = capsys.readouterr().out
    assert "order v1 → v2" in out
    assert '"side": "buy"' in out


def test_cli_inprocess_migrate_json(tmp_path, capsys):
    inp = _write(tmp_path, "order_v1.json", _order_v1())
    rc = main(["migrate", "--kind", "order", "--input", inp, "--json"])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["kind"] == "order" and doc["to_version"] == 2
    assert doc["document"]["side"] == "buy"


def test_cli_inprocess_migrate_output_atomic(tmp_path, capsys):
    inp = _write(tmp_path, "order_v1.json", _order_v1())
    out = str(tmp_path / "migrated.json")
    rc = main(["migrate", "--kind", "order", "--input", inp, "--output", out])
    assert rc == 0
    doc = json.loads((tmp_path / "migrated.json").read_text(encoding="utf-8"))
    assert doc["schema_version"] == 2 and doc["side"] == "buy"
    assert not (tmp_path / "migrated.json.tmp").exists()  # 原子替换无残留 tmp


def test_cli_inprocess_migrate_output_json(tmp_path, capsys):
    inp = _write(tmp_path, "order_v1.json", _order_v1())
    out = str(tmp_path / "migrated.json")
    rc = main(["migrate", "--kind", "order", "--input", inp, "--output", out, "--json"])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["kind"] == "order" and doc["output"] == out


def test_cli_inprocess_migrate_reject_overwrite_input(tmp_path, capsys):
    inp = _write(tmp_path, "order_v1.json", _order_v1())
    before = (tmp_path / "order_v1.json").read_bytes()
    rc = main(["migrate", "--kind", "order", "--input", inp, "--output", inp])
    assert rc == 4
    assert "覆盖输入" in capsys.readouterr().err
    assert (tmp_path / "order_v1.json").read_bytes() == before  # 原文件不变


def test_cli_inprocess_migrate_output_write_failure(tmp_path, capsys, monkeypatch):
    """migrate --output 写入失败 → rc=4、原文件不变、无 tmp、无 traceback。

    跨平台实现：monkeypatch 对 deadlatch.cli 模块内的 open 注入 PermissionError，
    只对 *.tmp 写入抛错（迁移输出先写同目录 .tmp 再 os.replace）；不依赖
    POSIX 只读目录权限语义（Windows 上 chmod 不阻止文件创建）。
    """
    import builtins

    import deadlatch.cli as cli_mod

    real_open = builtins.open  # cli.py 的 open 解析为 builtin；保存真实实现

    def deny_tmp(path, *args, **kwargs):
        if str(path).endswith(".tmp"):
            raise PermissionError("denied: tmp write")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(cli_mod, "open", deny_tmp, raising=False)

    inp = _write(tmp_path, "order_v1.json", _order_v1())
    out = tmp_path / "m.json"
    out.write_text("ORIGINAL", encoding="utf-8")
    rc = main(["migrate", "--kind", "order", "--input", inp, "--output", str(out)])
    captured = capsys.readouterr()
    assert rc == 4  # 写入失败 → exit 4
    assert "输出写入失败" in captured.err
    assert out.read_text(encoding="utf-8") == "ORIGINAL"  # 原文件字节级不变
    assert not (tmp_path / "m.json.tmp").exists()  # 临时文件创建前即失败，无残留
    assert "Traceback" not in captured.out and "Traceback" not in captured.err


def test_cli_inprocess_migrate_missing_input_exit4(tmp_path, capsys):
    missing = tmp_path / "no-such.json"
    rc = main(["migrate", "--kind", "order", "--input", str(missing)])
    assert rc == 4
    assert "输入文件不存在" in capsys.readouterr().err


def test_cli_inprocess_migrate_error_exit4(tmp_path, capsys):
    inp = _write(tmp_path, "bad.json", '{"schema_version": 99}')
    rc = main(["migrate", "--kind", "order", "--input", inp])
    assert rc == 4
    assert "error:" in capsys.readouterr().err


def test_cli_inprocess_migrate_internal_error_exit5(tmp_path, capsys, monkeypatch):
    import deadlatch.cli as cli_mod

    inp = _write(tmp_path, "order_v1.json", _order_v1())

    def boom(kind, text):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli_mod, "migrate_json", boom)
    rc = main(["migrate", "--kind", "order", "--input", inp])
    assert rc == 5
    captured = capsys.readouterr()
    assert "internal error" in captured.err
    assert "boom" not in captured.err  # 异常原文不回显


def test_cli_inprocess_check_internal_error_exit5(tmp_path, capsys, monkeypatch):
    import deadlatch.cli as cli_mod

    policy, order, _, portfolio, audit = _cli_files(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli_mod, "Guard", boom)
    rc = main(["check", "--policy", policy, "--order", order,
               "--portfolio", portfolio, "--audit-path", audit])
    assert rc == 5
    assert "internal error" in capsys.readouterr().err


def test_cli_inprocess_unparsable_json_exit4(tmp_path, capsys):
    policy, _, _, portfolio, audit = _cli_files(tmp_path)
    bad = _write(tmp_path, "bad.json", "not json {{{")
    rc = main(["check", "--policy", policy, "--order", bad,
               "--portfolio", portfolio, "--audit-path", audit])
    assert rc == 4
    assert "解析失败" in capsys.readouterr().err


def test_cli_inprocess_top_level_non_object_exit4(tmp_path, capsys):
    policy, _, _, portfolio, audit = _cli_files(tmp_path)
    bad = _write(tmp_path, "bad.json", "[1, 2, 3]")
    rc = main(["check", "--policy", policy, "--order", bad,
               "--portfolio", portfolio, "--audit-path", audit])
    assert rc == 4
    assert "顶层必须是对象" in capsys.readouterr().err


def test_cli_inprocess_report_by_rule_rows(tmp_path, capsys):
    policy, _, order_big, portfolio, audit = _cli_files(tmp_path)
    assert main(["check", "--policy", policy, "--order", order_big,
                 "--portfolio", portfolio, "--audit-path", audit]) == 3
    capsys.readouterr()  # 清掉 check 的人类输出
    rc = main(["shadow", "report", "--since", "30d", "--audit-path", audit])
    captured = capsys.readouterr()
    if audit_writes_ok():
        assert rc == 0
        assert "top rules" in captured.out
        assert "max_order_quantity" in captured.out
    else:
        assert rc == 5
        assert "audit_platform_unsupported" in captured.err
        assert_no_audit_artifacts(audit)
