# PostgreSQL State Store v16 contextual-search projection audit

## Direct-history reconciliation

- `b68ccbe76f4e85b539a4c05d69ec59ea8e06f636` — `Change-Id: I83b2756b073f43529edf63e0a9edee40`; migration v16 bounded browse projection, 42 tests. This did not route the complete contextual tool or authorize a backend cutover.

## Decision: no partial contextual projection is portable

No independently complete contextual `session_search` projection group is currently exposed through `StateStore`. PostgreSQL remains intentionally limited to raw lexical candidate rows. This document records a fail-closed non-implementation rather than returning lexical rows as a substitute for the existing contextual API.

## SQLite contract that a cutover must preserve

`SessionDB.search_messages()` returns canonical candidates in the field order `id`, `session_id`, `role`, `snippet`, `timestamp`, `tool_name`, `source`, `model`, `session_started`, and `context`. Unless `fields` omits `context`, each candidate is enriched in one batched query with its same-session chronological neighbours (anchor plus one physical row on either side, ordered by timestamp/id), decoded and shortened to 200 characters. Context never crosses sessions; tied timestamps retain stable identity order; duplicate candidates retain identical context.

The tool's discovery flow uses those raw candidates only as an input. It defaults roles to `user`/`assistant`, excludes `kanban`, `subagent`, and `tool` sources, applies stable cron demotion and lineage de-duplication, then hydrates a selected anchor with `get_anchored_view()`. The anchored view has a physical ±5 window, retains a tool anchor even when filtering its neighbouring rows to user/assistant, and supplies first/last three non-empty role-filtered bookends strictly outside that window. Scroll uses `get_messages_around()` with a clamped ±1..20 physical window and deliberate boundary-anchor repetition across pages. Browse/read use distinct bounded projections.

Candidate visibility excludes hidden display rows and, unless requested, requires active or compaction-archived messages. SQLite's FTS failures detach derived FTS structures, mark search stale, serve canonical `LIKE` fallback, and during deferred backfill supplement the unindexed high-water gap. Rebuild admission, quarantine, retries, and tool-visible incompleteness are all part of the observable behavior.

## Implemented PostgreSQL contextual health/rebuild capability

PostgreSQL now exposes the complete contextual contract and is admitted to public routing only after a tenant-scoped generated-search health check passes. The status is intentionally **not** SQLite FTS progress: it records the generated `tsvector` validity, GIN catalog validity, query-path availability, whether a per-tenant maintenance operation is active, last successful repair/error, and explicit `false` capabilities for SQLite-only corruption-detach, canonical-LIKE fallback, deferred backfill/high-water, retry, and quarantine semantics. A missing or malformed GIN entry causes new contextual admission and live discovery to fail closed even though PostgreSQL could otherwise choose a sequential scan.

`rebuild_search_index()` is a trusted-schema-only operational repair, not an automatic side effect of status. It takes a cross-process advisory lock on a dedicated autocommit connection and uses `REINDEX INDEX CONCURRENTLY`, creates a missing GIN index concurrently, or replaces an invalid same-named catalog object. It cannot select a tenant or interpolate caller input. PG18 live coverage exercises healthy status, missing-index drift, invalid-index drift, concurrent repair, read/search behavior across the missing/repaired transition, pool `search_path` reset, and tenant isolation. SQLite `fts_rebuild_status()` remains unchanged.

PostgreSQL direct `search_messages()` returns the canonical ordered candidate fields `id`, `session_id`, `role`, `snippet`, `timestamp`, `tool_name`, `source`, `model`, `session_started`, and conditional `context`. It batches up to the returned candidate set into tenant-schema-qualified, parameterized same-session chronological neighbour seeks using `(created_at, id)` ties, decodes JSON-encoded multimodal content, flattens it exactly as SQLite does, and caps each context item at 200 characters. Projections that omit `context` do not issue the enrichment query; repeated input identities share the same enriched context. PG18/SQLite coverage covers ties, session boundaries, structured context, hidden candidates, source/role filters, tool-body queries, model/session metadata, deterministic oldest pagination, and conditional fields.

## Implemented intermediate contract

`state_store.ContextualSessionSearchStore` now publishes the atomic contextual-read boundary independently from the incremental write-oriented `StateStore`. `SqliteContextualSessionSearchStore` delegates every required operation to the existing `SessionDB` implementation: canonical candidate search, title/session lookup, anchored detail and scroll views, message visibility state, bounded browse, and public search-index rebuild status.

`tools.session_search_tool` coerces both lazily acquired and injected SQLite stores through this capability before dispatching every public shape. The former tool-private `SessionDB._lock`/`_conn` visibility read now lives solely inside the SQLite adapter. Named-profile resolution likewise opens a read-only contextual store, preserving profile isolation and closing ownership.

## Profile-aware routing prerequisite

Contextual resolution now derives every root/default/named profile's backend from that profile's own `config.yaml`, not from the caller's profile metadata or a hard-coded `<profile>/state.db` path. The resolver reads the canonical profile directory directly, uses a context-local `HERMES_HOME` override only while acquiring PostgreSQL, and builds secrets from the same profile's `.env`; it never mutates process-global `HERMES_HOME` or borrows ambient secrets. SQLite retains its default and registry-backed behavior, with named-profile opens read-only.

For a configured PostgreSQL profile, the resolver enters the existing canonical-home tenant acquisition path and admits the store only after the full contextual-method and generated-search-health gate passes. It does not open that profile's SQLite database, does not select a schema from user input, and does not fall back across backend boundaries. Invalid backend configuration and injection-shaped profile names also fail before any SQLite fallback.

PostgreSQL is admitted by `contextual_session_search_store(backend="postgresql")` only when every contextual method is present and its generated-search status is valid. No lexical PostgreSQL rows are routed if catalog drift makes the contextual search path unavailable. No runtime backend cutover is implied.

## Required future atomic portability group

A valid PostgreSQL public cutover slice must introduce and differentially test the remaining backend-neutral capability covering: existing candidate/model/context search plus anchored views and scroll views; session lookup; public search storage/rebuild status; and bounded recent-session browse. It must preserve tenant-schema qualification, parameterized values, trusted schema derivation, and connection `search_path` reset. Its PG18/SQLite matrix must cover session boundaries, adjacent and duplicate hits, ties, role/source/visibility filters, tool-body behavior, snippets/context 200-character caps, pagination, cross-session isolation, update/delete/reindex maintenance, FTS corruption/deferred rebuild signaling, and tenant isolation.

## Exact remaining gaps / cutover boundary

Not implemented or claimed: FTS5 BM25/tokenizer/CJK/trigram equivalence; SQLite corruption-detach/canonical-LIKE fallback/deferred-rebuild parity; migration/import/export; RLS authorization; production deployment; runtime/config cutover; or production readiness. The public PostgreSQL contextual route is limited to a configured tenant with a valid generated-document/GIN health gate; repair is local explicit maintenance, not a deployment recovery plan.
