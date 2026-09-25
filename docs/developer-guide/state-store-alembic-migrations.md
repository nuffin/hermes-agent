# PostgreSQL State Store: Alembic Migration Authority

> **Status:** approved pre-implementation architecture requirement.
> **Scope:** PostgreSQL state-store tenant schemas only; this does not alter SQLite behavior or enable MySQL.

## Decision and boundaries

PostgreSQL schema evolution **MUST** be owned exclusively by Alembic. The existing hand-run PostgreSQL `schema_migrations` chain is not a second authority, bridge, or source for an Alembic stamp. Alembic revisions **MUST** be hand-authored; `--autogenerate` is prohibited. SQLite remains its existing implementation with no PostgreSQL fallback, import, or behavior change. MySQL is a future portability boundary only: no MySQL dialect, runtime selection, migration execution, or compatibility promise is introduced.

The migration environment **MUST** receive only the runtime-resolved, trusted PostgreSQL DSN and validated tenant schema. It **MUST NOT** derive either from caller text, stored rows, arbitrary configuration fragments, or a user-supplied Alembic URL/schema option. Tenant schemas retain the existing trusted routing model (legacy root compatibility schema where applicable; otherwise deterministic validated tenant schema), quoted identifier handling, and per-checkout `search_path` discipline.

## Alembic model

- A new/empty PostgreSQL tenant schema **MUST** be initialized by exactly one immutable, hand-authored **v32 baseline** revision. It creates the complete v1–v32 contract and its tenant-local `alembic_version` table in the same migration transaction.
- The baseline revision ID and its contents are immutable after release. Later schema changes **MUST** be normal, ordered Alembic child revisions in the same graph; no new synthetic baseline, runtime DDL loop, or version-number ledger is permitted.
- `alembic_version` **MUST** be per tenant schema, not global/default-schema state. Runtime migration commands target exactly one trusted tenant schema and may not enumerate or migrate unrelated tenants.
- Migration startup **MUST** retain the transaction-scoped PostgreSQL advisory lock scoped to the trusted tenant schema, the PostgreSQL 14+ gate, and atomic commit/rollback semantics.
- After upgrade (and on ordinary store open), the implementation **MUST** run the full catalog validator. It validates the complete head contract—columns, types/defaults/generated expressions, primary/foreign keys, indexes, and required tables—not merely Alembic revision presence. Drift remains a normalized, fail-closed configuration error.
- Database, connection, migration, unsupported-version, legacy-ledger, and catalog-drift failures **MUST** be mapped to the established normalized state-store error surface without leaking DSNs or raw driver diagnostics.

## Legacy-ledger refusal

If the trusted tenant schema contains the legacy `schema_migrations` relation—whether empty, populated, or otherwise readable—the runtime **MUST** stop before Alembic mutation, stamping, or repair with the formal error:

```text
StateStoreMigrationReinitializationRequired: legacy schema_migrations detected in tenant schema; formal reinitialization is required before Alembic migration
```

There is no bridge revision, `stamp`, ledger import, self-heal, or mixed-authority mode. Formal reinitialization is an explicit operator procedure outside normal runtime startup: preserve/audit required data, create a fresh approved tenant schema, initialize it from the v32 baseline, validate it, and then cut over deliberately.

## Immutable v32 baseline contract

The baseline **MUST** represent every capability below; versions are compatibility provenance, not rows in a replacement ledger.

| Legacy version | Contract included in the v32 baseline |
|---:|---|
| 1 | `sessions`, `messages`, session/message primary and foreign keys, and `messages_session_id_id`. |
| 2 | Session metadata (`user_id`, session/chat/thread identity, display/origin, model/config, parent, cwd/profile/git root) and source/session-key plus parent indexes. |
| 3 | Self-referential `sessions.parent_session_id` foreign key. |
| 4 | Validated v1–v3 compatibility checkpoint (no separate DDL). |
| 5 | Title, title source, hidden state, and unique non-null title index. |
| 6 | Archived/pinned visibility state and browse/pinned indexes. |
| 7 | Bounded message-record surface: tool/reasoning/Codex/platform identity, activity/compaction, API/display metadata. |
| 8 | Active-message resume projection index. |
| 9 | Content-addressed `system_prompts`, session prompt hash, and foreign key. |
| 10 | Session usage/billing aggregates plus task-dimensional `session_model_usage` attribution and indexes. |
| 11 | Non-prunable `(source, session_key)` `conversation_generations` ledger with generation default 0 and no session FK. |
| 12 | Validated mutable model/config lifecycle checkpoint, including JSONB `model_config` (no separate DDL). |
| 13 | Git branch and monotonically initialized git-metadata generation fields. |
| 14 | Stored generated `messages.search_document` `tsvector` over canonical searchable content. |
| 15 | GIN index for the generated search document. |
| 16 | Durable last-activity projection, historical timestamp backfill semantics, and effective-activity browse index. |
| 17 | Singleton search-index maintenance outcome state. |
| 18 | Fenced runtime owner/turn evidence tables and expiry/state indexes. |
| 19 | Compression coordination/activity fields, compression locks, turn leases, and expiry indexes. |
| 20 | Atomic compression-rotation receipt ledger, parent/child keys, and uniqueness/order indexes. |
| 21 | Revisioned goal/heartbeat/loop `session_control_state` and kind/status index. |
| 22 | Rewind count, rewind receipts, active-message target index, and receipt ordering index. |
| 23 | Foreign transcript import receipts with session uniqueness and session FK. |
| 24 | Gateway session-route authority with generation, flags/metadata, and tenant/session uniqueness. |
| 25 | Platform-message exactly-once and per-session lookup partial indexes. |
| 26 | `session_topics`, cascade-to-session ownership, topic activity index, `messages.topic_id`, and topic lookup index. |
| 27 | `state_meta` and `sessions.last_read_at`. |
| 28 | Message-reaction parity via existing display metadata and persisted session `tool_names`. |
| 29 | Gateway parity: prompt/count/handoff/expiry fields, `gateway_routing`, `gateway_heartbeats`, and heartbeat index. |
| 30 | Telegram DM topic mode/bindings, session cascade ownership, binding uniqueness, and user lookup index. |
| 31 | Validated statistics-compatibility checkpoint (no separate DDL). |
| 32 | Durable session expiry-finalization state and `gateway_hygiene_state` failure-streak state. |

## Acceptance, test, and operations requirements

1. Tests **MUST** prove empty trusted tenants upgrade from no schema to Alembic head, receive a tenant-local `alembic_version`, and pass the full catalog validator; test schemas are disposable and isolated.
2. Tests **MUST** prove the immutable baseline exactly satisfies the v1–v32 catalog contract, including v26 `session_topics` and v27–v32 state/gateway/Telegram/statistics/hygiene parity; revisions after baseline must be tested as graph upgrades.
3. Concurrency tests **MUST** prove one tenant migration proceeds under the advisory lock while a competing opener/migrator cannot create partial catalog or version state. Tests **MUST** retain PostgreSQL 14 rejection and normalized-error assertions.
4. Legacy tests **MUST** cover empty and populated `schema_migrations` and assert the exact formal reinitialization-required error, with no stamp, DDL, ledger mutation, or fallback.
5. Operational runbooks **MUST** require backup/audit and explicit formal reinitialization for legacy tenants; ordinary deploys run Alembic only against the selected trusted tenant and verify Alembic head plus catalog validation. Drift repair is not a runtime action.
6. SQLite regression coverage **MUST** demonstrate unchanged SQLite migrations/behavior and that PostgreSQL selection failures never open or mutate SQLite. MySQL remains excluded from runtime and test matrices.
