"""正股开平仓方向推断（rules-spec.md §0.1 NEW-10 裁定，引擎责任）。

引擎依据快照持仓独立推断，不信任调用方自述；推断结果进入
evidence（kill_switch 规则证据的 order_direction 条目），并驱动
R1（reduce_only）与 R3–R9 的平仓豁免判定。唯一可靠实现：
kill_switch / R3–R9 全部经本模块判定，禁止各自复制推断逻辑。

快照可信门（-B §2.4）：
- snapshot_at 缺失 / 不可解析 → fail-closed open（时间不可解析，任何方向都不可信）；
- 给出 policy + now 时：快照在未来或已陈旧（age > limits.max_snapshot_age_seconds）
  → fail-closed open，不得产生可信 close；R10/R11/R12 仍照常执行并 BLOCK；
- policy/now 缺省（纯函数调用，仅测试用）：只做可解析性校验，不做时效判定。

六种组合 + fail-closed：
| 订单 | 净持仓 | quantity 关系 | 推断 |
| buy  | < 0    | qty <= |net|  | close（回补）|
| buy  | < 0    | qty >  |net|  | open（更严格）|
| buy  | >= 0 / 无持仓 | 任意 | open |
| sell | > 0    | qty <= net    | close（减仓）|
| sell | > 0    | qty >  net    | open（更严格）|
| sell | <= 0 / 无持仓 | 任意 | open |

不确定（持仓数据矛盾/数量超额/数据缺失/快照不可信）→ open（fail-closed）。
期权订单自带四值开平仓语义，直接判定（同样受快照可信门约束）。
"""

from typing import Any

from ._timeutil import parse_rfc3339, to_epoch_seconds


def _snapshot_trustworthy(portfolio: Any, policy: Any = None, now: Any = None) -> bool:
    """快照是否可信：时间可解析，且（给出 policy+now 时）新鲜（非未来、未陈旧）。

    任一时间条件不满足 → False（方向 fail-closed open）。
    policy/now 缺省时只做可解析性校验（纯函数测试场景）。
    """
    snap = parse_rfc3339(portfolio.data.get("snapshot_at", ""))
    if snap is None:
        return False
    if policy is None or now is None:
        return True
    limit = policy.limits.get("max_snapshot_age_seconds")
    if limit is None:
        return False  # 配置缺失 → fail-closed（loader 本应拦截）
    age = to_epoch_seconds(now) - to_epoch_seconds(snap)
    return 0 <= age <= limit  # 与 R11 口径一致：未来/超龄均不可信，等于上限仍可信


def infer_order_direction(order: Any, portfolio: Any, policy: Any = None, now: Any = None) -> str:
    """返回 'open' 或 'close'。永不确定 / 快照不可信 → 'open'（更严格）。"""
    if not _snapshot_trustworthy(portfolio, policy, now):
        return "open"  # 快照缺失/不可解析/陈旧/未来 → fail-closed

    if order.instrument_type == "option":
        return "close" if order.side in ("buy_to_close", "sell_to_close") else "open"

    # 正股：按快照该 symbol 的股票持仓计算净持仓
    net = 0
    ambiguous = False
    for pos in portfolio.positions:
        if not isinstance(pos, dict):
            ambiguous = True  # 持仓元素非对象 → 数据不可用 → 整单按 open（fail-closed）
            continue
        if pos.get("symbol") == order.symbol and pos.get("instrument_type") == "stock":
            side = pos.get("side")
            qty = pos.get("quantity")
            if side not in ("long", "short") or not isinstance(qty, int) or qty <= 0:
                ambiguous = True  # 数据矛盾 → 整单按 open（fail-closed）
                continue
            net += qty if side == "long" else -qty

    order_qty = order.quantity
    if not isinstance(order_qty, int) or order_qty <= 0:
        return "open"

    if ambiguous:
        return "open"

    if order.side == "buy":
        return "close" if (net < 0 and order_qty <= abs(net)) else "open"
    # sell
    return "close" if (net > 0 and order_qty <= net) else "open"


def is_close_order(order: Any, portfolio: Any, policy: Any = None, now: Any = None) -> bool:
    """平仓豁免判定：期权按四值；正股按引擎推断。快照不可信 → False（不豁免）。"""
    return infer_order_direction(order, portfolio, policy, now) == "close"
