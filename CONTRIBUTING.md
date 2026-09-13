# Contributing

Thanks for considering contributing to Deadlatch. This project follows a
second-auditor acceptance loop: changes are verified on disk by an independent
reviewer before any commit/tag/release. Keep the working tree reviewable.

## Local environment

- Python 3.10+ (develop on 3.11; CI tests 3.10/3.11/3.12).
- Create the virtualenv and install editable with test/build/docs extras:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Verification commands (must all pass)

```bash
.venv/bin/python -m pytest -p no:cacheprovider tests/ -q          # full suite
.venv/bin/python tools/validate_schemas.py                        # schema + meta-schema
.venv/bin/python tools/scan_sensitive.py                          # sensitive-data scan
COVERAGE_FILE=/tmp/deadlatch.coverage .venv/bin/python -m pytest -p no:cacheprovider \
  --cov-branch --cov=deadlatch --cov-report=term tests/ -q   # branch coverage ≥90%
git diff --check
```

- No `skip`, no `xfail`. Branch coverage must stay ≥ 90%.
- Packaging verification after changing runtime resources:

```bash
.venv/bin/python -m build
.venv/bin/python tools/verify_wheel.py dist/*.whl
```

## Rule discipline

- **Schema is the contract.** Rule behavior, exit codes (0/2/3/4/5), and
  decision synthesis follow `rules-spec.md` and the versioned schemas in
  `schemas/`. The packaged copies under `src/deadlatch/schemas/` must
  stay byte-identical to the repo copies (`tests/test_resources.py` enforces it).
- **Decimal, never float, for money.** All amounts and ratios go through
  `as_decimal` (S-7 single intake).
- **Fail-closed.** Missing/malformed data → BLOCK/3; input errors → 4;
  internal errors → 5. Never report an uncertain state as PASS.
- **No network, no broker.** Business code must not open sockets, register
  HTTP/SSE routes, or import broker/network stacks.
- **No secrets, no drift.** Error text must never echo caller-supplied values
  (use the shared safe constructors in `_validation.py`). Tests must never
  contain real accounts, positions, tokens, paths, or logs — use string
  concatenation for sensitive-looking probes so the scanner stays clean.

## Test change discipline (NEW-11)

- Every test **added**, **rewritten**, or **deleted** must be declared in
  `CHANGELOG.md` with the reason, exactly like a design change.
- Do not weaken or delete existing assertions to make a suite green; if a test
  expectation is wrong, justify the correction in the changelog.
- Do not use `try/except: return` or `assume()` to discard generated samples,
  and do not delete fuzz samples to force green.

## What never goes in a commit

Real account numbers, holdings, credentials, tokens, cookies, absolute local
paths, audit JSONL, coverage output, or local policy/portfolio/order files.
The sensitive-data scanner (`tools/scan_sensitive.py`) runs in CI and fails
the build on any un-exempted hit; do not broaden its allowlist to mask
documented private paths.
