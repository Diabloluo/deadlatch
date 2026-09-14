"""CLI 薄封装。只读文件、调用库 API、渲染输出、返回退出码。

不复制任何规则逻辑；进程退出码精确映射 result.exit_code（0/2/3/4/5）。
文件/解析错误 → stderr + exit 4；审计/报告读取类错误 → stderr + exit 5（fail-closed）。
--json 的 stdout 只含 JSON（check 符合 result.schema.json；shadow report 符合
shadow-report.schema.json）。
"""

import argparse
import json
import os
import sys
from pathlib import Path

from ._validation import InputValidationError
from .audit import (
    AuditError,
    AuditMaintenanceError,
    DEFAULT_AUDIT_PATH,
    repair_audit,
    verify_audit,
)
from .guard import (
    MAX_ORDER_FILE_BYTES,
    MAX_PORTFOLIO_FILE_BYTES,
    Guard,
    check_file_size,
    resolve_audit_path,
)
from .migrations import MigrationError, migrate_json
from .model import Order, Portfolio
from .report import build_shadow_report, parse_since


def _load_json_file(path: str, label: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise InputValidationError([f"{label} 文件不存在: {path}"])
    check_file_size(p, MAX_ORDER_FILE_BYTES if label == "order" else MAX_PORTFOLIO_FILE_BYTES, label)  # size limit
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise InputValidationError(
            [f"{label} 文件解析失败: {path} ({type(exc).__name__})"]
        ) from exc
    if not isinstance(data, dict):
        raise InputValidationError([f"{label} 顶层必须是对象: {path}"])
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deadlatch",
        description="Local-first pre-trade risk evaluation library and CLI (advisory-only).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="check a single order against the risk policy")
    check.add_argument("--policy", required=True, help="policy.yaml/.yml/.json")
    check.add_argument("--order", required=True, help="order.json")
    check.add_argument("--portfolio", required=True, help="portfolio.json")
    check.add_argument("--json", action="store_true", dest="as_json", help="JSON output only")
    check.add_argument(
        "--audit-path",
        default=None,
        help="审计 JSONL 路径覆盖（缺省：$DEADLATCH_AUDIT_PATH 或 ~/.deadlatch/audit.jsonl）",
    )
    shadow = sub.add_parser("shadow", help="影子模式工具")
    shadow_sub = shadow.add_subparsers(dest="shadow_command", required=True)
    report = shadow_sub.add_parser("report", help="影子报告聚合（免费版）")
    report.add_argument("--since", required=True, help="聚合窗口，如 30d / 7d / 24h / 30m / 60s")
    report.add_argument("--json", action="store_true", dest="as_json", help="JSON output only")
    report.add_argument(
        "--audit-path",
        default=None,
        help="审计 JSONL 路径覆盖（缺省：$DEADLATCH_AUDIT_PATH 或 ~/.deadlatch/audit.jsonl）",
    )
    migrate = sub.add_parser("migrate", help="显式离线迁移旧版本输入")
    migrate.add_argument("--kind", required=True, choices=["order", "policy", "portfolio"],
                         help="输入文档类型（决定迁移链）")
    migrate.add_argument("--input", required=True, help="输入 JSON 文件路径")
    migrate.add_argument("--output", default=None,
                         help="可选输出文件（同目录临时文件 + fsync + 原子替换；拒绝覆盖输入文件本身）")
    migrate.add_argument("--json", action="store_true", dest="as_json", help="JSON output only")
    audit = sub.add_parser("audit", help="verify or repair a local audit JSONL file")
    audit_sub = audit.add_subparsers(dest="audit_command")
    verify = audit_sub.add_parser(
        "verify",
        help="scan an audit JSONL file without changing its contents (may create a .lock sidecar)",
    )
    verify.add_argument("--json", action="store_true", dest="as_json", help="JSON output only")
    verify.add_argument(
        "--audit-path",
        default=None,
        help="审计 JSONL 路径覆盖（缺省：$DEADLATCH_AUDIT_PATH 或 ~/.deadlatch/audit.jsonl）",
    )
    repair = audit_sub.add_parser(
        "repair",
        help="quarantine damaged audit lines (requires --quarantine)",
    )
    repair.add_argument(
        "--quarantine",
        action="store_true",
        help="required: isolate damaged lines into a unique 0600 JSON file",
    )
    repair.add_argument("--json", action="store_true", dest="as_json", help="JSON output only")
    repair.add_argument(
        "--audit-path",
        default=None,
        help="审计 JSONL 路径覆盖（缺省：$DEADLATCH_AUDIT_PATH 或 ~/.deadlatch/audit.jsonl）",
    )
    return parser


def _cmd_check(args) -> int:
    try:
        guard = Guard.from_policy(args.policy, audit_path=args.audit_path)
        order = Order.from_dict(_load_json_file(args.order, "order"))
        portfolio = Portfolio.from_dict(_load_json_file(args.portfolio, "portfolio"))
        result = guard.check(order, portfolio)
    except InputValidationError as exc:
        for d in exc.details:
            print(f"error: {d}", file=sys.stderr)
        return 4
    except Exception as exc:  # fail-closed：未知异常不得 traceback 泄漏到 stdout
        print(f"internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 5
    # 输出渲染（explain/JSON 序列化/打印）异常同样必须兜底 → exit 5，
    # stderr 只给通用错误，不泄漏 traceback、路径或输入
    try:
        if args.as_json:
            out = json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
        else:
            out = result.explain()
    except Exception:
        print("internal error: output rendering failed", file=sys.stderr)
        return 5
    print(out)
    return result.exit_code


def _cmd_shadow_report(args) -> int:
    try:
        since = parse_since(args.since)
        path = resolve_audit_path(args.audit_path)
        report = build_shadow_report(path, since)
    except (AuditError, ValueError) as exc:
        # fail-closed：报告/审计错误 → stderr + exit 5，无 traceback 泄漏到 stdout
        print(f"error: {exc}", file=sys.stderr)
        return 5
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    t = report["totals"]
    print("Shadow report")
    print(f"窗口: {report['window_start']} .. {report['window_end']}")
    print(f"orders_evaluated: {t['orders_evaluated']}")
    print(f"would_block: {t['would_block']} (block_rate={t['block_rate']})")
    print(f"would_warn: {t['would_warn']} (warn_rate={t['warn_rate']})")
    rows = report.get("by_rule") or []
    if rows:
        print("top rules (block_count desc, warn_count desc, rule_id asc):")
        for r in rows:
            print(f"  - {r['rule_id']}: block={r['block_count']} warn={r['warn_count']}")
    for note in report.get("notes") or []:
        print(f"note: {note}")
    return 0


def _cmd_migrate(args) -> int:
    """显式迁移：默认 stdout；--output 原子写（拒绝覆盖输入）；错误 exit 4。"""
    try:
        in_path = Path(args.input)
        if not in_path.exists():
            raise MigrationError("输入文件不存在")
        check_file_size(in_path, MAX_ORDER_FILE_BYTES if args.kind == "order"
                        else MAX_PORTFOLIO_FILE_BYTES if args.kind == "portfolio"
                        else MAX_PORTFOLIO_FILE_BYTES, args.kind)
        text = in_path.read_text(encoding="utf-8")
        result = migrate_json(args.kind, text)
    except (MigrationError, InputValidationError) as exc:
        print(f"error: {exc}", file=sys.stderr)  # 无 traceback/路径/secret
        return 4
    except Exception as exc:
        print(f"internal error: {type(exc).__name__}", file=sys.stderr)
        return 5
    if args.output:
        out_path = Path(args.output)
        if out_path.resolve() == in_path.resolve():
            print("error: 输出路径不得覆盖输入文件本身", file=sys.stderr)
            return 4
        try:
            tmp = out_path.with_name(out_path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(result["document"], f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, out_path)
        except OSError as exc:
            print(f"error: 输出写入失败（{type(exc).__name__}，原文件未改动）", file=sys.stderr)
            return 4
        if not args.as_json:
            print(f"已迁移 {args.kind} v{result['from_version']} → v{result['to_version']}，写入 {args.output}")
            for step in result["steps"]:
                print(f"  - {step}")
        else:
            print(json.dumps({"kind": result["kind"], "from_version": result["from_version"],
                              "to_version": result["to_version"], "steps": result["steps"],
                              "output": args.output}, ensure_ascii=False, indent=2))
        return 0
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"{args.kind} v{result['from_version']} → v{result['to_version']}")
    for step in result["steps"]:
        print(f"  - {step}")
    print(json.dumps(result["document"], ensure_ascii=False, indent=2))
    return 0


def _audit_error_json(command: str) -> str:
    return json.dumps({
        "schema_version": 1,
        "operation": command,
        "status": "error",
        "valid_lines": 0,
        "invalid_lines": 0,
        "issues": [],
        "issues_truncated": False,
    }, ensure_ascii=False, indent=2)


def _cmd_audit(args) -> int:
    command = getattr(args, "audit_command", None)
    if command not in ("verify", "repair"):
        print("error: audit command required (verify or repair)", file=sys.stderr)
        return 4
    if command == "repair" and not getattr(args, "quarantine", False):
        print("error: repair requires --quarantine", file=sys.stderr)
        return 4
    try:
        path = resolve_audit_path(args.audit_path)
        result = verify_audit(path) if command == "verify" else repair_audit(path)
    except AuditMaintenanceError as exc:
        print(exc.public_message, file=sys.stderr)
        if getattr(args, "as_json", False):
            print(_audit_error_json(command))
        return exc.exit_code
    except Exception:
        print("internal error: audit maintenance failed", file=sys.stderr)
        if getattr(args, "as_json", False):
            print(_audit_error_json(command if command in ("verify", "repair") else "verify"))
        return 5
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"{result['operation']} {result['status']}")
        print(f"valid_lines={result['valid_lines']} invalid_lines={result['invalid_lines']}")
        for item in result["issues"]:
            print(f"line {item['line_number']}: {item['reason_code']}")
        if result["issues_truncated"]:
            print("issues truncated after 100")
        if result.get("quarantine_name"):
            print(f"quarantine_name={result['quarantine_name']}")
    if command == "verify":
        return 3 if result["status"] == "invalid" else 0
    if result["status"] == "repaired":
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "check":
        return _cmd_check(args)
    if args.command == "shadow" and args.shadow_command == "report":
        return _cmd_shadow_report(args)
    if args.command == "migrate":
        return _cmd_migrate(args)
    if args.command == "audit":
        return _cmd_audit(args)
    print(f"unknown command: {args.command}", file=sys.stderr)
    return 4


if __name__ == "__main__":
    sys.exit(main())
