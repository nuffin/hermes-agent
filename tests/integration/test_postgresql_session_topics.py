"""Real PostgreSQL contract for the session_topics persistence slice."""

from __future__ import annotations

import importlib
import time
import uuid

import pytest

from state_store import MessageRecord, PostgreSQLStateStoreConfig
from postgresql_state_store_operations import PostgreSQLSandboxOperations, PostgreSQLSandboxOperationsError
from state_store_postgresql import PostgreSQLStateStore


def _psycopg():
    return importlib.import_module("psycopg")


@pytest.fixture
def store(postgresql_test_target):
    """One PostgreSQLStateStore bound to the fixture's disposable tenant schema."""
    instance = PostgreSQLStateStore(
        PostgreSQLStateStoreConfig(dsn_env="TEST_DSN", connect_timeout_seconds=5, pool_max_size=2),
        postgresql_test_target.dsn,
        schema=postgresql_test_target.schema,
    )
    try:
        yield instance
    finally:
        instance.close()


def _topics_by_id(store: PostgreSQLStateStore, session_id: str) -> dict[int, dict]:
    return {row["id"]: row for row in store.get_topics(session_id)}


def test_create_topic_returns_id_and_get_topics_orders_newest_active_first(store):
    session_id = f"topics-{uuid.uuid4()}"
    store.ensure_session(session_id, source="integration")

    first = store.create_topic(session_id, "first")
    assert isinstance(first, int)
    time.sleep(0.02)
    second = store.create_topic(session_id, "second", summary="second summary")
    assert isinstance(second, int) and second > first

    topics = store.get_topics(session_id)
    assert len(topics) == 2
    for row in topics:
        assert set(row.keys()) == {"id", "title", "summary", "message_count", "state", "created_at", "last_active_at"}
    assert [row["title"] for row in topics] == ["second", "first"]
    assert topics[0]["summary"] == "second summary"
    assert topics[0]["state"] == "active"
    assert topics[0]["message_count"] == 0


def test_get_active_topic_and_set_active_topic_transition_and_missing_guard(store):
    session_id = f"topics-active-{uuid.uuid4()}"
    store.ensure_session(session_id, source="integration")

    first = store.create_topic(session_id, "first")
    time.sleep(0.02)
    second = store.create_topic(session_id, "second")

    active = store.get_active_topic(session_id)
    assert active is not None
    assert active["id"] == second
    assert active["state"] == "active"

    assert store.set_active_topic(session_id, first) is True
    topics = _topics_by_id(store, session_id)
    assert topics[first]["state"] == "active"
    assert topics[second]["state"] == "warm"

    # Nonexistent topic returns False without mutating any topic state.
    assert store.set_active_topic(session_id, 999999) is False
    topics = _topics_by_id(store, session_id)
    assert topics[first]["state"] == "active"
    assert topics[second]["state"] == "warm"


def test_update_topic_message_count_applies_delta(store):
    session_id = f"topics-count-{uuid.uuid4()}"
    store.ensure_session(session_id, source="integration")

    topic_id = store.create_topic(session_id, "counting")
    store.update_topic_message_count(topic_id, 3)
    assert store.get_topics(session_id)[0]["message_count"] == 3


def test_get_topic_messages_filters_by_topic_and_decodes_content(store):
    session_id = f"topics-messages-{uuid.uuid4()}"
    store.ensure_session(session_id, source="integration")

    topic_a = store.create_topic(session_id, "a")
    topic_b = store.create_topic(session_id, "b")

    store.append_message_records(session_id, [MessageRecord(role="user", content="in a", topic_id=topic_a)])

    messages = store.get_topic_messages(session_id, topic_a)
    assert len(messages) == 1
    assert messages[0]["content"] == "in a"
    assert messages[0]["topic_id"] == topic_a

    assert store.get_topic_messages(session_id, topic_b) == []


def test_session_topics_cascade_when_session_is_deleted(store, postgresql_test_target):
    session_id = f"topics-cascade-{uuid.uuid4()}"
    store.ensure_session(session_id, source="integration")
    store.create_topic(session_id, "cascade")
    assert store.get_topics(session_id) != []

    with _psycopg().connect(postgresql_test_target.dsn, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(f"DELETE FROM {store._schema}.sessions WHERE id = %s", (session_id,))

    assert store.get_topics(session_id) == []


def test_topic_catalog_has_fks_checks_indexes_and_doctor_rejects_topic_drift(store, postgresql_test_target):
    """The v27 semantic contract is real catalog evidence, not a revision marker."""
    schema = store._schema
    with _psycopg().connect(postgresql_test_target.dsn, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid=%s::regclass ORDER BY conname",
            (f'{schema}.session_topics',),
        )
        assert {row[0] for row in cursor.fetchall()} >= {
            "session_topics_pkey", "session_topics_session_id_fkey", "session_topics_state_check",
            "session_topics_message_count_check",
        }
        cursor.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid=%s::regclass AND conname='messages_topic_id_fkey'",
            (f'{schema}.messages',),
        )
        assert cursor.fetchone() == ("messages_topic_id_fkey",)
        cursor.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname=%s AND indexname IN (%s, %s)",
            (schema, "session_topics_session_last_active", "messages_topic_id"),
        )
        assert {row[0] for row in cursor.fetchall()} == {"session_topics_session_last_active", "messages_topic_id"}

    operations = PostgreSQLSandboxOperations(
        PostgreSQLStateStoreConfig(dsn_env="TEST_DSN", connect_timeout_seconds=5, pool_max_size=2),
        postgresql_test_target.dsn, schema=postgresql_test_target.schema,
    )
    assert operations.doctor()["schema"] == schema
    postgresql_test_target.execute(f'DROP INDEX "{schema}".messages_topic_id')
    with pytest.raises(PostgreSQLSandboxOperationsError, match="Alembic/core catalog is unhealthy"):
        operations.doctor()
