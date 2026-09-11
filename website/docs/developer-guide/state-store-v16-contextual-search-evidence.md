# PostgreSQL State Store v16 contextual-search projection audit

## Decision: no partial contextual projection is portable

No independently complete contextual `session_search` projection group is currently exposed through `StateStore`. PostgreSQL remains intentionally limited to raw lexical candidate rows. This document records a fail-closed non-implementation rather than returning lexical rows as a substitute for the existing contextual API.

## SQLite contract that a cutover must preserve

`SessionDB.search_messages()` returns canonical candidates in the field order `id`, `session_id`, `role`, `snippet`, `timestamp`, `tool_name`, `source`, `model`, `session_started`, and `context`. Unless `fields` omits `context`, each candidate is enriched in one batched query with its same-session chronological neighbours (anchor plus one physical row on either side, ordered by timestamp/id), decoded and shortened to 200 characters. Context never crosses sessions; tied timestamps retain stable identity order; duplicate candidates retain identical context.

The tool's discovery flow uses those raw candidates only as an input. It defaults roles to `user`/`assistant`, excludes `kanban`, `subagent`, and `tool` sources, applies stable cron demotion and lineage de-duplication, then hydrates a selected anchor with `get_anchored_view()`. The anchored view has a physical ±5 window, retains a tool anchor even when filtering its neighbouring rows to user/assistant, and supplies first/last three non-empty role-filtered bookends strictly outside that window. Scroll uses `get_messages_around()` with a clamped ±1..20 physical window and deliberate boundary-anchor repetition across pages. Browse/read use distinct bounded projections.

Candidate visibility excludes hidden display rows and, unless requested, requires active or compaction-archived messages. SQLite's FTS failures detach derived FTS structures, mark search stale, serve canonical `LIKE` fallback, and during deferred backfill supplement the unindexed high-water gap. Rebuild admission, quarantine, retries, and tool-visible incompleteness are all part of the observable behavior.

## Why PostgreSQL cannot claim it

The current PostgreSQL `search_messages()` lacks the SQLite `model` and `context` projections. More importantly, it does not provide backend-neutral equivalents for `get_anchored_view()`, `get_messages_around()`, or the public storage/rebuild state read that discovery reports. `session_search_tool` additionally reads SQLite private connection/lock state for anchor status. Adding only neighbour SQL would therefore leave discovery, detail, scroll, and degradation semantics backend-specific.

## Implemented intermediate contract

`state_store.ContextualSessionSearchStore` now publishes the atomic contextual-read boundary independently from the incremental write-oriented `StateStore`. `SqliteContextualSessionSearchStore` delegates every required operation to the existing `SessionDB` implementation: canonical candidate search, title/session lookup, anchored detail and scroll views, message visibility state, bounded browse, and public search-index rebuild status.

`tools.session_search_tool` coerces both lazily acquired and injected SQLite stores through this capability before dispatching every public shape. The former tool-private `SessionDB._lock`/`_conn` visibility read now lives solely inside the SQLite adapter. Named-profile resolution likewise opens a read-only contextual store, preserving profile isolation and closing ownership.

PostgreSQL is explicitly rejected by `contextual_session_search_store(backend="postgresql")` with `ContextualSessionSearchUnavailable`; no lexical PostgreSQL rows are attached to the contextual tool. No v16 schema migration or runtime backend cutover is implied.

## Required future atomic portability group

A valid PostgreSQL cutover slice must introduce and differentially test one backend-neutral capability covering: canonical candidate fields including `model` and conditional context; anchored views and scroll views; session lookup; public search storage/rebuild status; and bounded recent-session browse. It must preserve tenant-schema qualification, parameterized values, trusted schema derivation, and connection `search_path` reset. Its PG18/SQLite matrix must cover session boundaries, adjacent and duplicate hits, ties, role/source/visibility filters, tool-body behavior, snippets, pagination, cross-session isolation, update/delete/reindex maintenance, FTS corruption/deferred rebuild signaling, and tenant isolation.

## Exact remaining gaps / cutover boundary

Not implemented or claimed: contextual neighbouring-message projection; anchored detail/bookends; scroll/browse/read projections; `model` candidate field; tool-private-state removal; FTS5 BM25/tokenizer/CJK/trigram equivalence; tool-body full-content rule; corruption fallback, deferred rebuild and recovery parity; migration/import/export; RLS authorization; runtime selection; production deployment; or cutover. PostgreSQL remains a tenant-safe, bounded lexical StateStore slice only.
