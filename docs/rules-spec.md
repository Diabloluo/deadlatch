# Rules spec (v0.1)

Public contract for Deadlatch's twelve pre-trade rules.
All trigger conditions are exact mathematical expressions; every `>` / `>=`
boundary is written here; all money and ratio math is Decimal (no float).

---

## 0. 总则（所有规则共同适用）

### 0.1 符号与口径约定

| 符号 | 含义 |
|---|---|
| `M` | 合约乘数，**从 `order.option.multiplier` 读取**。期权订单缺 `multiplier` → 输入错误（exit 4）。代码中禁止出现硬编码 `100` |
| `price` | 正股=每股价格；期权=每股权利金 |
| `qty` | 正股=股数；期权=合约张数（正整数） |

**敞口（Exposure）统一口径**——用于 R5/R6 及全部涉及敞口的规则：

| 标的/方向 | 敞口贡献 Δ | 说明 |
|---|---|---|
| 股票 buy（推断=开仓） | `+price × qty` | 多头建仓/加仓（净持仓 ≥ 0 或无持仓） |
| 股票 buy（推断=平仓） | `−price × qty`（按快照该标的市值） | 空头回补（净持仓 < 0 且 qty ≤ \|净持仓\|） |
| 股票 sell（推断=开仓） | `+price × qty` | 卖空建仓/加仓（净持仓 ≤ 0 或无持仓） |
| 股票 sell（推断=平仓） | `−price × qty`（按快照该标的市值） | 多头减仓（净持仓 > 0 且 qty ≤ 净持仓） |
| 期权 buy_to_open | `+price × M × qty` | 多头，最大损失=权利金 |
| 期权 sell_to_open | **`+strike × M × qty`（行权价口径，不是权利金！）** | 裸卖假设（v0.1 保守口径），最大风险=行权价值 |
| 期权 buy_to_close | `−strike × M × qty` | 减少空头敞口（按行权价口径扣减） |
| 期权 sell_to_close | `−price × M × qty` | 减少多头敞口（按权利金口径扣减） |

**持仓敞口（portfolio.positions 计算，引擎自算、不信任输入）**：

| 持仓 | 敞口 |
|---|---|
| 股票 long / short | `market_value` |
| 期权 long | `avg_cost × M × qty`（`avg_cost` 缺失 → `market_value`） |
| 期权 short | **`strike × M × qty`（裸卖假设，保守；备兑识别为 v0.2+）** |

**归组**：正股按 `symbol` 归组；期权按 `option.underlying` 归组，且与同 underlying 的正股合并（R5 单标的口径）。

**股票与期权开平仓推断（引擎责任，结果写入 `evidence.order_direction`）**：

- `side` 取值（Schema 条件枚举强制，见 order.schema.json allOf）：正股 = `buy` / `sell`；期权 = 四值（`buy_to_open` / `sell_to_open` / `buy_to_close` / `sell_to_close`）。这些值表达调用方意图，不能独自证明真实开平仓方向。
- 正股开平仓由**引擎**依据快照持仓推断，规则如下（`净持仓` = 快照中该 symbol 的 long − short）：

| 订单 | 快照净持仓 | quantity 关系 | 推断 |
|---|---|---|---|
| buy | < 0（空头） | qty ≤ \|净持仓\| | close（回补） |
| buy | < 0（空头） | qty > \|净持仓\| | **open**（整单按开仓，更严格） |
| buy | ≥ 0 或无持仓 | 任意 | open（建仓/加仓） |
| sell | > 0（多头） | qty ≤ 净持仓 | close（减仓） |
| sell | > 0（多头） | qty > 净持仓 | **open**（整单按开仓，更严格） |
| sell | ≤ 0 或无持仓 | 任意 | open（卖空建仓/加仓） |

- 期权 `buy_to_close` 只有在新鲜快照中找到同一完整合约的足量 short 持仓时才推断为 close；`sell_to_close` 只有在新鲜快照中找到同一完整合约的足量 long 持仓时才推断为 close。完整合约至少逐字段一致匹配 `symbol` 与 `option` 对象；方向相反、数量超额、合约不符或数据矛盾一律推断为 open。
- 无法证实的 `buy_to_close` 在风险计算中按 buy open 处理（权利金敞口与现金流出）；无法证实的 `sell_to_close` 按 sell open 处理（行权价敞口与卖方保证金）。不能只取消平仓豁免后仍沿用负敞口/零保证金的 close 口径。
- **推断不确定（数据矛盾、持仓缺失、数量超额）→ 按 `open` 处理**（fail-closed，更严格）。快照陈旧由 R11 先行 BLOCK（不豁免），推断只发生在新鲜快照上——与 R11 的关系：**R11 是推断的前置闸门，陈旧快照下订单根本到不了推断阶段**。
- **豁免语义随推断方向**：推断 `close` → 享受 §0.2 平仓豁免（R3–R9 豁免，R1/R10/R11/R12 不豁免）；推断 `open` → 不豁免。
- 调用方若已知开平仓语义（如自建执行层），可在 `order_summary` 之外另行注明，但引擎仍按上述规则独立推断并写入 evidence（引擎不信任调用方自述）。

### 0.2 平仓豁免裁定（写死，不得实现者自行变更）

| 规则 | 平仓（buy_to_close / sell_to_close）是否豁免 | 理由 |
|---|---|---|
| R1 kill_switch | full 模式**不豁免**；reduce_only 模式按定义仅放行 close | kill switch = 总停或仅减仓。full 下一切订单（含平仓）BLOCK，紧急减仓走券商端手动通道——这是设计意图，不是缺陷；reduce_only 下"放行平仓"是其定义而非豁免 |
| R2 input_validity | 不豁免（N/A） | 畸形平仓订单同样是输入错误 |
| R3 max_order_quantity | **豁免** | 平仓是降低风险动作，上限类规则不应困住减仓 |
| R4 max_order_value | **豁免** | 同上（大额平仓合法） |
| R5 max_symbol_exposure | **豁免** | 平仓减少敞口，天然不会超限；若仍被拦截则用户无法减仓 |
| R6 max_total_exposure | **豁免** | 同上 |
| R7 cash_margin_check | **豁免** | 平仓释放资金/保证金，无需现金充足性门槛 |
| R8 max_daily_loss | **豁免** | 日亏熔断拦住平仓 = 越亏越困，阻止止损 |
| R9 max_drawdown | **豁免** | 同上 |
| R10 order_time_validity | **不豁免** | 陈旧订单任何方向都不可执行（正确性前提） |
| R11 data_freshness | **不豁免** | 陈旧快照下任何方向都不可信（安全前提） |
| R12 missing_data_fail_closed | **不豁免** | 数据缺失时无法验证任何方向 |

豁免的平仓订单仍照常评估不豁免规则、照常写审计记录。对正股订单，豁免判定基于**引擎推断方向**（见 0.1 开平仓推断表）；推断为 `close` 才享受豁免。

### 0.3 退出码语义（全局）

`0`=PASS ｜ `2`=WARN ｜ `3`=BLOCK ｜ `4`=INPUT_ERROR（调用方输入错误，含币种不匹配）｜ `5`=INTERNAL_ERROR（引擎异常，一律按 BLOCK 处理，禁止 fail-open）。

**退出码分诊**：`4` = 调用方问题（订单/快照/配置/版本）；`5` = 引擎自身异常（规则计算抛错等）。policy 文件缺失或解析失败按 `4` 处理（配置问题归调用方，不误导排查引擎 bug）。无论 `4` 或 `5`，均不得产生 PASS。

### 0.4 边界判定总则

- **上限类规则**（R3 数量、R4 金额、R5 单标的上限、R6 总敞口、R7 现金/保证金）：**恰好等于阈值一律 PASS**（上限是包含式——等于上限 = 合法最大敞口），触发条件用严格 `>` / `<`；
- **熔断类规则**（R8 每日亏损、R9 账户回撤）：**达到即触发**（包含式，`<=` / `>=`）——熔断语义是"损失预算耗尽即停止"：恰好用尽预算仍继续开仓会立即进入超限状态，风控惯例为达到即触发。
- **启用/禁用裁定（全部阈值规则统一适用，不得逐条不同）**：
  - **强制规则（不可禁用，缺失即非法配置 exit 4）**：R1 `kill_switch`（顶层必填）、R8 `max_daily_loss_ratio`、R9 `max_drawdown_ratio`、R10 `max_order_age_seconds`、R11 `max_snapshot_age_seconds`（后四项列于 `limits.required`）；R2 / R12 恒启用。
  - **可选规则（禁用必须显式承认）**：其余 limits 字段缺失时，其规则 ID 必须列入 `policy.acknowledged_disabled`，否则 policy 无效（exit 4）——"静默禁用不可接受，可见的禁用才可接受"；`acknowledged_disabled` 中出现强制规则 ID → 非法配置（exit 4）。
  - **主方向 Schema 强制**："键缺失且未承认"的判定落在 **Schema 层（allOf 主方向 5 条条件，见 policy.schema.json）**——可选规则配置键缺失时 `acknowledged_disabled` 必须包含对应规则 ID（`cash_margin_check` 以 `min_cash` / `max_options_margin_ratio` 任一缺失为触发），不依赖实现者记得写代码；同时 **policy loader 在引擎初始化时做运行时双重校验**（与币种一致性同理，同属输入错误语义 exit 4），二者缺一不可。配置级校验归属 policy loader（作用于 policy，不归 R2；R2 作用于订单）。
  - **可见性**：每次 check 的 `evidence.inactive_rules` 列出全部已禁用规则；CLI 与 MCP 每次输出显示未启用规则数量。
  - **数值合法性**：阈值数值为 `0`、负值或非有限值 → 非法配置（exit 4）——数值 0 不承载任何"禁用"语义。例外：`min_cash` 的 0 与负值均为合法阈值（语义=最低现金线，允许保证金借款），仅"键缺失"且未在 `acknowledged_disabled` 承认时按可选规则处理。
- **acknowledged_disabled 完整性**：
  - **取值集合固定**：`acknowledged_disabled` 的元素必须是 12 个规则 ID 之一（Schema `enum` 强制），拼写错误直接 Schema 拒绝（exit 4），不允许"无效项被静默接受"；
  - **矛盾状态裁定**：某规则的配置键存在（=启用中）且其规则 ID 同时出现在 `acknowledged_disabled` → **非法配置（exit 4）**——安全产品不对矛盾输入作善意猜测；Schema 层以 allOf 条件拒绝，运行时双重校验；
  - **规则-配置键映射表**（判定"缺失"与"矛盾"的唯一依据）：`kill_switch`→`policy.kill_switch`；`input_validity`→（无键，恒启用）；`max_order_quantity`→`limits.max_order_quantity`；`max_order_value`→`limits.max_order_value`；`max_symbol_exposure`→`limits.max_symbol_exposure_ratio`；`max_total_exposure`→`limits.max_total_exposure_ratio`；`cash_margin_check`→`limits.min_cash` 与 `limits.max_options_margin_ratio`（两键同时存在才启用，任一缺失须整体承认禁用）；`max_daily_loss`→`limits.max_daily_loss_ratio`；`max_drawdown`→`limits.max_drawdown_ratio`；`order_time_validity`→`limits.max_order_age_seconds`；`data_freshness`→`limits.max_snapshot_age_seconds`；`missing_data_fail_closed`→（无键，恒启用）。
- **比例字段口径**：所有比例字段一律以 `_ratio` 后缀命名并采用**分数口径**（`0.03 = 3%`，禁止整数百分数）；Schema 层 `maximum: 1` 一致施加；字段 description 首句注明 `fraction, not percent`。旧百分比后缀命名已废弃（v0.1 改名成本为零；若需沿用，属破坏性变更，禁止）。
- **熔断口径**：每日亏损与账户回撤统一以**比例**表达：`daily_loss_ratio = daily_pnl / day_start_equity`（分母为快照提供的当日起始权益）；派生时点=每次评估即时计算，不跨请求缓存、不依赖任何历史状态；`max_daily_loss_ratio`（R8）与 `max_drawdown_ratio`（R9）同为比例字段，口径一致。
- **Schema 版本策略**：各 Schema 采用**独立版本号**（`schema_version` 各自自增）——仅发生破坏性变更的 Schema 递增，其余不动。破坏性变更定义：字段删除 / 改名 / 类型或枚举变更 / 必填集合变化。当前基线：`policy.schema.json` 2（kill_switch 三态化）、`order.schema.json` 2（side 条件枚举）、`portfolio.schema.json` 3（drawdown_ratio 改名 v2 + 权益类字段移除 exclusiveMinimum v3）、`result.schema.json` 2（evidence.inactive_rules 必填）；`audit-record.schema.json` 为 v1/v2 `oneOf`（v2 增加 prev_hash / record_hash，本地哈希链仅 tamper-evident）；`shadow-report.schema.json` 保持 1。实例携带的 `schema_version` 是显式迁移的唯一判据。本地哈希链不是数字签名；无外部锚时删除最后一条或整个可见集合不可靠可检测。v1 审计记录仅兼容 legacy 基准文件且必须出现在任何 v2 之前；日分片中的 v1 或 v2 之后再出现 v1 为版本降级，verify/repair/read 拒绝。影子报告与 MCP 读取在同一锁内校验链后才返回记录。
- **schema_version 运行时处理（fail-closed）**：引擎对每个输入实例（order / portfolio / policy / result 校验输入）执行**版本门**：
  - 实例 `schema_version` **高于**引擎已知版本 → **exit_code 4，拒绝运行**——无法安全解释未知语义，安全产品不得对未知输入作善意猜测（与矛盾状态同一原则）；
  - 实例 `schema_version` **低于**当前版本 → v0.1 一律拒绝（exit 4）；旧版本须走显式离线迁移后再评估；**禁止"按旧语义静默评估"**；
  - 实例**缺失** `schema_version` → exit 4（与必填字段一致）；
  - 判定点：policy 实例由 policy loader 执行；order / portfolio 等实例由 R2 input_validity 执行；Schema 层 `const` 已先于运行时门拒绝一切非当前版本（双保险）。
- 金额比较必须 Decimal（`x-precision: decimal`），禁止 float。
- 时间比较用 UTC 纪元秒（整数）做差。

### 0.5 缺失输入行为（R12 细则）

| 缺失对象 | 行为 |
|---|---|
| 被启用规则所需的 portfolio 字段（equity/daily_pnl/snapshot_at 等） | R12 → BLOCK（exit 3） |
| 订单结构字段（quantity/price/currency/created_at） | R2 → INPUT_ERROR（exit 4） |
| 期权订单的 multiplier / strike / expiry / right / underlying | R2 → INPUT_ERROR（exit 4） |
| **强制规则**的配置键缺失（`kill_switch` / `max_daily_loss_ratio` / `max_drawdown_ratio` / `max_order_age_seconds` / `max_snapshot_age_seconds`） | **非法配置，exit 4**（强制规则，不可禁用） |
| **可选规则**的配置键缺失，且规则 ID **未**列入 `policy.acknowledged_disabled` | **非法配置，exit 4**（静默禁用不可接受） |
| **可选规则**的配置键缺失，且规则 ID **已**在 `acknowledged_disabled` 中承认 | 规则不启用，写入 `evidence.inactive_rules` |
| policy 文件缺失/无法解析 | **非法配置，exit 4**（配置问题归调用方，非引擎内部异常；任何情况下不产生 PASS） |

---

## R1 — kill_switch（全局 Kill Switch）

1. **规则 ID**：`kill_switch`
2. **触发条件**（三态枚举）：
   - `policy.kill_switch == "off"` → 不拦截（放行）；
   - `policy.kill_switch == "full"` → 拦截**所有**订单（正股 buy/sell；期权四值，含全部平仓方向）——总停语义，紧急减仓须绕过 Guard 直连券商（此句写入 README）；
   - `policy.kill_switch == "reduce_only"` → 仅放行**引擎推断为 close** 的订单：正股按 §0.1 推断表；期权的 `buy_to_close` / `sell_to_close` 还必须由新鲜快照中的同合约、反方向、足量持仓证明；其余（推断=open）一律 BLOCK。裁决依据 = `evidence.order_direction`。
3. **输入字段**：`policy.kill_switch`；reduce_only 时另需推断所需快照持仓。
4. **缺失输入行为**：policy 无此字段 → schema 校验失败（INPUT_ERROR，exit 4）；policy 加载失败 → INPUT_ERROR（exit 4：配置问题归调用方，不误导排查引擎 bug）。**shadow 模式下依然强制生效（三种状态均不可被 shadow 绕过）。** reduce_only 下快照缺失/陈旧由 R12/R11 先行处理（均不豁免）。
5. **输出等级**：BLOCK（full=全部订单；reduce_only=推断为 open 的订单）。不可配置。
6. **Evidence 清单**：`kill_switch="full"|"reduce_only"`、`order_direction`（reduce_only 时）、`engaged_at`（可选）。
7. **边界情况**：三态枚举，无数值边界。非法值（如 `"enable"`）→ Schema 拒绝（exit 4）。`off` → PASS。
8. **豁免**：无（reduce_only 的"放行平仓"是其定义，不是豁免）。
9. **放行 ≠ 免检（写死）**：`reduce_only` 下推断为 close 的订单**仅 R1 这一关放行**——仍须通过 R2（输入合法性）、R10（订单时效）、R11（快照新鲜度），这三条**不豁免**；敞口/金额类（R3–R9）按 §0.2 平仓豁免执行。**绝不允许出现"reduce_only 就跳过全部检查"的实现。**
10. **滚仓行为**：`reduce_only` 下滚仓（roll）会被拆为两半——平旧仓放行、开新仓 BLOCK。**这是正确行为，不是缺陷**（已写入 README 能力边界）。
11. **降级摩擦（硬性约束）**：`full` → `reduce_only` 是**降低安全等级**的动作。MCP 没有任何写入或切换工具；policy 文件内容在下一次工具调用时自动校验重载。可选独立 kill-switch 文件每次调用无条件重读，但只能在 policy 基础上收紧，不能把 `full` 降为 `reduce_only` 或 `off`。
12. **验收测试点**：① `reduce_only` 下推断=open 的订单必须 BLOCK（含把开仓伪装成平仓的尝试——引擎独立推断、不信任调用方自述）；② `full` 下推断=close 的订单也必须 BLOCK（平仓不豁免）；③ policy 改动在下一次调用生效，非法重载 fail-closed；④ 独立开关每次重读且不能降低 policy 的安全等级。

## R2 — input_validity（必填字段与数值合法性）

1. **规则 ID**：`input_validity`
2. **触发条件**：订单违反 `order.schema.json` 任何约束，包括但不限于：
   - 缺必填字段（symbol/instrument_type/side/quantity/price/order_type/currency/created_at）
   - `side` 不在条件枚举内（正股：buy/sell；期权：buy_to_open/sell_to_open/buy_to_close/sell_to_close）
   - `quantity ≤ 0` 或非整数；`price ≤ 0`
   - `currency` 非 `^[A-Z]{3}$` 或 **`currency != policy.base_currency`**（v0.1 即 `!= "USD"`）
   - 期权订单缺 `option` 或 option 子字段非法；正股订单带 `option`
   - `created_at` 非 RFC3339 显式时区格式
   - 任一额外未知字段（additionalProperties 违规）
   → 结果为 **INPUT_ERROR（exit 4）**，**不是 BLOCK**——调用方用错了工具，不是风控事件。审计记录 `exit_code=4`。
3. **输入字段**：order 全字段 + `policy.base_currency`。
4. **缺失输入行为**：本规则即缺失检测器，恒启用、不可关闭。
5. **输出等级**：INPUT_ERROR（exit 4）——MCP 层对调用方返回明确错误，绝不产生 PASS。
6. **Evidence 清单**：违例字段名列表、期望值/实际值。
7. **边界情况**：`quantity == 0` → 非法（`exclusiveMinimum: 0`，即 `≤ 0` 全非法）；`price == 0` → 非法；`currency` 小写 → 非法（必须大写三字母）。
8. **豁免**：N/A（恒启用）。

## R3 — max_order_quantity（单笔数量上限）

1. **规则 ID**：`max_order_quantity`
2. **触发条件**：`order.quantity > limits.max_order_quantity`（**恰好相等 → PASS**）。
3. **输入字段**：`order.quantity`、`limits.max_order_quantity`。
4. **缺失输入行为**：缺 `max_order_quantity` 且规则 ID `max_order_quantity` **未**列入 `policy.acknowledged_disabled` → **非法配置（exit 4）**；缺 `max_order_quantity` 且**已**显式承认 → 规则不启用，写入 `evidence.inactive_rules`。**不存在无条件的"规则不启用"路径。** 值为 `0` / 负值 / 非有限 → 非法配置（exit 4）。缺 `quantity` → 已由 R2 拦截。
5. **输出等级**：BLOCK。
6. **Evidence 清单**：`triggered_value=quantity`、`limit`。
7. **边界情况**：`quantity == limit` → PASS；`quantity = 0 / 负数` → R2（exit 4）；极大值 → BLOCK。
8. **股票/期权口径**：数量口径一致（同一 `quantity` 字段，股/张）。期权**不需要**乘数——上限按张数计。
9. **豁免**：平仓豁免（见 0.2）。

## R4 — max_order_value（单笔金额上限）

1. **规则 ID**：`max_order_value`
2. **触发条件**：`order_value > limits.max_order_value`（**恰好相等 → PASS**）。
3. **order_value 计算口径**：
   - 股票（所有方向）：`order_value = price × quantity`
   - 期权（所有方向）：`order_value = price × M × quantity`（`M = order.option.multiplier`，从输入读取；禁止硬编码 100）
   - 说明：本规则衡量的是**订单现金流转金额**（买=支出、卖=收入），不是敞口；敞口口径见 R5/R6。
4. **输入字段**：`order.price / quantity / instrument_type / option.multiplier`、`limits.max_order_value`。
5. **缺失输入行为**：缺 `max_order_value` 且规则 ID `max_order_value` **未**列入 `policy.acknowledged_disabled` → **非法配置（exit 4）**；缺 `max_order_value` 且**已**显式承认 → 规则不启用，写入 `evidence.inactive_rules`。**不存在无条件的"规则不启用"路径。** 值为 `0` / 负值 / 非有限 → 非法配置（exit 4）。期权缺 `multiplier` → R2（exit 4）。
6. **输出等级**：BLOCK。
7. **Evidence 清单**：`order_value`、`limit`、`used_multiplier`（期权必带）。
8. **边界情况**：`price == 0` → R2；`order_value == limit` → PASS；极大 → BLOCK。
9. **豁免**：平仓豁免（0.2）。

**R4 算例**——`max_order_value` 是现金流上限，不是风险上限：

> 卖出 10 张行权价 190、权利金 2.50 的看跌期权（option sell_to_open，M=100），`max_order_value: 5000`，`max_symbol_exposure_ratio: 0.10`，权益 100,000：

| 度量 | 数值 | 裁决 | 该规则的 evidence |
|---|---|---|---|
| R4 `order_value = price × M × qty` | 2.50 × 100 × 10 = **2,500** | **PASS**（2,500 ≤ 5,000） | `order_value=2500, limit=5000, used_multiplier=100` |
| R5 `post_exposure = strike × M × qty` | 190 × 100 × 10 = **190,000**（占比 190%） | **BLOCK**（190,000 > 10,000） | `pre_exposure, order_delta=190000, post_exposure=190000, equity=100000, ratio=1.9, limit=0.10, used_multiplier=100` |

**结论**：把 `max_order_value` 当风险上限的用户会在 5,000 的"预算"内建立 190,000 的敞口——风险由 R5/R6（账户比例口径）兜底。单笔风险**不设绝对上限**（见 §4 能力边界）。

## R5 — max_symbol_exposure（单标的持仓比例）

1. **规则 ID**：`max_symbol_exposure`
2. **触发条件**：`post_exposure(symbol_group) / portfolio.equity > limits.max_symbol_exposure_ratio`（**恰好相等 → PASS**）。
   - `post_exposure = pre_exposure(组内) + Δ(order)`；`symbol_group` 归组规则见 0.1（正股按 symbol，期权按 underlying，同 underlying 正股+期权合并）。
   - `pre_exposure` 由引擎按 0.1 持仓敞口表**自行计算**（不信任快照中的任何敞口字段）。
   - `Δ(order)` 按 0.1 方向表（关键：期权 sell_to_open 用 **`strike × M × qty`**，不是权利金）。
3. **输入字段**：order 全字段、`portfolio.equity / positions`、`limits.max_symbol_exposure_ratio`。
4. **缺失输入行为**：缺 `max_symbol_exposure_ratio` 且规则 ID `max_symbol_exposure` **未**列入 `policy.acknowledged_disabled` → **非法配置（exit 4）**；缺 `max_symbol_exposure_ratio` 且**已**显式承认 → 规则不启用，写入 `evidence.inactive_rules`。**不存在无条件的"规则不启用"路径。** 值为 `0` / 负值 / 非有限 → 非法配置（exit 4）。缺 portfolio 或 equity → R12 BLOCK（exit 3）。
5. **输出等级**：BLOCK。
6. **Evidence 清单**：`pre_exposure`、`order_delta`、`post_exposure`、`equity`、`ratio`、`limit`、`used_multiplier`（期权）、`group_members`（组内标的列表）。
7. **边界情况**：
   - `equity <= 0` → BLOCK（分母非法，fail-closed）；
   - `post_exposure / equity == limit` 恰好 → PASS（严格 `>` 触发）；
   - `post_exposure == 0`（净敞口为零）→ PASS；
   - 期权 qty 巨大 → 按行权价口径 BLOCK（这正是"用权利金口径会漏"的场景）。
8. **豁免**：平仓豁免（0.2）。

## R6 — max_total_exposure（总敞口比例）

1. **规则 ID**：`max_total_exposure`
2. **触发条件**：`post_total_gross / portfolio.equity > limits.max_total_exposure_ratio`（**恰好相等 → PASS**）。
   - `post_total_gross = pre_total_gross + |Δ(order)|`（开仓方向）或 `pre_total_gross − 对应持仓敞口`（平仓方向——但本规则对平仓豁免）。
   - `pre_total_gross = Σ 全部持仓敞口绝对值`（多头+空头都计入**毛敞口**；v0.1 不做多腿/对冲合并——见 0.6 能力边界）。
   - 单笔敞口口径同 R5（期权 sell_to_open 用行权价）。
3. **输入字段**：同 R5 + `limits.max_total_exposure_ratio`。
4. **缺失输入行为**：同 R5（配置键 `max_total_exposure_ratio`，规则 ID `max_total_exposure`）。
5. **输出等级**：BLOCK。
6. **Evidence 清单**：`pre_total_gross`、`order_delta`、`post_total_gross`、`equity`、`ratio`、`limit`、`used_multiplier`。
7. **边界情况**：`equity <= 0` → BLOCK；恰好相等 → PASS；空组合（pre_total=0）+ 开仓 → 按订单 Δ 判定。
8. **豁免**：平仓豁免（0.2）。

## R7 — cash_margin_check（现金与保证金检查）

1. **规则 ID**：`cash_margin_check`（含两条子检查，任一触发即 BLOCK）
2. **触发条件**：
   - **R7a 现金充足性**：`post_trade_cash < limits.min_cash`（**恰好相等 → PASS**）；`post_trade_cash = portfolio.cash − outflow(order)`。
     - `outflow` 口径：
       - 股票 buy（推断=开仓）：`price × qty`
       - 期权 buy_to_open：`price × M × qty`
       - 股票 sell（推断=开仓，卖空）：`0`（卖空所得与保证金要求相抵，v0.1 简化口径，写入 evidence）
       - 期权 sell_to_open：`max(strike × M × qty − price × M × qty, 0)`（裸卖保证金占用 = 行权价值 − 已收权利金）
       - 平仓方向：定义完整（buy_to_close 期权 = `price × M × qty`；sell_to_close = 0），但本规则对平仓豁免
   - **R7b 卖出期权保证金占用**：`(existing_short_margin + new_short_margin) / portfolio.equity > limits.max_options_margin_ratio`（**恰好相等 → PASS**）。
     - `existing_short_margin = Σ 快照中 short 期权持仓 max(strike × M × qty − avg_cost × M × qty, 0)`（avg_cost 缺失 → `strike × M × qty`）
     - `new_short_margin` = 对 sell_to_open 订单按同式（avg_cost 位用 price）
3. **输入字段**：order 全字段、`portfolio.cash / equity / positions`、`limits.min_cash`、`limits.max_options_margin_ratio`。
4. **缺失输入行为**：本规则配置键为 `min_cash` 与 `max_options_margin_ratio` **两个**（§0.4 映射表）——**任一缺失即视为整条规则未启用**，须将规则 ID `cash_margin_check` **整体**列入 `policy.acknowledged_disabled`，否则 **非法配置（exit 4）**；已显式承认 → 规则不启用，写入 `evidence.inactive_rules`。**不存在无条件的"规则不启用"路径。** 例外：`min_cash` 的 `0` 与负值为**合法阈值**（语义=最低现金线，允许保证金借款），不适用"值为 0 → exit 4"；`max_options_margin_ratio` 的 `0` / 负值 / 非有限 → 非法配置（exit 4）。缺 cash/equity → R12 BLOCK。
5. **输出等级**：BLOCK。
6. **Evidence 清单**：R7a：`cash`、`outflow`、`post_trade_cash`、`min_cash`；R7b：`existing_short_margin`、`new_short_margin`、`equity`、`margin_ratio`、`limit`、`used_multiplier`。
7. **边界情况**：`cash` 为负 → 合法（保证金账户），由 `min_cash` 判定；`equity <= 0` → BLOCK；恰好相等 → PASS；`max_options_margin_ratio == 0` → 任何 sell_to_open 都 BLOCK（语义=禁止卖权，合法配置）。
8. **豁免**：平仓豁免（0.2）。

## R8 — max_daily_loss（每日亏损熔断）

1. **规则 ID**：`max_daily_loss`
2. **触发条件**：`daily_loss_ratio <= −limits.max_daily_loss_ratio` → BLOCK（**达到即触发**：亏损恰好等于限额同样拦截——熔断类规则采用包含式边界，理由见 §0.4）。其中 `daily_loss_ratio = portfolio.daily_pnl / portfolio.day_start_equity`（比例口径，与 `max_drawdown_ratio` 一致；派生时点=每次评估即时计算，见 §0.4 熔断口径）。
3. **输入字段**：`portfolio.daily_pnl`、`portfolio.day_start_equity`、`limits.max_daily_loss_ratio`。
4. **缺失输入行为**：缺 `max_daily_loss_ratio` → **非法配置（exit 4）**——本规则为**强制规则**，不可禁用、不可静默跳过，且不得出现在 `acknowledged_disabled` 中。值为 `0` / 负值 / 非有限 → 同样 exit 4（§0.4 阈值取值约定）。缺 `daily_pnl` 或缺 `day_start_equity` → R12 BLOCK；`day_start_equity <= 0` → R12 BLOCK（分母非法，fail-closed）。
5. **输出等级**：BLOCK。
6. **Evidence 清单**：`daily_pnl`、`day_start_equity`、`daily_loss_ratio`、`limit`。
7. **边界情况**：`daily_loss_ratio == −limit` 恰好 → **BLOCK**（熔断包含式）；`daily_loss_ratio > −limit`（含正值）→ PASS。
8. **股票/期权口径**：账户级规则，不区分标的类型，无分口径。
9. **豁免**：平仓豁免（0.2）——止损减仓不被日亏熔断拦住。

## R9 — max_drawdown（账户回撤熔断）

1. **规则 ID**：`max_drawdown`
2. **触发条件**：`portfolio.drawdown_ratio >= limits.max_drawdown_ratio` → BLOCK（**达到即触发**：回撤恰好等于上限同样拦截——熔断类规则采用包含式边界，理由见 §0.4）。
   - `drawdown_ratio` 定义：`(peak_equity − equity) / peak_equity`，非负；快照直接提供，引擎校验 `0 ≤ drawdown_ratio` 且与 equity/peak_equity 自洽（偏差 > 0.5% → R12 BLOCK，防伪造快照）。
3. **输入字段**：`portfolio.drawdown_ratio / equity / peak_equity`、`limits.max_drawdown_ratio`。
4. **缺失输入行为**：缺 `max_drawdown_ratio` → **非法配置（exit 4）**——本规则为**强制规则**，不可禁用、不可静默跳过，且不得出现在 `acknowledged_disabled` 中。值为 `0` / 负值 / 非有限 → 同样 exit 4（§0.4 阈值取值约定）。缺 drawdown_ratio / peak_equity → R12 BLOCK。
5. **输出等级**：BLOCK。
6. **Evidence 清单**：`drawdown_ratio`、`equity`、`peak_equity`、`limit`。
7. **边界情况**：`drawdown_ratio == 0`（新高）→ PASS；`drawdown_ratio == limit` 恰好 → **BLOCK**（熔断包含式）；`peak_equity <= 0` → BLOCK（数据非法）。
8. **豁免**：平仓豁免（0.2）。

## R10 — order_time_validity（订单时间有效性）

1. **规则 ID**：`order_time_validity`
2. **触发条件**：`(now − created_at) > limits.max_order_age_seconds`（**恰好相等 → PASS**）；`now` = 引擎评估时刻（UTC 整数秒）。
   - **未来时间防护**：`created_at − now > 300`（时钟偏差容忍，常量 300s）→ BLOCK（防未来时间注入）；偏差 ≤ 300s 按 `age = 0` 处理。
3. **输入字段**：`order.created_at`、`limits.max_order_age_seconds`。
4. **缺失输入行为**：缺 `max_order_age_seconds` → **非法配置（exit 4）**——本规则为**强制规则**，不可禁用、不可静默跳过，且不得出现在 `acknowledged_disabled` 中。值为 `0` / 负值 / 非有限 → 同样 exit 4（§0.4 阈值取值约定）。缺 created_at → R2（exit 4）。
5. **输出等级**：BLOCK。
6. **Evidence 清单**：`created_at`、`now`、`age_seconds`、`limit`、`future_skew_seconds`（如适用）。
7. **边界情况**：`age == limit` → PASS；created_at 为未来 → 见 2；created_at 无时区 → R2。
8. **豁免**：**不豁免**（0.2）——陈旧订单任何方向都不可执行。

## R11 — data_freshness（持仓快照陈旧检查）

1. **规则 ID**：`data_freshness`
2. **触发条件**：`(now − portfolio.snapshot_at) > limits.max_snapshot_age_seconds`（**恰好相等 → PASS**）。
   - `snapshot_at` 在未来 → BLOCK（数据可疑，fail-closed）。
   - 新鲜度按单一阈值实现；调用方可按日频或周频自行配置 `max_snapshot_age_seconds`。
3. **输入字段**：`portfolio.snapshot_at`、`limits.max_snapshot_age_seconds`。
4. **缺失输入行为**：缺 `max_snapshot_age_seconds` → **非法配置（exit 4）**——本规则为**强制规则**，不可禁用、不可静默跳过，且不得出现在 `acknowledged_disabled` 中。值为 `0` / 负值 / 非有限 → 同样 exit 4（§0.4 阈值取值约定）。缺 snapshot_at → R12 BLOCK。
5. **输出等级**：BLOCK。
6. **Evidence 清单**：`snapshot_at`、`now`、`age_seconds`、`limit`。
7. **边界情况**：`age == limit` → PASS；未来时间戳 → BLOCK；age 为负（未来）→ 见 2。
8. **豁免**：**不豁免**（0.2）——陈旧快照下任何方向都不可信。

## R12 — missing_data_fail_closed（关键数据缺失 fail-closed）

1. **规则 ID**：`missing_data_fail_closed`
2. **触发条件**：任一**已启用规则**所需的输入字段缺失、为 null 或类型非法 → BLOCK（exit 3），detail 标注缺失字段与受影响规则。
   - 例外：输入格式非法且属调用方错误范畴（R2 清单）→ exit 4，不归本规则。
   - 引擎内部异常（**规则计算抛错等**）→ exit 5，**同样按 BLOCK 处理**——绝不允许异常产生 PASS（fail-open 是最高危缺陷）；
   - policy 文件缺失/加载失败 → **exit 4**（配置问题归调用方，非引擎内部异常），同样不产生 PASS。
3. **输入字段**：全部规则输入。
4. **缺失输入行为**：本规则即缺失处理本体，恒启用、不可关闭。
5. **输出等级**：BLOCK（exit 3 或 5）。
6. **Evidence 清单**：`missing_fields[]`、`affected_rules[]`、`exception_type`（exit 5 时）。
7. **边界情况**：`positions == []`（空数组）→ 合法，不是缺失；`equity == null` → 缺失；字段存在但为字符串 `"123"` → 类型非法 → 缺失。
8. **豁免**：**不豁免**（0.2）。

---

## 1. 反向检查（手工验算）

**场景：卖出 10 张行权价 190 的 ACME 看跌期权（sell_to_open，put，strike=190，M=100，qty=10，权利金 2.50；ACME 为虚构标的）**

| 项 | 计算 | 结果 |
|---|---|---|
| R5 敞口 Δ（行权价口径） | `190 × 100 × 10` | **190,000 USD** ✅ |
| 若误用权利金口径 | `2.50 × 100 × 10` | 2,500 USD ❌（低估 **76 倍**） |
| R4 订单现金价值（权利金口径，正确） | `2.50 × 100 × 10` | 2,500 USD |
| R7b 保证金占用 | `max(190×100×10 − 2.50×100×10, 0)` | 187,500 USD |

**结论**：`max_symbol_exposure` / `max_total_exposure` 对裸卖期权必须用**行权价 × 乘数 × 数量**；用权利金口径会系统性低估 76 倍，属高危缺陷。✅ 已确认。

---

## 2. Schema 与规则交叉一致性

| Schema 字段 | 使用规则 |
|---|---|
| order.symbol / instrument_type / side / quantity / price / order_type / currency / created_at | R2（合法性）、R3–R7（金额/数量/敞口）、R10（时间） |
| order.option.underlying / expiry / strike / right / multiplier | R2（必填）、R4–R7（乘数/行权价口径）、R5 归组 |
| portfolio.equity | R5/R6/R7b 分母、R12 |
| portfolio.cash | R7a |
| portfolio.day_start_equity | R8 证据、报告 |
| portfolio.peak_equity | R9 证据与自洽校验 |
| portfolio.daily_pnl | R8 |
| portfolio.drawdown_ratio | R9 |
| portfolio.snapshot_at | R11 |
| portfolio.base_currency | R2（与 policy 比对） |
| portfolio.positions[].symbol / instrument_type / side / quantity / market_value / avg_cost / currency / option.* | R5/R6/R7b 持仓敞口计算（引擎自算） |
| policy.mode | 全局（shadow/enforce） |
| policy.base_currency | R2（币种比对） |
| policy.kill_switch | R1 |
| policy.version | 审计记录 |
| policy.limits.* | 各规则阈值（出现即启用） |
| result.* / audit-record.* / shadow-report.* | 全部规则的输出与聚合 |

**Schema 中每个字段都有用途或已注明用途；规则用到的每个字段 Schema 都有。** ✅

---

## 3. v0.2 候选规则（不在 v0.1 冻结 12 条内，仅登记）

| 候选 | 来源 | 说明 |
|---|---|---|
| 限频（max_orders_per_window / rate_window_seconds） | 后续候选 | 订单频率控制；需持久化计数（并发安全） |
| 黑名单（symbol/underlying/sector） | 后续候选 | 开仓 BLOCK、平仓豁免 |
| 财报黑窗 | 后续候选（对称窗口） | 需外部财报日历（v0.1 零外部依赖） |
| IV Rank 底线 | 后续候选 | v0.1 明确不做 Greeks/IV |
| 宏观事件窗 | 后续候选 | 需外部日历 |
| 行业/主题集中度 | 后续候选 | 需调用方提供行业映射 |
| 连亏暂停 | 后续候选 | 策略层自适应 |
| 同向冲突 | 后续候选 | 仓位管理策略 |

---

## 4. 能力边界（写入 README）

- v0.1 **不做**：Greeks、IV、组合保证金计算、多腿策略整体风险合并。多腿订单按**单腿逐一检查**。
- v0.1 **不做**：任何货币折算、任何联网取汇率。
- v0.1 **不做**：自动下单；只回答"这笔订单现在允许执行吗"。
- **单笔风险不设绝对上限**：`max_order_value` 是**现金流上限，不是风险上限**（对卖方期权 = 已收权利金，非行权风险）；单笔风险仅受账户比例约束（`max_symbol_exposure_ratio` / `max_total_exposure_ratio`）。绝对单笔风险上限（`max_order_exposure`）列入 v0.2 候选。
- **side 语义**：正股订单 `side` 为 `buy` / `sell`，期权订单为四值意图枚举；两者的真实开平仓方向都由引擎按快照持仓推断（见 §0.1），推断不确定按开仓处理（fail-closed）。
- **v0.1 卖空保证金简化**：股票卖空开仓的现金流出按 `0` 计（卖空所得与保证金要求相抵），会低估实际保证金占用。完整卖空保证金模型列入 v0.2 候选。
