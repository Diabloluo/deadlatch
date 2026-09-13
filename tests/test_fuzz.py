""" §二 模糊输入与资源边界（T10）+ FIX-005-5：畸形输入不得 PASS。

- 单字段畸形变异：从合法基线出发逐字段变异，每个样本记录预期非法原因；
  Schema 能识别的先用独立 Draft 2020-12 validator 断言非法，再要求三入口
  （库 API / CLI / MCP check_order）均为 exit 4 / isError+input_error+fail_closed；
- NaN/±Infinity 等 Schema 不足以拒绝的特殊值单列：断言入口有限数检查 exit 4；
- "Schema 合法边界"策略：合法订单不得得到输入错误 exit 4，允许按风险 PASS/WARN/BLOCK，
  不得异常 exit 5；
- 错误输出注入 fake token/Cookie/path：断言三入口不回显；
- 不使用 try/except 丢弃生成样本、不使用 assume() 过滤；确定性由
  conftest 注册的 deadlatch-deterministic profile 保证。
"""

import asyncio
import contextlib
import io
import json
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, strategies as st
from jsonschema import Draft202012Validator

from tests.conftest import NOW, fresh_portfolio, full_policy, make_standard
from deadlatch import Guard, load_policy_file
from deadlatch._validation import InputValidationError, _validator
from deadlatch.audit import read_audit_records
from deadlatch.model import Order

REPO = Path(__file__).resolve().parents[1]

POL = full_policy()
BASE_ORDER = {
    "schema_version": 2, "symbol": "AAA", "instrument_type": "stock",
    "side": "buy", "quantity": 20, "price": 190.0, "order_type": "limit",
    "currency": "USD", "created_at": "2026-08-29T10:00:00Z",
}


def _mutate(**changes) -> dict:
    return {**BASE_ORDER, **changes}


# 单字段畸形变异表：(标签, 变异, 类别)。类别 = "schema"（独立 validator 必须拒绝）
# 或 "runtime"（schema 接受但运行时必须 exit 4）。
MUTATIONS = [
    ("schema_version 降级", _mutate(schema_version=1), "schema"),
    ("schema_version 类型", _mutate(schema_version="2"), "schema"),
    ("instrument_type 非法", _mutate(instrument_type="future"), "schema"),
    ("正股 side 四值", _mutate(side="buy_to_open"), "schema"),
    ("quantity 为 0", _mutate(quantity=0), "schema"),
    ("quantity 负数", _mutate(quantity=-5), "schema"),
    ("quantity 浮点", _mutate(quantity=1.5), "schema"),
    ("price 为 0", _mutate(price=0), "schema"),
    ("price 字符串", _mutate(price="abc"), "schema"),
    ("currency 小写", _mutate(currency="usd"), "schema"),
    ("created_at 无时区", _mutate(created_at="2026-08-29T10:00:00"), "schema"),
    ("symbol 超长", _mutate(symbol="x" * 65), "schema"),
    ("未知字段", _mutate(hack=1), "schema"),
    ("正股带 option", _mutate(option={"underlying": "AAA", "expiry": "2026-09-18",
                                      "strike": 190.0, "right": "put", "multiplier": 100}), "schema"),
    ("currency 不一致", _mutate(currency="EUR"), "runtime"),
]

# （quantity=501 触发 R3 规则层 BLOCK/3 而非输入错误，由 test_rules_basic 覆盖，不在本表）


@pytest.mark.parametrize("label,doc,category", MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_mutation_single_field_never_passes(label, doc, category):
    # 独立 validator 预判
    schema_errors = list(_validator("order").iter_errors(doc))
    if category == "schema":
        assert schema_errors, f"{label} 应为 schema 非法"
    else:
        assert not schema_errors, f"{label} 应为 schema 合法（运行时拒绝）"
    # 库 API：exit 4（BLOCK，输入错误，不 PASS）
    r = make_standard(POL).check(Order.from_dict(doc), fresh_portfolio(), now=NOW)
    assert r.exit_code == 4, (label, r.exit_code, r.decision)
    assert r.decision == "BLOCK"
    # CLI：exit 4 无 traceback
    rc, out, err = _cli_check(doc)
    assert rc == 4, (label, rc)
    assert "Traceback" not in out and "Traceback" not in err
    # MCP：isError + input_error + fail_closed + exit 4
    err_mcp, body = _mcp_check(doc)
    assert err_mcp is True, label
    assert body["input_error"] is True and body["fail_closed"] is True
    assert body["exit_code"] == 4, label


_POLICY_YAML = (
    "schema_version: 2\nversion: '1.0.0'\nmode: enforce\nbase_currency: USD\n"
    'kill_switch: "off"\nacknowledged_disabled: []\nlimits:\n'
    "  max_order_quantity: 500\n  max_order_value: 5000.0\n"
    "  max_symbol_exposure_ratio: 0.10\n  max_total_exposure_ratio: 0.60\n"
    "  min_cash: 0.0\n  max_options_margin_ratio: 0.35\n"
    "  max_daily_loss_ratio: 0.03\n  max_drawdown_ratio: 0.15\n"
    "  max_order_age_seconds: 300\n  max_snapshot_age_seconds: 300\n"
)


def _cli_check(doc, pf_dict=None):
    from deadlatch.cli import main

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        policy = tmp / "policy.yaml"
        policy.write_text(_POLICY_YAML, encoding="utf-8")
        order = tmp / "order.json"
        order.write_text(json.dumps(doc), encoding="utf-8")
        portfolio = tmp / "portfolio.json"
        portfolio.write_text(json.dumps(pf_dict if pf_dict is not None else {
            "schema_version": 3, "equity": 123456.78, "cash": 30000.0,
            "day_start_equity": 125000.0, "peak_equity": 128000.0,
            "daily_pnl": -1200.5, "drawdown_ratio": 0.0355,
            "snapshot_at": "2026-08-29T09:59:00Z", "base_currency": "USD", "positions": [],
        }), encoding="utf-8")
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            rc = main(["check", "--policy", str(policy), "--order", str(order),
                       "--portfolio", str(portfolio)])
        return rc, out_buf.getvalue(), err_buf.getvalue()


def _mcp_check(doc, portfolio_kwargs=None):
    from deadlatch.mcp_server import MCPGuardServer
    from tests.test_mcp_server import _write_audit, _write_policy, _write_portfolio

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        srv = MCPGuardServer(str(_write_policy(tmp)),
                             str(_write_portfolio(tmp, **(portfolio_kwargs or {}))),
                             str(_write_audit(tmp, [])))
        params = type("P", (), {"name": "check_order", "arguments": {"order": doc}})()
        r = asyncio.run(srv._call_tool(None, params))
        return r.is_error, json.loads(r.content[0].text)


# ---------------- NaN / ±Infinity（Schema 不足以拒绝，入口有限数检查） ----------------

@pytest.mark.parametrize("bad_price, schema_rejects", [
    (float("nan"), False),
    (float("inf"), False),
    # jsonschema 的 minimum 比较会拒绝 -inf（比较语义），同样零 PASS——入口拒绝即可
    (float("-inf"), True),
])
def test_non_finite_price_rejected_by_all_entries(bad_price, schema_rejects):
    doc = _mutate(price=bad_price)
    # Schema 层面：nan/inf 是 number 不被拒；-inf 命中 minimum 被拒（均为"不得 PASS"）
    assert bool(list(_validator("order").iter_errors(doc))) == schema_rejects
    # 库 API：无论 schema 是否拒绝，入口都必须 exit 4（有限数检查 / schema 门）
    r = make_standard(POL).check(Order.from_dict(doc), fresh_portfolio(), now=NOW)
    assert r.exit_code == 4
    assert r.decision == "BLOCK"
    # CLI：exit 4 无 traceback
    rc, out, err = _cli_check(doc)
    assert rc == 4
    assert "Traceback" not in out and "Traceback" not in err
    # MCP：isError + fail_closed
    err_mcp, body = _mcp_check(doc)
    assert err_mcp is True and body["fail_closed"] is True


# ---------------- Schema 合法边界：不得输入错误，允许风险裁决，不得 exit 5 ----------------

@given(
    side=st.sampled_from(["buy", "sell"]),
    quantity=st.integers(min_value=1, max_value=500),
    price=st.floats(min_value=0.01, max_value=100000.0, allow_nan=False, allow_infinity=False),
)
def test_schema_valid_orders_never_input_error_or_exit5(side, quantity, price):
    doc = _mutate(side=side, quantity=quantity, price=price)
    assert not list(_validator("order").iter_errors(doc))  # schema 合法
    r = make_standard(POL).check(Order.from_dict(doc), fresh_portfolio(), now=NOW)
    assert r.exit_code != 4, (side, quantity, price)  # 合法订单不得输入错误
    assert r.exit_code != 5  # 不得异常
    assert r.exit_code in (0, 2, 3)  # 允许 PASS/WARN/BLOCK


# ---------------- 错误输出不得回显注入的敏感值 ----------------

@pytest.mark.parametrize("secret", ["sk-" + "FAK" + "..." + "cdef",
                                    "Cookie: " + "sessionid=FAKECOOKIE123456789",
                                    "/Users/" + "DEADLATCH_TEST_USER/secret"])
def test_error_output_never_echoes_injected_secret(secret):
    # side 枚举非法且值含敏感形态 → 三入口错误输出零明文
    doc = _mutate(side=secret)
    r = make_standard(POL).check(Order.from_dict(doc), fresh_portfolio(), now=NOW)
    assert r.exit_code == 4
    rc, out, err = _cli_check(doc)
    assert rc == 4
    assert secret not in out and secret not in err
    err_mcp, body = _mcp_check(doc)
    assert err_mcp is True
    assert secret not in json.dumps(body)


# ---------------- FIX-005-6/7/8：版本门/币种/YAML kill_switch 零原始值 ----------------

def _s(kind: str) -> str:
    """恶意值构造（字符串拼接防扫描器自命中）。"""
    return {
        "token": "sk-" + "FAKESECRET1234567890abcdef",
        "cookie": "Cookie: " + "sessionid=FAKECOOKIE123456789",
        "path": "/Users/" + "DEADLATCH_TEST_USER/secret",
        # 3 大写字母可过 ^[A-Z]{3}$ pattern → 走币种 mismatch 分支（Codex 泄漏路径）
        "iso3": "TOK",
    }[kind]


def _all_forms(secret) -> list[str]:
    """注入值的全部可回显形态（对象展开为序列化文本与内部字符串）。"""
    forms: list[str] = []

    def _walk(v):
        if isinstance(v, str):
            forms.append(v)
        elif isinstance(v, dict):
            forms.append(json.dumps(v, ensure_ascii=False, sort_keys=True))
            for x in v.values():
                _walk(x)

    _walk(secret)
    return forms


def _assert_forms_absent(secret, *texts) -> None:
    for f in _all_forms(secret):
        for t in texts:
            assert f not in t, f"泄漏形态 {f!r}"


_PF_BASE = {
    "schema_version": 3, "equity": 123456.78, "cash": 30000.0,
    "day_start_equity": 125000.0, "peak_equity": 128000.0,
    "daily_pnl": -1200.5, "drawdown_ratio": 0.0355,
    "snapshot_at": "2026-08-29T09:59:00Z", "base_currency": "USD", "positions": [],
}

_POS_USD = {"symbol": "AAA", "instrument_type": "stock", "side": "long",
            "quantity": 100, "market_value": 19000.0, "currency": "USD"}


# ---- FIX-005-6：版本门不回显实际值 ----

@pytest.mark.parametrize("bad_version", [_s("token"), _s("cookie"), _s("path"),
                                         {"inner": _s("token")}],
                         ids=["token", "cookie", "path", "object"])
def test_fix56_order_schema_version_never_echoed(bad_version):
    doc = _mutate(schema_version=bad_version)
    r = make_standard(POL).check(Order.from_dict(doc), fresh_portfolio(), now=NOW)
    assert r.exit_code == 4 and r.decision == "BLOCK"
    blob = json.dumps(r.to_dict(), ensure_ascii=False)
    _assert_forms_absent(bad_version, blob)
    assert "schema_version" in blob  # 定位信息存在（字段路径 + 固定类别）
    rc, out, err = _cli_check(doc)
    assert rc == 4
    _assert_forms_absent(bad_version, out, err)
    assert "Traceback" not in out and "Traceback" not in err
    err_mcp, body = _mcp_check(doc)
    assert err_mcp is True and body["input_error"] is True
    assert body["fail_closed"] is True and body["exit_code"] == 4
    _assert_forms_absent(bad_version, json.dumps(body, ensure_ascii=False))


@pytest.mark.parametrize("bad_version", [_s("token"), {"v": _s("token")}],
                         ids=["token", "object"])
def test_fix56_portfolio_schema_version_never_echoed(bad_version):
    pf = fresh_portfolio(schema_version=bad_version)
    r = make_standard(POL).check(Order.from_dict(_mutate()), pf, now=NOW)
    assert r.exit_code == 4
    blob = json.dumps(r.to_dict(), ensure_ascii=False)
    _assert_forms_absent(bad_version, blob)
    assert "schema_version" in blob
    rc, out, err = _cli_check(_mutate(), pf_dict={**_PF_BASE, "schema_version": bad_version})
    assert rc == 4
    _assert_forms_absent(bad_version, out, err)
    assert "Traceback" not in out and "Traceback" not in err
    # MCP：portfolio 是服务器配置 → fail-closed（exit 3，非 input_error）
    err_mcp, body = _mcp_check(_mutate(), portfolio_kwargs={"schema_version": bad_version})
    assert err_mcp is True and body["fail_closed"] is True
    _assert_forms_absent(bad_version, json.dumps(body, ensure_ascii=False))


@pytest.mark.parametrize("bad_version", [_s("token"), {"v": _s("token")}],
                         ids=["token", "object"])
def test_fix56_policy_schema_version_never_echoed(bad_version, tmp_path):
    data = POL.to_dict()
    data["schema_version"] = bad_version
    p = tmp_path / "policy.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(InputValidationError) as ei:
        load_policy_file(str(p))
    _assert_forms_absent(bad_version, str(ei.value))
    assert "schema_version" in str(ei.value)


# ---- FIX-005-7：币种 mismatch 不回显调用方值 ----

@pytest.mark.parametrize("bad_currency", [_s("token"), _s("cookie"), _s("path"), _s("iso3")],
                         ids=["token", "cookie", "path", "iso3"])
def test_fix57_order_currency_never_echoed(bad_currency):
    doc = _mutate(currency=bad_currency)
    r = make_standard(POL).check(Order.from_dict(doc), fresh_portfolio(), now=NOW)
    assert r.exit_code == 4
    # 敏感形态（token/cookie/path）：全 Result（含 evidence.order_summary）零原文；
    # iso3 为非敏感 3 字母（用于覆盖 mismatch 分支），断言错误文本/CLI/MCP 零原文
    if bad_currency != _s("iso3"):
        blob = json.dumps(r.to_dict(), ensure_ascii=False)
        _assert_forms_absent(bad_currency, blob)
    detail = " ".join(v["detail"] for v in r.to_dict()["violations"])
    assert bad_currency not in detail
    assert "order.currency" in detail  # 安全字段路径存在
    if bad_currency == _s("iso3"):
        assert "currency mismatch" in detail  # mismatch 分支固定类别
    rc, out, err = _cli_check(doc)
    assert rc == 4
    _assert_forms_absent(bad_currency, out, err)
    assert "Traceback" not in out and "Traceback" not in err
    err_mcp, body = _mcp_check(doc)
    assert err_mcp is True and body["input_error"] is True
    assert body["fail_closed"] is True and body["exit_code"] == 4
    _assert_forms_absent(bad_currency, json.dumps(body, ensure_ascii=False))


@pytest.mark.parametrize("bad_currency", [_s("token"), _s("path"), _s("iso3")],
                         ids=["token", "path", "iso3"])
def test_fix57_portfolio_base_currency_never_echoed(bad_currency):
    pf = fresh_portfolio(base_currency=bad_currency)
    r = make_standard(POL).check(Order.from_dict(_mutate()), pf, now=NOW)
    assert r.exit_code == 4
    if bad_currency != _s("iso3"):
        blob = json.dumps(r.to_dict(), ensure_ascii=False)
        _assert_forms_absent(bad_currency, blob)
    detail = " ".join(v["detail"] for v in r.to_dict()["violations"])
    assert bad_currency not in detail
    assert "portfolio.base_currency" in detail
    if bad_currency == _s("iso3"):
        assert "currency mismatch" in detail
    rc, out, err = _cli_check(_mutate(), pf_dict={**_PF_BASE, "base_currency": bad_currency})
    assert rc == 4
    _assert_forms_absent(bad_currency, out, err)
    assert "Traceback" not in out and "Traceback" not in err
    err_mcp, body = _mcp_check(_mutate(), portfolio_kwargs={"base_currency": bad_currency})
    assert err_mcp is True and body["fail_closed"] is True
    _assert_forms_absent(bad_currency, json.dumps(body, ensure_ascii=False))


@pytest.mark.parametrize("bad_currency", [_s("token"), _s("cookie"), _s("iso3")],
                         ids=["token", "cookie", "iso3"])
def test_fix57_positions_currency_never_echoed(bad_currency):
    pos = {**_POS_USD, "currency": bad_currency}
    pf = fresh_portfolio(positions=[pos])
    r = make_standard(POL).check(Order.from_dict(_mutate()), pf, now=NOW)
    assert r.exit_code == 4
    if bad_currency != _s("iso3"):
        blob = json.dumps(r.to_dict(), ensure_ascii=False)
        _assert_forms_absent(bad_currency, blob)
    detail = " ".join(v["detail"] for v in r.to_dict()["violations"])
    assert bad_currency not in detail
    assert "positions[0].currency" in detail
    if bad_currency == _s("iso3"):
        assert "currency mismatch" in detail
    rc, out, err = _cli_check(_mutate(), pf_dict={**_PF_BASE, "positions": [pos]})
    assert rc == 4
    _assert_forms_absent(bad_currency, out, err)
    assert "Traceback" not in out and "Traceback" not in err
    err_mcp, body = _mcp_check(_mutate(), portfolio_kwargs={"positions": [pos]})
    assert err_mcp is True and body["fail_closed"] is True
    _assert_forms_absent(bad_currency, json.dumps(body, ensure_ascii=False))


def test_fix57_audit_jsonl_never_echoes_injected_values(tmp_path):
    """产生审计记录的 Guard 路径：审计 JSONL 零原文。"""
    policy = tmp_path / "policy.yaml"
    policy.write_text(_POLICY_YAML, encoding="utf-8")
    audit = tmp_path / "audit.jsonl"
    guard = Guard.from_policy(str(policy), audit_path=str(audit))
    for doc in (_mutate(schema_version=_s("token")),
                _mutate(currency=_s("cookie")),
                _mutate(currency=_s("path"))):
        r = guard.check(Order.from_dict(doc), fresh_portfolio(), now=NOW)
        assert r.exit_code == 4
    records = read_audit_records(audit)
    assert len(records) == 3
    raw = "\n".join(json.dumps(rec, ensure_ascii=False) for rec in records)
    for sec in (_s("token"), _s("cookie"), _s("path")):
        assert sec not in raw


# ---- FIX-005-8：YAML kill_switch 类型错误不回显值 ----

@pytest.mark.parametrize("bad_yaml", [
    "kill_switch: false",   # YAML 裸布尔（YAML 1.1 陷阱）
    "kill_switch:\n  secret: \"" + _s("token") + "\"",
    "kill_switch:\n  cookie: \"" + _s("cookie") + "\"\n  path: \"" + _s("path") + "\"",
], ids=["bool_false", "object_token", "nested_cookie_path"])
def test_fix58_yaml_kill_switch_never_echoed(bad_yaml, tmp_path):
    from deadlatch.cli import main

    yaml_text = ("schema_version: 2\nversion: '1.0.0'\nmode: enforce\nbase_currency: USD\n"
                 + bad_yaml + "\n"
                 + "acknowledged_disabled: []\nlimits:\n  max_order_quantity: 500\n"
                   "  max_order_value: 5000.0\n  max_symbol_exposure_ratio: 0.10\n"
                   "  max_total_exposure_ratio: 0.60\n  min_cash: 0.0\n"
                   "  max_options_margin_ratio: 0.35\n  max_daily_loss_ratio: 0.03\n"
                   "  max_drawdown_ratio: 0.15\n  max_order_age_seconds: 300\n"
                   "  max_snapshot_age_seconds: 300\n")
    p = tmp_path / "policy.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    # loader：InputValidationError，details 零原文、定位信息存在
    with pytest.raises(InputValidationError) as ei:
        load_policy_file(str(p))
    msg = str(ei.value)
    assert "kill_switch" in msg
    assert "字符串" in msg and "off" in msg
    for sec in (_s("token"), _s("cookie"), _s("path")):
        assert sec not in msg
    # CLI：exit 4，stderr 零原文零 traceback
    order = tmp_path / "order.json"
    order.write_text(json.dumps(_mutate()), encoding="utf-8")
    pf = tmp_path / "portfolio.json"
    pf.write_text(json.dumps(_PF_BASE), encoding="utf-8")
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        rc = main(["check", "--policy", str(p), "--order", str(order),
                   "--portfolio", str(pf)])
    assert rc == 4
    out, err = out_buf.getvalue(), err_buf.getvalue()
    for sec in (_s("token"), _s("cookie"), _s("path")):
        assert sec not in out and sec not in err
    assert "Traceback" not in out and "Traceback" not in err
