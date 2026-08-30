# Deadlatch — 虚构示例（全部标的行为虚构代码，可离线运行）

## 运行方式（在仓库根目录）

示例时间戳为生成时刻的静态值，随墙上时间推移会触发 R10/R11 陈旧 BLOCK（预期行为）。
**CLI 复现前先刷新时间戳**（完全离线，无网络依赖）：

```bash
python tools/refresh_examples.py        # 把全部示例时间戳重写为当前时刻（订单 now−1s，快照 now−61s）
python tools/refresh_examples.py --check  # 只检查是否过期
```

然后直接执行（退出码即结果）：

```bash
.venv/bin/python -m deadlatch.cli check \
  --policy examples/01_pass/policy.yaml \
  --order examples/01_pass/order.json \
  --portfolio examples/01_pass/portfolio.json
```

| 场景 | 目录 | 预期退出码 | 说明 |
|---|---|---|---|
| 正常 PASS | `01_pass/` | 0 | 全部规则通过 |
| 单标的敞口 BLOCK | `02_symbol_exposure/` | 3 | 大额买入使单标的敞口超 10% 权益（R5；R3/R4 亦触发） |
| 日亏损熔断 BLOCK | `03_daily_loss/` | 3 | daily_pnl 达 -8% ≤ -3% 熔断线（R8 达到即触发） |
| kill switch `full` BLOCK | `04_kill_full/` | 3 | R1 full 拦截一切订单（含平仓） |
| kill switch `reduce_only` | `05_kill_reduce_only/` | open→3 / close→0 | open_order.json 推断为开仓 → BLOCK；close_order.json 推断为平仓 → 放行 |
| 卖方期权 R4 PASS / R5 BLOCK | `06_seller_option/` | 3 | 卖 10 张行权价 190 看跌，权利金 2.5：R4 现金流 2,500 ≤ 5,000（PASS），R5 敞口 190,000 > 10% 权益（BLOCK）；R7 保证金占用亦触发 |

## 测试与固定时钟

`tests/test_examples.py` 的评估时钟由示例文件自身时间戳派生
（`max(created_at, snapshot_at) + 1s`），不随墙上时间漂移——无需刷新即可反复运行：

```bash
.venv/bin/python -m pytest -p no:cacheprovider tests/test_examples.py -q
```

`policy.yaml` 为完整 12 规则启用的标准策略；刷新工具只改写 `created_at` /
`snapshot_at` 两个时间字段，其余内容原样保留。
