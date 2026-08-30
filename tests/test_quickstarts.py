""" §六/§十一：三个 60 秒 Quick Start 从同源脚本自动复现（防文档漂移）。

README 展示的 Quick Start 与 docs/quickstart/ 下的脚本同源；本文件直接执行
脚本并断言关键输出，保证文档中的命令真实可复现。
"""

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
QS = REPO / "docs" / "quickstart"


def _run(args: list[str], env_extra: dict | None = None, timeout: int = 180):
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    if env_extra:
        env.update(env_extra)
    # -A 修补：外部验证环境可通过 DEADLATCH_TEST_BIN 指定命令目录
    # （默认仓库 .venv/bin）；避免硬编码候选内 .venv。
    bin_dir = os.environ.get("DEADLATCH_TEST_BIN") or str(REPO / ".venv" / "bin")
    env["PATH"] = f"{bin_dir}:{env.get('PATH', os.environ.get('PATH', ''))}"
    return subprocess.run(args, capture_output=True, text=True, cwd=str(REPO),
                          env=env, timeout=timeout)


def test_quickstart_python_reproducible():
    r = _run([sys.executable, str(QS / "python.py")])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "decision=PASS exit_code=0" in r.stdout
    assert "decision=BLOCK exit_code=3" in r.stdout
    assert "Order NOT submitted" in r.stdout
    assert "audit records: 2" in r.stdout


def test_quickstart_cli_reproducible():
    r = _run(["bash", str(QS / "cli.sh")])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "exit=0" in r.stdout and "exit=3" in r.stdout and "exit=4" in r.stdout
    assert "decision: PASS   exit_code: 0" in r.stdout
    assert "decision: BLOCK   exit_code: 3" in r.stdout
    assert "CLI Quick Start OK" in r.stdout


def test_quickstart_mcp_reproducible():
    r = _run([sys.executable, str(QS / "mcp_client.py")])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "check_order, get_account_status, get_policy, kill_switch_status, recent_decisions" in r.stdout
    assert "decision=PASS exit_code=0" in r.stdout
    assert "decision=BLOCK exit_code=3" in r.stdout
    assert "Order NOT submitted" in r.stdout
    assert "MCP Quick Start OK" in r.stdout


def test_readme_references_existing_quickstart_files():
    """README 引用的 Quick Start 文件必须存在（防文档引用漂移）。"""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    for ref in ("docs/quickstart/python.py", "docs/quickstart/cli.sh",
                "docs/quickstart/mcp_client.py"):
        assert ref in readme, ref
        assert (QS / ref.rsplit("/", 1)[-1]).exists()
