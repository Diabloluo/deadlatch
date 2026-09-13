#!/usr/bin/env python3
"""Verify MANIFEST.sha256.json against files on disk.

Used on a sanitized candidate or public tree. Any stale, missing, or extra
hashed path fails. The hash file does not list itself.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

HASH_MANIFEST_NAME = "MANIFEST.sha256.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_hash_manifest(root: Path) -> list[str]:
    """Return human-readable problems; empty list means every listed hash matches."""
    manifest_path = root / HASH_MANIFEST_NAME
    if not manifest_path.is_file():
        return [f"missing {HASH_MANIFEST_NAME}"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return [f"unreadable {HASH_MANIFEST_NAME}"]
    if not isinstance(manifest, dict) or not manifest:
        return [f"empty or invalid {HASH_MANIFEST_NAME}"]
    problems: list[str] = []
    if HASH_MANIFEST_NAME in manifest:
        problems.append(f"{HASH_MANIFEST_NAME} must not hash itself")
    for rel, expected in sorted(manifest.items()):
        if not isinstance(rel, str) or not isinstance(expected, str):
            problems.append("non-string manifest entry")
            continue
        path = root / rel
        if not path.is_file():
            problems.append(f"missing {rel}")
            continue
        actual = sha256_file(path)
        if actual != expected:
            problems.append(f"stale {rel}")
    return problems


def main(argv: list[str] | None = None) -> int:
    root = Path(argv[0] if argv else ".").resolve()
    problems = verify_hash_manifest(root)
    if problems:
        print(f"哈希清单校验失败（{len(problems)}）:", file=sys.stderr)
        for item in problems[:20]:
            print(f"  {item}", file=sys.stderr)
        return 1
    print("哈希清单校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
