# State-store architecture (incremental replacement)

## Status

This is the approved target architecture. The first functional session/message slice now provides a backend-selected factory, an unchanged SQLite adapter, and a PostgreSQL 18 integration implementation with a bounded internal pool, migration metadata, and capability probe. SQLite remains the default and existing installations continue to use it unchanged. PostgreSQL cannot be selected for production until phases 6–13 have supplied complete schema, consumer, migration, rollback, and runtime evidence.

## Boundary

All state consumers will depend on a backend-neutral `StateStore` contract and factory, not `sqlite3.Connection`, file paths, SQLite pragmas, or FTS5 commands. The contract owns transaction lifecycle, schema head, health, sessions/messages/search, operational gateway state, migration, and change feed behavior. A backend may expose implementation-private connection types only beneath that boundary.

The existing `SessionDB` API is the compatibility facade during the migration. `SqliteStateStore` preserves its current behavior and becomes the default backend. `PostgresStateStore` is a first-class selectable backend with an explicit optional client dependency and capability probe; it will reach production readiness only when all current SessionDB and gateway raw-opener operations have moved through the contract.

## Configuration

The core `state_store` section is deliberately a new top-level core section, not a plugin or observability namespace:

```yaml
state_store:
  backend: sqlite # sqlite (default) | postgresql
  postgresql:
    dsn_env: HERMES_STATE_STORE_POSTGRES_DSN
    connect_timeout_seconds: 10
    pool_max_size: 8
```

`backend` is profile-local because `HERMES_HOME` is profile-local. Configuration is resolved by the existing `load_config()` path, including its profile scope, deep merge, managed overlay, and `${ENV}` secret expansion. The DSN itself is never persisted in `config.yaml`; `dsn_env` names one profile-scoped secret. A missing, malformed, or empty secret fails closed only when `backend: postgresql` is selected. SQLite ignores PostgreSQL-only configuration.

There is no global-to-profile inheritance beyond Hermes’s existing profile configuration semantics. A root profile and each named profile resolve their own config and consequently their own backend target. The PostgreSQL target must derive a stable tenant/database/schema identity from the resolved profile identity, never merely from a PID or a file path.

## PostgreSQL topology and semantics

The initial support target is PostgreSQL 18 with pgvector image `pgvector/pgvector:pg18`; `pg_trgm` and `vector` are capability-probed extensions. `vector` is available for future semantic indexes but is not a substitute for FTS semantics. `pg_trgm` is optional and its absence must select a documented bounded fallback rather than silently claim CJK/substring equivalence.

The implemented tenant acquisition boundary resolves the active `HERMES_HOME` once with the canonical `profile_name_for_home()` resolver. The fixed legacy `hermes_state_store_slice` schema is the root/default compatibility tenant only; every named profile maps to a deterministic `hermes_state_store_tenant_<sha256>` schema derived from its canonical home and canonical profile identity. Profile text, caller metadata, and config never enter SQL identifiers. Migrations and ledgers run independently under a tenant-specific advisory lock, and every pool checkout resets a quoted schema search path before use. Existing shared fixed-schema rows are not attributed or backfilled: operators must explicitly audit/export them before moving a formerly shared target to named-profile tenancy.

Critical writes use PostgreSQL transactions with explicit retry classification. Fencing uses installation UUID + host identity + process generation + monotonically increasing lease generation; PID-only checks and SQLite file locks are not portable. A durable change sequence plus `LISTEN/NOTIFY` wake-up provides notification; observers must catch up by sequence after reconnect.

## Migration and rollback

The currently implemented session/message slice has a transactionally locked, catalog-validated migration ledger: v1 creates sessions/messages, v2 adds session metadata and indexes, v3 records the parent-session foreign key, v4 is a documented compatibility checkpoint validating the previously unledgered v1–v3 contract, v5 adds title fields/indexes, v6 adds visibility fields/indexes, v7 adds bounded active-message record columns, and v8 adds the resume-projection index. Every recorded version is revalidated against PostgreSQL catalogs on open; missing or mismatched objects fail closed rather than being silently trusted. The v4 checkpoint intentionally has no DDL because it only makes the legacy parent-key transition durably auditable. v8 adds bounded read-only compression-lineage root/tip, model/display projection, prefix, and resume-guard APIs; it does not claim SessionDB's complete replay transformation, rewrite, compaction, FTS, or runtime-consumer parity. v9 adds content-addressed system-prompt snapshots (`system_prompts`, SHA-256 session reference, validated FK, read/set/clear and orphan collection). v10 adds PostgreSQL atomic model-usage persistence through the shared `TokenUsageTransport`: summary counters, billing route metadata, and six-dimensional (`session_id`, model, provider, base URL, mode, task) `session_model_usage` attribution rows migrate and validate transactionally. It does not switch runtime consumers or select PostgreSQL by default. v11 adds the source-qualified, non-prunable conversation-generation ledger for reset lifecycle parity. v12 is a sequential catalog-validation checkpoint for the mutable model/config lifecycle: flush-before-switch model and billing updates, prompt invalidation, atomic JSON merge/delete-on-`None`, and tolerant config reads. It adds no DDL because v2/v9/v10 already establish and validate its columns and prompt FK. This remains a bounded slice, not a full state.db upgrade or SQLite-to-PostgreSQL import contract. v13 adds the separately bounded cwd/Git publication lifecycle: a PostgreSQL `git_branch` plus monotonic `git_metadata_generation` migration, catalog validation, atomic cwd claims, and exact `(session_id, cwd, generation)` stale-probe fencing. It does not switch runtime consumers or claim tenant/profile ownership, browser policy/leases, import, cutover, or recovery parity.

Migration is offline and single-authority: quiesce the selected SQLite profile/root writer, capture a consistent SQLite backup including WAL, import canonical rows in FK order, restore sequence high-water marks, rebuild derived indexes, verify counts/invariants/search, and only then select PostgreSQL. No dual write is permitted.

Pre-write rollback switches back only after preserving the PostgreSQL candidate. After PostgreSQL has accepted writes, rollback requires a verified reverse export or restore of a PostgreSQL backup to a separately selected SQLite candidate; changing config alone is prohibited because it would lose post-cutover writes. Existing live databases are out of scope for this branch run.

## Implementation phases

1. Configuration, factory, capability probing, and SQLite compatibility (current earliest slice).
2. Backend-neutral SessionDB operations and complete canonical PostgreSQL schema.
3. PostgreSQL search and all gateway/hosted-room operational consumers.
4. Native recovery/doctor/change-feed behavior.
5. Resumable migration, rollback, and real sandbox conformance.
