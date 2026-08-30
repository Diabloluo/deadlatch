# Deadlatch — MCP Server 本地接入（）

本地 stdio MCP Server（只读、fail-closed、advisory-only）。五个工具：
`check_order` / `get_account_status` / `get_policy` / `kill_switch_status` /
`recent_decisions`。

## 启动

```bash
deadlatch-mcp --policy /path/to/policy.yaml \
  --portfolio /path/to/portfolio.json \
  [--audit-path /path/to/audit.jsonl]
```

- `policy` / `portfolio` / `audit-path` 是**服务器进程启动配置**，不能作为任何工具入参；
- 传输仅 stdio（stdout 只承载 MCP 协议帧）；服务器不监听任何端口、不发起网络请求。

## Claude Desktop

把 `claude_desktop_config.json` 合并进 Claude Desktop 的 MCP 配置（替换其中的
占位路径为本地虚构/真实路径）。

## Cursor

把 `cursor-mcp.json` 的内容加入 Cursor 的 MCP 配置（Settings → MCP → Add，
command 模式选择 `deadlatch-mcp`，参数同上）。

## 安全模型与诚实边界

- 服务器**不持有券商凭据、不下单、无网络**；五个工具全部只读/纯检查，零副作用；
- `portfolio` 只从服务器配置的本地路径读取——Agent 无法上传、替换或指定快照；
- 不存在任何修改 policy / limits / mode / kill switch / 审计记录或路径的工具；
  规则与 kill switch 变更只能编辑本地 `policy.yaml` 并重启服务器进程；
- Agent **必须先调用 `check_order` 并遵守 BLOCK**（BLOCK = 不得下单、不得同参
  重试、调用失败按 BLOCK 处理）。
- 诚实边界：Guard 无法阻止一个完全绕过它的调用方（例如从不调用本服务器、或
  无视 BLOCK 直接通过其它通道下单的 Agent）。是否调用、是否遵守取决于集成方。

## 示例配置说明

示例中的 `/path/to/your/...` 均为虚构占位路径，不含任何真实账户信息。
