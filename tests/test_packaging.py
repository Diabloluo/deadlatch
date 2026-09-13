""" §三/§十一：构建产物内容、元数据与可重复性检查。

- wheel/sdist 存在且通过 tools/inspect_dist.py 检查（源码/Schema/LICENSE/元数据）；
- 相同源码状态连续两次构建：文件清单与关键元数据一致；
  ZIP 时间戳导致的字节差异如实记录（不伪报 bit-for-bit reproducible）。
"""

import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

SCHEMA_NAMES = ("order", "portfolio", "policy", "result", "audit-record", "shadow-report")


@pytest.fixture(scope="module")
def built_dist(tmp_path_factory):
    """-A 修补：在 pytest 临时目录自行构建，不依赖预先存在的 dist/。"""
    out = tmp_path_factory.mktemp("dist")
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(out)],
        cwd=str(REPO), check=True, capture_output=True, timeout=600,
    )
    return out


def _build(outdir: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(outdir)],
        cwd=str(REPO), check=True, capture_output=True, timeout=600,
    )


def _wheel_names(artifact: Path) -> list[str]:
    with zipfile.ZipFile(artifact) as z:
        return sorted(z.namelist())


def _sdist_names(artifact: Path) -> list[str]:
    with tarfile.open(artifact, "r:gz") as t:
        return sorted(n for n in t.getnames() if not n.endswith("/"))


def test_dist_artifacts_exist_and_pass_inspection(built_dist):
    whls = sorted(built_dist.glob("deadlatch-*.whl"))
    sdists = sorted(built_dist.glob("deadlatch-*.tar.gz"))
    assert whls and sdists, "临时构建目录缺产物"
    # 用 inspect_dist 的真实检查逻辑（与 tools/ 同源，importlib 加载避免静态依赖）
    import importlib.util

    spec = importlib.util.spec_from_file_location("inspect_dist", REPO / "tools" / "inspect_dist.py")
    assert spec is not None and spec.loader is not None
    inspect_dist = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inspect_dist)

    for artifact in [*whls, *sdists]:
        problems = inspect_dist.inspect(artifact)
        assert not problems, f"{artifact.name}: {problems}"


def test_wheel_contains_schemas_and_resources(built_dist):
    whl = sorted(built_dist.glob("deadlatch-*.whl"))[-1]
    names = _wheel_names(whl)
    for s in SCHEMA_NAMES:
        assert f"deadlatch/schemas/{s}.schema.json" in names, s
    assert "deadlatch/_resources.py" in names
    assert any(n.endswith("dist-info/entry_points.txt") for n in names)
    assert any(n.endswith("dist-info/licenses/LICENSE") for n in names)


def test_sdist_contains_sources_and_governance(built_dist):
    sdist = sorted(built_dist.glob("deadlatch-*.tar.gz"))[-1]
    names = _sdist_names(sdist)
    joined = "\n".join(names)
    for s in SCHEMA_NAMES:
        assert f"src/deadlatch/schemas/{s}.schema.json" in joined, s
    for req in ("LICENSE", "README.md", "README.zh-CN.md", "pyproject.toml",
                "SECURITY.md", "CONTRIBUTING.md", "DISCLAIMER.md"):
        assert f"/{req}" in joined or joined.startswith(req), req
    # FIX-006-1：文档资产（演示 GIF + 三个 Quick Start）必须随 sdist 发布
    for doc_asset in ("docs/assets/agent-blocked.gif",
                      "docs/quickstart/python.py",
                      "docs/quickstart/cli.sh",
                      "docs/quickstart/mcp_client.py"):
        assert f"/{doc_asset}" in joined, doc_asset
    # 不得把 tests/运行数据/audit/coverage 打入发布物
    assert "/tests/" not in joined, "sdist 不得包含 tests"
    assert ".audit.jsonl" not in joined and ".coverage" not in joined


def test_sdist_readme_links_resolve_after_extract(tmp_path, built_dist):
    """FIX-006-1：README 引用的 Quick Start/GIF 在 sdist 解包后不断链。"""
    import re
    import tarfile

    sdist = sorted(built_dist.glob("deadlatch-*.tar.gz"))[-1]
    dest = tmp_path / "unpacked"
    dest.mkdir()
    with tarfile.open(sdist, "r:gz") as t:
        t.extractall(dest)
    root = dest / "deadlatch-0.1.0.dev1"
    readme = (root / "README.md").read_text(encoding="utf-8")
    # README 中所有 (docs/...) 相对引用在解包树中必须存在
    refs = set(re.findall(r"\(docs/[^)]+\)", readme))
    for ref in refs:
        rel = ref[1:-1]
        assert (root / rel).exists(), f"sdist 解包后断链: {rel}"
    # 四个文档资产显式存在
    for asset in ("docs/assets/agent-blocked.gif",
                  "docs/quickstart/python.py",
                  "docs/quickstart/cli.sh",
                  "docs/quickstart/mcp_client.py"):
        assert (root / asset).exists(), asset


def test_wheel_and_sdist_license_is_deadlatch(built_dist):
    """-R：wheel/sdist 的 LICENSE 版权主体为 Deadlatch contributors，无旧品牌。"""
    whl = sorted(built_dist.glob("deadlatch-*.whl"))[-1]
    with zipfile.ZipFile(whl) as z:
        lic_names = [n for n in z.namelist() if n.endswith("dist-info/licenses/LICENSE")]
        assert lic_names
        lic = z.read(lic_names[0]).decode("utf-8")
        assert "Deadlatch contributors" in lic
        assert "Quant" + "Ops" not in lic and "quant" + "ops" not in lic  # 拼接防自命中
    sdist = sorted(built_dist.glob("deadlatch-*.tar.gz"))[-1]
    with tarfile.open(sdist, "r:gz") as t:
        member = t.extractfile("deadlatch-0.1.0.dev1/LICENSE")
        assert member is not None
        lic = member.read().decode("utf-8")
        assert "Deadlatch contributors" in lic
        assert "Quant" + "Ops" not in lic and "quant" + "ops" not in lic


def test_repeat_build_file_lists_and_metadata_identical(tmp_path):
    """连续两次构建：文件清单与关键元数据一致（ZIP 时间戳差异如实记录）。"""
    out1, out2 = tmp_path / "b1", tmp_path / "b2"
    out1.mkdir(), out2.mkdir()
    _build(out1)
    _build(out2)
    w1 = sorted(out1.glob("*.whl"))[0]
    w2 = sorted(out2.glob("*.whl"))[0]
    assert _wheel_names(w1) == _wheel_names(w2), "wheel 文件清单两次构建不一致"
    s1 = sorted(out1.glob("*.tar.gz"))[0]
    s2 = sorted(out2.glob("*.tar.gz"))[0]
    assert _sdist_names(s1) == _sdist_names(s2), "sdist 文件清单两次构建不一致"
    # 关键元数据（METADATA / entry_points）逐字节一致
    with zipfile.ZipFile(w1) as z1, zipfile.ZipFile(w2) as z2:
        for name in ("deadlatch-0.1.0.dev1.dist-info/METADATA",
                     "deadlatch-0.1.0.dev1.dist-info/entry_points.txt"):
            assert z1.read(name) == z2.read(name), f"元数据 {name} 两次构建不一致"
    # 说明：字节哈希可能因 ZIP 时间戳不同而变化，不伪报 bit-for-bit reproducible
    b1 = w1.read_bytes()
    b2 = w2.read_bytes()
    if b1 != b2:
        print("note: wheel 字节因 ZIP 时间戳不同而变化（文件清单/元数据一致，非 bit-for-bit reproducible）")
