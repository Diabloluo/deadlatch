# Deadlatch

**你的交易 Agent 的盘前闩锁。仅建议（advisory-only）。**

开始之前先记住三件事：

1. **你需要一个独立、跨券商、由你自己控制的闸门。** 如果 Agent 能在你的账户上下单，Agent 与券商之间的最后一道检查不应由 Agent 自己完成，也不应被锁死在某一家券商界面或规则里。
2. **Deadlatch 不预测、不推荐、不下单。** 它只回答一个问题：*这笔订单现在是否允许？* 它不是信号发生器，也不是券商。
3. **每一个答案都带原因、证据和本地审计记录。** PASS / WARN / BLOCK 从不只是一句结论——你能看到命中哪条规则、为什么、评估了什么；每次检查都会追加到本地 JSONL 审计日志。

**诚实边界（请务必阅读）：** Deadlatch 是建议性的。它无法强制一个完全不调用它的 Agent 调用检查，也无法阻止忽略 BLOCK 的 Agent 去别处提交订单。Agent 是否调用 Guard、是否遵守结果，由集成方决定。不要把它当作避免损失的保证——它是闸门，不是保险。

- **许可证：** MIT — 见 [LICENSE](LICENSE)。
- **English:** [README.md](README.md)
- **安全：** [SECURITY.md](SECURITY.md) · **贡献：** [CONTRIBUTING.md](CONTRIBUTING.md) · **免责声明：** [DISCLAIMER.md](DISCLAIMER.md)

## 从这里开始

<!-- mcp-name: io.github.Diabloluo/deadlatch -->

`0.1.0` 是第一条稳定版本线。**PyPI 与 MCP Registry 尚未发布。** 在 https://pypi.org/p/deadlatch 能看到 `deadlatch==0.1.0` 之前，不要把下面的命令当成已经可安装。在此之前请使用当前 GitHub 预发布 wheel：[v0.1.0.dev1](https://github.com/Diabloluo/deadlatch/releases/tag/v0.1.0.dev1)。

**一行安装（待 PyPI 回读成功后）：**

```bash
pip install deadlatch==0.1.0
```

**MCP-first 启动（同样待回读成功后）：**

```bash
uvx --from deadlatch==0.1.0 deadlatch-mcp --policy policy.yaml --portfolio portfolio.json
```

`--policy` 与 `--portfolio` 是必需的本地文件；`--audit-path` 与 `--kill-switch-path` 可选。只使用虚构数据或你自己的模拟输入。Deadlatch 仅建议：永不下单，也无法阻止从不调用它的 Agent。期权订单的 `symbol` 必须是券商唯一完整合约码。

然后：

1. **用虚构数据跑下面的快速开始**（Python、CLI 或 MCP）。确认 `PASS` → `BLOCK` → 本地审计。
2. **申请 20 分钟接入评估**：仅当你已经有订单意图或模拟执行链路时，[打开评估表单](https://github.com/Diabloluo/deadlatch/issues/new?template=integration-assessment.yml)。

该 GitHub Issue **是公开的**。不要粘贴账户、持仓、订单、API key、token、客户名称或私人路径。安全漏洞必须通过 [GitHub Security Advisories](https://github.com/Diabloluo/deadlatch/security/advisories) 报告，不要开公开 Issue。

---

## 快速开始（每个 60 秒）

三个快速开始全部使用虚构数据与临时审计路径；它们由同一份源脚本执行并被测试套件自动复现，不会与文档漂移。

### 1. Python API

```bash
pip install dist/deadlatch-*.whl        # 或：pip install -e .
python docs/quickstart/python.py
```

演示 `Guard.from_policy(...)` → `Order` / `Portfolio` → `guard.check(...)`：
合法订单返回 `PASS / 0`；超量订单返回 `BLOCK / 3` 并列出命中规则；
收到 `BLOCK` 时示例调用方自行停止——不会发起任何券商调用。

### 2. CLI

```bash
bash docs/quickstart/cli.sh                  # 需要 deadlatch 在 PATH
```

在临时目录生成新鲜输入（动态时间戳，永不因静态时间自然变红），依次运行
`deadlatch check` 的 PASS（`exit 0`）、BLOCK（`exit 3`）、输入错误
（`exit 4`）与 `--json` 检查，并基于审计运行 `shadow report --json`。

### 3. MCP（stdio）

```bash
python docs/quickstart/mcp_client.py         # 需要已安装 deadlatch
```

以真实子进程 stdio 启动 `deadlatch-mcp`，列出五个工具，调用
`check_order` 一次 PASS、一次 BLOCK。`policy` / `portfolio` / `audit` 路径是
**服务器启动配置**——Agent 不能把它们作为工具参数替换。BLOCK 是调用方必须
遵守的约束语义；技术上 Guard 无法强制一个完全绕过它的 Agent 调用检查。

---

## 它是什么 / 不是什么

| Deadlatch **是** | Deadlatch **不是** |
|---|---|
| 提交前评估的本地、确定性风控闸门 | 信号发生器、推荐器或组合优化器 |
| 库 + CLI + stdio MCP 服务器——无券商连接、不修改策略 | 券商适配器、执行引擎或行情源 |
| 可审计：每次评估写入本地 JSONL 日志 | 云服务、数据库或遥测汇集点 |
| v0.1：USD-only、单腿订单、每次检查一份快照 | 多腿、多币种、Greeks/IV（见限制） |

## 适用与不适用人群

**适用：** 已经有订单意图或模拟执行链路、希望加一道独立确定性盘前闸门并保留本地审计的团队；希望用自己控制的 YAML 策略评估订单的开发者；需要一个轻量、依赖少、fail-closed 构建块的集成方。

**不适用：** 期望盈利保证、回测引擎、组合管理、或"能自我执行"的工具的人。如果 Agent 从不调用 Guard，或忽略 BLOCK，本仓库里没有任何东西能拦住它。

## 核心安全边界

- **本地：** 一切运行在你的机器上；不存储、不读取、不传输任何账户凭据。
- **无网络核心路径：** 库、CLI、MCP 服务器从不打开 socket、不注册 HTTP/SSE 路由、不向外请求行情或任何东西（MCP SDK 的 HTTP 栈是传递依赖，业务代码从不 import）。
- **永不下单：** 核心包（库、CLI、MCP 服务器）不含券商连接、从不提交订单。
  实验性只读映射示例仅存在于开发工作区，不进入公开候选或 wheel，也不是已完成真实账户验证的集成。
- **Fail-closed：** 缺失/畸形数据 → BLOCK（`exit 3`）；输入/配置错误 → `exit 4`；内部错误 → `exit 5`。不确定状态绝不报告为 PASS。
- **方向由快照推断：** 订单 `side` 的文字自述不能证明它是平仓。无论正股或期权，只有新鲜快照中存在同一标的/合约、方向相反且数量足够的持仓，才承认平仓意图。
  期权 `symbol` 必须使用券商返回的唯一完整合约码；不同到期日、行权价或权利方向不得复用底层证券代码作为 `symbol`。
- **USD-only（v0.1）：** 任何币种不一致（订单/快照/持仓）都是输入错误（`exit 4`）；MCP 账户状态工具在不一致时 fail-closed。

**写入面：** 本工具从不修改 `policy`、`portfolio` 或 kill-switch 状态，
从不连接券商，从不下单。存在两类有意的本地文件写入：

1. **审计子系统：** `Guard.check()` / `check_order` 向本地审计 JSONL 追加
   一条脱敏记录（30 天保留）；shadow report 入口（`deadlatch shadow
   report`）会触发同一保留清理——存在过期记录时原子重写审计文件；审计
   实现使用 lock/tmp 文件与 `os.replace` 保证每个事务原子。
2. **显式迁移输出：** `deadlatch migrate --output <file>` 仅在显式
   传入 `--output` 时写出迁移后的文档。

## 12 条规则（v0.1）

| # | 规则 | 守护什么 |
|---|---|---|
| R1 | `kill_switch` | 全局开关：`off` / `full`（全部拦截）/ `reduce_only`（仅放行推断为平仓的订单） |
| R2 | `input_validity` | 订单过 Schema、版本门、币种一致性、金额有限性（违例 → `exit 4`） |
| R3 | `max_order_quantity` | 单笔数量上限 |
| R4 | `max_order_value` | 单笔金额上限（期权：价格 × 乘数 × 数量） |
| R5 | `max_symbol_exposure` | 单标的名义敞口（期权按行权价 × 乘数 × 数量） |
| R6 | `max_total_exposure` | 组合毛敞口比例 |
| R7 | `cash_margin_check` | 成交后现金下限与卖权保证金 |
| R8 | `max_daily_loss` | 当日亏损比例（PnL / 日初权益） |
| R9 | `max_drawdown` | 相对峰值的回撤比例 |
| R10 | `order_time_validity` | 订单时效/未来时间（不可解析 → fail-closed BLOCK） |
| R11 | `data_freshness` | 快照新鲜度（未来快照 → fail-closed） |
| R12 | `missing_data_fail_closed` | 缺失/null/畸形快照数据 → `exit 3`（数据不可用 = 风险） |

可选规则（R3–R7）由 `policy.yaml` 中的配置键开关；缺失的可选键必须列入
`acknowledged_disabled`，否则策略被拒（`exit 4`）。强制规则（R1、R2、R8–R12）
永不可禁用。

## 退出码

| 码 | 含义 |
|---|---|
| `0` | PASS — 订单按当前形态允许 |
| `2` | WARN — 仅当你的执行策略明确允许警告时才可继续 |
| `3` | BLOCK — 不得提交（风控规则或 fail-closed 数据） |
| `4` | 输入/配置错误 — 调用方用错了工具，不是风控事件 |
| `5` | 内部/规则异常 — 按 BLOCK 处理（fail-closed） |

shadow 模式下内部裁决写入 `shadow_verdict`，对外投影为 `PASS / 0`；
kill switch 命中与 `exit 4/5` 永不被投影掉。

## 数据契约与迁移

Schema 为带版本号的 JSON Schema 2020-12 文件，随包安装：
`order`、`portfolio`、`policy`、`result`、`audit-record`、`shadow-report`。
旧版本文档提供显式离线迁移：

```bash
deadlatch migrate --kind order    --input order_v1.json    [--output out.json]
deadlatch migrate --kind policy   --input policy_v1.json   [--output out.json]
deadlatch migrate --kind portfolio --input portfolio_v1.json [--output out.json]
```

迁移只转换已裁定字段（如 policy v1 布尔 kill switch → `off`/`full`），
绝不猜测业务字段。正常评估入口拒绝旧版本（`exit 4`），不做隐式迁移。

## 审计日志

每次 `Guard.check()` 向本地 JSONL 审计文件追加一条记录（默认
`~/.deadlatch/audit.jsonl`，可用 `--audit-path` / `DEADLATCH_AUDIT_PATH`
覆盖）。记录经 Schema 校验、脱敏（凭据/Cookie/绝对路径不明文落盘），并在
**追加同一锁事务内**按 **30 天保留**窗口清理。审计写失败时返回结果只升不降地
降级：PASS/0 → WARN/2；BLOCK/3/4/5 保持原裁决并附 `audit_write_failed` 警告——
磁盘与返回的 Result 永不互相矛盾。

## MCP 服务器

`deadlatch-mcp` 是 **stdio-only** 的 MCP 服务器（无 TCP 监听、无 HTTP/SSE
路由）。五个工具**只读**：没有任何工具能修改 `policy`、`portfolio` 或
kill-switch 状态（这些路径是启动配置，不是工具参数）。policy 内容变更会在
下一次工具调用时自动校验并重载；可选的独立 kill-switch 文件在每次调用时
无条件重读，且只能让策略更严格。注意：服务器仍会把
每次 `check_order` 评估追加到本地审计日志——这是设计行为，不是工具能力。
五个工具：

| 工具 | 用途 |
|---|---|
| `check_order` | 评估一笔订单；返回完整 `result`（decision、exit code、violations、evidence） |
| `get_account_status` | 快照新鲜度、权益、现金、盈亏、回撤、敞口利用率 |
| `get_policy` | 当前生效策略的只读投影 |
| `kill_switch_status` | 当前 kill-switch 状态（只读；没有任何工具能修改它） |
| `recent_decisions` | 近期审计记录（oldest-first，可选 `since`/`limit`） |

启动：

```bash
deadlatch-mcp --policy policy.yaml --portfolio portfolio.json \
  [--audit-path audit.jsonl] [--kill-switch-path kill-switch]
```

各路径参数仅为启动配置，其文件内容仍是实时本地状态。配置了独立开关文件时，
文件内容必须严格为 `off`、`reduce_only` 或 `full`；它不能解除 policy 中更严格
的模式。实时 policy/开关缺失、损坏或并发改写不稳定时一律 fail-closed：工具错误
为 `isError=true` + `fail_closed`，配置错误带 `input_error=true` + `exit_code=4`。

## 演示

Agent 用一笔超量订单调用 `check_order`；Guard 返回 `BLOCK / 3` 并列出命中
规则；Agent 明确停止，不调用任何券商工具。GIF 由真实本地 MCP stdio 运行
（虚构数据）生成（`tools/make_demo_gif.py`）：

![Agent blocked by Deadlatch](docs/assets/agent-blocked.gif)

## 已知限制（v0.1）

- 裸卖认购期权的上行风险无上限；v0.1 采用基于行权价的敞口近似，不能把它当作空头认购风险的保守上界。

- 卖空现金流按 `0` 建模（文档化简化）。
- 无 Greeks、IV、多腿策略或多币种账本。
- 审计跨进程锁依赖 POSIX `fcntl`；非 POSIX 平台退化为进程内锁（无跨进程保证）。
- Guard 无法阻止完全绕过：从不调用它、或忽略 BLOCK 直接调用券商的 Agent，
  本工具拦不住。
- 仓库内示例仅使用虚构代码与数据。

## 治理

- [SECURITY.md](SECURITY.md) — 支持版本、漏洞范围、报告渠道。
- [CONTRIBUTING.md](CONTRIBUTING.md) — 环境、测试/Schema/扫描/coverage 命令、规则纪律。
- [DISCLAIMER.md](DISCLAIMER.md) — 完整法律/风险免责声明（摘要见下）。
- [README.md](README.md) — English version.

**免责声明（摘要）：** 非投资建议；不保证避免损失；请自行验证输入与规则；
Guard 永不下单；所有示例均为虚构；未经模拟请勿直接实盘；无 SLA。
全文见 [DISCLAIMER.md](DISCLAIMER.md)。
