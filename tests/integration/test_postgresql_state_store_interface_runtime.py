"""Runtime conformance of the PostgreSQL CLI facade to StateStoreInterface."""
from __future__ import annotations

from cli_session_store import open_cli_session_store
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from state_store_interface import StateStoreInterface


_DSN = "postgresql://hermes_state_store_test@127.0.0.1:5432/hermes_state_store_test"
_CONFIG = {
    "state_store": {
        "backend": "postgresql",
        "postgresql": {
            "dsn_env": "HERMES_STATE_STORE_TEST_DSN",
            "connect_timeout_seconds": 5,
            "pool_max_size": 2,
        },
    },
}


def test_postgresql_cli_facade_is_runtime_interface_compatible(tmp_path, monkeypatch, postgresql_test_target):
    home = tmp_path / ".hermes-interface-runtime"
    home.mkdir()
    monkeypatch.setenv("HERMES_STATE_STORE_TEST_DSN", _DSN)
    import state_store

    monkeypatch.setattr(state_store, "postgresql_tenant_schema", lambda *_args, **_kwargs: postgresql_test_target.schema)
    token = set_hermes_home_override(str(home))
    store = open_cli_session_store(_CONFIG)
    session_id = "interface-runtime-session"
    try:
        assert isinstance(store, StateStoreInterface)
        store.create_session(
            session_id,
            "cli",
            model="test-model",
            model_config={"provider": "local"},
            system_prompt="runtime interface prompt",
            cwd=str(tmp_path),
            profile_name="pg-interface-test",
        )
        assert store.sanitize_title("  interface\ncontract  ") == "interface contract"
        assert store.set_auto_title(session_id, "Interface runtime", source="llm") is True
        assert store.get_session_title(session_id) == "Interface runtime"
        assert store.get_session_title_source(session_id) == "llm"
        assert store.refresh_auto_title(session_id, "Interface runtime refreshed", source="llm") is True
        assert store.get_session(session_id)["id"] == session_id

        store.append_message(session_id, "user", "PostgreSQL interface runtime evidence")
        conversation = store.get_messages_as_conversation(session_id)
        assert conversation and conversation[-1]["content"] == "PostgreSQL interface runtime evidence"
        assert store.search_messages("interface runtime")
        assert store.has_archived_messages(session_id) is False
        assert store.set_topic_session_title(session_id) is None

        cleared = store.clear_stored_system_prompts()
        assert cleared["cleared"] == 1
        assert store.get_session(session_id)["system_prompt"] is None
    finally:
        store.close()
        reset_hermes_home_override(token)
