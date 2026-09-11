# PostgreSQL State Store v16 contextual-search projection audit

## Direct-history reconciliation

- `b68ccbe76f4e85b539a4c05d69ec59ea8e06f636` — `Change-Id: I83b2756b073f43529edf63e0a9edee40`; migration v16 bounded browse projection, 42 tests. This did not route the complete contextual tool or authorize a backend cutover.

## Decision: no partial contextual projection is portable

No independently complete contextual `session_search` projection group is currently exposed through `StateStore`. PostgreSQL remains intentionally limited to raw lexical candidate rows. This document records a fail-closed non-implementation rather than returning lexical rows as a substitute for the existing contextual API.

## SQLite contract that a cutover must preserve

`SessionDB.search_messages()` returns canonical candidates in the field order `id`, `session_id`, `role`, `snippet`, `timestamp`, `tool_name`, `source`, `model`, `session_started`, and `context`. Unless `fields` omits `context`, each candidate is enriched in one batched query with its same-session chronological neighbours (anchor plus one physical row on either side, ordered by timestamp/id), decoded and shortened to 200 characters. Context never crosses sessions; tied timestamps retain stable identity order; duplicate candidates retain identical context.

The tool's discovery flow uses those raw candidates only as an input. It defaults roles to `user`/`assistant`, excludes `kanban`, `subagent`, and `tool` sources, applies stable cron demotion and lineage de-duplication, then hydrates a selected anchor with `get_anchored_view()`. The anchored view has a physical ±5 window, retains a tool anchor even when filtering its neighbouring rows to user/assistant, and supplies first/last three non-empty role-filtered bookends strictly outside that window. Scroll uses `get_messages_around()` with a clamped ±1..20 physical window and deliberate boundary-anchor repetition across pages. Browse/read use distinct bounded projections.

Candidate visibility excludes hidden display rows and, unless requested, requires active or compaction-archived messages. SQLite's FTS failures detach derived FTS structures, mark search stale, serve canonical `LIKE` fallback, and during deferred backfill supplement the unindexed high-water gap. Rebuild admission, quarantine, retries, and tool-visible incompleteness are all part of the observable behavior.

## Implemented PostgreSQL subcapability (still not a contextual cutover)

PostgreSQL now has tenant-schema-qualified, parameterized anchored-detail/bookend and scroll primitives. They preserve the SQLite physical transcript identity order (`id`; timestamps are not a safe adjacency key), foreign-anchor empty view, anchor inclusion/repetition, role-filtered detail with tool-anchor retention, and strictly non-overlapping non-empty bookends. PG18 differential coverage exercises regressing timestamps, boundaries, tool anchors, same-session isolation, and repeated scroll anchors.

This remains deliberately incomplete for public contextual routing: PostgreSQL now returns the canonical `model` and conditional neighbour-context candidate projection, but it has no equivalent public search-index/rebuild health status. The tool continues to reject PostgreSQL through `ContextualSessionSearchStore`; no lexical PostgreSQL rows or direct primitives are routed into contextual discovery, detail, scroll, browse, or read.

PostgreSQL direct `search_messages()` now returns the canonical ordered candidate fields `id`, `session_id`, `role`, `snippet`, `timestamp`, `tool_name`, `source`, `model`, `session_started`, and conditional `context`. It batches up to the returned candidate set into tenant-schema-qualified, parameterized same-session chronological neighbour seeks using `(created_at, id)` ties, decodes JSON-encoded multimodal content, flattens it exactly as SQLite does, and caps each context item at 200 characters. Projections that omit `context` do not issue the enrichment query; repeated input identities share the same enriched context. PG18/SQLite coverage covers ties, session boundaries, structured context, hidden candidates, source/role filters, tool-body queries, model/session metadata, deterministic oldest pagination, and conditional fields.

## Implemented intermediate contract

`state_store.ContextualSessionSearchStore` now publishes the atomic contextual-read boundary independently from the incremental write-oriented `StateStore`. `SqliteContextualSessionSearchStore` delegates every required operation to the existing `SessionDB` implementation: canonical candidate search, title/session lookup, anchored detail and scroll views, message visibility state, bounded browse, and public search-index rebuild status.

`tools.session_search_tool` coerces both lazily acquired and injected SQLite stores through this capability before dispatching every public shape. The former tool-private `SessionDB._lock`/`_conn` visibility read now lives solely inside the SQLite adapter. Named-profile resolution likewise opens a read-only contextual store, preserving profile isolation and closing ownership.

## Profile-aware routing prerequisite

Contextual resolution now derives every root/default/named profile's backend from that profile's own `config.yaml`, not from the caller's profile metadata or a hard-coded `<profile>/state.db` path. The resolver reads the canonical profile directory directly, uses a context-local `HERMES_HOME` override only while acquiring PostgreSQL, and builds secrets from the same profile's `.env`; it never mutates process-global `HERMES_HOME` or borrows ambient secrets. SQLite retains its default and registry-backed behavior, with named-profile opens read-only.

For a configured PostgreSQL profile, the resolver enters the existing canonical-home tenant acquisition path and then raises `ContextualSessionSearchUnavailable`, closing the partial store. It does not open that profile's SQLite database, does not select a schema from user input, and does not fall back across backend boundaries. Invalid backend configuration and injection-shaped profile names also fail before any SQLite fallback.

PostgreSQL is explicitly rejected by `contextual_session_search_store(backend="postgresql")` with `ContextualSessionSearchUnavailable`; no lexical PostgreSQL rows are attached to the contextual tool. No v16 schema migration or runtime backend cutover is implied.

## Required future atomic portability group

A valid PostgreSQL public cutover slice must introduce and differentially test the remaining backend-neutral capability covering: existing candidate/model/context search plus anchored views and scroll views; session lookup; public search storage/rebuild status; and bounded recent-session browse. It must preserve tenant-schema qualification, parameterized values, trusted schema derivation, and connection `search_path` reset. Its PG18/SQLite matrix must cover session boundaries, adjacent and duplicate hits, ties, role/source/visibility filters, tool-body behavior, snippets/context 200-character caps, pagination, cross-session isolation, update/delete/reindex maintenance, FTS corruption/deferred rebuild signaling, and tenant isolation.

## Exact remaining gaps / cutover boundary

Not implemented or claimed: PostgreSQL public contextual routing or search-index/rebuild health; complete FTS5 BM25/tokenizer/CJK/trigram equivalence; corruption fallback, deferred rebuild and recovery parity; migration/import/export; RLS authorization; production deployment; or cutover. Direct anchored/detail/bookend, scroll, browse/read primitives and canonical candidate/model/context projection exist but remain deliberately unrouted until the complete contextual capability truthfully reports health and rebuild state.
