"""CLI 测试：退出码 0/2/3/4/5、JSON stdout、stdout/stderr 分流、无 traceback。"""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

REPO = Path(__file__).resolve().parents[1]
RESULT_SCHEMA = json.loads((REPO / "schemas" / "result.schema.json").read_text())


def _run_cli(*args, cwd: Path, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {
        "PYTHONPATH": str(REPO / "src"),
        # 审计写入隔离：CLI 审计 JSONL 落在 cwd（每测试 tmp_path 独立），不写用户真实目录
        "DEADLATCH_AUDIT_PATH": str(cwd / "audit.jsonl"),
    }
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "deadlatch.cli", *args],
        capture_output=True, text=True, cwd=str(cwd), env=env, timeout=60,
    )


def _write(tmp_path: Path, name: str, content: str) -> str:
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

ORDER_OK = """{
  "schema_version": 2, "symbol": "AAA", "instrument_type": "stock",
  "side": "buy", "quantity": 20, "price": 190.0, "order_type": "limit",
  "currency": "USD", "created_at": "2026-08-29T10:00:00Z"
}"""

ORDER_BIG = ORDER_OK.replace('"quantity": 20', '"quantity": 1000')

PORTFOLIO_OK = """{
  "schema_version": 3, "equity": 123456.78, "cash": 30000.0,
  "day_start_equity": 125000.0, "peak_equity": 128000.0,
  "daily_pnl": -1200.5, "drawdown_ratio": 0.0355,
  "snapshot_at": "2026-08-29T09:59:00Z", "base_currency": "USD", "positions": []
}"""


@pytest.fixture
def cli_files(tmp_path):
    # 动态时间戳：相对真实 now 保持新鲜（R10/R11 不因陈旧而误触发）
    now = datetime.now(timezone.utc)
    order_ts = (now - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    snap_ts = (now - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    policy = _write(tmp_path, "policy.yaml", POLICY_YAML)
    order = _write(tmp_path, "order.json", ORDER_OK.replace("2026-08-29T10:00:00Z", order_ts))
    order_big = _write(tmp_path, "order_big.json", ORDER_BIG.replace("2026-08-29T10:00:00Z", order_ts))
    portfolio = _write(tmp_path, "portfolio.json", PORTFOLIO_OK.replace("2026-08-29T09:59:00Z", snap_ts))
    return tmp_path, policy, order, order_big, portfolio


def test_cli_pass_exit0(cli_files):
    tmp, policy, order, _, portfolio = cli_files
    r = _run_cli("check", "--policy", policy, "--order", order, "--portfolio", portfolio, cwd=tmp)
    assert r.returncode == 0
    assert "exit_code: 0" in r.stdout
    assert r.stderr == ""


def test_cli_block_exit3(cli_files):
    tmp, policy, _, order_big, portfolio = cli_files
    r = _run_cli("check", "--policy", policy, "--order", order_big, "--portfolio", portfolio, cwd=tmp)
    assert r.returncode == 3
    assert "风控拦截" in r.stdout


def test_cli_json_output_validates_schema(cli_files):
    tmp, policy, order, _, portfolio = cli_files
    r = _run_cli("check", "--policy", policy, "--order", order, "--portfolio", portfolio,
                 "--json", cwd=tmp)
    assert r.returncode == 0
    doc = json.loads(r.stdout)  # stdout 只含 JSON
    Draft202012Validator(RESULT_SCHEMA).validate(doc)
    assert doc["decision"] == "PASS"
    assert doc["exit_code"] == 0


def test_cli_input_error_exit4(cli_files):
    tmp, policy, _, _, portfolio = cli_files
    bad_order = _write(tmp, "order_bad.json", ORDER_OK.replace('"currency": "USD"', '"currency": "HKD"'))
    r = _run_cli("check", "--policy", policy, "--order", bad_order, "--portfolio", portfolio, cwd=tmp)
    assert r.returncode == 4
    assert "输入/配置错误" in r.stdout or "非风控拦截" in r.stdout


def test_cli_missing_file_exit4_stderr(cli_files):
    tmp, policy, _, _, portfolio = cli_files
    r = _run_cli("check", "--policy", policy, "--order", str(tmp / "missing.json"),
                 "--portfolio", portfolio, cwd=tmp)
    assert r.returncode == 4
    assert "error:" in r.stderr
    assert "Traceback" not in r.stdout
    assert "Traceback" not in r.stderr


def test_cli_bad_policy_exit4_stderr(cli_files):
    tmp, _, order, _, portfolio = cli_files
    bad_policy = _write(tmp, "bad_policy.yaml", POLICY_YAML.replace('kill_switch: "off"', "kill_switch: enable"))
    r = _run_cli("check", "--policy", bad_policy, "--order", order, "--portfolio", portfolio, cwd=tmp)
    assert r.returncode == 4
    assert "Traceback" not in r.stderr


def test_cli_json_block_stdout_only_json(cli_files):
    tmp, policy, _, order_big, portfolio = cli_files
    r = _run_cli("check", "--policy", policy, "--order", order_big, "--portfolio", portfolio,
                 "--json", cwd=tmp)
    assert r.returncode == 3
    doc = json.loads(r.stdout)
    assert doc["decision"] == "BLOCK"
    assert doc["exit_code"] == 3
