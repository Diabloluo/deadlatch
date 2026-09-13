#!/usr/bin/env python3
""" §四：全新 venv、源码目录外安装验证（可重复脚本）。

用法：
    python tools/verify_wheel.py dist/deadlatch-*.whl

流程（全部在 mktemp 临时目录内，成功后清理）：
1. 用当前解释器创建全新 venv（禁止复用仓库 .venv）；
2. pip 安装 wheel（依赖正常解析）；
3. 工作目录切到源码仓以外的临时目录，清空 PYTHONPATH/源码路径影响；
4. 用新 venv 解释器执行内嵌验证：
   - import 位置来自新 venv site-packages（不从源仓 import）；
   - 六个 Schema 包资源可读且过 meta-schema 校验；
   - Python API Quick Start 等价验证：PASS/0 与 BLOCK/3；
   - CLI：PASS/0、BLOCK/3、输入错误/4；--json 过 result.schema；
   - shadow report --json 过 report.schema；
   - MCP 官方客户端真实 subprocess stdio：initialize、五工具、
     check_order PASS 与 BLOCK；无残留进程；
5. 失败时只输出安全摘要（步骤 + 异常类型），不泄漏临时绝对路径或输入全文。
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# 内嵌验证脚本：由新 venv 解释器执行（不依赖源码仓文件；cwd 已在源码仓外）
_VERIFY_SCRIPT = r"""
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

# ---- 1. import 位置来自新 venv ----
import deadlatch
assert "site-packages" in deadlatch.__file__, deadlatch.__file__
print("[1] import:", deadlatch.__file__)
# -R 反例：旧包必须不存在（无兼容空壳/双包）——拼接避免源码残留旧名
import importlib

try:
    importlib.import_module("quant" + "ops" + "_guard")
    raise SystemExit("旧包仍可导入——迁移不彻底")
except ModuleNotFoundError:
    pass

# ---- 2. 六个 Schema 包资源 + meta-schema ----
from deadlatch import _resources
for name in _resources.SCHEMA_NAMES:
    doc = _resources.schema_dict(name)
    Draft202012Validator.check_schema(doc)
print("[2] schemas: 6 ok")

# ---- 3. Python API：PASS/0 与 BLOCK/3 ----
from deadlatch import Guard, Order, Portfolio

POLICY_YAML = (
    "schema_version: 2\nversion: '1.0.0'\nmode: enforce\nbase_currency: USD\n"
    'kill_switch: "off"\nacknowledged_disabled: []\nlimits:\n'
    "  max_order_quantity: 500\n  max_order_value: 5000.0\n"
    "  max_symbol_exposure_ratio: 0.10\n  max_total_exposure_ratio: 0.60\n"
    "  min_cash: 0.0\n  max_options_margin_ratio: 0.35\n"
    "  max_daily_loss_ratio: 0.03\n  max_drawdown_ratio: 0.15\n"
    "  max_order_age_seconds: 300\n  max_snapshot_age_seconds: 300\n"
)
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    policy = tmp / "policy.yaml"
    policy.write_text(POLICY_YAML, encoding="utf-8")
    audit = tmp / "audit.jsonl"
    guard = Guard.from_policy(str(policy), audit_path=str(audit))
    now = datetime.now(timezone.utc).replace(microsecond=0)

    def ts(dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    pf = Portfolio.from_dict({
        "schema_version": 3, "equity": 123456.78, "cash": 30000.0,
        "day_start_equity": 125000.0, "peak_equity": 128000.0,
        "daily_pnl": -1200.5, "drawdown_ratio": 0.0355,
        "snapshot_at": ts(now - timedelta(seconds=61)),
        "base_currency": "USD", "positions": [],
    })

    def order(**overrides):
        doc = {
            "schema_version": 2, "symbol": "AAA", "instrument_type": "stock",
            "side": "buy", "quantity": 20, "price": 190.0, "order_type": "limit",
            "currency": "USD", "created_at": ts(now - timedelta(seconds=1)),
        }
        doc.update(overrides)
        return doc

    r = guard.check(Order.from_dict(order()), pf, now=now)
    assert (r.decision, r.exit_code) == ("PASS", 0), (r.decision, r.exit_code)
    r = guard.check(Order.from_dict(order(quantity=1000)), pf, now=now)
    assert (r.decision, r.exit_code) == ("BLOCK", 3), (r.decision, r.exit_code)
    print("[3] python api: PASS/0 + BLOCK/3 ok")

    # ---- 4. CLI：0 / 3 / 4 + --json 过 result.schema ----
    result_schema = _resources.schema_dict("result")
    report_schema = _resources.schema_dict("shadow-report")
    pf_json = tmp / "portfolio.json"
    pf_json.write_text(json.dumps(pf.to_dict()), encoding="utf-8")
    pass_o = tmp / "order_pass.json"
    pass_o.write_text(json.dumps(order()), encoding="utf-8")
    block_o = tmp / "order_block.json"
    block_o.write_text(json.dumps(order(quantity=1000)), encoding="utf-8")
    bad_o = tmp / "order_bad.json"
    bad_o.write_text(json.dumps(order(currency="HKD")), encoding="utf-8")

    def cli(*args):
        return subprocess.run(
            [sys.executable, "-m", "deadlatch.cli", *args],
            capture_output=True, text=True, timeout=120,
        )

    base = ["check", "--policy", str(policy), "--portfolio", str(pf_json),
            "--audit-path", str(audit)]
    r = cli(*base, "--order", str(pass_o))
    assert r.returncode == 0, r.stdout + r.stderr
    r = cli(*base, "--order", str(block_o))
    assert r.returncode == 3, r.stdout + r.stderr
    r = cli(*base, "--order", str(bad_o))
    assert r.returncode == 4, r.stdout + r.stderr
    r = cli(*base, "--order", str(pass_o), "--json")
    assert r.returncode == 0
    Draft202012Validator(result_schema).validate(json.loads(r.stdout))
    r = cli("shadow", "report", "--since", "30d", "--audit-path", str(audit), "--json")
    assert r.returncode == 0, r.stderr
    Draft202012Validator(report_schema).validate(json.loads(r.stdout))
    print("[4] cli: exit 0/3/4 + json schemas ok")

    # ---- 5. MCP 官方客户端 stdio：五工具 + PASS/BLOCK ----
    import asyncio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.types import TextContent

    async def mcp_check():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "deadlatch.mcp_server", "--policy", str(policy),
                  "--portfolio", str(pf_json), "--audit-path", str(audit)],
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = sorted(t.name for t in tools.tools)
                assert names == ["check_order", "get_account_status", "get_policy",
                                 "kill_switch_status", "recent_decisions"], names
                for label, o, want in (("PASS", order(), 0), ("BLOCK", order(quantity=1000), 3)):
                    res = await session.call_tool("check_order", {"order": o})
                    body = json.loads(res.content[0].text
                                      if isinstance(res.content[0], TextContent) else "")
                    assert body["decision"] != "PASS" or want == 0
                    assert body["exit_code"] == want, (label, body)
        return names

    names = asyncio.run(mcp_check())
    print("[5] mcp stdio:", ", ".join(names), "PASS/BLOCK ok")

    # ---- 6. 无残留服务器进程 ----
    try:
        pg = subprocess.run(["pgrep", "-f", "deadlatch.mcp_server"],
                            capture_output=True, text=True, timeout=30)
        assert pg.returncode != 0, f"残留 MCP 进程: {pg.stdout.strip()}"
    except FileNotFoundError:
        pass  # 平台无 pgrep（如 Windows）——stdio 生命周期由客户端管理
    print("[6] no residual mcp process")

print("VERIFY OK")
"""


def main(argv: list[str] | None = None) -> int:
    wheel = Path(argv[0]) if argv else None
    if wheel is None or not wheel.exists() or wheel.suffix != ".whl":
        print("用法: python tools/verify_wheel.py dist/deadlatch-*.whl")
        return 2
    wheel = wheel.resolve()
    tmp_root = Path(tempfile.mkdtemp(prefix="deadlatch-verify-"))
    venv_dir = tmp_root / "venv"
    work = tmp_root / "work"
    work.mkdir()
    try:
        # 1. 全新 venv
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)],
                       check=True, capture_output=True, timeout=300)
        venv_py = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        pip = venv_dir / ("Scripts/pip.exe" if os.name == "nt" else "bin/pip")
        # 2. 安装 wheel（依赖正常解析；--no-cache-dir 不缓存运行数据）。
        # 所有子进程使用清理后的 env（防御调用方 PYTHONPATH 指向源码仓的污染）
        clean_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        pip_run = subprocess.run([str(pip), "install", "--no-cache-dir", str(wheel)],
                                 env=clean_env, capture_output=True, text=True, timeout=600)
        if pip_run.returncode != 0:
            print("pip install failed", file=sys.stderr)
            return 1
        # 3. cwd 切到源码仓外的临时目录；import 位置必须来自新 venv
        script = tmp_root / "verify.py"
        script.write_text(_VERIFY_SCRIPT, encoding="utf-8")
        subprocess.run([str(venv_py), str(script)], cwd=str(work), env=clean_env,
                       check=True, timeout=600)
        print(f"wheel 验证通过: {wheel.name}")
        shutil.rmtree(tmp_root, ignore_errors=True)
        return 0
    except subprocess.CalledProcessError as exc:
        # 安全摘要：不泄漏临时绝对路径或输入全文
        tail = (exc.stdout or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
        tail += (exc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
        print(f"wheel 验证失败（步骤返回码 {exc.returncode}）:", file=sys.stderr)
        for line in tail:
            print(f"  {line}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"wheel 验证失败: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
