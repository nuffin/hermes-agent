"""Executable isolated PostgreSQL CLI session lifecycle contract."""
from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_session_store import PostgreSQLCLISessionCapabilityError, open_cli_session_store
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from state_store_runtime_readiness import trap_state_db_opens

_DSN = "postgresql://hermes_state_store_test@127.0.0.1:5432/hermes_state_store_test"
_CONFIG = {"state_store": {"backend": "postgresql", "postgresql": {
    "dsn_env": "HERMES_STATE_STORE_TEST_DSN", "connect_timeout_seconds": 5, "pool_max_size": 2,
}}}


def _psycopg():
    return importlib.import_module("psycopg")


@pytest.fixture
def pg_cli_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes-dev-postgresql-state-store"
    home.mkdir()
    (home / "config.yaml").write_text(
        "state_store:\n  backend: postgresql\n  postgresql:\n    dsn_env: HERMES_STATE_STORE_TEST_DSN\n    connect_timeout_seconds: 5\n    pool_max_size: 2\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_STATE_STORE_TEST_DSN", _DSN)
    token = set_hermes_home_override(str(home))
    stores = []
    try:
        yield home, stores
    finally:
        for store in stores:
            try:
                schema = store._store._schema
                store.close()
                with _psycopg().connect(_DSN, autocommit=True) as conn, conn.cursor() as cursor:
                    cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            except Exception:
                pass
        reset_hermes_home_override(token)


def _open(stores):
    store = open_cli_session_store(_CONFIG)
    stores.append(store)
    return store


def test_fresh_end_resume_prompt_messages_and_search_never_open_sqlite(pg_cli_home, monkeypatch):
    home, stores = pg_cli_home
    session_id = "20260914_010203_pgcli"
    with trap_state_db_opens(home) as opens:
        # Exercise HermesCLI's real early acquisition seam, without a provider or TUI.
        import cli
        shell = cli.HermesCLI.__new__(cli.HermesCLI)
        monkeypatch.setattr(cli, "CLI_CONFIG", _CONFIG)
        shell._init_session_store()
        fresh = shell._session_db
        assert fresh is not None
        stores.append(fresh)
        fresh.create_session(session_id, "cli", model="test-model", model_config={"provider": "local"},
                             system_prompt="stable system prompt", cwd="/tmp", profile_name="pg-cli-test")
        single_id = fresh.append_message(
            session_id, "user", {"text": "remember postgresql resume"},
            platform_message_id="single-identity", timestamp=1_789_000_000,
            display_metadata={"origin": "single"},
        )
        assert single_id > 0
        assert fresh.append_messages_batch(session_id, [
            {"role": "assistant", "content": "persisted answer", "finish_reason": "stop"},
            {"role": "tool", "content": None, "tool_call_id": "null-content"},
        ]) == 2
        records = fresh._store.get_message_records(session_id)
        assert [(row["content"], row["platform_message_id"], row["tool_call_id"])
                for row in records] == [
            ({"text": "remember postgresql resume"}, "single-identity", None),
            ("persisted answer", None, None),
            (None, None, "null-content"),
        ]
        with pytest.raises(PostgreSQLCLISessionCapabilityError, match="single-message controls"):
            fresh.append_message(session_id, "user", "must not write", compression_lock_holder="sqlite-only")
        assert len(fresh._store.get_message_records(session_id)) == 3
        fresh.end_session(session_id, "cli_close")
        assert fresh.get_session(session_id)["ended_at"] is not None
        fresh.close()

        resumed = _open(stores)
        assert resumed.get_session(session_id)["system_prompt"] == "stable system prompt"
        restored, display = resumed.get_resume_conversations(session_id)
        assert [row["content"] for row in restored] == [{"text": "remember postgresql resume"}, "persisted answer", None]
        assert [row["content"] for row in display] == [{"text": "remember postgresql resume"}, "persisted answer", None]
        resumed.reopen_session(session_id)
        assert resumed.get_session(session_id)["ended_at"] is None
        assert resumed.search_sessions(source="cli")[0]["id"] == session_id
        resumed.set_session_title(session_id, "PG resume title")
        assert resumed.resolve_session_by_title("PG resume title") == session_id
        assert resumed.search_sessions(source="cli", workspace_key="/tmp")[0]["id"] == session_id
        before_rotation = {
            "session": dict(resumed.get_session(session_id)),
            "messages": list(resumed._store.get_message_records(session_id)),
        }
        with pytest.raises(PostgreSQLCLISessionCapabilityError, match="no SQLite fallback"):
            resumed.archive_and_compact(session_id)
        assert resumed.get_session(session_id) == before_rotation["session"]
        assert resumed._store.get_message_records(session_id) == before_rotation["messages"]
    assert opens == []
    assert not (home / "state.db").exists()


def test_cli_delete_contract_removes_postgresql_session_without_sqlite(pg_cli_home):
    home, stores = pg_cli_home
    with trap_state_db_opens(home) as opens:
        store = _open(stores)
        store.create_session("delete-pg-cli", "cli")
        store.append_messages_batch("delete-pg-cli", [{"role": "user", "content": "remove me"}])
        assert store.delete_session("delete-pg-cli", sessions_dir=home / "sessions")
        assert store.get_session("delete-pg-cli") is None
    assert opens == []
    assert not (home / "state.db").exists()


def test_agent_lazy_recall_acquisition_persists_and_resumes_without_sqlite(pg_cli_home):
    """The real AIAgent lifecycle selects the CLI PG facade, not the SQLite registry."""
    from run_agent import AIAgent

    home, stores = pg_cli_home
    session_id = "20260914_010203_pg_agent"
    tool_defs = [{"type": "function", "function": {
        "name": "local_test", "description": "offline test tool",
        "parameters": {"type": "object", "properties": {}},
    }}]
    with (
        trap_state_db_opens(home) as opens,
        patch("model_tools.get_tool_definitions", return_value=tool_defs),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890", base_url="http://127.0.0.1/offline",
            quiet_mode=True, skip_context_files=True, skip_memory=True, session_id=session_id,
        )
        agent.client = MagicMock()
        with patch("hermes_cli.config.load_config", return_value=_CONFIG):
            store = agent._get_session_db_for_recall()
        assert store.__class__.__name__ == "PostgreSQLCLISessionStore"
        stores.append(store)
        agent._ensure_db_session()
        assert agent._session_db_created
        agent._touch_activity("selected-pg fake-agent preflight", force_persist=True)
        assert store.get_session(session_id)["last_activity_description"] == "selected-pg fake-agent preflight"
        agent._reset_activity_labels_after_turn()
        assert store.get_session(session_id)["last_activity_description"] == ""
        prompt = store.get_session(session_id)["system_prompt"]
        assert prompt == agent._cached_system_prompt

        messages = [
            {"role": "user", "content": "fresh agent turn"},
            {"role": "assistant", "content": "persisted agent answer", "finish_reason": "stop"},
        ]
        assert agent._flush_messages_to_session_db(messages, []) is True
        assert [row["content"] for row in store.get_messages_as_conversation(session_id)] == [
            "fresh agent turn", "persisted agent answer",
        ]
        with pytest.raises(PostgreSQLCLISessionCapabilityError, match="does not implement compression or turn lease"):
            store.append_messages_batch(
                session_id, [{"role": "user", "content": "must not write"}], turn_lease_holder="active-lease")
        assert len(store._store.get_message_records(session_id)) == 2
        store.end_session(session_id, "agent_close")
        store.close()

        resumed = _open(stores)
        assert resumed.get_session(session_id)["system_prompt"] == prompt
        restored, _display = resumed.get_resume_conversations(session_id)
        assert [row["content"] for row in restored] == ["fresh agent turn", "persisted agent answer"]
        resumed.reopen_session(session_id)
        assert resumed.get_session(session_id)["ended_at"] is None
    assert opens == []
    assert not (home / "state.db").exists()


def test_default_sqlite_factory_path_remains_legacy(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import hermes_state
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", home / "state.db")
    store = open_cli_session_store({})
    try:
        assert store.db_path == home / "state.db"
        assert (home / "state.db").exists()
    finally:
        store.close()
