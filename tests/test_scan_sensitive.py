""" 敏感信息扫描（T8 + FIX-005-1）：pytest 自动执行 tools/scan_sensitive.py。

- 全仓扫描零未豁免命中（任何 CI 运行 tests/ 自动执行）；
- FIX-005-1 反例：同行使假 key + password 必须命中 R-TOKEN、假路径 + Cookie 必须
  命中 R-COOKIE；sentinel 出现在未授权文件必须命中；真实用户名全仓零命中；
  cwd 无关；输出只给相对文件/规则 ID/行号（不打印 secret）。
- 本文件自身的敏感探针全部用字符串拼接构造，避免自命中。
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_scan_sensitive_zero_unexempted_hits():
    r = subprocess.run(
        [sys.executable, str(REPO / "tools" / "scan_sensitive.py")],
        capture_output=True, text=True, cwd=str(REPO), timeout=120,
    )
    assert r.returncode == 0, f"敏感信息扫描命中（rc={r.returncode}）:\n{r.stdout}\n{r.stderr}"
    assert "零未豁免命中" in r.stdout


def test_scan_sensitive_cwd_independent(tmp_path):
    # 从任意 cwd 调用必须扫描仓库根（绝对路径读取）
    r = subprocess.run(
        [sys.executable, str(REPO / "tools" / "scan_sensitive.py")],
        capture_output=True, text=True, cwd=str(tmp_path), timeout=120,
    )
    assert r.returncode == 0, r.stdout


def test_scan_no_username_leak_across_repo():
    # 真实本机用户名路径全仓零命中（直接全仓搜索，不依赖扫描器）
    import getpass
    import sys as _sys

    _sys.path.insert(0, str(REPO / "tools"))
    import scan_sensitive as ss

    real = "/Users/" + getpass.getuser()
    assert real.startswith("/Users/") and "QOG_TEST" not in real  # 确认是真实用户
    for rel in ss._iter_files(ss.REPO):
        text = (ss.REPO / rel).read_text(encoding="utf-8", errors="replace")
        assert real not in text, rel


def test_scan_same_line_fake_key_plus_password_hits(monkeypatch):
    # 同一行：假 key 被精确豁免后，password 片段仍必须命中 R-TOKEN（不掩盖第二个泄漏）
    import sys as _sys

    _sys.path.insert(0, str(REPO / "tools"))
    import scan_sensitive as ss

    root = REPO / "tests" / "_scan_tmp"
    root.mkdir(parents=True, exist_ok=True)
    try:
        f = root / "leak.py"
        fake_key = "sk-" + "FAKEKEY1234567890"
        real_pw = "password=" + "REALLEAKSECRET42"
        f.write_text(f'creds = "{fake_key}" + " {real_pw}"\n', encoding="utf-8")
        monkeypatch.setattr(ss, "ALLOWLIST", ss.ALLOWLIST | {
            ("leak.py", "R-TOKEN", fake_key),
        })
        hits = ss.scan(root)
        assert any(rid == "R-TOKEN" for _, rid, _ in hits), hits  # password 泄漏仍检出
    finally:
        import shutil

        shutil.rmtree(root)


def test_scan_same_line_fake_path_plus_cookie_hits(monkeypatch):
    # 同一行：假路径被精确豁免后，Cookie 片段仍必须命中 R-COOKIE
    import sys as _sys

    _sys.path.insert(0, str(REPO / "tools"))
    import scan_sensitive as ss

    root = REPO / "tests" / "_scan_tmp"
    root.mkdir(parents=True, exist_ok=True)
    try:
        f = root / "leak.py"
        fake_path = "/Users/" + "QOG_TEST_USER/x"
        real_cookie = "Cookie: " + "sessionid=" + "REALLEAKCOOKIE99"
        f.write_text(f'path = "{fake_path}" + " {real_cookie}"\n', encoding="utf-8")
        monkeypatch.setattr(ss, "ALLOWLIST", ss.ALLOWLIST | {
            ("leak.py", "R-PATH", fake_path),
        })
        hits = ss.scan(root)
        assert any(rid == "R-COOKIE" for _, rid, _ in hits), hits  # Cookie 泄漏仍检出
    finally:
        import shutil

        shutil.rmtree(root)


def test_scan_sentinel_in_unauthorized_file_hits():
    # 相同 sentinel 出现在未授权文件（非 allowlist 三元组）→ 必须命中
    import sys as _sys

    _sys.path.insert(0, str(REPO / "tools"))
    import scan_sensitive as ss

    root = REPO / "tests" / "_scan_tmp"
    root.mkdir(parents=True, exist_ok=True)
    try:
        f = root / "not_authorized.txt"
        f.write_text("sk-" + "FAKEKEY1234567890\n", encoding="utf-8")
        hits = ss.scan(root)
        assert any(rid == "R-TOKEN" for _, rid, _ in hits), hits
    finally:
        import shutil

        shutil.rmtree(root)


def test_scan_output_never_prints_secret():
    import sys as _sys

    _sys.path.insert(0, str(REPO / "tools"))
    import scan_sensitive as ss

    root = REPO / "tests" / "_scan_tmp"
    root.mkdir(parents=True, exist_ok=True)
    try:
        leak = "sk-" + "REALLEAK1234567890abcdef"
        (root / "leak.py").write_text(f'token = "{leak}"\n', encoding="utf-8")
        hits = ss.scan(root)
        assert hits  # 确会命中
        for rel, rid, lineno in hits:
            assert "REALLEAK" not in rel and "REALLEAK" not in rid and "REALLEAK" not in lineno
            assert isinstance(rel, str) and rel  # 相对文件路径
            assert "\\" not in rel  # 跨平台统一正斜杠相对路径（顶层文件可无斜杠）
            assert rid.startswith("R-") and lineno.isdigit()  # 输出格式（不打印 secret）
    finally:
        import shutil

        shutil.rmtree(root)


def test_relative_key_normalizes_windows_paths():
    """Windows 反斜杠路径 → 正斜杠键，且能精确命中 allowlist（不依赖 Windows runner）。"""
    import sys as _sys
    from pathlib import PureWindowsPath

    _sys.path.insert(0, str(REPO / "tools"))
    import scan_sensitive as ss

    win_rel = PureWindowsPath("tests") / "test_audit.py"
    assert isinstance(win_rel, PureWindowsPath)
    key = ss._relative_key(win_rel)
    assert key == "tests/test_audit.py"  # 规范化后为正斜杠
    assert "\\" not in key
    # 从现有 ALLOWLIST 选取对应合法三元组（无新增敏感字面量）：
    # 取 test_audit.py 的 R-COOKIE 精确豁免值，证明规范化后可以精确命中
    cookie_entry = [t for t in ss.ALLOWLIST
                    if t[0] == "tests/test_audit.py" and t[1] == "R-COOKIE"]
    assert cookie_entry, "ALLOWLIST 应含 test_audit.py 的 R-COOKIE 三元组"
    rel_key, rule_id, exact_value = cookie_entry[0]
    # 模拟 scan 的豁免判定：Windows 风格 rel 经规范化后必须匹配
    assert (ss._relative_key(PureWindowsPath(rel_key)), rule_id, exact_value) in ss.ALLOWLIST
    # 未授权文件（同值不同文件）仍命中
    assert ("tests/other_file.py", rule_id, exact_value) not in ss.ALLOWLIST


def test_scan_sensitive_rules_detect_real_shapes():
    """规则能力验证：真实形态 secret 必须被检出（不允许规则形同虚设）。

    探针用字符串拼接构造，避免测试文件自身被扫描命中。
    """
    import sys as _sys

    _sys.path.insert(0, str(REPO / "tools"))
    from scan_sensitive import RULES

    probes = [
        'token = "' + "sk-" + "REALLEAK1234567890abcdef" + '"',
        "Cookie: " + "sessionid=" + "REALLEAKCOOKIE42",
        "-----BEGIN " + "RSA PRIVATE KEY-----",
        "/Users/" + "someuser/.ssh/id_rsa",
    ]
    for probe in probes:
        assert any(pat.search(probe) for _, pat in RULES), probe
