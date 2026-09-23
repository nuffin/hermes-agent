"""Real PostgreSQL coverage for the final SessionDB parity gaps."""
from __future__ import annotations

import uuid

import pytest

from cli_session_store import PostgreSQLCLISessionStore
from hermes_state import SessionDB
from state_store import MessageRecord, PostgreSQLStateStoreConfig
from state_store_interface import StateStoreInterface
from state_store_postgresql import PostgreSQLStateStore
from tests.integration.postgresql_test_target import TEST_DSN, OwnedPostgreSQLTestTarget


@pytest.fixture
def store(postgresql_test_target: OwnedPostgreSQLTestTarget):
    settings = PostgreSQLStateStoreConfig(
        dsn_env="HERMES_STATE_STORE_TEST_DSN", connect_timeout_seconds=5, pool_max_size=2,
    )
    handle = PostgreSQLStateStore(settings, TEST_DSN, schema=postgresql_test_target.schema)
    try:
        yield handle
    finally:
        handle.close()


def _session(store, prefix: str) -> str:
    session_id = f"{prefix}-{uuid.uuid4()}"
    store.ensure_session(session_id, source="integration")
    return session_id


def test_remaining_parity_gaps_are_transactional_and_facade_visible(store, tmp_path):
    session_id = _session(store, "remaining")
    row_id = store.append_message_record(session_id, MessageRecord(role="user", content="before"))
    metadata = {"delegation_id": f"delegation-{uuid.uuid4()}", "delivery_notice": "notice"}

    first_delivery = store.append_delegation_delivery(session_id, "detached result", metadata)
    assert store.append_delegation_delivery(session_id, "detached result", metadata) == first_delivery
    assert store.count_messages_all(session_id) == 2

    store.replace_messages(session_id, [{"role": "assistant", "content": "after"}], archive_dropped=True)
    assert store.has_archived_messages(session_id)
    assert any(message.get("content") == "before" for message in store.get_messages_as_conversation(session_id, include_inactive=True))

    store.create_topic(session_id, "first topic")
    store.create_topic(session_id, "second topic")
    assert store.set_topic_session_title(session_id) == "second topic (+1 topics)"
    assert store.get_session_title(session_id) == "second topic (+1 topics)"

    facade = PostgreSQLCLISessionStore(store)
    assert isinstance(facade, StateStoreInterface)
    assert facade.has_archived_messages(session_id)
    assert facade.set_topic_session_title(session_id) == "second topic (+1 topics)"
    assert facade.append_delegation_delivery(session_id, "detached result", metadata) == first_delivery

    sqlite = SessionDB(tmp_path / "state.db")
    try:
        assert isinstance(sqlite, StateStoreInterface)
    finally:
        sqlite.close()
