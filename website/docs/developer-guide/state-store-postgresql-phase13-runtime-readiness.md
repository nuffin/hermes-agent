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

A selected PostgreSQL profile reports its canonical profile home/name and derived tenant schema without retaining a DSN or opening a database. The supported slice is only:

- narrow StateStore records; and
- profile-derived PostgreSQL tenant schema selection.

Activation remains blocked by these capabilities:

- complete SessionDB runtime contract;
- contextual session search/lineage contract;
- gateway delivery-ledger routing; and
- async-delegation ledger routing.

The error names the missing capabilities and points here. This is deliberately a pre-side-effect refusal, not a claim that an interactive, gateway, cron, TUI, or ACP runtime is PostgreSQL-ready.

## Sandbox fixture and proof

`tests/fixtures/postgresql-state-store-runtime-sandbox-config.yaml` is non-secret: it selects PostgreSQL and only names `HERMES_STATE_STORE_TEST_DSN`. The loopback PG18 fixture supplies the trust-only DSN in the test process; no DSN is committed to configuration.

`tests/test_state_store_runtime_readiness.py` proves:

- AST inventory retains raw unported runtime openers;
- a legacy SQLite open is trapped with exact caller information; SQLite test isolation may materialize the fixture before the trapped native connect;
- a selected named PostgreSQL sandbox profile derives its tenant schema, reports missing capabilities, opens no `state.db`, and has no fallback event;
- default SQLite construction still creates/opens its configured `state.db`; and
- the checked-in report contains no PostgreSQL DSN.

Run the focused gate with:

```bash
scripts/run_tests.sh tests/test_state_store_runtime_readiness.py tests/test_state_store_config.py tests/test_state_store_factory.py
```

## Remaining work

Do not set the isolated wrapper's profile to PostgreSQL for normal agent startup until every unported entry in the machine-readable inventory has an approved backend-neutral routing contract and equivalent lifecycle, ownership, delivery, contextual-search, and recovery behavior. SQLite remains the migration source/reference only; it is not a selected-PG fallback.
