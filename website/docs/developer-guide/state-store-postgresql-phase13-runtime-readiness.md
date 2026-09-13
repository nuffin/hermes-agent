# Phase 13 — PostgreSQL runtime-activation readiness gate

## Scope

Phase 13 makes an explicit `state_store.backend: postgresql` selection measurable and fail-closed. It does **not** enable PostgreSQL as the normal Hermes runtime, alter the default SQLite configuration, create a SQLite fallback, change a service, or cut over data.

`state_store_runtime_readiness.py` owns three testable boundaries:

1. `static_raw_state_db_inventory()` parses the approved runtime modules and publishes each direct `SessionDB`, shared-registry `acquire`, `sqlite3.connect`, and shared `open_db` opener with its caller symbol and porting classification.
2. `trap_state_db_opens()` intercepts root/profile `state.db` access through both Python `open()` and `sqlite3.connect`, recording the exact caller path and symbol. It is test instrumentation only.
3. `SessionDB.__init__` calls `require_legacy_state_db_runtime()` **before** test isolation, directory creation, connection, pragma, or schema work. When PostgreSQL is selected, it refuses the legacy SessionDB path with a capability report instead of silently opening or creating `state.db`.

The static inventory is checked into `website/docs/developer-guide/state-store-postgresql-runtime-callsite-report.json`. Regenerate it from the feature worktree with:

```bash
python -c 'from pathlib import Path; from state_store_runtime_readiness import write_runtime_callsite_report; write_runtime_callsite_report(Path("website/docs/developer-guide/state-store-postgresql-runtime-callsite-report.json"))'
```

## Selected-backend capability report

A selected PostgreSQL profile reports its canonical profile home/name and derived tenant schema without retaining a DSN or opening a database. The supported slice is:

- narrow StateStore records; and
- profile-derived PostgreSQL tenant schema selection; and
- CLI fresh/resume acquisition through `cli_session_store.open_cli_session_store()`; and
- bounded `AIAgent` lazy recall acquisition and append-only persistence through that same facade.

The CLI facade persists a real session row, single-message and batch structured
messages (including identity, order, timestamps, and null content), system-prompt
snapshot, end/reopen transition, and bounded recent-session lookup. It rejects
unimplemented SessionDB methods with a capability error before any SQLite
fallback. The legacy SQLite CLI path continues to construct `SessionDB`.

The adapter-only `atomic-compression-rotation-v1` contract is separately proven
by the PG18 rotation acceptance harness. The real public `AIAgent` route is verified with deterministic offline providers for
selected-PG lazy acquisition → `run_conversation()` → `ContextCompressor` →
fenced parent/child publication, a post-commit lost-acknowledgement receipt
adoption, provider-cancellation reopen/retry with no receipt replay, and an
expired-owner server-clock successor rotation. Each public route runs under a
trapped `state.db` opener. This remains narrower than the adapter harness:
delivery retries and unrelated runtime-owner consumers retain their direct-adapter
acceptance coverage and are not claims that those runtime consumers are activated.

Activation remains blocked outside that bounded CLI path by these capabilities:

- contextual session search/lineage contract;
- gateway delivery-ledger runtime/recovery routing (except the explicit,
  dependency-injected final-response consumer described below); and
- async-delegation ledger routing.

The error names the missing capabilities and points here. This is deliberately
not a claim that gateway, cron, TUI, ACP, async-delegation, or hosted-room
runtime is PostgreSQL-ready.

## Bounded injected final-delivery ledger consumer

`BasePlatformAdapter.send_final_ledgered()` is the sole gateway consumer that
may receive a PostgreSQL ledger, and only when a caller explicitly constructs
`open_configured_delivery_ledger()` and injects it as `adapter.delivery_ledger`.
The five-method port is restricted to obligation, fenced claim, delivered,
failed, and unsent-claim release transitions; no raw connection is available to
the consumer. PostgreSQL configuration alone does not inject it, start a
gateway, route sessions, or select a recovery sweep. SQLite remains the
default when no ledger is injected.

`tests/integration/test_postgresql_delivery_ledger.py` uses an owned PG18 UUID
schema and traps `sqlite3.connect`: final obligation/claim/ack completes with
no `state.db` open and a fake sender is never invoked by the ledger bracket.
This is consumer/ledger evidence only. Gateway startup recovery, session
persistence/routing, cron, TUI/API, ACP, hosted rooms, async delegation, and
contextual search remain unported and fail closed under selected PostgreSQL.

## Sandbox fixture and proof

`tests/fixtures/postgresql-state-store-runtime-sandbox-config.yaml` is non-secret: it selects PostgreSQL and only names `HERMES_STATE_STORE_TEST_DSN`. The loopback PG18 fixture supplies the trust-only DSN in the test process; no DSN is committed to configuration.

`tests/test_state_store_runtime_readiness.py` proves:

- AST inventory retains raw unported runtime openers;
- a legacy SQLite open is trapped with exact caller information; SQLite test isolation may materialize the fixture before the trapped native connect;
- a selected named PostgreSQL sandbox profile derives its tenant schema, reports missing capabilities, opens no `state.db`, and has no fallback event;
- selected-PG `delegate_task(background=true)` reaches the async-dispatch boundary, rejects before its injected runner/external-child side effect, and opens no `state.db`;
- default SQLite construction still creates/opens its configured `state.db`; and
- the checked-in report contains no PostgreSQL DSN.

## Async-delegation boundary

The current durable `async_delegations` row is not a portable job/execution
protocol. It records a local daemon runner's routing metadata and completion
for replay into the originating process's in-memory completion queue. The
runner closure, future/executor state, interrupt callback, progress monitor,
process ownership, and queue admission/acknowledgement boundary are
process-local and cannot be reconstructed from the row. A PostgreSQL table
that accepts dispatches before it carries that complete protocol would claim
recovery it cannot provide and could either duplicate or lose an external
child execution.

Accordingly, `tools.async_delegation._persist_dispatch()` retains the legacy
SQLite adapter only, and its mandatory selected-backend gate raises
`PostgreSQLRuntimeActivationError` before directory creation, SQLite open,
executor submission, or runner invocation. This is direct-test evidence of a
safe failure, not PostgreSQL async-delegation routing. Porting may proceed only
with a backend-neutral ledger plus end-to-end fenced claim, cancellation,
completion, recovery, and completion-queue delivery contracts across every
caller; until then the selected PostgreSQL sandbox intentionally refuses the
operation.

`tests/integration/test_postgresql_cli_session_store.py` exercises a fresh
isolated `HERMES_HOME` against loopback PostgreSQL: session creation, ordered
single and batch message persistence, prompt snapshot, end, a new-store
resume/reopen, session lookup, unsupported-call refusal, and the file-open
trap. Its deterministic offline-agent harness constructs the real `AIAgent`
with local mocks only, proving lazy selected-PG acquisition, first-turn
persistence, public compression publication, lost-ack durable-receipt adoption,
provider-abort reopen/retry without replay, stale-owner successor rotation,
prompt/history restoration, end/reopen, and active lease-holder rejection
before a write. The v19 coordination slice additionally persists activity labels, cooldown snapshot/restore, anti-thrash counters, and fenced server-clock leases; migration v20 adds the adapter-owned atomic parent/child compression publication receipt. `tests/integration/test_postgresql_compression_rotation_acceptance.py` exercises its fenced/idempotent handoff without SQLite fallback. This does not make the gateway, cron, TUI, ACP, hosted, or async paths PostgreSQL-ready; it also proves the default SQLite factory path is unchanged.

Run the focused gate with:

```bash
scripts/run_tests.sh -o "addopts=" tests/integration/test_postgresql_cli_session_store.py; scripts/run_tests.sh tests/test_state_store_runtime_readiness.py tests/test_state_store_config.py tests/test_state_store_factory.py
```

## Remaining work

Do not route gateway, cron, TUI, ACP, async-delegation, or hosted-room startup
to PostgreSQL until every listed runtime consumer has an approved
backend-neutral contract and equivalent lifecycle, ownership, delivery,
contextual-search, and recovery behavior. SQLite remains the migration
source/reference only; it is not a selected-PG fallback.
