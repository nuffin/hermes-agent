"""Real PostgreSQL contract for the session_topics persistence slice."""

from __future__ import annotations

import importlib
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from state_store import MessageRecord, PostgreSQLStateStoreConfig
from postgresql_state_store_operations import PostgreSQLSandboxOperations, PostgreSQLSandboxOperationsError
from tests.integration.postgresql_test_target import OwnedPostgreSQLTestTarget
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



def test_ensure_and_activate_topics_adopt_filter_and_retag_durable_rows(store):
    session_id = f"topics-runtime-{uuid.uuid4()}"
    store.ensure_session(session_id, source="integration")
    legacy_user = store.append_message_record(session_id, MessageRecord(role="user", content="legacy user"))
    legacy_assistant = store.append_message_record(session_id, MessageRecord(role="assistant", content="legacy answer"))

    first = store.ensure_session_topic(session_id, "git")
    assert first["state"] == "active" and first["message_count"] == 2
    initial = store.get_messages_as_conversation(session_id, include_ancestors=True, include_row_ids=True, topic_id=first["id"])
    assert [row["content"] for row in initial] == ["legacy user", "legacy answer"]
    assert [row["_row_id"] for row in initial] == [legacy_user, legacy_assistant]
    assert {row["_topic_id"] for row in initial} == {first["id"]}

    current = store.append_message_record(session_id, MessageRecord(role="user", content="cook tonight", topic_id=first["id"]))
    second = store.activate_topic_for_messages(session_id, title="cooking", message_ids=[current])
    assert second["state"] == "active" and second["message_count"] == 1
    assert store.get_active_topic(session_id)["id"] == second["id"]
    assert [row["content"] for row in store.get_messages_as_conversation(session_id, topic_id=first["id"])] == [
        "legacy user", "legacy answer"
    ]
    assert [row["content"] for row in store.get_messages_as_conversation(session_id, topic_id=second["id"])] == [
        "cook tonight"
    ]


def test_cross_session_topic_append_is_rejected_before_message_mutation(store, postgresql_test_target):
    first_session, second_session = f"topics-first-{uuid.uuid4()}", f"topics-second-{uuid.uuid4()}"
    store.ensure_session(first_session, source="integration")
    store.ensure_session(second_session, source="integration")
    foreign_topic = store.create_topic(first_session, "first")

    with pytest.raises(ValueError, match="topic_id does not belong to session"):
        store.append_message_records(
            second_session, [MessageRecord(role="user", content="must not write", topic_id=foreign_topic)]
        )
    # The v27 composite FK independently rejects a direct PostgreSQL append.
    with pytest.raises(_psycopg().errors.ForeignKeyViolation):
        postgresql_test_target.execute(
            f"INSERT INTO {store._schema}.messages (session_id, role, content, created_at, topic_id) "
            "VALUES (%s, 'user', 'must not write directly', 0, %s)",
            (second_session, foreign_topic),
        )
    assert store.get_messages_as_conversation(second_session) == []
    # Read filtering must reject foreign IDs rather than misrepresent the
    # session as having an empty topic transcript.
    with pytest.raises(ValueError, match="does not belong to session"):
        store.get_topic_messages(second_session, foreign_topic)
    with pytest.raises(ValueError, match="does not belong to session"):
        store.get_messages_as_conversation(second_session, topic_id=foreign_topic)


def test_topic_delete_nulls_its_messages_topic_id_without_deleting_messages(store, postgresql_test_target):
    session_id = f"topics-delete-{uuid.uuid4()}"
    store.ensure_session(session_id, source="integration")
    topic_id = store.create_topic(session_id, "delete")
    message_id = store.append_message_records(
        session_id, [MessageRecord(role="user", content="retained", topic_id=topic_id)]
    )
    assert message_id == 1

    postgresql_test_target.execute(
        f"DELETE FROM {store._schema}.session_topics WHERE id=%s", (topic_id,)
    )
    with _psycopg().connect(postgresql_test_target.dsn) as connection, connection.cursor() as cursor:
        cursor.execute(f"SELECT topic_id, content FROM {store._schema}.messages WHERE session_id=%s", (session_id,))
        assert cursor.fetchall() == [(None, "retained")]


def test_topicless_session_remains_valid_for_runtime_and_doctor(store, postgresql_test_target):
    session_id = f"topics-none-{uuid.uuid4()}"
    store.ensure_session(session_id, source="integration")
    assert store.get_topics(session_id) == []
    operations = PostgreSQLSandboxOperations(
        PostgreSQLStateStoreConfig(dsn_env="TEST_DSN", connect_timeout_seconds=5, pool_max_size=2),
        postgresql_test_target.dsn, schema=postgresql_test_target.schema,
    )
    assert operations.doctor()["invariants"]["invalid_topic_sessions"] == 0


def test_warm_only_topics_fail_closed_in_runtime_semantic_validation_and_doctor(store, postgresql_test_target):
    session_id = f"topics-warm-only-{uuid.uuid4()}"
    store.ensure_session(session_id, source="integration")
    store.create_topic(session_id, "only")
    postgresql_test_target.execute(
        f"UPDATE {store._schema}.session_topics SET state='warm' WHERE session_id=%s", (session_id,)
    )

    with pytest.raises(ValueError, match="exactly one active topic"):
        store.get_topics(session_id)
    operations = PostgreSQLSandboxOperations(
        PostgreSQLStateStoreConfig(dsn_env="TEST_DSN", connect_timeout_seconds=5, pool_max_size=2),
        postgresql_test_target.dsn, schema=postgresql_test_target.schema,
    )
    with pytest.raises(PostgreSQLSandboxOperationsError, match="Alembic/core catalog is unhealthy"):
        operations.doctor()


def _assert_topic_operation_blocks_on_session_lock(postgresql_test_target, store, session_id, operation):
    """Prove the public contender is waiting on this exact row-lock query."""
    started, completed = Event(), Event()
    application_name = f"topic-row-lock-{uuid.uuid4().hex}"
    contender_dsn = f"{postgresql_test_target.dsn}?application_name={application_name}"

    def contend():
        contender = PostgreSQLStateStore(
            PostgreSQLStateStoreConfig(dsn_env="TEST_DSN", connect_timeout_seconds=5, pool_max_size=1),
            contender_dsn, schema=postgresql_test_target.schema,
        )
        try:
            started.set()
            return operation(contender)
        finally:
            completed.set()
            contender.close()

    with _psycopg().connect(postgresql_test_target.dsn) as holder, holder.cursor() as cursor:
        cursor.execute(f"SELECT id FROM {store._schema}.sessions WHERE id=%s FOR UPDATE", (session_id,))
        assert cursor.fetchone() == (session_id,)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(contend)
            assert started.wait(timeout=2)
            deadline = time.monotonic() + 2
            blocked = False
            while time.monotonic() < deadline:
                cursor.execute(
                    "SELECT state, wait_event_type, query FROM pg_stat_activity "
                    "WHERE application_name=%s AND datname=current_database()",
                    (application_name,),
                )
                row = cursor.fetchone()
                if (
                    row is not None
                    and row[0] == "active"
                    and row[1] == "Lock"
                    and "sessions" in row[2]
                    and "FOR UPDATE" in row[2]
                ):
                    blocked = True
                    break
                assert not completed.wait(timeout=0.05)
            assert blocked, "contender never reached the session SELECT ... FOR UPDATE lock"
            assert not completed.is_set()
            holder.commit()
            return future.result(timeout=5)


def test_create_topic_blocks_on_real_session_lock_then_leaves_one_active_topic(postgresql_test_target, store):
    session_id = f"topics-concurrent-create-{uuid.uuid4()}"
    store.ensure_session(session_id, source="integration")
    first = store.create_topic(session_id, "one")

    second = _assert_topic_operation_blocks_on_session_lock(
        postgresql_test_target, store, session_id, lambda contender: contender.create_topic(session_id, "two"),
    )

    topics = _topics_by_id(store, session_id)
    assert set(topics) == {first, second}
    assert [topic["id"] for topic in topics.values() if topic["state"] == "active"] == [second]


def test_switch_topic_blocks_on_real_session_lock_then_leaves_one_active_topic(postgresql_test_target, store):
    session_id = f"topics-concurrent-switch-{uuid.uuid4()}"
    store.ensure_session(session_id, source="integration")
    first, second = store.create_topic(session_id, "one"), store.create_topic(session_id, "two")

    assert _assert_topic_operation_blocks_on_session_lock(
        postgresql_test_target, store, session_id, lambda contender: contender.set_active_topic(session_id, first),
    ) is True

    topics = _topics_by_id(store, session_id)
    assert topics[first]["state"] == "active"
    assert topics[second]["state"] == "warm"


def test_session_topics_cascade_when_session_is_deleted(store, postgresql_test_target):
    session_id = f"topics-cascade-{uuid.uuid4()}"
    store.ensure_session(session_id, source="integration")
    store.create_topic(session_id, "cascade")
    assert store.get_topics(session_id) != []

    postgresql_test_target.execute(
        f"DELETE FROM {store._schema}.sessions WHERE id=%s", (session_id,)
    )

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
            "SELECT pg_get_constraintdef(oid, true) FROM pg_constraint "
            "WHERE conrelid=%s::regclass AND conname='messages_topic_id_fkey'",
            (f'{schema}.messages',),
        )
        assert cursor.fetchone() == (
            f"FOREIGN KEY (session_id, topic_id) REFERENCES {schema}.session_topics(session_id, id) ON DELETE SET NULL (topic_id)",
        )
        cursor.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname=%s AND indexname IN (%s, %s, %s, %s)",
            (schema, "session_topics_session_last_active", "session_topics_session_id_id_unique", "session_topics_one_active_per_session", "messages_topic_id"),
        )
        assert {row[0] for row in cursor.fetchall()} == {
            "session_topics_session_last_active", "session_topics_session_id_id_unique",
            "session_topics_one_active_per_session", "messages_topic_id",
        }

    operations = PostgreSQLSandboxOperations(
        PostgreSQLStateStoreConfig(dsn_env="TEST_DSN", connect_timeout_seconds=5, pool_max_size=2),
        postgresql_test_target.dsn, schema=postgresql_test_target.schema,
    )
    assert operations.doctor()["schema"] == schema
    postgresql_test_target.execute(f'DROP INDEX "{schema}".messages_topic_id')
    with pytest.raises(PostgreSQLSandboxOperationsError, match="Alembic/core catalog is unhealthy"):
        operations.doctor()


def test_topic_catalog_rejects_same_name_fk_target_outside_tenant_schema(store, postgresql_test_target):
    foreign_target = OwnedPostgreSQLTestTarget(postgresql_test_target.dsn).allocate()
    schema = store._schema
    foreign_schema = foreign_target.schema
    try:
        foreign_target.execute(
            f"CREATE TABLE {foreign_schema}.session_topics "
            "(session_id text NOT NULL, id bigint NOT NULL, UNIQUE (session_id, id))"
        )
        postgresql_test_target.execute(
            f"ALTER TABLE {schema}.messages DROP CONSTRAINT messages_topic_id_fkey"
        )
        postgresql_test_target.execute_referencing_owned_target(
            f"ALTER TABLE {schema}.messages ADD CONSTRAINT messages_topic_id_fkey "
            f"FOREIGN KEY (session_id, topic_id) REFERENCES {foreign_schema}.session_topics "
            "(session_id, id) ON DELETE SET NULL (topic_id)",
            foreign_target,
        )
        operations = PostgreSQLSandboxOperations(
            PostgreSQLStateStoreConfig(dsn_env="TEST_DSN", connect_timeout_seconds=5, pool_max_size=2),
            postgresql_test_target.dsn, schema=postgresql_test_target.schema,
        )
        with pytest.raises(PostgreSQLSandboxOperationsError, match="Alembic/core catalog is unhealthy"):
            operations.doctor()
    finally:
        foreign_target.drop()
