"""Real PostgreSQL coverage for message/reaction parity (b2)."""
from __future__ import annotations

import uuid

import pytest

from hermes_state import SessionDB
from state_store import MessageRecord, PostgreSQLStateStoreConfig
from state_store_postgresql import PostgreSQLStateStore
from cli_session_store import PostgreSQLCLISessionStore
from tests.integration.postgresql_test_target import TEST_DSN, OwnedPostgreSQLTestTarget


@pytest.fixture
def store(postgresql_test_target: OwnedPostgreSQLTestTarget):
    settings = PostgreSQLStateStoreConfig(dsn_env="HERMES_STATE_STORE_TEST_DSN", connect_timeout_seconds=5, pool_max_size=2)
    handle = PostgreSQLStateStore(settings, TEST_DSN, schema=postgresql_test_target.schema)
    try:
        yield handle
    finally:
        handle.close()


def _session(store, prefix: str) -> str:
    session_id = f"{prefix}-{uuid.uuid4()}"
    store.ensure_session(session_id, source="integration")
    return session_id


def test_reactions_roundtrip_take_once_and_session_isolation(store):
    session_id = _session(store, "reactions")
    other_id = _session(store, "other-reactions")
    row_id = store.append_message_record(session_id, MessageRecord(role="assistant", content="answer"))
    other_row_id = store.append_message_record(other_id, MessageRecord(role="assistant", content="other"))

    assert store.set_message_reaction(session_id, row_id, "👍") == [
        {"emoji": "👍", "author": "user", "at": pytest.approx(store.get_message_reactions(session_id, row_id)[0]["at"])}
    ]
    reactions = store.get_message_reactions(session_id, row_id)
    assert reactions[0]["emoji"] == "👍"
    assert reactions[0]["author"] == "user"
    assert store.get_message_reactions(other_id, row_id) == []
    assert store.set_message_reaction(other_id, other_row_id, "👎", author="agent")[0]["author"] == "agent"

    pending = store.take_unseen_reactions(session_id)
    assert [(item["row_id"], item["emoji"], item["text"]) for item in pending] == [(row_id, "👍", "answer")]
    assert store.take_unseen_reactions(session_id) == []
    assert store.get_message_reactions(session_id, row_id)[0]["seen"] is True

    # Same tapback toggles off, while a different emoji replaces it.
    assert store.set_message_reaction(session_id, row_id, "👍") == []
    assert store.set_message_reaction(session_id, row_id, "❤️")[0]["emoji"] == "❤️"


def test_message_edit_display_kind_queries_and_clear(store):
    session_id = _session(store, "messages")
    first = store.append_message_record(session_id, MessageRecord(role="user", content="old prompt"))
    second = store.append_message_record(session_id, MessageRecord(role="assistant", content="answer"))
    tool = store.append_message_record(session_id, MessageRecord(role="tool", content="PR https://github.com/x/y/pull/7"))

    assert store.get_message_role(session_id, first) == "user"
    store.update_session_tool_names(session_id, ["search", "react"])
    assert store.get_session(session_id)["tool_names"] == '["search", "react"]'
    assert store.set_user_message_content(session_id, first, "expanded prompt") == 1
    assert store.get_messages_as_conversation(session_id)[0]["content"] == "expanded prompt"
    assert store.set_latest_matching_message_display_kind(
        session_id, role="assistant", content="answer", display_kind="steer", display_metadata={"source": "test"}
    )
    assert store.get_messages_as_conversation(session_id)[1]["display_kind"] == "steer"
    assert store.list_recent_user_messages(session_id) == [
        {"id": first, "timestamp": pytest.approx(store.list_recent_user_messages(session_id)[0]["timestamp"]), "preview": "expanded prompt"}
    ]
    assert store.find_pr_url_messages([session_id]) == [
        {"session_id": session_id, "content": "PR https://github.com/x/y/pull/7"}
    ]
    assert store.count_messages_all(session_id) == 3
    store.clear_messages(session_id)
    assert store.count_messages_all(session_id) == 0
    assert store.get_messages_as_conversation(session_id) == []
    assert store.get_message_role(session_id, second) is None
    assert tool > second


def test_list_recent_user_messages_matches_sqlite_and_facade_passthrough(store, tmp_path):
    session_id = _session(store, "diff")
    sqlite = SessionDB(tmp_path / "state.db")
    try:
        sqlite.ensure_session(session_id, source="integration")
        for content in ("first question", "[CONTEXT COMPACTION — REFERENCE ONLY]", "second question"):
            store.append_message_record(session_id, MessageRecord(role="user", content=content))
            sqlite.append_message(session_id, role="user", content=content)
        expected = [{key: row[key] for key in ("preview",)} for row in sqlite.list_recent_user_messages(session_id)]
        actual = [{key: row[key] for key in ("preview",)} for row in store.list_recent_user_messages(session_id)]
        assert actual == expected

        facade = PostgreSQLCLISessionStore(store)
        assert facade.count_messages_all(session_id) == 3
        assert facade.list_recent_user_messages(session_id) == store.list_recent_user_messages(session_id)
    finally:
        sqlite.close()
