# Changelog — Deadlatch

Deadlatch is a local-first, broker-agnostic, pre-trade risk gate for AI trading
agents. Advisory-only: whether an agent calls the guard and honors the result
is the integrator's decision.

> Product changelog. Detailed internal development history is preserved in the
> local git history of the source workspace, not in this file.

## v0.1.0.dev0 (unreleased)

Local checkpoint of the v0.1 development line.

### Features

- G1/F2 live safety controls: the stdio MCP server now reloads a changed
  `policy` on the next tool call. Invalid or concurrently changing policy
  fails closed instead of continuing with the cached copy. The optional
  startup-only `--kill-switch-path` points to a tiny local file containing
  `off`, `reduce_only`, or `full`; it is read on every tool call, can only
  tighten the policy's own switch, and has no MCP write/disarm method.
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

- G1/F1 security fix (2026-09-13): option `buy_to_close` / `sell_to_close`
  text is no longer trusted as proof of a closing order. The direction engine
  now requires a fresh snapshot containing the same full option contract,
  opposite-side position, and sufficient quantity. Rewrote the former
  empty-portfolio "close" expectations, added explicit no-position,
  opposite-position, over-quantity, contract-mismatch, and positive-close
  regressions, ambiguous-data branch coverage, and the $1.8m ghost-close R1
  regression. Unproven close labels are also regression-tested against the
  opening-side exposure, cash, and short-option margin calculations so they
  cannot retain negative/zero close-side risk math. Existing
  reduce-only positive tests now provide the matching option position they
  claim to close.
- G1/F2 tests mutate policy in a live server, prove invalid reloads block with
  exit 4 and recover after repair, and change the independent switch while
  preserving both file size and mtime to prove its contents are not cached.
- G1/F10 removes the obsolete pre-publication prohibition from the live GitHub
  Actions workflow and replaces the retired project prefix in source, test
  fixtures, temporary paths, coverage filenames, and Hypothesis profile names.
  Assertions and test behavior are unchanged; the candidate builder continues
  to reject both current internal ticket prefixes and constructed legacy input.
- Two scopes are recorded separately and are not interchangeable.
- Development workspace: 555 tests (including experimental adapter
  example tests that are not shipped), no skip, no xfail; local branch
  coverage 92%.
- Sanitized public candidate: 488 tests covering the packaged library,
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
- GATE-4 (2026-09-13): README and the public Issue Form add a
  pre-release link, fictional Quick Start, and a 20-minute
  integration-assessment CTA. Existing test functions were extended
  (counts remain 539 / 472). The sanitized candidate now includes
  `.github/ISSUE_TEMPLATE/` (121 manifest files).
- G1 (2026-09-13): F1/F2/F10 bring the current development workspace to
  555 tests and the deterministic 121-file public candidate to 488 tests.
  Both counts were executed separately; the development run retained 92%
  branch coverage.

### Documentation

- G1 independent-review condition (2026-09-13): README and MCP integration
  instructions require each option `symbol` to be the broker's unique full
  contract code, not a reused underlying ticker; the MCP example now documents
  live policy reload and `--kill-switch-path` instead of requiring a restart.
- GATE-2 (2026-09-13): `SECURITY.md` Reporting names GitHub Security
  Advisories after Private vulnerability reporting is enabled on the
  public repository. No mailbox and no SLA are claimed. Public issues
  are not the vulnerability channel.
- Grok review follow-up (2026-09-13): README / README.zh-CN state that
  the core package never connects or places orders. Experimental
  mapping examples, when present, live only in the development
  workspace; they are not live-verified and are not in the public
  candidate.
- GATE-4 (2026-09-13): public intake is a GitHub Issue Form only.
  No mailbox. Security defects stay on GitHub Security Advisories.
  No paid offer, design-partnership, or live-account onboarding copy.

### Known limitations

- Advisory-only: the guard cannot force a bypassing agent to call it, and
  cannot stop an agent that ignores a BLOCK from submitting elsewhere.
- v0.1 is USD-only, single-leg, no Greeks/IV, one snapshot per check.
- Naked short-call upside risk is unlimited; the v0.1 strike-based exposure
  approximation is not a conservative bound for short calls.
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
