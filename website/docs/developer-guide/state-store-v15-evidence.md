# PostgreSQL State Store v15 search evidence

## Implemented bounded contract

PostgreSQL 18 now has a tenant-schema-local, generated `search_document` using PostgreSQL's built-in `simple` `tsvector`, plus a tenant-schema-local GIN index. Migrations v14 (generated document) and v15 (GIN index) are transactionally advisory-locked and catalog-validated on every open.

`PostgreSQLStateStore.search_messages()` supports nonempty whitespace lexical terms, visibility/source/role filters, `newest`/`oldest` pagination, bounded snippets, and selected result projection. Default ranking uses `ts_rank_cd`; its ties are not SQLite BM25 equivalence. Standard text uses `plainto_tsquery('simple', query)`. CJK-containing input uses a parameterized canonical-row substring fallback because no CJK tokenizer is assumed. Every query is fixed to the store's trusted canonical-`HERMES_HOME` tenant schema; callers cannot select a tenant.

## Verified

On PostgreSQL 18.6, the real integration fixture exercised fresh/upgrade ledgers through v15 and catalog drift checks. The live SQLite/PG differential corpus covered lexical and CJK hits, source and role filters, snippets, order/page behavior, generated-document update/delete maintenance, and `REINDEX` rebuild. The test database had both `pg_trgm` and `vector` installed, but the implementation does not probe, require, or query either extension; its search path is unchanged if either is unavailable.

## Explicitly not implemented

This is not FTS5 equivalence or a cutover. It does not implement FTS5 phrase/prefix/AND-OR-NOT grammar, BM25 parity, FTS5 CJK-bigram/trigram ranking, full tool-body rules, deferred rebuild/fail-open semantics, contextual neighboring-message projection, query-plan guarantees, RLS/role-based tenant authorization, runtime consumer selection, SQLite import/export, dual-write, or production configuration/deployment. pgvector is not used as FTS.
