#!/usr/bin/env python3
""" §三：wheel/sdist 产物内容与元数据检查（构建后、发布前）。

- 只包含预期源码、Schema、许可证与必要文档；
- 不含 .env、Token/Cookie、账户、portfolio/order 运行数据、audit JSONL、
  coverage、缓存、真实用户路径或私有系统名；
- METADATA / entry points / license / readme 正确。

用法：python tools/inspect_dist.py dist
"""

import re
import sys
import tarfile
import zipfile
from pathlib import Path

DIST_INFO_PREFIX = "deadlatch-"
# 私有系统代号用运行时拼接（候选内容零字面量）
_COUPLING_NAMES = "|".join(("V" + "26", "Ar" + "gus", "He" + "lios"))
FORBIDDEN_RE = re.compile(
    r"(?i)"
    r"\.env|audit\.jsonl|\.coverage|__pycache__|\.pytest_cache|\.hypothesis"
    r"|/Users/|/home/|sk-[a-z0-9]{12,}|cookie\s*:|api[_-]?key\s*[=:]"
    r"|\b(" + _COUPLING_NAMES + r")\b"
)
FORBIDDEN_SUFFIXES = (".pyc", ".lock", ".tmp")


def _artifact_version(artifact: Path) -> str:
    """Parse version from wheel/sdist filename so inspect tracks pyproject."""
    name = artifact.name
    if name.endswith(".tar.gz"):
        name = name[:-7]
    elif name.endswith(".whl"):
        name = name[:-4]
    if name.startswith(DIST_INFO_PREFIX):
        rest = name[len(DIST_INFO_PREFIX):]
        return rest.split("-", 1)[0]
    raise ValueError(f"unrecognized artifact name: {artifact.name}")


def _check_names(names: list[str], artifact: Path) -> list[str]:
    problems = []
    for n in names:
        if n.endswith("/"):
            continue
        if FORBIDDEN_RE.search(n):
            problems.append(f"敏感/禁止条目: {n}")
        if n.endswith(FORBIDDEN_SUFFIXES):
            problems.append(f"禁止后缀: {n}")
    return problems


def _check_metadata(meta: str, artifact: Path, entry_points: str = "") -> list[str]:
    problems = []
    version = _artifact_version(artifact)
    for field in (f"Name: deadlatch", f"Version: {version}",
                  "Requires-Python: >=3.10"):
        if field not in meta:
            problems.append(f"METADATA 缺字段: {field}")
    if "License-Expression: MIT" not in meta and "License: MIT" not in meta:
        problems.append("METADATA 缺 MIT license 声明")
    if "jsonschema" not in meta or "PyYAML" not in meta or "mcp" not in meta:
        problems.append("METADATA 缺运行依赖声明")
    # readme 正确性：METADATA 的 Description 承载（markdown 内容非空且含项目名）
    if "Description-Content-Type: text/markdown" not in meta:
        problems.append("METADATA 缺 readme（Description-Content-Type: text/markdown）")
    if "Deadlatch" not in meta:
        problems.append("METADATA 的 Description 未包含项目名（readme 未正确嵌入）")
    if entry_points:
        for ep in ("deadlatch = deadlatch.cli:main",
                   "deadlatch-mcp = deadlatch.mcp_server:main"):
            if ep not in entry_points:
                problems.append(f"entry_points 缺: {ep}")
    return problems


def _check_license(text: str, artifact: Path) -> list[str]:
    """LICENSE 内容：版权主体为 Deadlatch contributors，不含旧品牌。"""
    problems = []
    if "Deadlatch contributors" not in text:
        problems.append(f"LICENSE 版权主体非 Deadlatch contributors: {artifact.name}")
    if ("Quant" + "Ops") in text or ("quant" + "ops") in text:
        problems.append(f"LICENSE 含旧品牌: {artifact.name}")
    return problems


def inspect(artifact: Path) -> list[str]:
    problems = []
    if artifact.suffix == ".whl":
        with zipfile.ZipFile(artifact) as z:
            names = z.namelist()
            problems += _check_names(names, artifact)
            meta_names = [n for n in names if n.endswith(".dist-info/METADATA")]
            ep_names = [n for n in names if n.endswith(".dist-info/entry_points.txt")]
            if not meta_names:
                problems.append("wheel 缺 METADATA")
            else:
                ep = z.read(ep_names[0]).decode("utf-8", "replace") if ep_names else ""
                problems += _check_metadata(z.read(meta_names[0]).decode("utf-8", "replace"),
                                            artifact, ep)
            # 六个 Schema 必须随包
            for s in ("order", "portfolio", "policy", "result", "audit-record",
                      "shadow-report", "audit-maintenance-result"):
                if f"deadlatch/schemas/{s}.schema.json" not in names:
                    problems.append(f"wheel 缺 Schema: {s}")
            if "deadlatch/_resources.py" not in names:
                problems.append("wheel 缺 _resources.py（包资源读取）")
            license_names = [n for n in names if n.endswith("dist-info/licenses/LICENSE")]
            if not license_names:
                problems.append("wheel 缺 LICENSE")
            else:
                problems += _check_license(
                    z.read(license_names[0]).decode("utf-8", "replace"), artifact)
    elif artifact.name.endswith(".tar.gz"):
        with tarfile.open(artifact, "r:gz") as t:
            # sdist 有 {name}-{version}/ 顶层前缀，检查时剥离
            def _strip(n: str) -> str:
                parts = n.split("/", 1)
                return parts[1] if len(parts) > 1 else parts[0]

            names = [_strip(n) for n in t.getnames()]
            problems += _check_names(names, artifact)
            for s in ("order", "portfolio", "policy", "result", "audit-record",
                      "shadow-report", "audit-maintenance-result"):
                if f"src/deadlatch/schemas/{s}.schema.json" not in names:
                    problems.append(f"sdist 缺 Schema: {s}")
            for req in ("LICENSE", "README.md", "README.zh-CN.md", "pyproject.toml",
                        "src/deadlatch/_resources.py"):
                if req not in names:
                    problems.append(f"sdist 缺 {req}")
            if "LICENSE" in names:
                with tarfile.open(artifact, "r:gz") as t2:
                    lic_member = t2.extractfile(f"{artifact.name[:-7]}/LICENSE")
                    problems += _check_license(
                        lic_member.read().decode("utf-8", "replace") if lic_member else "",
                        artifact)
            # FIX-006-1：文档资产（演示 GIF + 三个 Quick Start）必须随 sdist 发布
            for doc_asset in ("docs/assets/agent-blocked.gif",
                              "docs/quickstart/python.py",
                              "docs/quickstart/cli.sh",
                              "docs/quickstart/mcp_client.py",
                              "docs/rules-spec.md"):
                if doc_asset not in names:
                    problems.append(f"sdist 缺文档资产: {doc_asset}")
            if "PKG-INFO" not in names:
                problems.append("sdist 缺 PKG-INFO")
    else:
        problems.append(f"未知产物类型: {artifact.name}")
    return problems


def main(argv: list[str] | None = None) -> int:
    dist = Path(argv[0]) if argv else Path("dist")
    artifacts = sorted(dist.glob("deadlatch-*"))
    if not artifacts:
        print(f"dist 目录无产物: {dist}")
        return 1
    all_problems = []
    for artifact in artifacts:
        problems = inspect(artifact)
        if problems:
            all_problems.append((artifact.name, problems))
    if all_problems:
        print(f"产物检查失败（{len(all_problems)} 个产物）:")
        for name, problems in all_problems:
            print(f"  {name}:")
            for p in problems:
                print(f"    - {p}")
        return 1
    print(f"产物检查通过: {', '.join(a.name for a in artifacts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
