"""Real PostgreSQL coverage for b1 core parity contracts."""
from __future__ import annotations

import time
import uuid

import pytest

from state_store import MessageRecord, PostgreSQLStateStoreConfig
from state_store_postgresql import PostgreSQLStateStore


@pytest.fixture
def core_store(postgresql_test_target):
    store = PostgreSQLStateStore(
        PostgreSQLStateStoreConfig("HERMES_STATE_STORE_TEST_DSN", 5, 2),
        postgresql_test_target.dsn,
        schema=postgresql_test_target.schema,
    )
    try:
        yield store
    finally:
        store.close()


def test_state_meta_round_trip_and_literal_prefix(core_store):
    core_store.set_meta("loop:a", "one")
    core_store.set_meta("loop:b", "two")
    core_store.set_meta("loop:a", "updated")
    core_store.set_meta("loop:%literal", "literal")
    assert core_store.get_meta("loop:a") == "updated"
    assert sorted(core_store.list_meta_prefix("loop:")) == sorted([
        ("loop:%literal", "literal"), ("loop:a", "updated"), ("loop:b", "two")
    ])
    assert core_store.list_meta_prefix("") == []


def test_archive_and_compact_archives_rows_and_is_idempotently_repeatable(core_store):
    session_id = f"compact-{uuid.uuid4().hex}"
    core_store.ensure_session(session_id, source="integration")
    first = core_store.append_message_record(session_id, MessageRecord("user", "before"))
    second = core_store.append_message_record(session_id, MessageRecord("assistant", "answer"))
    watermark = core_store.get_active_message_watermark(session_id)
    result = core_store.archive_and_compact(
        session_id, [{"role": "assistant", "content": "summary", "timestamp": time.time()}], watermark=watermark
    )
    assert result == 1
    with core_store._connection() as connection, connection.cursor() as cursor:
        cursor.execute(f"SELECT active, compacted FROM {core_store.tenant_schema}.messages WHERE id = ANY(%s)", ([first, second],))
        assert cursor.fetchall() == [(False, True), (False, True)]
        cursor.execute(f"SELECT COUNT(*) FROM {core_store.tenant_schema}.messages WHERE session_id=%s AND active", (session_id,))
        assert cursor.fetchone()[0] == 1
    second_result = core_store.archive_and_compact(
        session_id, [{"role": "assistant", "content": "summary-2", "timestamp": time.time()}]
    )
    assert second_result == 1
    assert len(core_store.get_message_records(session_id)) == 1


def test_session_flags_and_refresh_auto_title_match_sqlite_contract(core_store):
    session_id = f"flags-{uuid.uuid4().hex}"
    core_store.ensure_session(session_id, source="integration")
    core_store.append_message(session_id, role="user", content="hello")
    assert core_store.session_unread(core_store.get_session(session_id)) is False
    assert core_store.set_session_read(session_id, read=False) is True
    assert core_store.session_unread(core_store.get_session(session_id)) is True
    assert core_store.set_session_read(session_id, read=True) is True
    assert core_store.session_unread(core_store.get_session(session_id)) is False
    core_store.set_session_yolo(session_id, True)
    assert core_store.session_yolo_enabled(core_store.get_session(session_id)) is True
    assert core_store.set_auto_title(session_id, "first", source="derived") is True
    assert core_store.refresh_auto_title(session_id, "second", source="derived") is True
    assert core_store.get_session_title(session_id) == "second"
    core_store.set_session_title(session_id, "user-owned")
    assert core_store.refresh_auto_title(session_id, "ignored", source="derived") is False


def test_refresh_auto_title_rejects_cross_session_conflict(core_store):
    left, right = f"title-left-{uuid.uuid4().hex}", f"title-right-{uuid.uuid4().hex}"
    core_store.ensure_session(left, source="integration")
    core_store.ensure_session(right, source="integration")
    assert core_store.set_auto_title(left, "shared", source="derived") is True
    assert core_store.set_auto_title(right, "other", source="derived") is True
    with pytest.raises(ValueError, match="already in use"):
        core_store.refresh_auto_title(right, "shared", source="derived")
