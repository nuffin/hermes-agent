"""Real PostgreSQL contract for the first session/message State Store slice."""

from __future__ import annotations

import importlib
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any, cast

import pytest

from state_store import MessageRecord, StateStoreConfigurationError, open_state_store


_DSN_ENV = "HERMES_STATE_STORE_TEST_DSN"


def _config() -> dict[str, object]:
    return {
        "state_store": {
            "backend": "postgresql",
            "postgresql": {
                "dsn_env": _DSN_ENV,
                "connect_timeout_seconds": 5,
                "pool_max_size": 2,
            },
        },
    }


def _psycopg():
    return importlib.import_module("psycopg")


def _reset_schema(dsn: str) -> None:
    with _psycopg().connect(dsn, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA IF EXISTS hermes_state_store_slice CASCADE")


def _seed_v2_schema(dsn: str, ledger_versions: tuple[int, ...]) -> None:
    """Seed the actual v1/v2 shape, optionally with historical ledger rows."""
    with _psycopg().connect(dsn, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("CREATE SCHEMA hermes_state_store_slice")
        cursor.execute("CREATE TABLE hermes_state_store_slice.schema_migrations (version integer PRIMARY KEY, applied_at double precision NOT NULL)")
        cursor.execute("CREATE TABLE hermes_state_store_slice.sessions (id text PRIMARY KEY, source text NOT NULL, started_at double precision NOT NULL, ended_at double precision, end_reason text)")
        cursor.execute("CREATE TABLE hermes_state_store_slice.messages (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, session_id text NOT NULL REFERENCES hermes_state_store_slice.sessions(id), role text NOT NULL, content text, created_at double precision NOT NULL)")
        cursor.execute("CREATE INDEX messages_session_id_id ON hermes_state_store_slice.messages (session_id, id)")
        for column, type_name in (("user_id", "text"), ("session_key", "text"), ("chat_id", "text"), ("chat_type", "text"), ("thread_id", "text"), ("display_name", "text"), ("origin_json", "text"), ("model", "text"), ("model_config", "jsonb"), ("parent_session_id", "text"), ("cwd", "text"), ("profile_name", "text"), ("git_repo_root", "text")):
            cursor.execute(f"ALTER TABLE hermes_state_store_slice.sessions ADD COLUMN {column} {type_name}")
        cursor.execute("CREATE INDEX sessions_source_session_key ON hermes_state_store_slice.sessions (source, session_key)")
        cursor.execute("CREATE INDEX sessions_parent_session_id ON hermes_state_store_slice.sessions (parent_session_id)")
        for version in ledger_versions:
            cursor.execute("INSERT INTO hermes_state_store_slice.schema_migrations (version, applied_at) VALUES (%s, 1)", (version,))


def _migration_versions(dsn: str) -> list[int]:
    with _psycopg().connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version FROM hermes_state_store_slice.schema_migrations ORDER BY version")
        return [int(row[0]) for row in cursor.fetchall()]


def test_postgresql_store_persists_session_messages_and_closes_pool(monkeypatch):
    monkeypatch.setenv(_DSN_ENV, "postgresql://hermes_state_store_test@127.0.0.1:5432/hermes_state_store_test")
    store = open_state_store(_config())
    session_id = f"state-store-slice-{uuid.uuid4()}"
    try:
        assert store.ensure_session(session_id, source="integration") == session_id
        first_id = store.append_message(session_id, role="user", content="first")
        second_id = store.append_message(session_id, role="assistant", content="second")
        assert second_id > first_id
        assert [(message["role"], message["content"]) for message in store.get_messages(session_id)] == [
            ("user", "first"),
            ("assistant", "second"),
        ]
        store.end_session(session_id, "integration_complete")
        assert store.get_session(session_id)["end_reason"] == "integration_complete"
    finally:
        store.close()


def test_sqlite_and_postgresql_preserve_session_creation_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv(_DSN_ENV, "postgresql://hermes_state_store_test@127.0.0.1:5432/hermes_state_store_test")
    metadata = {
        "user_id": "user", "session_key": "key", "chat_id": "chat", "chat_type": "direct",
        "thread_id": "thread", "display_name": "Display", "origin_json": "{\"platform\": \"test\"}",
        "model": "model", "model_config": {"provider": "test"}, "parent_session_id": None,
        "cwd": "/work", "profile_name": "test", "git_repo_root": "/repo",
    }
    stores = (
        open_state_store({}, db_path=tmp_path / "state.db"),
        open_state_store(_config()),
    )
    sessions = []
    try:
        for store in stores:
            session_id = f"state-store-metadata-{uuid.uuid4()}"
            parent_id = f"state-store-parent-{uuid.uuid4()}"
            store.ensure_session(parent_id, source="integration")
            store.ensure_session(session_id, source="integration", metadata={"user_id": "user"})
            store.ensure_session(session_id, source="changed", metadata={**metadata, "parent_session_id": parent_id})
            session = store.get_session(session_id)
            assert session is not None
            sessions.append(session)
        assert [session["source"] for session in sessions] == ["integration", "integration"]
        expected_without_model_config = {key: value for key, value in metadata.items() if key != "model_config"}
        assert [{key: session[key] for key in expected_without_model_config} for session in sessions] == [
            {**expected_without_model_config, "parent_session_id": session["parent_session_id"]}
            for session in sessions
        ]
        assert [json.loads(session["model_config"]) if isinstance(session["model_config"], str) else session["model_config"]
                for session in sessions] == [metadata["model_config"]] * 2
        for store in stores:
            with pytest.raises(Exception):
                store.ensure_session(
                    f"state-store-orphan-{uuid.uuid4()}", source="integration",
                    metadata={"parent_session_id": f"missing-parent-{uuid.uuid4()}"},
                )
    finally:
        for store in stores:
            store.close()


def test_postgresql_store_rejects_missing_driver_before_open(monkeypatch):
    monkeypatch.delenv(_DSN_ENV, raising=False)
    # A missing secret is rejected before a connection attempt and never falls back to SQLite.
    try:
        open_state_store(_config())
    except Exception as exc:
        assert _DSN_ENV in str(exc)
    else:
        raise AssertionError("PostgreSQL selection unexpectedly opened a fallback store")


def test_sqlite_and_postgresql_title_contract_parity(monkeypatch, tmp_path):
    monkeypatch.setenv(_DSN_ENV, "postgresql://hermes_state_store_test@127.0.0.1:5432/hermes_state_store_test")
    stores = (open_state_store({}, db_path=tmp_path / "state.db"), open_state_store(_config()))
    observations = []
    try:
        for store in stores:
            root, tip = f"title-root-{uuid.uuid4()}", f"title-tip-{uuid.uuid4()}"
            base_title = f"Project Plan {uuid.uuid4()}"
            store.ensure_session(root, source="integration")
            store.end_session(root, "compression")
            store.ensure_session(tip, source="integration", metadata={"parent_session_id": root})
            assert store.set_session_title(root, base_title)
            assert store.set_session_title(tip, f"{base_title} #2")
            assert store.set_session_title(tip, base_title)
            assert store.get_session_title(root) is None
            assert store.get_session_title(tip) == base_title
            assert store.get_session_by_title(base_title)["id"] == tip
            assert store.resolve_session_by_title(base_title) == tip
            assert store.get_next_title_in_lineage(base_title) == f"{base_title} #2"
            derived_title, llm_title = f"Derived {uuid.uuid4()}", f"LLM {uuid.uuid4()}"
            assert store.set_auto_title(tip, derived_title, source="derived") is False
            assert store.set_session_title_source(tip, "derived")
            assert store.set_auto_title(tip, llm_title, source="llm")
            assert store.get_session_title_source(tip) == "llm"
            canonical = store.get_session_by_title("Bot Chat")
            if canonical is None:
                ordinary = f"title-ordinary-{uuid.uuid4()}"
                store.ensure_session(ordinary, source="integration")
                assert store.set_session_title(ordinary, "Bot Chat")
                assert store.set_session_hidden(ordinary, True)
            else:
                ordinary = canonical["id"]
                assert bool(canonical["hidden"]) is True
            with pytest.raises(ValueError, match="canonical Bot Chat"):
                store.set_session_title(ordinary, "renamed")
            surrogate_id = f"title-surrogate-{uuid.uuid4()}"
            store.ensure_session(surrogate_id, source="integration")
            assert store.set_session_title(surrogate_id, "\ud800 Clean\u200b Title")
            assert store.get_session_title(surrogate_id) == "� Clean Title"
            observations.append({
                "tip_source": store.get_session_title_source(tip),
                "canonical_title": store.get_session_title(ordinary),
                "canonical_hidden": bool(store.get_session(ordinary)["hidden"]),
            })
        assert observations == [{"tip_source": "llm", "canonical_title": "Bot Chat", "canonical_hidden": True}] * 2
    finally:
        for store in stores:
            store.close()


def test_sqlite_and_postgresql_visibility_summary_contract_parity(monkeypatch, tmp_path):
    monkeypatch.setenv(_DSN_ENV, "postgresql://hermes_state_store_test@127.0.0.1:5432/hermes_state_store_test")
    stores = (open_state_store({}, db_path=tmp_path / "state.db"), open_state_store(_config()))
    observations = []
    root, tip = f"visibility-root-{uuid.uuid4()}", f"visibility-tip-{uuid.uuid4()}"
    hidden, archived, excluded, pinned = (f"visibility-{name}-{uuid.uuid4()}" for name in ("hidden", "archived", "excluded", "pinned"))
    try:
        for store in stores:
            store.ensure_session(root, source="visible")
            store.append_message(root, role="user", content="root")
            store.end_session(root, "compression")
            store.ensure_session(tip, source="visible", metadata={"parent_session_id": root})
            store.append_message(tip, role="assistant", content="tip")
            store.ensure_session(hidden, source="visible")
            store.ensure_session(archived, source="visible")
            store.ensure_session(excluded, source="excluded")
            store.ensure_session(pinned, source="visible")
            assert store.set_session_hidden(hidden, True)
            assert store.set_session_archived(root, True)
            assert store.set_session_pinned(pinned, True)
            assert store.set_session_pinned(hidden, True)
            assert store.get_session(root)["archived"] is True
            assert store.get_session(tip)["archived"] is True
            assert store.get_session(hidden)["hidden"] is False
            assert store.set_session_archived("missing", True) is False
            assert store.set_session_pinned("missing", True) is False
            normal = store.list_session_summaries(source="visible", limit=2)
            with_pins = store.list_session_summaries(source="visible", limit=2, include_pinned=True)
            archived_rows = store.list_session_summaries(source="visible", archived_only=True, include_hidden=True)
            visible_rows = store.list_session_summaries(source="visible", exclude_sources=("excluded",), include_hidden=True)
            all_rows = store.list_session_summaries(source="visible", include_archived=True, include_hidden=True)
            observations.append({
                "normal_ids": [row["id"] for row in normal],
                "with_pins_ids": [row["id"] for row in with_pins],
                "archived_ids": sorted(row["id"] for row in archived_rows),
                "visible_ids": sorted(row["id"] for row in visible_rows),
                "tip_message_count": next(row["message_count"] for row in all_rows if row["id"] == tip),
                "boolean_types": [type(row[key]) is bool for row in with_pins for key in ("hidden", "archived", "pinned")],
            })
        assert observations[0] == observations[1]
        assert observations[0]["normal_ids"] == observations[0]["with_pins_ids"][:2]
        assert pinned in observations[0]["with_pins_ids"]
        assert observations[0]["archived_ids"] == sorted([root, tip])
        assert excluded not in observations[0]["visible_ids"]
        assert observations[0]["tip_message_count"] == 1
        assert all(observations[0]["boolean_types"])
    finally:
        for store in stores:
            store.close()


def test_sqlite_and_postgresql_active_message_record_contract_parity(monkeypatch, tmp_path):
    monkeypatch.setenv(_DSN_ENV, "postgresql://hermes_state_store_test@127.0.0.1:5432/hermes_state_store_test")
    stores = (open_state_store({}, db_path=tmp_path / "state.db"), open_state_store(_config()))
    expected_records = [
        MessageRecord(
            role="assistant", content=[{"type": "text", "text": "structured"}],
            tool_call_id="call-1", tool_calls=[{"id": "call-1", "type": "function"}], tool_name="tool",
            effect_disposition="applied", timestamp=1234.5, token_count=17, finish_reason="stop",
            reasoning="because", reasoning_content="details", reasoning_details={"trace": [1]},
            codex_reasoning_items=[{"kind": "reasoning"}], codex_message_items=[{"kind": "message"}],
            platform_message_id="platform-1", observed=True, _compressed_summary=True, api_content="exact api",
            display_kind="tool_result", display_metadata={"task_count": 1},
        ),
        MessageRecord(role="tool", content=None, timestamp=1235.5),
    ]
    observations = []
    try:
        for store in stores:
            session_id = f"message-record-{uuid.uuid4()}"
            store.ensure_session(session_id, source="integration")
            assert store.append_message_records(session_id, expected_records) == 2
            records = store.get_message_records(session_id)
            assert [record["id"] for record in records] == sorted(record["id"] for record in records)
            observations.append([{key: value for key, value in record.items() if key not in {"id", "session_id"}} for record in records])
        assert observations[0] == observations[1]
        assert observations[0][0]["content"] == expected_records[0].content
        assert observations[0][1]["content"] is None
    finally:
        for store in stores:
            store.close()


def test_message_record_batch_failure_is_atomic_for_both_backends(monkeypatch, tmp_path):
    monkeypatch.setenv(_DSN_ENV, "postgresql://hermes_state_store_test@127.0.0.1:5432/hermes_state_store_test")
    stores = (open_state_store({}, db_path=tmp_path / "state.db"), open_state_store(_config()))
    try:
        for store in stores:
            session_id = f"message-record-atomic-{uuid.uuid4()}"
            with pytest.raises(Exception):
                store.append_message_records(session_id, [MessageRecord(role="user", content="must rollback")])
            assert store.get_message_records(session_id) == []
    finally:
        for store in stores:
            store.close()


def test_sqlite_and_postgresql_content_addressed_system_prompt_parity(monkeypatch, tmp_path):
    monkeypatch.setenv(_DSN_ENV, "postgresql://hermes_state_store_test@127.0.0.1:5432/hermes_state_store_test")
    stores = (open_state_store({}, db_path=tmp_path / "state.db"), open_state_store(_config()))
    prompt = "System policy\\n" + ("cache-safe instruction\\n" * 4)
    observations = []
    try:
        for store in stores:
            first, second = f"prompt-first-{uuid.uuid4()}", f"prompt-second-{uuid.uuid4()}"
            store.ensure_session(first, source="integration")
            store.ensure_session(second, source="integration")
            store.set_system_prompt(first, prompt)
            store.set_system_prompt(second, prompt)
            assert store.get_system_prompt(first) == prompt
            assert store.get_session(first)["system_prompt"] == prompt
            store.set_system_prompt(first, None)
            store.set_system_prompt(f"missing-{uuid.uuid4()}", "orphan must roll back")
            assert store.get_system_prompt(f"missing-{uuid.uuid4()}") is None
            observations.append((store.get_system_prompt(first), store.get_system_prompt(second)))
        assert observations == [(None, prompt), (None, prompt)]
    finally:
        for store in stores:
            store.close()


def test_postgresql_fresh_migration_contract_is_linear_idempotent_and_catalog_complete(monkeypatch):
    dsn = "postgresql://hermes_state_store_test@127.0.0.1:5432/hermes_state_store_test"
    monkeypatch.setenv(_DSN_ENV, dsn)
    _reset_schema(dsn)
    try:
        store = open_state_store(_config())
        store.close()
        assert _migration_versions(dsn) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
        with _psycopg().connect(dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = 'hermes_state_store_slice' AND table_name = 'sessions'")
            columns = {row[0] for row in cursor.fetchall()}
            assert {"id", "source", "started_at", "parent_session_id", "system_prompt_hash", "title", "title_source", "hidden", "archived", "pinned"} <= columns
            cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = 'hermes_state_store_slice' AND table_name = 'system_prompts'")
            assert {"hash", "prompt"} <= {row[0] for row in cursor.fetchall()}
            cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = 'hermes_state_store_slice' AND table_name = 'messages'")
            message_columns = {row[0] for row in cursor.fetchall()}
            assert {"tool_calls", "reasoning_details", "display_metadata", "active", "compacted"} <= message_columns
            cursor.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'hermes_state_store_slice'")
            assert {"messages_session_id_id", "messages_resume_projection", "sessions_source_session_key", "sessions_parent_session_id", "sessions_title_unique", "sessions_visibility_started_at", "sessions_pinned_started_at"} <= {row[0] for row in cursor.fetchall()}
            cursor.execute("SELECT conname, convalidated FROM pg_constraint WHERE conrelid = 'hermes_state_store_slice.sessions'::regclass AND contype = 'f' ORDER BY conname")
            assert cursor.fetchall() == [
                ("sessions_parent_session_id_fkey", False),
                ("sessions_system_prompt_hash_fkey", True),
            ]
            cursor.execute(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'hermes_state_store_slice.conversation_generations'::regclass "
                "AND contype = 'p'"
            )
            assert cursor.fetchall() == [("conversation_generations_pkey",)]
        store = open_state_store(_config())
        store.close()
        assert _migration_versions(dsn) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
        with _psycopg().connect(dsn, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute("ALTER TABLE hermes_state_store_slice.conversation_generations DROP CONSTRAINT conversation_generations_pkey")
            cursor.execute("ALTER TABLE hermes_state_store_slice.conversation_generations ADD CONSTRAINT conversation_generations_pkey PRIMARY KEY (session_key, source)")
        with pytest.raises(StateStoreConfigurationError, match="primary key must be"):
            open_state_store(_config())
    finally:
        _reset_schema(dsn)


def test_postgresql_upgrade_migrations_accept_v2_and_legacy_v5_ledgers(monkeypatch):
    dsn = "postgresql://hermes_state_store_test@127.0.0.1:5432/hermes_state_store_test"
    monkeypatch.setenv(_DSN_ENV, dsn)
    _reset_schema(dsn)
    try:
        _seed_v2_schema(dsn, (1, 2))
        with _psycopg().connect(dsn, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO hermes_state_store_slice.sessions (id, source, started_at) VALUES ('survives-v2', 'fixture', 1)")
        store = open_state_store(_config())
        session = store.get_session("survives-v2")
        assert session is not None
        assert session["source"] == "fixture"
        store.close()
        assert _migration_versions(dsn) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]

        _reset_schema(dsn)
        _seed_v2_schema(dsn, (1, 2, 3, 4))
        with _psycopg().connect(dsn, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute("ALTER TABLE hermes_state_store_slice.sessions ADD CONSTRAINT sessions_parent_session_id_fkey FOREIGN KEY (parent_session_id) REFERENCES hermes_state_store_slice.sessions(id) NOT VALID")
        store = open_state_store(_config())
        store.close()
        assert _migration_versions(dsn) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]

        _reset_schema(dsn)
        _seed_v2_schema(dsn, (1, 2, 5))
        with _psycopg().connect(dsn, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute("ALTER TABLE hermes_state_store_slice.sessions ADD COLUMN title text")
            cursor.execute("ALTER TABLE hermes_state_store_slice.sessions ADD COLUMN title_source text")
            cursor.execute("ALTER TABLE hermes_state_store_slice.sessions ADD COLUMN hidden boolean NOT NULL DEFAULT false")
            cursor.execute("CREATE UNIQUE INDEX sessions_title_unique ON hermes_state_store_slice.sessions (title) WHERE title IS NOT NULL")
            cursor.execute("ALTER TABLE hermes_state_store_slice.sessions ADD CONSTRAINT sessions_parent_session_id_fkey FOREIGN KEY (parent_session_id) REFERENCES hermes_state_store_slice.sessions(id) NOT VALID")
        store = open_state_store(_config())
        store.close()
        assert _migration_versions(dsn) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
        with _psycopg().connect(dsn, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute("DROP INDEX hermes_state_store_slice.sessions_title_unique")
        with pytest.raises(StateStoreConfigurationError, match="sessions_title_unique"):
            open_state_store(_config())
    finally:
        _reset_schema(dsn)


def test_sqlite_and_postgresql_generation_lifecycle_parity_and_aba_survival(monkeypatch, tmp_path):
    """Reset generation is source/key scoped, first-stamp-wins, and outlives sessions."""
    dsn = "postgresql://hermes_state_store_test@127.0.0.1:5432/hermes_state_store_test"
    monkeypatch.setenv(_DSN_ENV, dsn)
    stores = (open_state_store({}, db_path=tmp_path / "state.db"), open_state_store(_config()))
    observations = []
    try:
        for store in stores:
            raw_store = cast(Any, store)
            source, key = f"generation-source-{uuid.uuid4()}", f"generation-key-{uuid.uuid4()}"
            session = f"generation-session-{uuid.uuid4()}"
            store.ensure_session(session, source=source, metadata={"session_key": key})
            assert store.latest_conversation_boundary(key, source) is None
            store.end_session(session, "compression")
            assert store.latest_conversation_boundary(key, source) is None
            assert store.promote_to_session_reset(session) is False

            promoted = f"generation-promoted-{uuid.uuid4()}"
            store.ensure_session(promoted, source=source, metadata={"session_key": key})
            store.end_session(promoted, "agent_close")
            assert store.promote_to_session_reset(promoted)
            assert store.promote_to_session_reset(promoted) is False
            store.end_session(promoted, "idle")
            ended = store.get_session(promoted)
            assert ended is not None and ended["end_reason"] == "session_reset"
            assert store.latest_conversation_boundary(key, source) == 1

            unkeyed = f"generation-unkeyed-{uuid.uuid4()}"
            store.ensure_session(unkeyed, source=source)
            store.end_session(unkeyed, "session_reset")
            assert store.latest_conversation_boundary(key, source) == 1
            assert store.latest_conversation_boundary(key, f"other-{source}") is None

            if hasattr(store, "_session_db"):
                assert raw_store._session_db.delete_session(promoted)
            else:
                with _psycopg().connect(dsn) as connection, connection.cursor() as cursor:
                    cursor.execute("DELETE FROM hermes_state_store_slice.sessions WHERE id = %s", (promoted,))
                    connection.commit()
            successor = f"generation-successor-{uuid.uuid4()}"
            store.ensure_session(successor, source=source, metadata={"session_key": key})
            store.end_session(successor, "session_reset")
            successor_row = store.get_session(successor)
            assert successor_row is not None
            observations.append((store.latest_conversation_boundary(key, source), successor_row["end_reason"]))
        assert observations == [(2, "session_reset"), (2, "session_reset")]
    finally:
        for store in stores:
            store.close()


def test_postgresql_generation_end_and_promotion_are_atomic_under_concurrency(monkeypatch):
    """Concurrent close/promotion commits one reset and one generation, never a half-boundary."""
    dsn = "postgresql://hermes_state_store_test@127.0.0.1:5432/hermes_state_store_test"
    monkeypatch.setenv(_DSN_ENV, dsn)
    store = open_state_store(_config())
    competing_store = open_state_store(_config())
    source, key = f"concurrent-source-{uuid.uuid4()}", f"concurrent-key-{uuid.uuid4()}"
    session = f"concurrent-session-{uuid.uuid4()}"
    try:
        store.ensure_session(session, source=source, metadata={"session_key": key})
        start = Barrier(2)

        def close() -> None:
            start.wait()
            store.end_session(session, "agent_close")

        def promote() -> bool:
            start.wait()
            return competing_store.promote_to_session_reset(session)

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda operation: operation(), (close, promote)))
        session_row = store.get_session(session)
        assert session_row is not None and session_row["end_reason"] == "session_reset"
        assert store.latest_conversation_boundary(key, source) == 1

        duplicate = f"duplicate-session-{uuid.uuid4()}"
        store.ensure_session(duplicate, source=source, metadata={"session_key": key})
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _: store.end_session(duplicate, "session_reset"), range(2)))
        duplicate_row = store.get_session(duplicate)
        assert duplicate_row is not None and duplicate_row["end_reason"] == "session_reset"
        assert store.latest_conversation_boundary(key, source) == 2
    finally:
        store.close()
        competing_store.close()


def test_postgresql_generation_update_rolls_back_with_failed_boundary(monkeypatch):
    """A failed transaction cannot leave a generation whose end stamp vanished."""
    dsn = "postgresql://hermes_state_store_test@127.0.0.1:5432/hermes_state_store_test"
    monkeypatch.setenv(_DSN_ENV, dsn)
    store = open_state_store(_config())
    source, key = f"rollback-source-{uuid.uuid4()}", f"rollback-key-{uuid.uuid4()}"
    session = f"rollback-session-{uuid.uuid4()}"
    raw_store = cast(Any, store)
    original = raw_store._bump_conversation_generation
    try:
        store.ensure_session(session, source=source, metadata={"session_key": key})
        raw_store._bump_conversation_generation = lambda *_args: (_ for _ in ()).throw(RuntimeError("inject rollback"))
        with pytest.raises(RuntimeError, match="inject rollback"):
            store.end_session(session, "session_reset")
        session_row = store.get_session(session)
        assert session_row is not None and session_row["end_reason"] is None
        assert store.latest_conversation_boundary(key, source) is None
        promotion = f"rollback-promotion-{uuid.uuid4()}"
        store.ensure_session(promotion, source=source, metadata={"session_key": key})
        assert store.promote_to_session_reset(promotion) is False
        promotion_row = store.get_session(promotion)
        assert promotion_row is not None and promotion_row["end_reason"] is None
        assert store.latest_conversation_boundary(key, source) is None
    finally:
        raw_store._bump_conversation_generation = original
        store.close()


def test_sqlite_and_postgresql_resume_projection_and_lineage_parity(monkeypatch, tmp_path):
    monkeypatch.setenv(_DSN_ENV, "postgresql://hermes_state_store_test@127.0.0.1:5432/hermes_state_store_test")
    stores = (open_state_store({}, db_path=tmp_path / "state.db"), open_state_store(_config()))
    observations = []
    root, tip = f"resume-root-{uuid.uuid4()}", f"resume-tip-{uuid.uuid4()}"
    try:
        for store in stores:
            store.ensure_session(root, source="integration")
            store.append_message_record(root, MessageRecord(role="user", content="before compression", timestamp=1))
            store.end_session(root, "compression")
            store.ensure_session(tip, source="integration", metadata={"parent_session_id": root})
            store.append_message_record(tip, MessageRecord(role="assistant", content="after compression", timestamp=2))
            model, display = store.get_resume_conversations(tip)
            observations.append({
                "tip": store.get_compression_tip(root),
                "lineage": store.get_compression_lineage(tip),
                "root": store.get_conversation_root(tip),
                "model": [(row["role"], row["content"]) for row in model],
                "display": [(row["role"], row["content"]) for row in display],
                "ancestor": [(row["role"], row["content"]) for row in store.get_ancestor_display_prefix(tip)],
                "all_count": store.get_resume_message_count(tip),
                "tip_count": store.get_resume_message_count(tip, tip_only=True),
                "guard": store.assert_resume_safe(tip, 2),
            })
        assert observations[0] == observations[1]
        assert observations[0]["tip"] == observations[0]["lineage"][-1]
        assert observations[0]["root"] == observations[0]["lineage"][0]
        assert observations[0]["model"] == [("assistant", "after compression")]
        assert observations[0]["display"] == [("user", "before compression"), ("assistant", "after compression")]
        assert observations[0]["ancestor"] == [("user", "before compression")]
        assert observations[0]["all_count"] == observations[0]["guard"] == 2
        assert observations[0]["tip_count"] == 1
        for store, observation in zip(stores, observations):
            with pytest.raises(Exception):
                store.assert_resume_safe(observation["lineage"][-1], 1)
    finally:
        for store in stores:
            store.close()


def test_sqlite_and_postgresql_token_usage_transport_parity(monkeypatch, tmp_path):
    monkeypatch.setenv(_DSN_ENV, "postgresql://hermes_state_store_test@127.0.0.1:5432/hermes_state_store_test")
    stores = (open_state_store({}, db_path=tmp_path / "state.db"), open_state_store(_config()))
    observations = []
    try:
        for store in stores:
            session_id = f"usage-{uuid.uuid4()}"
            route = dict(model="m1", billing_provider="p1", billing_base_url="url", billing_mode="mode")
            store.queue_token_counts(session_id, input_tokens=2, api_call_count=1, **route)
            store.queue_token_counts(session_id, input_tokens=3, api_call_count=1, **route)
            store.queue_token_counts(session_id, input_tokens=50, api_call_count=3, absolute=True, **route)
            assert store.flush_token_counts()
            store.record_auxiliary_usage(session_id, "vision", model="aux", billing_provider="auxp", input_tokens=7)
            if hasattr(store, "_connection"):
                with store._connection() as connection, connection.cursor() as cursor:
                    cursor.execute("SELECT input_tokens, api_call_count FROM hermes_state_store_slice.sessions WHERE id=%s", (session_id,))
                    observations.append(cursor.fetchone())
                    cursor.execute("SELECT task, model, input_tokens FROM hermes_state_store_slice.session_model_usage WHERE session_id=%s ORDER BY task", (session_id,))
                    assert cursor.fetchall() == [("", "m1", 5), ("vision", "aux", 7)]
            else:
                rows = store._session_db._read_all("SELECT task, model, input_tokens FROM session_model_usage WHERE session_id=? ORDER BY task", (session_id,))
                assert [(row["task"], row["model"], row["input_tokens"]) for row in rows] == [("", "m1", 5), ("vision", "aux", 7)]
                summary = store._session_db._read_one("SELECT input_tokens, api_call_count FROM sessions WHERE id=?", (session_id,))
                observations.append((summary["input_tokens"], summary["api_call_count"]))
        assert observations == [(50, 3), (50, 3)]
    finally:
        for store in stores:
            store.close()


def test_postgresql_token_usage_delta_rolls_back_summary_when_attribution_fails(monkeypatch):
    monkeypatch.setenv(_DSN_ENV, "postgresql://hermes_state_store_test@127.0.0.1:5432/hermes_state_store_test")
    store = open_state_store(_config())
    session_id = f"usage-rollback-{uuid.uuid4()}"
    try:
        store.ensure_session(session_id)
        monkeypatch.setattr(store, "_record_model_usage", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("injected attribution failure")))
        with pytest.raises(RuntimeError, match="injected attribution failure"):
            store.update_token_counts(session_id, input_tokens=9, model="m", billing_provider="p", api_call_count=1)
        with store._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT input_tokens, api_call_count FROM hermes_state_store_slice.sessions WHERE id=%s", (session_id,))
            assert cursor.fetchone() == (0, 0)
            cursor.execute("SELECT COUNT(*) FROM hermes_state_store_slice.session_model_usage WHERE session_id=%s", (session_id,))
            assert cursor.fetchone() == (0,)
    finally:
        store.close()
