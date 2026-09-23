"""Real PostgreSQL evidence for administrative/maintenance parity."""
from __future__ import annotations

import uuid

from state_store import MessageRecord, PostgreSQLStateStoreConfig
from state_store_postgresql import PostgreSQLStateStore


_DSN = "postgresql://hermes_state_store_test@127.0.0.1:5432/hermes_state_store_test"


def _store(target):
    return PostgreSQLStateStore(
        PostgreSQLStateStoreConfig("TEST_DSN", 5, 2), _DSN, schema=target.schema
    )


def test_postgresql_export_import_round_trip_and_export_guard(postgresql_test_target):
    store = _store(postgresql_test_target)
    try:
        session_id = f"admin-export-{uuid.uuid4().hex}"
        store.ensure_session(session_id, source="integration", metadata={"profile_name": "admin"})
        store.append_message_record(session_id, MessageRecord("user", "hello", timestamp=100.0))
        store.append_message_record(session_id, MessageRecord("assistant", "world", timestamp=101.0))
        exported = store.export_session(session_id)
        assert exported is not None
        assert exported["id"] == session_id
        assert [row["content"] for row in exported["messages"]] == ["hello", "world"]
        assert exported["timings"]["message_timestamps"]["available"] == 2
        assert store.assert_export_safe(session_id, max_messages=2) == 2
        from hermes_state import SessionExportTooLargeError
        try:
            store.assert_export_safe(session_id, max_messages=1)
        except SessionExportTooLargeError:
            pass
        else:
            raise AssertionError("export guard accepted an oversized transcript")
        assert store.delete_session(session_id) is True
        result = store.import_sessions([exported])
        assert result["ok"] is True and result["imported"] == 1
        assert [row["content"] for row in store.get_messages(session_id)] == ["hello", "world"]
    finally:
        store.close()


def test_postgresql_delete_sessions_removes_messages_usage_and_empty_rows(postgresql_test_target):
    store = _store(postgresql_test_target)
    try:
        session_id = f"admin-delete-{uuid.uuid4().hex}"
        empty_id = f"admin-empty-{uuid.uuid4().hex}"
        store.ensure_session(session_id, source="integration")
        store.append_message(session_id, role="user", content="delete me")
        store.ensure_session(empty_id, source="integration")
        store.end_session(empty_id, "cli_close")
        with store._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {store._schema}.session_model_usage "
                "(session_id,model,task) VALUES (%s,%s,%s)",
                (session_id, "test-model", ""),
            )
        assert store.delete_sessions([session_id]) == 1
        assert store.count_empty_sessions() == 1
        assert store.delete_empty_sessions() == 1
        with store._connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM {store._schema}.messages WHERE session_id=%s", (session_id,))
            assert cursor.fetchone()[0] == 0
            cursor.execute(f"SELECT COUNT(*) FROM {store._schema}.session_model_usage WHERE session_id=%s", (session_id,))
            assert cursor.fetchone()[0] == 0
    finally:
        store.close()


def test_postgresql_profile_rekey_search_maintenance_and_size(postgresql_test_target):
    store = _store(postgresql_test_target)
    try:
        session_id = f"admin-profile-{uuid.uuid4().hex}"
        store.ensure_session(session_id, source="telegram", metadata={"session_key": "agent:old:telegram:dm:1", "profile_name": "old", "chat_id": "1"})
        store.set_meta("admin-marker", "1")
        assert store.rekey_profile_state("old", "new")["sessions_session_key"] == 1
        assert store.get_session(session_id)["session_key"] == "agent:new:telegram:dm:1"
        assert store.backfill_null_session_profiles("new") == 0
        assert store.logical_size_bytes() > 0
        status = store.fts_rebuild_status()
        assert status["backend"] == "postgresql"
        assert status["percent"] == 100
        assert store.fts_cjk_rebuild_status()["available"] is False
        assert store.vacuum() > 0
    finally:
        store.close()
