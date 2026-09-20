"""Executable isolated PostgreSQL CLI session lifecycle contract."""
from __future__ import annotations


import json
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from cli_session_store import PostgreSQLCLISessionCapabilityError, open_cli_session_store
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from state_store_runtime_readiness import trap_state_db_opens

_DSN = "postgresql://hermes_state_store_test@127.0.0.1:5432/hermes_state_store_test"
_CONFIG = {"state_store": {"backend": "postgresql", "postgresql": {
    "dsn_env": "HERMES_STATE_STORE_TEST_DSN", "connect_timeout_seconds": 5, "pool_max_size": 2,
}}}

pytestmark = pytest.mark.integration


@pytest.fixture
def pg_cli_home(tmp_path, monkeypatch, postgresql_test_target):
    home = tmp_path / ".hermes-dev-postgresql-state-store"
    home.mkdir()
    (home / "config.yaml").write_text(
        "state_store:\n  backend: postgresql\n  postgresql:\n    dsn_env: HERMES_STATE_STORE_TEST_DSN\n    connect_timeout_seconds: 5\n    pool_max_size: 2\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_STATE_STORE_TEST_DSN", _DSN)
    import state_store
    monkeypatch.setattr(state_store, "postgresql_tenant_schema", lambda *_args, **_kwargs: postgresql_test_target.schema)
    token = set_hermes_home_override(str(home))
    stores = []
    try:
        yield home, stores
    finally:
        for store in stores:
            try:
                store.close()
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


def test_postgresql_cli_history_listing_and_bare_resume_never_open_sqlite(pg_cli_home, monkeypatch, capsys):
    """The CLI list and bare ``/resume`` enumerate the selected PG profile history."""
    from cli import HermesCLI
    from hermes_cli.sessions_cmd import cmd_sessions

    home, stores = pg_cli_home
    with trap_state_db_opens(home) as opens:
        store = _open(stores)
        store.create_session("pg-cli-history", "cli", cwd="/tmp/pg-history")
        store.set_session_title("pg-cli-history", "PostgreSQL history")
        store.append_message("pg-cli-history", "user", "persisted PostgreSQL preview", timestamp=100)
        store.create_session("pg-oneshot-history", "oneshot", cwd="/tmp/pg-oneshot")
        store.set_session_title("pg-oneshot-history", "PostgreSQL one-shot history")
        store.append_message("pg-oneshot-history", "user", "persisted one-shot preview", timestamp=101)
        store.create_session("pg-gateway-history", "telegram", cwd="/tmp/pg-gateway")
        store.set_session_title("pg-gateway-history", "Gateway history must stay hidden")
        store.append_message("pg-gateway-history", "user", "gateway preview", timestamp=102)
        rows = store.list_sessions_rich(source="cli", limit=10, order_by_last_active=True)
        assert rows[0]["id"] == "pg-cli-history"
        assert {"id", "source", "title", "preview", "last_active", "message_count", "cwd"} <= rows[0].keys()
        assert rows[0]["preview"] == "persisted PostgreSQL preview"
        assert rows[0]["message_count"] == 1

        # The exact listing facade drives both the interactive renderer and the
        # command-line list action; no SessionDB() is permitted for this profile.
        shell = HermesCLI.__new__(HermesCLI)
        shell.session_id = "current-session"
        shell._session_db = store
        shell._pending_resume_sessions = None
        shell.conversation_history = []
        shell.agent = None
        shell._handle_resume_command("/resume")
        pending_ids = [row["id"] for row in shell._pending_resume_sessions]
        assert {"pg-cli-history", "pg-oneshot-history"} <= set(pending_ids)
        assert "pg-gateway-history" not in pending_ids
        assert shell._resolve_resume_target("2")[0] == pending_ids[1]
        resume_output = capsys.readouterr().out
        assert "PostgreSQL history" in resume_output
        assert "PostgreSQL one-shot history" in resume_output
        assert "Gateway history must stay hidden" not in resume_output

        monkeypatch.setattr("hermes_cli.config.load_config", lambda: _CONFIG)
        result = cmd_sessions(SimpleNamespace(
            sessions_action="list", limit=10, source=None, workspace=None,
        ))
        assert result is None
        listed = capsys.readouterr().out
        assert "PostgreSQL history" in listed
        assert "pg-cli-history" in listed

        assert cmd_sessions(SimpleNamespace(sessions_action="stats")) == 2
        unsupported = capsys.readouterr().out
        assert "does not support `hermes sessions stats` yet" in unsupported
        assert "no SQLite fallback" in unsupported
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
        assert store.try_acquire_session_turn_lease(session_id, "active-lease")
        assert store.append_messages_batch(
            session_id, [{"role": "user", "content": "lease-owned write"}], turn_lease_holder="active-lease") == 1
        store.release_session_turn_lease(session_id, "active-lease")
        assert len(store._store.get_message_records(session_id)) == 3
        store.end_session(session_id, "agent_close")
        store.close()

        resumed = _open(stores)
        assert resumed.get_session(session_id)["system_prompt"] == prompt
        restored, _display = resumed.get_resume_conversations(session_id)
        assert [row["content"] for row in restored] == ["fresh agent turn", "persisted agent answer", "lease-owned write"]
        resumed.reopen_session(session_id)
        assert resumed.get_session(session_id)["ended_at"] is None
    assert opens == []
    assert not (home / "state.db").exists()


def test_inline_session_search_uses_selected_postgresql_contextual_contract_without_sqlite(pg_cli_home):
    """The agent's public inline tool keeps the selected tenant and full response shapes."""
    from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
    from run_agent import AIAgent

    home, stores = pg_cli_home
    session_id, history_id = "20260914_010203_pg_search_live", "20260914_010203_pg_search_history"
    with (
        trap_state_db_opens(home) as opens,
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
        patch("hermes_cli.config.load_config", return_value=_CONFIG),
    ):
        agent = AIAgent(api_key="test-key-1234567890", base_url="http://127.0.0.1/offline",
                       quiet_mode=True, skip_context_files=True, skip_memory=True, session_id=session_id)
        store = cast(Any, agent._get_session_db_for_recall())
        stores.append(store)
        store.create_session(history_id, "cli")
        store.set_session_title(history_id, "Selected PostgreSQL contextual history")
        store.append_message(history_id, "user", "opening contextual evidence", timestamp=10)
        anchor = store.append_message(history_id, "assistant", "中文记忆 selected PostgreSQL anchor", timestamp=20)
        store.append_message(history_id, "tool", "tool boundary", timestamp=30)
        store.append_message(history_id, "assistant", "closing contextual evidence", timestamp=40)
        assert store.search_index_status()["backend"] == "postgresql"
        from state_store import contextual_session_search_store
        from tools.session_search_tool import session_search
        assert contextual_session_search_store(store) is store
        direct = json.loads(session_search(query="中文记忆", db=store))
        assert direct["success"] is True, direct
        assert agent._get_session_db_for_recall() is store

        def invoke(args):
            return json.loads(INLINE_TOOL_EXECUTORS["session_search"](
                agent, args, InlineToolContext(effective_task_id="selected-pg-contextual")))

        discovered = invoke({"query": "中文记忆", "detail": "full", "limit": 3})
        assert discovered["success"] is True, discovered
        assert discovered["mode"] == "discover"
        assert discovered["search_index"]["backend"] == "postgresql"
        assert discovered["search_index"]["available"] is True
        hit = next(result for result in discovered["results"] if result["session_id"] == history_id)
        assert hit["match_message_id"] == anchor
        assert [message["content"] for message in hit["messages"]] == [
            "opening contextual evidence", "中文记忆 selected PostgreSQL anchor", "closing contextual evidence",
        ]
        history_started = int(store.get_session(history_id)["started_at"])
        started_bound = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(history_started))
        next_bound = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(history_started + 1))
        bounded = invoke({"query": "selected PostgreSQL", "after": started_bound, "before": next_bound})
        assert bounded["success"] is True, bounded
        assert [result["session_id"] for result in bounded["results"]] == [history_id]
        before_window = invoke({"query": "中文记忆", "before": started_bound})
        assert before_window["success"] is True, before_window
        assert before_window["results"] == []

        scrolled = invoke({"session_id": history_id, "around_message_id": anchor, "window": 1})
        assert scrolled["success"] is True and scrolled["mode"] == "scroll"
        assert [message["id"] for message in scrolled["messages"]] == [anchor - 1, anchor, anchor + 1]
        read = invoke({"session_id": history_id})
        assert read["success"] is True and read["mode"] == "read" and read["message_count"] == 4
        browsed = invoke({})
        assert browsed["success"] is True and browsed["mode"] == "browse"
        assert history_id in [result["session_id"] for result in browsed["results"]]
        grammar_error = invoke({"query": "中文 AND memory"})
        assert grammar_error["success"] is False
        assert "unsupported or ambiguous" in grammar_error["error"]
        rebuilt = store.rebuild_search_index()
        assert rebuilt["backend"] == "postgresql" and rebuilt["available"] is True
        assert invoke({"query": "中文记忆"})["success"] is True
    assert opens == []
    assert not (home / "state.db").exists()


def test_public_agent_turn_rotates_selected_postgresql_with_real_compressor(pg_cli_home):
    """A real public turn reaches ContextCompressor then the PG child publisher."""
    from run_agent import AIAgent

    home, stores = pg_cli_home
    parent = "20260914_010203_pg_rotation_parent"
    history = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"durable history {index}: " + ("evidence " * 500)}
        for index in range(60)
    ]
    main_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content="final deterministic answer", reasoning_content=None, reasoning=None, tool_calls=None,
        ), finish_reason="stop")], model="oracle/model", usage=None,
    )
    summary_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="deterministic provider summary"), finish_reason="stop")]
    )
    with (
        trap_state_db_opens(home) as opens,
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
        patch("agent.context_compressor.call_llm", return_value=summary_response) as fake_provider,
    ):
        agent = AIAgent(
            api_key="test-key-1234567890", base_url="http://127.0.0.1/offline", model="oracle/model",
            quiet_mode=True, skip_context_files=True, skip_memory=True, session_id=parent,
        )
        agent.client = MagicMock()
        agent.client.chat.completions.create.return_value = main_response
        with patch("hermes_cli.config.load_config", return_value=_CONFIG):
            store = agent._get_session_db_for_recall()
        stores.append(store)
        agent._ensure_db_session()
        store.set_session_title(parent, "PostgreSQL rotation title")
        store.set_session_title_source(parent, "user")
        store.patch_session_model_config(parent, {"provider": "deterministic", "oracle": True})
        agent.compression_in_place = False
        agent.context_compressor.threshold_tokens = 1
        agent.context_compressor.note_usage_less_response()
        agent.max_compression_attempts = 1
        result = agent.run_conversation("live public input", conversation_history=history)

        child = agent.session_id
        assert result["completed"] is True
        assert result["final_response"] == "final deterministic answer"
        assert fake_provider.call_count >= 1
        assert child != parent
        parent_row, child_row = store.get_session(parent), store.get_session(child)
        assert parent_row["end_reason"] == "compression" and parent_row["ended_at"] is not None
        assert child_row["parent_session_id"] == parent
        assert child_row["title"] == "PostgreSQL rotation title"
        assert child_row["title_source"] == "user"
        assert child_row["model"] == "oracle/model"
        assert child_row["system_prompt"] == agent._cached_system_prompt
        assert store.get_compression_tip(parent) == child
        assert store.get_conversation_root(child) == parent
        assert store.get_compression_lineage(child) == [parent, child]
        assert store.get_active_message_watermark(child) > 0
        assert store.get_compression_fallback_streak(child) == 0
        assert store.get_compression_ineffective_count(child) == 0
        assert store.get_compression_recovery_deadline(child) == 0.0
        child_messages = store.get_messages_as_conversation(child)
        assert any(row["content"] == "final deterministic answer" for row in child_messages)
        assert any("deterministic provider summary" in str(row["content"]) for row in child_messages)
    assert opens == []
    assert not (home / "state.db").exists()


def test_public_agent_turn_recovers_lost_publish_ack_from_durable_receipt(pg_cli_home):
    """A public retry-classification path adopts the one committed PG child."""
    from run_agent import AIAgent

    home, stores = pg_cli_home
    parent = "20260914_010203_pg_lost_ack_parent"
    history = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"history {index}: " + ("evidence " * 500)}
        for index in range(60)
    ]
    main_response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content="lost acknowledgement recovered", reasoning_content=None, reasoning=None, tool_calls=None,
    ), finish_reason="stop")], model="oracle/model", usage=None)
    summary_response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content="durable receipt summary"), finish_reason="stop")])
    with (
        trap_state_db_opens(home) as opens,
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
        patch("agent.context_compressor.call_llm", return_value=summary_response),
    ):
        agent = AIAgent(api_key="test-key-1234567890", base_url="http://127.0.0.1/offline", model="oracle/model",
                        quiet_mode=True, skip_context_files=True, skip_memory=True, session_id=parent)
        agent.client = MagicMock()
        agent.client.chat.completions.create.return_value = main_response
        with patch("hermes_cli.config.load_config", return_value=_CONFIG):
            store = agent._get_session_db_for_recall()
        stores.append(store)
        agent._ensure_db_session()
        original_publish = store.publish_compression_child
        request_ids = []
        def commit_then_lose_ack(**kwargs):
            request_ids.append(kwargs["request_id"])
            original_publish(**kwargs)
            raise ConnectionError("simulated lost acknowledgement after commit")
        store.publish_compression_child = commit_then_lose_ack
        agent.compression_in_place = False
        agent.context_compressor.threshold_tokens = 1
        agent.context_compressor.note_usage_less_response()
        agent.max_compression_attempts = 1
        result = agent.run_conversation("lost ack public input", conversation_history=history)
        assert result["completed"] is True and len(request_ids) == 1
        receipt = store.get_compression_publication_receipt(request_ids[0])
        assert receipt is not None
        assert receipt["parent_session_id"] == parent == store.get_session(receipt["child_session_id"])["parent_session_id"]
        assert agent.session_id == receipt["child_session_id"]
        with store._store._connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT count(*) FROM {store._store._schema}.sessions WHERE parent_session_id=%s", (parent,))
            assert cursor.fetchone()[0] == 1
    assert opens == []
    assert not (home / "state.db").exists()


def test_public_provider_abort_reopens_selected_postgresql_without_receipt_replay(pg_cli_home):
    """A public provider cancellation leaves the PG parent writable and retryable."""
    from agent.auxiliary_client import AuxiliaryExplicitCancellation
    from run_agent import AIAgent

    home, stores = pg_cli_home
    parent = "20260914_010203_pg_provider_abort_parent"
    history = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"abort history {index}: " + ("evidence " * 500)}
        for index in range(60)
    ]
    main_response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content="provider abort retry answer", reasoning_content=None, reasoning=None, tool_calls=None,
    ), finish_reason="stop")], model="oracle/model", usage=None)
    summary_response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content="provider abort retry summary"), finish_reason="stop")])
    with (
        trap_state_db_opens(home) as opens,
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
        patch("agent.context_compressor.call_llm", side_effect=[AuxiliaryExplicitCancellation(), summary_response]) as provider,
    ):
        agent = AIAgent(api_key="test-key-1234567890", base_url="http://127.0.0.1/offline", model="oracle/model",
                        quiet_mode=True, skip_context_files=True, skip_memory=True, session_id=parent)
        agent.client = MagicMock()
        agent.client.chat.completions.create.return_value = main_response
        with patch("hermes_cli.config.load_config", return_value=_CONFIG):
            store = agent._get_session_db_for_recall()
        stores.append(store)
        agent._ensure_db_session()
        agent.compression_in_place = False
        agent.context_compressor.threshold_tokens = 1
        agent.context_compressor.note_usage_less_response()
        agent.max_compression_attempts = 1
        first = agent.run_conversation("abort public input", conversation_history=history)
        assert first["completed"] is True
        assert agent.session_id == parent
        assert store.get_session(parent)["ended_at"] is None
        assert store.get_compression_tip(parent) == parent
        assert store.get_compression_publication_receipt("not-a-real-request") is None
        reopened = _open(stores)
        assert reopened.get_session(parent)["ended_at"] is None
        agent._session_db = reopened
        retry = agent.run_conversation("retry public input", conversation_history=history)
        assert retry["completed"] is True
        assert agent.session_id != parent
        assert store.get_session(parent)["end_reason"] == "compression"
        assert provider.call_count == 2
    assert opens == []
    assert not (home / "state.db").exists()


def test_public_stale_owner_successor_rotates_selected_postgresql(pg_cli_home):
    """A stale public owner cannot strand the parent; the successor publishes once."""
    from run_agent import AIAgent

    home, stores = pg_cli_home
    parent = "20260914_010203_pg_stale_owner_parent"
    history = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"stale history {index}: " + ("evidence " * 500)}
        for index in range(60)
    ]
    main_response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content="successor answer", reasoning_content=None, reasoning=None, tool_calls=None,
    ), finish_reason="stop")], model="oracle/model", usage=None)
    summary_response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content="successor server-clock summary"), finish_reason="stop")])
    with (
        trap_state_db_opens(home) as opens,
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
        patch("agent.context_compressor.call_llm", return_value=summary_response),
    ):
        agent = AIAgent(api_key="test-key-1234567890", base_url="http://127.0.0.1/offline", model="oracle/model",
                        quiet_mode=True, skip_context_files=True, skip_memory=True, session_id=parent)
        agent.client = MagicMock()
        agent.client.chat.completions.create.return_value = main_response
        with patch("hermes_cli.config.load_config", return_value=_CONFIG):
            store = agent._get_session_db_for_recall()
        stores.append(store)
        agent._ensure_db_session()
        assert store.try_acquire_compression_lock(parent, "stale-public-owner", ttl_seconds=0.1)
        time.sleep(0.2)
        agent.compression_in_place = False
        agent.context_compressor.threshold_tokens = 1
        agent.context_compressor.note_usage_less_response()
        agent.max_compression_attempts = 1
        result = agent.run_conversation("successor public input", conversation_history=history)
        assert result["completed"] is True and agent.session_id != parent
        child = agent.session_id
        assert store.get_session(parent)["end_reason"] == "compression"
        assert store.get_compression_tip(parent) == child
        assert store.get_conversation_root(child) == parent
        with store._store._connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT holder, fence FROM {store._store._schema}.compression_rotation_receipts WHERE parent_session_id=%s", (parent,))
            holder, fence = cursor.fetchone()
        assert holder != "stale-public-owner" and fence > 1
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
