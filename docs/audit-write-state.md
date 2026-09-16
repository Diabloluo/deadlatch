# Audit write-date watermark

Unreleased `0.1.2` adds a **fixed-size write-date watermark** for each local
audit collection. It is a sequential-control file, not a signature, not a
chain-head cache, and not proof against an attacker who can rewrite the
directory.

Logical base `BASE` is unchanged (`--audit-path` / `DEADLATCH_AUDIT_PATH` /
`~/.deadlatch/audit.jsonl`). The watermark lives at `BASE.name + ".state.json"`
(for example `audit.jsonl.state.json`). The collection still uses one lock at
`BASE.name + ".lock"`.

`reserved_through` (H) is the highest UTC calendar day that has been durably
reserved for a write. It is not “last successful append time”. H never
decreases: not on write failure, restart, prune, repair, or a second init.

## One-time init (not on the append hot path)

New collections must be initialized once before any append:

```bash
deadlatch audit init --audit-path BASE
```

```python
from deadlatch.audit import initialize_audit_state
initialize_audit_state(base)
```

Init holds the collection lock and may enumerate the directory (O(files)).
That scan is maintenance, not part of the append performance promise.

- Brand-new collection (no legacy file, no matching shard names): default init
  publishes `reserved_through=null`.
- If a legacy file or any strictly matching shard name exists (including an
  empty shard), pass `--adopt-existing` / `adopt_existing=True`. Stop old
  writers first and keep a directory backup. This round only tests fictional
  temporary collections.
- Adopted H is the maximum UTC date in matching shard **names**, including
  empty, expired, and far-future shards. A legacy-only collection gets
  `H=null`. Physical write day is never inferred from `evaluated_at`.
- A matching name that is a symlink, directory, or other non-regular file is
  refused. Unrelated / quarantine / temp names are ignored and never deleted.
- Init does not parse or rewrite business records and does not mean verify
  clean. A structurally damaged collection may still receive a conservative H;
  existing verify/repair handle the logs afterwards.
- A valid existing state is checked read-only: if H already covers the max
  shard date, init returns `unchanged` and does not rewrite state bytes. If
  state lags the shards, the Python API raises `AuditError` with
  `.code=audit_state_inconsistent` and the CLI prints that error (JSON
  `status=error`); it does not return a success dict or raise H.
- Damaged or unknown-version state cannot be overwritten. There is no
  `--force`, reset, or lower. The Python API raises the matching `AuditError`
  (keep `.code`); the CLI prints the error and a schema-valid JSON error
  document. Callers should not expect every Python failure to come back as a
  status dictionary.
- Missing state always refuses append. `--adopt-existing` is a first-time
  operator acknowledgement, not recovery after state loss: a reserved-but-
  never-written day cannot be reconstructed from the logs. The program cannot
  tell first-time upgrade apart from a deleted state file.

Guard.check, MCP tools, append, and process start never auto-scan or migrate.
MCP still exposes the original five tools; init is Python/CLI only.

## Append protocol (O(1))

Under the same collection lock:

1. Decide `write_now` (sample the default clock after the lock, or normalize
   an explicit `now` to UTC).
2. Bound-read and validate the state file (max 4,097 bytes). Missing /
   damaged / unknown version refuse. Append does not enumerate the directory
   and does not rebuild state.
3. If write date D < H, raise `audit_date_regression`. Log and state bytes
   stay unchanged.
4. Validate the new record, bounded tail-read, hashes, and size **before**
   raising H. Invalid input must not advance the watermark.
5. If H is null or D > H, atomically publish H=D (hidden exclusive tmp,
   fsync, replace, parent-directory fsync). Any failure here does not write
   the business log.
6. If H=D, the JSON is not rewritten; the existing state file and parent
   directory are fsynced before the log write. Unsupported fsync is a hard
   failure.
7. Then append the record with flush/fsync. A newly created shard also fsyncs
   the parent directory before success is reported. Chain heads still come
   from the on-disk tail, never from the watermark.

A successful date reservation followed by a failed append may leave “H
advanced, no record for that day”. That is conservative, not a successful
record, and H is not rolled back. Later calls with D >= H may continue;
earlier dates are refused.

## Read / verify / repair / prune

- Collections without state remain readable. `verify` clean means the logs
  verified, not “safe to append”. Legacy / uninitialized collections are
  read-only for writers.
- With state, the existing full-directory verify/read paths also check that H
  is not below any matching shard date (including far-future shards). Bad or
  lagging state fail-closed. Future visibility is not narrowed.
- State-only (initialized empty): verify returns clean, read returns `[]`.
  A missing path still uses the original not-found / exit 4 behaviour.
- Prune never deletes or lowers state. Deleting every expired shard keeps H.
  Uninitialized collections may still be pruned explicitly. With state, prune
  checks consistency first. Prune does not glob-delete `.state.tmp` files;
  only the publisher that created a temp path may unlink that exact path in
  its own `finally`. Crash leftovers stay until their writer finishes.
- Repair never lowers H. v2 tail repair of the last shard requires that
  shard’s date already be covered by durable H. Before any quarantine or log
  replace, a mutating v2 repair fsyncs the existing state file and parent
  directory; barrier failure is exit 5, original bytes unchanged, zero new
  quarantine. Clean/no-op and chain-integrity refusal do not take that
  barrier. Legacy-only G3A repair without state stays compatible and does not
  silently create state. A damaged v2 collection without state can be adopted
  first, then repaired.
- H greater than the newest remaining shard (failed reserve or prune) is
  legal, not corruption.

## Honest limits

The guarantee applies to protocol-compliant local writers on one collection.
Old binaries, manual file copy/rollback, and a writer with full directory
permissions are not constrained. Mixed old/new writers are unsupported: stop
and upgrade every writer before relying on the watermark.

The watermark cannot discover arbitrary directory tampering and does not
replace the hash chain. Without an external immutable anchor, deleting the
current last record or the whole visible set is still not reliably detectable
from collection data alone.
