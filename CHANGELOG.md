# Changelog — Deadlatch

Deadlatch is a local-first, broker-agnostic, pre-trade risk gate for AI trading
agents. Advisory-only: whether an agent calls the guard and honors the result
is the integrator's decision.

> Product changelog. Detailed internal development history is preserved in the
> local git history of the source workspace, not in this file.

## v0.1.2 (unreleased)

Unreleased candidate in the development workspace. Published install remains
`deadlatch==0.1.1`. This is G3B work (UTC day shards, O(1) append, local hash
chain, prune-by-segment, fictional F1 post-mortem). It is not a PyPI /
Registry / GitHub Release event and is not evidence of users, paid adoption,
live-account use, or a real would-block observation.

### Audit collection

- Logical `--audit-path` / `DEADLATCH_AUDIT_PATH` / `~/.deadlatch/audit.jsonl`
  now names a collection. New records append to `audit-YYYY-MM-DD.jsonl` UTC
  shards. Pre-v0.1.2 files remain legacy v1, read-only compatible, and are
  never auto-deleted or rewritten on append.
- Append is a bounded reverse-tail read of today's shard (or a fixed 30-day
  candidate set plus optional legacy tail) then one JSONL line with
  flush/fsync. It does not `readlines()`, parse all history, or rewrite the
  shard. Per-shard and per-record size caps fail closed. Ordinary append is
  not crash-atomic across files. POSIX flock remains cross-process; Windows
  remains process-local. v0.1.2 durable audit writes (`init` / `append` /
  `repair` / `prune` / shadow report) are supported on Linux and macOS only.
  Other platforms refuse those entries with `audit_platform_unsupported`
  before creating parent directories, locks, or state, including when a
  collection already exists or looks like a no-op. POSIX `fsync(dirfd)` is
  required on supported platforms; file and directory fsync failures stay
  hard failures. Windows is not a complete trusted audit-chain platform.
  Offline `verify` / `read` of a stopped snapshot remain available. Guard
  still computes rules on unsupported platforms: PASS/0 becomes WARN/2 with
  `audit_write_failed` / `error_code=audit_platform_unsupported`; BLOCK/3,
  4, and 5 keep their severity.
- AuditRecord v2 adds `prev_hash` / `record_hash` (canonical SHA-256). The
  chain is tamper-evident, not a signature and not tamper-proof. Without an
  external anchor, deleting the last record or the whole visible set is not
  reliably detectable. Callers cannot self-report hashes.
- `deadlatch audit prune` deletes whole shards older than today UTC plus the
  previous 29 UTC days. Shadow report still triggers the same retention.
  Future shards are kept and counted. Prune does not glob-delete
  `.state.tmp` files; a publisher only unlinks the exact temp path it
  created. Crash leftovers are left in place.
- Mutating v2 repair fsyncs the existing write-state file and parent
  directory before quarantine or log replace. Barrier failure is exit 5
  with schema-valid `--json`, original log/state bytes unchanged, and
  zero new quarantine. Clean/no-op and chain-integrity refusal do not
  take that barrier. Legacy-only G3A repair without state is unchanged.
- Collection `verify` reports `legacy_records`, `chained_records`, and
  chain heads. Verify/read/report use the same in-window shard set as
  prune/append (today UTC plus the previous 29 UTC days, plus future
  shards that prune keeps). Unpruned expired shards must not make an
  in-window chain look invalid. v2 repair only isolates a truncated last
  line of the last shard; hash / duplicate-ID / version-downgrade /
  mid-chain damage refuses automatic relink (exit 3, zero writes).
  v1 records are legal only in the legacy baseline file and only before
  any v2 record; a later UTC day shard or new write must not start at a
  lower schema/chain version. Incoming business v1 still upgrades to v2
  on append. Trusted reads (`read_audit_records`, shadow report, MCP
  `recent_decisions`) verify hashes, chain links, duplicate IDs, and
  version location in the same lock before returning records.
- Append decides the UTC shard date after the collection lock is held
  (default clock is sampled under the lock). Chain-head probe and shard
  selection use that same instant. `evaluated_at` is not rewritten. A
  durable write-date watermark refuses any earlier UTC day after a later
  day has been reserved (including +30/+365 gaps). New collections need
  one explicit `deadlatch audit init` / `initialize_audit_state` before
  append; missing state is not rebuilt on the hot path. See
  `docs/audit-write-state.md`. The watermark is sequential control, not a
  signature, and mixed old/new writers are unsupported.
- Append refuses extra trailing blank lines (LF, CRLF, or consecutive)
  and leaves the original file bytes unchanged. A single terminating
  newline remains legal and the new record must chain from that last
  complete record. The tail window is `MAX_RECORD_BYTES` plus one
  preceding delimiter byte so a max-size last line is not mistaken for a
  truncated record.

### Docs and tests

- Documented v0.1.2 write-audit support as Linux/macOS only. Windows CI keeps
  the original six core files plus `tests/test_audit_platform.py`.
- Added `docs/postmortem-option-direction.md` (fictional option order only).
- Development-workspace tests and public-candidate tests gain shard, chain,
  O(1), prune, and packaging coverage for `0.1.2`.
- Added regressions for unpruned `today-31` verify-without-prune, extra
  trailing blank-line refuse (LF/CRLF/consecutive) with unchanged bytes,
  single-terminator append still chaining, and below/equal/over
  `MAX_RECORD_BYTES` tail reads.
- Added negative regressions: v1 in a day shard (or v1 after v2) is
  `schema_version_downgrade` (verify/repair/read/append refuse, no silent
  write); `read_audit_records` / shadow report / MCP `recent_decisions`
  propagate chain errors instead of returning tampered decisions.
- Added a concurrent pre-midnight delay regression (default clock, no
  caller `now`): delayed lock-boundary writer after the next UTC day has
  been written still verifies clean and trusted-read returns every unique
  record. Four-process cross-day append again asserts chain integrity, not
  only raw line counts. Explicit earlier `now` after a later shard exists
  is refused with unchanged bytes.
- Prune no longer asserts cross-collection `.state.tmp` deletion. New
  regressions cover A prune during B's in-flight state publish, prune
  keeping unattributed temps and other collections' files, v2 repair
  after a failed adopt directory fsync (barrier fail keeps bytes / zero
  quarantine; recovered sync can repair/verify/append), and CLI exit 5
  JSON for a refused state-sync barrier. The extra prune listdir-error
  test for glob tmp cleanup was removed with that cleanup.

## v0.1.1 (2026-09-14)

Published to PyPI as `deadlatch==0.1.1` and to the MCP Registry as
`io.github.Diabloluo/deadlatch` version `0.1.1` on 2026-09-14. The stable
GitHub Release is cut from this exact public source version. This is G3A work
(audit verify/repair, root/`lsof` test guards, public rules spec), not complete
G3: daily rotation, O(1) append, and hash chains remain for G3B / `v0.1.2`.
Publication is not evidence of users, paid adoption, live-account use, or a
real would-block observation.

### Audit maintenance

- Added `deadlatch audit verify` and `deadlatch audit repair --quarantine`.
  Verify does not change audit content or write quarantine. For an existing
  regular log it may create a `.lock` sidecar so it shares the append lock;
  a missing target returns 4 and creates neither parent directories nor a
  lock file. Repair isolates damaged lines into a unique `0600` JSON
  quarantine file (hidden tmp + fsync + exclusive publish), rewrites valid
  lines in original byte order, inserts a JSONL newline before the
  maintenance marker when the last kept record has no terminator, and
  appends one schema-valid maintenance marker
  (`policy_version=audit-maintenance/1`, `rule_id=audit_repaired`).
  Size, read, and lock failures map to exit 5 with schema-valid `--json`.
- Shadow reports exclude only the complete maintenance combination from
  order totals and count those events in `notes`.
- New `audit-maintenance-result` schema ships in the package.

### Tests and environment

- Added capability guards `requires_nonroot` and `requires_lsof`. Ordinary
  CI still runs those tests on capable runners. Linux root container job
  added; other fail-closed tests are never skipped.
- Test changes: new `tests/test_audit_maintenance.py` for verify/repair
  path errors, four damage classes, issue truncation, quarantine
  round-trip, replace failure, concurrent lock sharing, shadow-report
  exclusion, and leak checks. Existing F8 tests gained skip guards only.
  Documentation, packaging, schema, candidate, and wheel-verify tests
  were extended for `0.1.1` and `docs/rules-spec.md`. Counts become
  577 development-workspace tests and 509 public-candidate tests.
  The sanitized candidate lists 128 hashed files.

### Public rules spec

- Published `docs/rules-spec.md` as the public 12-rule contract. The root
  `rules-spec.md` is a pointer only. README and CONTRIBUTING link the
  public file.

## v0.1.0 (2026-09-14)

First stable version line. Published to PyPI as `deadlatch==0.1.0` and to
the MCP Registry as `io.github.Diabloluo/deadlatch` version `0.1.0` on
2026-09-14. A stable GitHub tag and Release `v0.1.0` exist and point at
`d43196de23ad2dd4a1ee4ab720c1610cd22add83`. This publication does not
claim users, paid adoption, live-account results, or would-block evidence.

### Packaging and install path

- Version is `0.1.0`. setuptools metadata now uses an SPDX `MIT` license
  expression and `license-files`; the MIT license text itself is unchanged.
- GitHub Actions `release.yml` publishes only via Trusted Publisher
  (`id-token: write` on the publish job, environment `pypi`, no API token).
  The workflow is `workflow_dispatch` only and requires test, Windows core,
  schema, sensitive scan, build, inspect, and fresh-venv verification first.
- Root hash inventory renamed to `MANIFEST.sha256.json`. The candidate
  generator recomputes it and can copy it back with `--sync-source`.
  `tools/verify_hash_manifest.py` fails on any stale entry.
- `server.json` describes `io.github.Diabloluo/deadlatch` as a PyPI stdio
  server launched with `uvx --from deadlatch==0.1.0 deadlatch-mcp`.
  `--policy` and `--portfolio` are required file arguments. The same
  name and version are now the published MCP Registry entry.
- README documents the verified one-line install
  `pip install deadlatch==0.1.0`, the MCP-first start, the PyPI project
  page, and the exact `mcp-name: io.github.Diabloluo/deadlatch`
  ownership marker. `v0.1.0.dev1` remains a historical pre-release
  link, not the current install path.
- CI official actions moved off Node 20 runtimes (`actions/checkout@v5`,
  `actions/setup-python@v6`). Coverage remains macOS/Linux full,
  Windows core, and independent build/inspect/fresh-venv.
- G2 publish hardening (2026-09-13): `release.yml` pins
  `pypa/gh-action-pypi-publish` to commit
  `dc37677b2e1c63e2034f94d8a5b11f265b73ba33` (official `release/v1`
  readback). The publish job prints each final `dist/*` name, size, and
  SHA-256 after inspect/fresh-venv and before OIDC upload. Ordinary CI
  or local build hashes are reference only; PyPI readback must match
  that publish-set output.

### Test suite

- Added one negative test that a stale `MANIFEST.sha256.json` entry fails
  candidate verification. Existing documentation and workflow tests were
  extended. Counts become 556 development-workspace tests and 488 public
  candidate tests. The sanitized candidate lists 124 hashed files.
- G2 honesty follow-up (2026-09-14): README and this changelog now record
  the verified PyPI and MCP Registry publication. Existing documentation
  assertions were updated; test counts remain 556 / 488.

## v0.1.0.dev1 (2026-09-13)

Security-focused pre-release. It supersedes `v0.1.0.dev0`; users should not
continue installing the older artifact because it predates the F1/F2 fixes.

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

## v0.1.0.dev0 (2026-09-13)

Initial public pre-release of the local, broker-agnostic risk gate, including
the 12-rule engine, CLI, stdio MCP server, JSON Schema contracts, audit log,
fictional quick starts, and governance documents. Superseded by
`v0.1.0.dev1` after independent review identified the F1 option-direction and
F2 live-policy issues.
