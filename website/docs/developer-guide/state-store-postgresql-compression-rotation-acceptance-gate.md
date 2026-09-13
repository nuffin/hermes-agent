# PostgreSQL compression-rotation acceptance gate

## Status and scope

**Selected PostgreSQL rotation is unsupported.** This gate does not enable it,
does not route a selected PostgreSQL runtime through SQLite, and does not add a
production rotation implementation. The only supported PostgreSQL compression
slice is non-destructive observation, cooldown/counter state, and fenced
coordination.

`tests/integration/test_postgresql_compression_rotation_acceptance.py` is the
future adapter gate. Its currently absent-adapter cases are strict runtime
`xfail`s, not passing placeholders: if an adapter exposes
`publish_compression_child` without satisfying the full contract, the test
becomes a failure. The selected-runtime refusal assertion is intentionally
GREEN now and lives in
`tests/integration/test_postgresql_cli_session_store.py`.

The SQLite oracle is a real `AIAgent` plus a deterministic fake compressor and
a real temporary `SessionDB`; it is not a mocked publisher. It establishes the
observable contract before PostgreSQL can advertise
`atomic-compression-rotation-v1`.

## Adapter capability report

Before the capability may be advertised, a machine-readable report must state:

- `capability`: exactly `atomic-compression-rotation-v1`;
- PostgreSQL server version, driver version, adapter commit, schema migration,
  test database name, and generated tenant schema(s) (never a DSN);
- command, worker count, serial/parallel result counts, and test duration;
- SQLite oracle fixture IDs and their exact parent/child snapshots;
- actor identities, parent ID, child ID, owner fence/lease generation,
  transaction ID, backend PID, and publication phase for every race/fault run;
- per-phase result for `lease-revalidated`, `child-row-inserted`,
  `handoff-inserted`, `parent-close-issued`, and `commit-returned`;
- injected exception, `pg_terminate_backend` result, SIGKILL exit status, and
  independent reopen audit result for every fault cell;
- parent/child/message/handoff counts before and after, publication receipt,
  retry result, recovery tip, and namespace-isolation audit; and
- explicit declaration that no `state.db` opener event or SQLite fallback
  occurred under selected PostgreSQL.

A missing field, shared schema, wall-clock sleep race, mock publisher, or an
unexplained skip is a gate failure. Infrastructure unavailability may skip only
with its precise prerequisite reason; it is never an `xfail`.

## Oracle invariants

The future implementation must match these externally observable SQLite facts:

1. A successful handoff atomically closes only the parent (`end_reason` is
   `compression`) and creates one live child whose `parent_session_id` is the
   parent. There is never a closed parent without one complete child.
2. The child receives the compacted handoff exactly once and starts its full
   transcript flush at offset zero. Existing parent rows above the requested
   watermark are copied through the watermark ceiling exactly once.
3. Parent metadata follows the child: source, model, model configuration,
   system prompt, CWD, repository root/branch, profile/routing/origin fields,
   title/title source, and compression lineage. The successful rotation resets
   stale activity/cooldown and anti-thrash counters for the new generation.
4. A failed pre-commit publication leaves the original parent live, creates no
   visible child/handoff, leaves live messages unstamped, and does not mutate
   the caller's original message list.
5. Reopen finds the compression-chain tip. Repeating a completed publication is
   idempotent: no second child, duplicated message, parent close, or lineage
   edge is published.
6. A stale owner fence or expired/changed lease is rejected before publication;
   a loser may retry only after it independently obtains the next valid lease.
   No stale acknowledgement may settle a later owner generation.

## Phase gates

### Phase 7 — deterministic semantic oracle

Run the SQLite oracle with the real compression path and fake deterministic
provider. Cover success, exact metadata inheritance, activity/cooldown/counter
and watermark behavior, title/model/prompt/lineage preservation, deduplicated
handoff, failed publication rollback, cold reopen tip, and idempotent retry.
The selected-PG refusal must also prove zero `state.db` opens and zero parent,
child, or message mutation.

### Phase 12 — fault and concurrency proof

Against PostgreSQL 18+ only, create a UUID-derived schema for every test and
instantiate every actor with an independent real adapter/pool. Run serial and
parallel modes. Block real transaction boundaries (not a fake return value) at
all five publication phases. At every pre-commit phase inject a server error,
`pg_terminate_backend`, and a spawned-process SIGKILL; then audit with a fresh
adapter. Two spawned owners must contend on one parent: exactly one may commit;
the loser is explicit, stale-fence retry is rejected, and a newly acquired
lease may perform the only valid retry. The audit requires all-or-nothing
parent/child/handoff state, no post-commit acknowledgement before commit
returns, no orphan, and an intact compression-tip resume.

### Phase 13 — selected-runtime capability boundary

The runtime may advertise `atomic-compression-rotation-v1` only after Phase 7
and Phase 12 reports pass for both serial and parallel runs. Until then every
rotation entry point, including CLI `archive_and_compact`, fails before mutation
with the dedicated PostgreSQL capability error; `trap_state_db_opens` must be
empty. No successful SQLite fallback is acceptable.

## Required commands

```bash
# SQLite oracle plus fail-closed selected-PG contract
scripts/run_tests.sh \
  tests/agent/test_postgresql_compression_rotation_oracle.py \
  tests/agent/test_compression_rotation_state.py \
  tests/agent/test_session_rotation_flush_cold_resume_68454.py \
  tests/run_agent/test_compression_persistence.py \
  tests/test_state_store_runtime_readiness.py \
  tests/test_state_store_config.py -v --tb=short

# New PG18 adapter gate is explicitly marked integration.
HERMES_TEST_WORKERS=1 scripts/run_tests.sh \
  tests/integration/test_postgresql_compression_rotation_acceptance.py \
  -m integration -v --tb=short

# Established PG18 fault/runtime/operations/import regressions have no marker
# and must run separately (otherwise pytest deselects them).
HERMES_TEST_WORKERS=1 scripts/run_tests.sh \
  tests/integration/test_postgresql_phase12_fault_harness.py \
  tests/integration/test_postgresql_compression_coordination.py \
  tests/integration/test_postgresql_state_store_operations.py \
  tests/integration/test_postgresql_state_store_sqlite_import.py \
  tests/integration/test_postgresql_cli_session_store.py \
  -v --tb=short
```

Parallel validation must repeat the second command with the repository's
parallel-worker setting, while retaining per-test UUID schemas. A pass is valid
only if the report records both modes and no test shares a fixture schema.
