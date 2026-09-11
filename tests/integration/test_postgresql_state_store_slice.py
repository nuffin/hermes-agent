"""Real PostgreSQL contract for the first session/message State Store slice."""

from __future__ import annotations

import json
import os
import uuid

import pytest

from state_store import open_state_store


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
