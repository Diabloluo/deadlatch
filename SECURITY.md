# Security Policy

## Supported versions

Deadlatch is pre-1.0 (current version: `0.1.0.dev0`). Only the latest
state of the repository is supported. No long-term-support commitment exists
until the project reaches a stable release.

## Local security model

- Everything runs locally; the package never stores, reads, or transmits
  account credentials.
- No network core path: library, CLI, and MCP server never open sockets and
  never register HTTP/SSE routes. The MCP SDK's HTTP stack is a transitive
  dependency that business code never imports.
- The package never places orders and contains no broker connectivity.
- Fail-closed semantics: missing/malformed data → BLOCK (`exit 3`), input or
  configuration errors → `exit 4`, internal/rule exceptions → `exit 5`.
  An uncertain state is never reported as PASS.
- Every `Guard.check()` appends a sanitized record to a local JSONL audit log
  with 30-day retention; audit-write failures degrade severity-only-up and
  never contradict the returned Result.
- The MCP server is stdio-only; the five tools are read-only (none can modify
  `policy`, `portfolio`, or kill-switch state — those paths are startup
  configuration, not tool arguments). Two kinds of intentional local file
  writes exist: the audit subsystem (`Guard.check` / `check_order` appends;
  the shadow-report entry point triggers 30-day retention pruning with an
  atomic rewrite when expired records exist; lock/tmp + `os.replace`
  transactions) and explicit `migrate --output` output.
- Advisory boundary: the guard cannot force an agent that never calls it to
  call it, and cannot stop an agent that ignores a BLOCK. This is by design.

## Vulnerability scope

We care about defects that break the fail-closed guarantees above, including:

- **False PASS:** an order that should be blocked is reported as allowed
  (any exit code 0/2 produced where 3/4/5 was required).
- **Kill-switch bypass:** a kill-switch state of `full` / `reduce_only`
  failing to block the orders it must block.
- **Sensitive leakage:** credentials, cookies, tokens, absolute paths, or
  account data appearing in error messages, Result/evidence, audit records,
  CLI/MCP output, or artifacts.
- **Audit corruption:** malformed or schema-invalid audit lines being skipped,
  partial writes, retention violations, or disk/Result contradictions.
- **Unauthorized mutation of state:** any code path that can modify, truncate,
  or forge `policy`, `portfolio`, kill-switch state, or audit records outside
  the designed semantics. By-design audit appends (`Guard.check` /
  `check_order`) and the 30-day retention pruning are explicitly **not**
  vulnerabilities; a defect is when those writes corrupt, truncate, or forge
  records, or when any other path can mutate these files.
- **Network listening:** any transport other than stdio, or any outbound
  network call from business code.

## Reporting

Report vulnerabilities through this repository's **GitHub Security Advisories**
(private vulnerability reporting), once that GitHub setting is enabled on the
public repository. Do **not** open a public issue for a security defect. Do
**not** send credentials, account data, cookies, tokens, or private paths in
any report.

There is no dedicated security mailbox and no response or fix SLA. Only the
latest repository state is supported while the project remains pre-1.0.

Please include: affected version/commit, a minimal reproduction that contains
no sensitive data, expected vs actual behavior, and whether it affects the
fail-closed guarantees above.
