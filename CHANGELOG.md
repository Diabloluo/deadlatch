# Changelog — Deadlatch

Deadlatch is a local-first, broker-agnostic, pre-trade risk gate for AI trading
agents. Advisory-only: whether an agent calls the guard and honors the result
is the integrator's decision.

> Product changelog. Detailed internal development history is preserved in the
> local git history of the source workspace, not in this file.

## v0.1.0.dev0 (unreleased)

Local checkpoint of the v0.1 development line.

### Features

- Deterministic 12-rule evaluation engine (R1–R12) with fixed rule order:
  kill switch, input validity, order quantity/value limits, symbol and total
  exposure, cash margin, daily loss, drawdown, order time validity, data
  freshness, and fail-closed missing-data handling.
- Decision semantics: PASS / 0, WARN / 2, BLOCK / 3, input error / 4,
  internal error / 5. Shadow mode records the internal verdict while
  projecting PASS / 0 externally (kill-switch hits and exit 4/5 are never
  projected away).
- Versioned JSON Schema 2020-12 contracts (order, portfolio, policy, result,
  audit-record, shadow-report) shipped inside the package; explicit offline
  migration for legacy documents; old versions rejected at evaluation entry.
- Local JSONL audit log with 30-day retention, sanitized records, and
  severity-only-up degradation on write failure.
- stdio-only, read-only MCP server with five tools: `check_order`,
  `get_account_status`, `get_policy`, `kill_switch_status`,
  `recent_decisions`.
- CLI: `check`, `shadow report`, `migrate`; deterministic output; `--json`
  output validates against the result/report schemas.
- Packaging: wheel + sdist with package-data schemas; fresh-venv verification
  script (`tools/verify_wheel.py`); artifact inspection (`tools/inspect_dist.py`).
- Bilingual README (English / 简体中文), MIT license, security, contributing
  and disclaimer documentation; CI workflow (macOS/Linux full matrix,
  Windows core, independent build job) validated locally.
- Demo GIF generated from real local MCP stdio output with fictional data.

### Test suite

- 532 tests in the development workspace (including 54 read-only adapter
  example tests), no skip, no xfail; branch coverage 92%.
- Schema validation, sensitive-data scan, packaging inspection and fresh-venv
  wheel verification all green locally. GitHub Actions has not been run
  (no remote repository exists yet).

### Known limitations

- Advisory-only: the guard cannot force a bypassing agent to call it, and
  cannot stop an agent that ignores a BLOCK from submitting elsewhere.
- v0.1 is USD-only, single-leg, no Greeks/IV, one snapshot per check.
- Short-sell cash outflow is modeled as 0.
- Audit cross-process locking relies on POSIX `fcntl`; non-POSIX platforms
  degrade to a process-local lock.
- Experimental read-only adapter work is **not included** in this public
  candidate (broker adapter examples live in the development workspace only).
  Tiger schema closure is not complete; no five-day shadow observation has
  run; Longbridge/IBKR adapters are offline examples only; the pre-release
  adapter validation remains blocked on two external data gaps.
- 0 external users, 0 paid users; no real order was ever shadowed or blocked
  by this software.
