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

- Two scopes are recorded separately and are not interchangeable.
- Development workspace: 539 tests (including experimental adapter
  example tests that are not shipped), no skip, no xfail; local branch
  coverage 92%.
- Sanitized public candidate: 472 tests covering the packaged library,
  CLI, MCP server, schemas, documentation, and shipped examples.
  Experimental adapter examples and their tests are not in the public
  candidate or the wheel.
- A 2026-08-30 public commit had green GitHub Actions; that run is not
  evidence for this candidate.
- GATE-1 (2026-09-13, development workspace only): adapter-example
  fixtures now pin the fake broker timestamp to the frozen evaluation
  clock minus 60 seconds, so wall-clock drift after 2026-08-30 no longer
  trips fail-closed `future_timestamp` on positive-path cases.
  Assertions were not weakened. This fixture is not part of the public
  candidate.
- GATE-2 (2026-09-13): `tests/test_docs.py` now requires
  `SECURITY.md` to name GitHub Security Advisories, require the
  “once that GitHub setting is enabled” / private-vulnerability-reporting
  caveat, forbid a public issue for vulnerabilities, and state that no
  mailbox or SLA exists. Removed the obsolete “no public reporting
  channel / no remote” assertions.
- GATE-3A (2026-09-13): README and this changelog no longer name
  development-only adapter paths, and they no longer treat the
  development-workspace count as the public-candidate count.

### Documentation

- GATE-2 (2026-09-13): `SECURITY.md` Reporting names GitHub Security
  Advisories after Private vulnerability reporting is enabled on the
  public repository. No mailbox and no SLA are claimed. Public issues
  are not the vulnerability channel.
- Grok review follow-up (2026-09-13): README / README.zh-CN state that
  the core package never connects or places orders. Experimental
  mapping examples, when present, live only in the development
  workspace; they are not live-verified and are not in the public
  candidate.

### Known limitations

- Advisory-only: the guard cannot force a bypassing agent to call it, and
  cannot stop an agent that ignores a BLOCK from submitting elsewhere.
- v0.1 is USD-only, single-leg, no Greeks/IV, one snapshot per check.
- Short-sell cash outflow is modeled as 0.
- Audit cross-process locking relies on POSIX `fcntl`; non-POSIX platforms
  degrade to a process-local lock.
- Experimental read-only mapping examples, when present, live only in
  the development workspace. They are not part of the core package, not
  imported by `deadlatch`, not shipped in the wheel, and not included in
  the sanitized public candidate. None has public live-account
  verification, a five-day shadow observation, or a real order shadowed
  or blocked by this software.
- 0 external users, 0 paid users.
