#!/usr/bin/env python3
""" §九：生成演示 GIF（docs/assets/agent-blocked.gif）。

- 内容来自**真实本地命令/协议输出**的虚构场景：真实运行一次 MCP stdio
  check_order（BLOCK 场景），把请求/响应/Agent 停止渲染为终端风格动画；
- 无真实账户、标的组合、用户名、绝对路径、Token、Cookie（全部虚构 AAA 等）；
- 生成依赖 Pillow（仅 dev/documentation extra，不进运行依赖）；
- 可重复：相同源码状态下输出一致。

用法：python tools/make_demo_gif.py [--out docs/assets/agent-blocked.gif]
"""

import argparse
import asyncio
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError as exc:  # pragma: no cover
    raise SystemExit("需要 Pillow（dev/docs extra）：pip install -e '.[docs]'") from exc

REPO = Path(__file__).resolve().parent.parent

POLICY_YAML = """\
schema_version: 2
version: '1.0.0'
mode: enforce
base_currency: USD
kill_switch: "off"
acknowledged_disabled: []
limits:
  max_order_quantity: 500
  max_order_value: 5000.0
  max_symbol_exposure_ratio: 0.10
  max_total_exposure_ratio: 0.60
  min_cash: 0.0
  max_options_margin_ratio: 0.35
  max_daily_loss_ratio: 0.03
  max_drawdown_ratio: 0.15
  max_order_age_seconds: 300
  max_snapshot_age_seconds: 300
"""


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _real_block_output() -> dict:
    """真实运行一次 MCP check_order（BLOCK 场景），返回响应体。"""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.types import TextContent

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        policy = tmp / "policy.yaml"
        policy.write_text(POLICY_YAML, encoding="utf-8")
        pf = tmp / "portfolio.json"
        pf.write_text(json.dumps({
            "schema_version": 3, "equity": 123456.78, "cash": 30000.0,
            "day_start_equity": 125000.0, "peak_equity": 128000.0,
            "daily_pnl": -1200.5, "drawdown_ratio": 0.0355,
            "snapshot_at": _ts(now - timedelta(seconds=61)),
            "base_currency": "USD", "positions": [],
        }), encoding="utf-8")
        audit = tmp / "audit.jsonl"
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "deadlatch.mcp_server", "--policy", str(policy),
                  "--portfolio", str(pf), "--audit-path", str(audit)],
        )
        order = {
            "schema_version": 2, "symbol": "AAA", "instrument_type": "stock",
            "side": "buy", "quantity": 1000, "price": 190.0, "order_type": "limit",
            "currency": "USD", "created_at": _ts(now - timedelta(seconds=1)),
        }

        async def run():
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    res = await session.call_tool("check_order", {"order": order})
                    content = res.content[0]
                    return json.loads(content.text if isinstance(content, TextContent) else "")

        return asyncio.run(run())


# ---------------------------------------------------------------- 渲染

BG = (18, 22, 30)
FG = (216, 222, 233)
DIM = (120, 132, 150)
GREEN = (82, 200, 120)
RED = (240, 100, 100)
AMBER = (240, 180, 90)
BLUE = (96, 165, 250)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in ("/System/Library/Fonts/Menlo.ttc",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                 "C:\\Windows\\Fonts\\consola.ttf"):
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _wrap(text: str, width: int) -> list[str]:
    lines = []
    for raw in text.splitlines():
        while len(raw) > width:
            lines.append(raw[:width])
            raw = raw[width:]
        lines.append(raw)
    return lines


def _frame(draw: ImageDraw.ImageDraw, font, lines: list[tuple[str, tuple]], title: str) -> None:
    draw.rectangle([0, 0, W, H], fill=BG)
    draw.text((16, 12), title, font=font, fill=BLUE)
    draw.line([(16, 40), (W - 16, 40)], fill=(50, 60, 80))
    y = 56
    for text, color in lines:
        for line in _wrap(text, 96):
            draw.text((16, y), line, font=font, fill=color)
            y += 22
        y += 6


W, H = 960, 520


def render(body: dict, out: Path) -> None:
    font = _font(15)
    decision = body.get("decision", "BLOCK")
    exit_code = body.get("exit_code", 3)
    violations = body.get("violations", [])

    req = (
        'Agent -> check_order({"order": {"symbol": "AAA", "quantity": 1000, '
        '"price": 190.0, "side": "buy", "currency": "USD", ...}})'
    )
    resp_lines = [(f"Guard: {decision}  exit_code={exit_code}", RED), ("", DIM)]
    for v in violations[:4]:
        resp_lines.append((f"  - [{v.get('rule_id')}] {v.get('detail')}", FG))
    if len(violations) > 4:
        resp_lines.append((f"  ... and {len(violations) - 4} more", DIM))

    frames = [
        ("Agent sends order for pre-trade check",
         [(req, FG), ("", DIM),
          ("(via MCP stdio: deadlatch-mcp --policy policy.yaml --portfolio portfolio.json)", DIM),
          ("", DIM), ("waiting for the risk gate ...", DIM)]),
        ("Guard evaluates 12 rules (R1..R12)",
         [(f"decision: {decision}   exit_code: {exit_code}", RED),
          ("", DIM), *resp_lines]),
        ("Agent honors the BLOCK",
         [("Agent: decision=BLOCK — order NOT submitted.", RED),
          ("Agent: no broker call issued. Stop.", GREEN),
          ("", DIM),
          ("(advisory boundary: the guard cannot force a fully bypassing", DIM),
          (" agent to call it — but this agent called it and honored it.)", DIM)]),
    ]

    imgs = []
    for title, lines in frames:
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        _frame(d, font, lines, title)
        imgs.append(img)
    # 每帧加光标闪烁变体（动感）
    gif_frames = []
    for img in imgs:
        gif_frames.append(img)
        img2 = img.copy()
        d2 = ImageDraw.Draw(img2)
        d2.rectangle([16, H - 34, 28, H - 22], fill=FG)
        gif_frames.append(img2)
    gif_frames[0].save(out, save_all=True, append_images=gif_frames[1:],
                       duration=700, loop=0)
    print(f"GIF 已生成: {out}（{len(gif_frames)} 帧, {out.stat().st_size} 字节）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 agent-blocked 演示 GIF")
    parser.add_argument("--out", default=str(REPO / "docs" / "assets" / "agent-blocked.gif"))
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    body = _real_block_output()
    if body.get("decision") != "BLOCK":
        print(f"意外: 真实运行未得到 BLOCK（{body}）", file=sys.stderr)
        return 1
    render(body, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
