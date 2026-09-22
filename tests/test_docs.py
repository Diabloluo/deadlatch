""" §五/§八/§九/§十一：文档事实一致性、CI workflow、演示 GIF、禁止宣传语。

- 英中 README 关键事实一致（12 规则、退出码、MCP 五工具、30 天审计、USD-only）；
- README 无绝对“cannot bypass”类宣传语、无收益/安全保证、无不存在的命令；
- CI workflow YAML 可解析、矩阵结构符合工单 §八、最小权限、无 secrets；
- 演示 GIF 存在、可解码、帧数 > 1、无敏感元数据/字符串；
- 治理文档齐全且含关键章节。
"""

import json
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]

KEY_FACTS = (
    "exit_code", "max_order_quantity", "max_daily_loss", "kill_switch",
    "reduce_only", "check_order", "get_account_status", "get_policy",
    "kill_switch_status", "recent_decisions", "30-day", "30 天",
    "USD-only", "never places orders", "永不下单", "stdio",
)

FORBIDDEN_PHRASES = (
    "cannot bypass", "can't be bypassed", "impossible to bypass",
    "guaranteed profit", "guarantee profit", "guarantees that",
    "guaranteed returns", "prevent all losses", "no risk", "risk-free",
    "zero risk",
    # 不存在的 CLI/MCP 能力（kill switch 概念本身合法，命令形态才禁止）
    "deadlatch kill", "deadlatch stop", "deadlatch kill on",
    "place_order(", "set_policy", "modify_policy",
    # GATE-4：公开入口不得暗示设计合作、实盘接入、收益或强制拦截
    "免费设计合作", "实盘接入", "收益提升", "强制拦截",
)


def _readme(path: str) -> str:
    return (REPO / path).read_text(encoding="utf-8")


# ---------------- 双语 README 一致性 ----------------

def test_readme_zh_en_both_exist_and_share_key_facts():
    en = _readme("README.md")
    zh = _readme("README.zh-CN.md")
    assert "Deadlatch" in en and "Deadlatch" in zh
    # 中英互链
    assert "README.zh-CN.md" in en and "README.md" in zh
    # 关键事实双方都覆盖（每条至少在一份文档中出现；英文优先为事实源）
    for fact in KEY_FACTS:
        assert fact.lower() in en.lower() or fact in zh, f"关键事实缺失: {fact}"
    # GATE-3A：公开候选不含该目录；源文案不得把它写成仓库内路径
    assert "examples/adapters" not in en and "examples/adapters" not in zh


def test_readme_zh_en_rule_count_and_exit_codes_align():
    en = _readme("README.md")
    zh = _readme("README.zh-CN.md")
    for doc in (en, zh):
        assert "R12" in doc and "missing_data_fail_closed" in doc
        for code in ("`0`", "`2`", "`3`", "`4`", "`5`"):
            assert code in doc, code
        assert "acknowledged_disabled" in doc


def test_readme_no_forbidden_claims():
    for path in ("README.md", "README.zh-CN.md"):
        text = _readme(path).lower()
        for phrase in FORBIDDEN_PHRASES:
            assert phrase not in text, f"{path} 含禁止表述: {phrase}"
        # 不得出现不存在的 CLI/MCP 能力
        for nonexistent in ("kill on", "kill_switch set", "set_policy",
                            "modify_policy", "place_order"):
            assert nonexistent not in text, f"{path} 提及不存在命令: {nonexistent}"


def test_readme_advisory_boundary_present():
    for path in ("README.md", "README.zh-CN.md"):
        text = _readme(path)
        assert "advisory" in text.lower() or "建议" in text
        assert "ignore" in text or "忽略" in text  # 诚实边界：无法阻止忽略结果的 Agent
    # 当前稳定版、历史预发布、虚构快速开始、20 分钟接入评估
    for path in ("README.md", "README.zh-CN.md"):
        text = _readme(path)
        assert "v0.1.0.dev1" in text
        assert "releases/tag/v0.1.0.dev1" in text
        assert "pip install deadlatch==0.1.2" in text
        assert "uvx --from deadlatch==0.1.2 deadlatch-mcp" in text
        assert "deadlatch==0.1.1" not in text
        assert "io.github.Diabloluo/deadlatch" in text
        assert "pypi.org/project/deadlatch/0.1.2" in text
        assert "pypi.org/project/deadlatch/0.1.1" not in text
        assert "PyPI and the MCP Registry are not published yet" not in text
        assert "PyPI 与 MCP Registry 尚未发布" not in text
        assert "Intended one-line install" not in text
        assert "一行安装（待 PyPI 回读成功后）" not in text
        assert "current install path" in text or "当前安装入口" in text
        assert "0.1.1" in text
        assert "0.1.2" in text
        assert "unreleased candidate" not in text.lower()
        assert "未发布候选" not in text
        assert "Registry publication is pending" in text or "Registry 发布仍待完成" in text
        assert "tag / Release is pending" in text or "tag / Release 尚未创建" in text
        assert "2026-09-22" in text
        assert "docs/rules-spec.md" in text
        assert "docs/postmortem-option-direction.md" in text
        assert "integration-assessment.yml" in text
        assert "20-minute integration assessment" in text or "20 分钟接入评估" in text
        assert "GitHub Security Advisories" in text
        assert "public" in text.lower() or "公开" in text
        lowered = text.lower()
        assert "api key" in lowered
        assert "token" in lowered
        assert "Do not paste" in text or "不要粘贴" in text
        assert "unique full contract code" in text or "唯一完整合约码" in text
    assert "mcp-name: io.github.Diabloluo/deadlatch" in _readme("README.md")
    mcp_readme = (REPO / "examples" / "mcp" / "README.md").read_text(encoding="utf-8")
    assert "--kill-switch-path" in mcp_readme
    assert "每次调用无条件重读" in mcp_readme
    assert "并重启服务器进程" not in mcp_readme
    assert "唯一完整合约码" in mcp_readme


# ---------------- FIX-006-1：GIF 展示与写面事实 ----------------

def test_readme_embeds_demo_gif():
    """英中 README 都实际嵌入 docs/assets/agent-blocked.gif（markdown 图片）。"""
    for path in ("README.md", "README.zh-CN.md"):
        text = _readme(path)
        assert "docs/assets/agent-blocked.gif" in text, path
        assert "![Agent blocked by Deadlatch](docs/assets/agent-blocked.gif)" in text, path
        # 被引用资产真实存在（README 内引用不断链）
        assert (REPO / "docs" / "assets" / "agent-blocked.gif").exists()


def test_readme_no_vague_all_readonly_claim():
    """FIX-006-2：不得有“库/CLI/MCP 全部只读”的笼统表述；写入面须如实说明。"""
    for path in ("README.md", "README.zh-CN.md"):
        text = _readme(path)
        assert "all read-only" not in text, path
        assert "全部只读" not in text, path
        # 明确写面：审计追加 + shadow report 保留清理 + migrate --output
        assert "Guard.check" in text and "audit" in text.lower(), path
        assert "migrate --output" in text, path
        # 不修改 policy/portfolio/kill switch
        assert "kill-switch state" in text or "kill-switch 状态" in text, path
        assert "never places an order" in text or "从不下单" in text, path


# ----------------  文案级最终修补：写入面不可证实的绝对句 ----------------

ABSOLUTE_WRITE_PHRASES = (
    "exactly this, nothing more",
    "No other write paths exist",
    "These are the only write paths",
    "仅此两处，再无其他",
    "除此之外不存在任何写入路径",
    "仅两处写入",
)


def test_write_surface_docs_no_absolute_claims():
    """英中 README 与 SECURITY 不得声称“仅两处写入”等不可证实的绝对句。"""
    for path in ("README.md", "README.zh-CN.md", "SECURITY.md"):
        text = (REPO / path).read_text(encoding="utf-8")
        for phrase in ABSOLUTE_WRITE_PHRASES:
            assert phrase not in text, f"{path} 含绝对写入句: {phrase}"


def test_write_surface_docs_mention_shadow_report_retention_prune():
    """写入面须如实包含：shadow report/报告入口会触发审计 30 天保留清理。"""
    en = _readme("README.md")
    zh = _readme("README.zh-CN.md")
    security = (REPO / "SECURITY.md").read_text(encoding="utf-8")
    for text, label in ((en, "README.md"), (zh, "README.zh-CN.md"), (security, "SECURITY.md")):
        assert "shadow report" in text or "shadow-report" in text, label
        assert "retention" in text.lower() or "保留" in text, label
        assert "os.replace" in text or "atomic" in text.lower() \
            or "原子" in text, label


def test_security_md_write_surface_and_scope():
    """FIX-006-2/3：SECURITY 不把正常审计追加当漏洞；无虚构报告渠道。"""
    security = (REPO / "SECURITY.md").read_text(encoding="utf-8")
    # 正常 audit append 明确除外
    assert "By-design audit appends" in security
    assert "explicitly **not**" in security and "vulnerabilities" in security
    assert "Unauthorized mutation of state" in security
    assert "modify, truncate," in security and "forge" in security
    assert "MCP write capability" not in security  # 旧漏洞定义已替换
    # GATE-2：公开仓库使用 GitHub 私密安全通报；不虚构邮箱或 SLA
    assert "GitHub Security Advisories" in security
    assert "once that GitHub setting is enabled" in security
    assert "private vulnerability reporting" in security.lower()
    assert "no dedicated security mailbox" in security
    assert "no response or fix SLA" in security
    assert "Do **not** open a public issue" in security
    assert "no public reporting channel" not in security
    assert "security@example" not in security.lower() and "mailto:" not in security
    # 不得把正常 check_order 审计追加描述为写能力漏洞
    assert "`check_order` appends" in security


# ---------------- 治理文档 ----------------

def test_governance_docs_exist_with_key_sections():
    license_text = (REPO / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in license_text
    assert "Permission is hereby granted" in license_text
    security = (REPO / "SECURITY.md").read_text(encoding="utf-8")
    for section in ("Supported versions", "Vulnerability scope", "False PASS",
                    "Kill-switch bypass", "Sensitive leakage",
                    "Unauthorized mutation of state", "Reporting"):
        assert section in security, section
    contributing = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    for section in ("pytest", "validate_schemas", "scan_sensitive",
                    "Test change discipline", "Decimal",
                    "requires_nonroot", "docs/rules-spec.md"):
        assert section in contributing, section
    disclaimer = (REPO / "DISCLAIMER.md").read_text(encoding="utf-8")
    for section in ("Not investment advice", "No guarantee against loss",
                    "never places orders", "fictional", "No SLA"):
        assert section in disclaimer, section
    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "examples/adapters" not in changelog
    assert "test_adapters.py" not in changelog
    assert "488" in changelog and "555" in changelog and "556" in changelog
    assert "577" in changelog and "509" in changelog
    assert "## v0.1.2 (2026-09-22)" in changelog
    assert "## v0.1.2 (unreleased)" not in changelog
    v012 = changelog.split("## v0.1.2 (2026-09-22)", 1)[1].split("## v0.1.1", 1)[0]
    assert "Published to PyPI as `deadlatch==0.1.2`" in v012
    assert "35730012985" in v012
    assert "`server.json` is the pending `0.1.2` Registry candidate" in v012
    assert "stable GitHub `v0.1.2` tag / Release is pending" in v012
    assert "Publication is not evidence of" in v012
    assert "## v0.1.1 (2026-09-14)" in changelog
    assert "deadlatch==0.1.1" in changelog
    assert "io.github.Diabloluo/deadlatch" in changelog
    assert "d43196de23ad2dd4a1ee4ab720c1610cd22add83" in changelog
    assert "has not been created yet" not in changelog
    assert "development workspace" in changelog.lower()
    assert "public candidate" in changelog.lower()
    server = json.loads((REPO / "server.json").read_text(encoding="utf-8"))
    assert server["$schema"].endswith("2025-12-11/server.schema.json")
    assert server["name"] == "io.github.Diabloluo/deadlatch"
    assert server["version"] == "0.1.2"
    assert len(server["description"]) <= 100
    pkg = server["packages"][0]
    assert pkg["registryType"] == "pypi"
    assert pkg["identifier"] == "deadlatch"
    assert pkg["version"] == "0.1.2"
    assert pkg["runtimeHint"] == "uvx"
    from_args = [arg for arg in pkg["runtimeArguments"] if arg.get("name") == "--from"]
    assert len(from_args) == 1
    assert from_args[0]["value"] == f"{pkg['identifier']}=={server['version']}"
    assert any(arg.get("type") == "positional" and arg.get("value") == "deadlatch-mcp"
               for arg in pkg["runtimeArguments"])
    assert pkg["transport"]["type"] == "stdio"
    arg_names = [item.get("name") for item in pkg["packageArguments"]]
    assert arg_names[:2] == ["--policy", "--portfolio"]
    assert all(item.get("isRequired") for item in pkg["packageArguments"][:2])
    form = yaml.safe_load(
        (REPO / ".github" / "ISSUE_TEMPLATE" / "integration-assessment.yml").read_text(
            encoding="utf-8"))
    assert form["name"] == "Integration assessment / 接入评估"
    body_ids = [item.get("id") for item in form["body"] if "id" in item]
    assert body_ids == ["use_case", "existing_path", "integration",
                        "block_scenarios", "contact_window", "public_safety"]
    blob = yaml.dump(form, allow_unicode=True)
    assert "API keys" in blob and "GitHub Security Advisories" in blob
    assert "public GitHub issue" in blob
    assert "security@" not in blob.lower() and "mailto:" not in blob
    cfg = yaml.safe_load(
        (REPO / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text(encoding="utf-8"))
    links = cfg["contact_links"]
    assert any("security/advisories/new" in c.get("url", "") for c in links)
    # README 含 DISCLAIMER 摘要并链接全文
    for path in ("README.md", "README.zh-CN.md"):
        text = _readme(path)
        assert "DISCLAIMER.md" in text
        assert "Disclaimer" in text or "免责声明" in text


# ---------------- CI workflow ----------------

def test_ci_workflow_yaml_valid_and_matrix():
    wf = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    assert wf["permissions"] == {"contents": "read"}
    jobs = wf["jobs"]
    assert "test" in jobs and "test-windows" in jobs and "build" in jobs
    assert "test-linux-root" in jobs
    root_job = jobs["test-linux-root"]
    assert root_job["container"]["image"].startswith("python:3.11")
    assert "--user 0" in str(root_job["container"].get("options", ""))
    # macOS/Linux × 3.10/3.11/3.12
    test_job = jobs["test"]
    assert test_job["runs-on"] == "${{ matrix.os }}"
    matrix = test_job["strategy"]["matrix"]
    assert matrix["os"] == ["macos-latest", "ubuntu-latest"]
    assert matrix["python-version"] == ["3.10", "3.11", "3.12"]
    win = jobs["test-windows"]["strategy"]["matrix"]["python-version"]
    assert win == ["3.10", "3.11", "3.12"]
    win_run = " ".join(
        str(s.get("run", "")) for s in jobs["test-windows"]["steps"]
    )
    for name in (
        "tests/test_rules_basic.py",
        "tests/test_rules_r12.py",
        "tests/test_engine.py",
        "tests/test_guard_api.py",
        "tests/test_cli.py",
        "tests/test_cli_inprocess.py",
        "tests/test_audit_platform.py",
    ):
        assert name in win_run
    assert "continue-on-error" not in yaml.dump(jobs["test-windows"])
    # build job 调 verify_wheel
    build_steps = " ".join(s.get("run", "") for s in jobs["build"]["steps"])
    assert "python -m build" in build_steps
    assert "verify_wheel.py" in build_steps
    # 无 secrets / 无 artifact 上传 / actions 固定 major
    raw = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "secrets:" not in raw and "upload-artifact" not in raw
    for action in ("actions/checkout@", "actions/setup-python@"):
        assert action in raw
    assert "actions/checkout@v4" not in raw
    assert "actions/setup-python@v5" not in raw
    assert "actions/checkout@v5" in raw
    assert "actions/setup-python@v6" in raw
    release_raw = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    release = yaml.safe_load(release_raw)
    header = release_raw.split("jobs:", 1)[0]
    assert "workflow_dispatch:" in header
    assert "push:" not in header and "pull_request:" not in header
    assert release["permissions"] == {"contents": "read"}
    assert "secrets:" not in release_raw
    assert "PYPI_API_TOKEN" not in release_raw
    assert "password:" not in release_raw
    publish = release["jobs"]["publish"]
    assert publish["environment"]["name"] == "pypi"
    assert publish["permissions"] == {"contents": "read", "id-token": "write"}
    assert "test" in publish["needs"] and "build" in publish["needs"]
    assert "test-windows" in publish["needs"]
    release_win = " ".join(
        str(s.get("run", "")) for s in release["jobs"]["test-windows"]["steps"]
    )
    assert "tests/test_audit_platform.py" in release_win
    assert "continue-on-error" not in yaml.dump(release["jobs"]["test-windows"])
    assert "pypa/gh-action-pypi-publish@release/v1" not in release_raw
    pinned = "pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33"
    assert pinned in release_raw
    assert "2026-09-13" in release_raw and "release/v1" in release_raw
    assert "confirm does not match pyproject version" in release_raw
    assert "refusing to publish a pre-release" in release_raw
    pub_steps = publish["steps"]
    hash_idx = next(
        i for i, step in enumerate(pub_steps)
        if "Print publish-set hashes" in str(step.get("name", "")))
    upload_idx = next(
        i for i, step in enumerate(pub_steps)
        if pinned in str(step.get("uses", "")))
    assert hash_idx < upload_idx
    hash_run = pub_steps[hash_idx]["run"]
    assert "sha256" in hash_run.lower()
    assert "path.name" in hash_run and "st_size" in hash_run
    assert "/Users/" not in hash_run and "environ" not in hash_run
    assert "actions/checkout@v5" in release_raw
    assert "actions/setup-python@v6" in release_raw
    assert "id-token: write" not in (
        (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    for job_name, job in release["jobs"].items():
        if job_name == "publish":
            continue
        job_perms = job.get("permissions")
        assert job_perms is None or job_perms.get("id-token") != "write"


# ---------------- 演示 GIF ----------------

def test_demo_gif_exists_decodable_multiframe_clean():
    from PIL import Image

    gif = REPO / "docs" / "assets" / "agent-blocked.gif"
    assert gif.exists() and gif.stat().st_size > 0
    with Image.open(gif) as im:
        assert im.format == "GIF"
        frames = 0
        try:
            while True:
                frames += 1
                im.seek(im.tell() + 1)
        except EOFError:
            pass
        assert frames > 1, "GIF 必须多帧（动画）"
        w, h = im.size
        assert 200 <= w <= 2000 and 200 <= h <= 1200, (w, h)
    # 原始字节与逐帧文本无敏感内容
    data = gif.read_bytes()
    for bad in (b"sk-", b"Bearer ", b"/Users/", b"Cookie:", b"api_key", b"nini"):
        assert bad not in data, f"GIF 二进制含敏感形态: {bad!r}"
    with Image.open(gif) as im:
        for i in range(frames):
            im.seek(i)
            import io

            buf = io.BytesIO()
            im.save(buf, format="PNG")
            assert b"/Users/" not in buf.getvalue() and b"sk-" not in buf.getvalue(), f"帧 {i} 含敏感内容"


def test_demo_gif_generator_script_exists():
    script = REPO / "tools" / "make_demo_gif.py"
    assert script.exists()
    text = script.read_text(encoding="utf-8")
    assert "docs/assets/agent-blocked.gif" in text
    assert "Pillow" in text  # 生成依赖声明在 docs extra，不进运行依赖
