#!/usr/bin/env python3
"""敏感信息自动扫描（ §六 / threat-model T8，FIX-005-1）。

扫描版本控制中的生产源码、Schema、examples、配置示例、README/CHANGELOG、
构建元数据；排除 .git/.venv/__pycache__/coverage/cache/运行时临时文件。

规则（命中输出相对文件 + 规则 ID + 行号，不打印 secret 本身）：
- R-PATH  真实用户路径（/Users/<name>、/home/<name>）与私有目录形态
- R-EMAIL 邮箱
- R-TOKEN API key / Bearer / sk- 前缀 / token / secret / session
- R-COOKIE Cookie / Set-Cookie 头
- R-PRIVKEY 私钥头（-----BEGIN ... PRIVATE KEY-----）
- R-COUPLING 私有系统代号 / 私有耦合路径
- R-TRACEBACK Traceback 泄漏模式

FIX-005-1 allowlist 语义（防掩盖第二个泄漏）：
- 豁免 = (相对文件, rule_id, 精确匹配值) 三元组，逐片段精确匹配；
- 一个匹配被豁免后继续检查同一行剩余片段与其他规则；
- 不允许整行/整文件/整目录豁免；sentinel 出现在未授权文件必须命中。
scan(root) 从任意 cwd 可调用，root 缺省为仓库根（绝对路径）。
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

EXCLUDE_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".hypothesis", ".coverage", "node_modules"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".coverage", ".tmp", ".lock"}
INCLUDE_SUFFIXES = {".py", ".md", ".json", ".yaml", ".yml", ".toml", ".txt", ".sh", ".cfg", ".ini"}

# 测试/文档中统一使用的虚构 sentinel 豁免表（窄范围、逐项）：
# (相对文件, rule_id, 精确匹配值)。仅这些"文件 + 规则 + 值"组合豁免；
# 一个匹配被豁免后继续检查同一行剩余片段与其他规则；
# 相同值出现在未授权文件或命中其他规则仍会被检出。
# 私有系统代号与敏感假值用运行时构造（hex/拼接，源码零字面量）。
_P1, _P2, _P3 = "V" + "26", "Ar" + "gus", "He" + "lios"


def _s(*hex_parts: str) -> str:
    """hex 字面量 → 字符串（避免源码出现敏感形态字面量）。"""
    return bytes.fromhex("".join(hex_parts)).decode()


ALLOWLIST = {
    # ---- tests/test_audit.py：脱敏断言假值（hex 构造，防自命中）----
    ("tests/test_audit.py", "R-TOKEN", _s("736b2d3132333435363738393061626364656631323334")),  # sk-1234567890abcdef1234
    ("tests/test_audit.py", "R-TOKEN", _s("6170695f6b65793a20736b2d3132333435363738393061626364656631323334")),  # api_key: sk-1234567890abcdef1234
    ("tests/test_audit.py", "R-TOKEN", _s("4265617265722065794a68624763694f694a49557a49314e694a392e65794a7a645749694f6949784d6a4d304e5459334f446b77496e30")),  # Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0
    ("tests/test_audit.py", "R-COOKIE", _s("5365742d436f6f6b69653a20617574683d46414b45434f4f4b4945313233343536373839")),  # Set-Cookie: auth=FAKECOOKIE123456789
    ("tests/test_audit.py", "R-TOKEN", _s("736b2d46414b454b455931323334353637383930616263646566")),  # sk-FAKEKEY1234567890abcdef
    ("tests/test_audit.py", "R-TOKEN", _s("736b2d46414b45544f4b454e31323334353637383930")),  # sk-FAKETOKEN1234567890
    ("tests/test_audit.py", "R-TOKEN", _s("736b2d46414b45544f4b454e31323334353637383930")),
    ("tests/test_audit.py", "R-PATH", "/Users/QOG_TEST_USER"),
    ("tests/test_audit.py", "R-TOKEN", _s("7365637265743d73757065722d7365637265742d76616c7565")),  # secret=super-secret-value
    ("tests/test_audit.py", "R-COOKIE", _s("436f6f6b69653a2073657373696f6e69643d46414b45434f4f4b4945313233343536373839")),  # Cookie: sessionid=FAKECOOKIE123456789
    ("tests/test_audit.py", "R-COOKIE", _s("436f6f6b69653a2073657373696f6e69643d46414b45434f4f4b4945313233343536373839")),
    # ---- tests/test_mcp_server.py：脱敏/注入假值 ----
    ("tests/test_mcp_server.py", "R-TOKEN", _s("736b2d46414b454b455931323334353637383930")),  # sk-FAKEKEY1234567890
    ("tests/test_mcp_server.py", "R-TOKEN", _s("736b2d46414b4553454352455431323334353637383930")),  # sk-FAKESECRET1234567890
    # ---- tools/scan_sensitive.py：allowlist 声明行与规则注释（精确假值自引用）----
    ("tools/scan_sensitive.py", "R-TOKEN", _s("736b2d3132333435363738393061626364656631323334")),
    ("tools/scan_sensitive.py", "R-TOKEN", _s("6170695f6b65793a20736b2d3132333435363738393061626364656631323334")),
    ("tools/scan_sensitive.py", "R-TOKEN", _s("4265617265722065794a68624763694f694a49557a49314e694a392e65794a7a645749694f6949784d6a4d304e5459334f446b77496e30")),
    ("tools/scan_sensitive.py", "R-COOKIE", _s("5365742d436f6f6b69653a20617574683d46414b45434f4f4b4945313233343536373839")),
    ("tools/scan_sensitive.py", "R-TOKEN", _s("736b2d46414b454b455931323334353637383930616263646566")),
    ("tools/scan_sensitive.py", "R-TOKEN", _s("736b2d46414b454b455931323334353637383930")),
    ("tools/scan_sensitive.py", "R-TOKEN", _s("736b2d46414b4553454352455431323334353637383930")),
    ("tools/scan_sensitive.py", "R-TOKEN", _s("736b2d46414b45544f4b454e31323334353637383930")),
    ("tools/scan_sensitive.py", "R-TOKEN", _s("7365637265743d73757065722d7365637265742d76616c7565")),
    ("tools/scan_sensitive.py", "R-PATH", "/Users/QOG_TEST_USER"),
    ("tools/scan_sensitive.py", "R-COOKIE", _s("436f6f6b69653a2073657373696f6e69643d46414b45434f4f4b4945313233343536373839")),
    ("tools/scan_sensitive.py", "R-COOKIE", _s("436f6f6b69653a2073657373696f6e69643d46414b45434f4f4b4945313233343536373839")),
    ("tools/scan_sensitive.py", "R-COUPLING", _P1),
    ("tools/scan_sensitive.py", "R-COUPLING", _P2),
    ("tools/scan_sensitive.py", "R-COUPLING", _P3),
    # ---- CHANGELOG.md：边界说明（私有系统名）----
    ("CHANGELOG.md", "R-COUPLING", _P1),
    ("CHANGELOG.md", "R-COUPLING", _P2),
    ("CHANGELOG.md", "R-COUPLING", _P3),
}

RULES = [
    ("R-PATH", re.compile(r"/(Users|home|private|var/root)/[A-Za-z0-9_.\-]+")),
    ("R-EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("R-TOKEN", re.compile(
        r"(?i)\b(sk-|pk-|rk-)[a-zA-Z0-9_-]{12,}"
        r"|\b(api[_-]?key|access[_-]?token|auth[_-]?token|passwd|password)\b\s*[=:]\s*[^\s,\"']+"
        r"|\bsecret\b\s*[=:]\s*[^\s,\"']{4,}"   # secret 键名要求值 >= 4 字符（防变量名误报）
        r"|\bbearer\s+[a-zA-Z0-9._~+/=-]{8,}"
    )),
    ("R-COOKIE", re.compile(r"(?i)\b(?:cookie|set-cookie)\s*[:=]\s*[a-zA-Z0-9_\-]+=[^\s;\"']+")),
    ("R-PRIVKEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    # R-COUPLING：私有系统代号（运行时拼接，候选内容零字面量）
    ("R-COUPLING", re.compile(r"(?i)\b(" + "|".join((_P1, _P2, _P3)) + r")\b")),
    ("R-TRACEBACK", re.compile(r"Traceback \(most recent call last\)")),
]


def _iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if path.suffix.lower() in EXCLUDE_SUFFIXES:
            continue
        if path.suffix.lower() not in INCLUDE_SUFFIXES:
            continue
        yield rel


def _relative_key(rel: Path) -> str:
    """相对路径规范化为正斜杠（Windows Path 为反斜杠，与 allowlist 正斜杠键失配）。"""
    return rel.as_posix()


def scan(root: Path | None = None) -> list[tuple[str, str, str]]:
    """返回 [(相对路径, 规则ID, 行号)]；不包含 secret 本身。

    FIX-005-1：逐规则逐片段检查；豁免 = 三元组精确匹配，豁免后继续检查
    同一行剩余片段与其他规则；root 缺省为仓库根（cwd 无关）。
    相对路径输出统一使用正斜杠（跨平台一致）。
    """
    root = (root or REPO).resolve()
    hits: list[tuple[str, str, str]] = []
    for rel in _iter_files(root):
        rel_key = _relative_key(rel)  # 每文件只生成一次规范键
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")  # 绝对读取，cwd 无关
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for rule_id, pattern in RULES:
                for m in pattern.finditer(line):
                    if (rel_key, rule_id, m.group(0)) in ALLOWLIST:
                        continue  # 精确豁免此片段，继续检查剩余片段/其他规则
                    hits.append((rel_key, rule_id, str(lineno)))
                    break  # 该规则已命中（一行一条），继续下一规则
    return hits


def main(argv: list[str] | None = None) -> int:
    try:
        hits = scan()
    except Exception as exc:  # 内部错误：不泄漏路径/内容
        print(f"scan error: {type(exc).__name__}", file=sys.stderr)
        return 2
    if not hits:
        print("敏感信息扫描：零未豁免命中")
        return 0
    print(f"敏感信息扫描：{len(hits)} 处命中（未打印具体内容）")
    for rel, rule_id, lineno in hits:
        print(f"  {rel}:{lineno} [{rule_id}]")
    return 1


if __name__ == "__main__":
    sys.exit(main())
