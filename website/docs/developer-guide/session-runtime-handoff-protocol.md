# Session Runtime Ownership and Turn Handoff Protocol

## Status and boundary

This document defines an **additive SQLite compatibility foundation**. It is not selected by a live runtime, does not implement PostgreSQL or Redis, and does not authorize hosted-room retries. Existing `session_turn_leases` remains the local runtime path.

A future consumer may route to this protocol only when it carries the exact ownership receipt through admission, transcript publication, finalization, and every external-effect boundary.

## Identity and authoritative time

An owner is `(installation_id, host, process_generation)`. A PID alone is invalid cross-machine identity. The runtime namespace and session ID are trusted routing inputs, not caller-selected tenant SQL.

SQLite stores compatibility evidence using its transaction and local clock. PostgreSQL must use a single server-time transaction/CAS for acquire, renewal, expiry takeover, turn transition, and fence-guarded acknowledgement. Optional Redis may notify/wake waiters only; it is never an authority or fence source.

## State machine

`session_runtime_owners` holds one row per `(namespace, session_id)` with an increment-only `fence`.

- acquire absent row → fence `1`;
- same owner acquire → renews without changing fence;
- foreign non-expired owner → no receipt;
- expired foreign owner → replaces identity, increments fence, and marks older `running` turns `indeterminate`;
- renew/release require the exact identity and fence.

`session_runtime_turns` is keyed by `(namespace, session_id, turn_id)`.

- `begin` requires the current unexpired receipt and creates `running`; same receipt duplicate is idempotent;
- takeover converts prior-fence `running` to `indeterminate`;
- a settled turn requires a verified receipt payload; storage success alone is not an external-effect receipt;
- stale fences cannot begin or resolve turns; an indeterminate turn is not implicitly retried. A new turn ID is required after explicit recovery assessment.

Browser/CUA, approval waits, and other process-local capabilities are explicitly excluded from handoff. They must fail closed on destination recovery.

## Required PostgreSQL completion plan

1. Add a backend-neutral protocol/factory and migrate the SQLite implementation plus one complete consumer vertical slice.
2. Implement PostgreSQL schema/catalog validation and server-time fenced CAS. Every durable transcript, compaction, finalization, and acknowledgement write must require the receipt.
3. Add differential SQLite/PG tests for contention, expiry takeover, stale fences, duplicate payload conflicts, crash/SIGKILL turns, namespace isolation, and receipt-backed external-effect resolution.
4. Add process-local capability rejection and no-retry evidence for browser/CUA and uncertain effects.
5. Only after those tests and a separately authorized cutover plan may runtime selection be considered. Redis signals, live configuration, and hosted-room work remain outside this protocol.
