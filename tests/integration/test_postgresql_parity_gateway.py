"""Real PostgreSQL coverage for gateway coordination parity."""
from __future__ import annotations

from state_store import PostgreSQLStateStoreConfig
from state_store_postgresql import PostgreSQLStateStore

_SETTINGS = PostgreSQLStateStoreConfig(
    dsn_env="HERMES_STATE_STORE_TEST_DSN", connect_timeout_seconds=5, pool_max_size=2
)


def _store(target):
    return PostgreSQLStateStore(_SETTINGS, target.dsn, schema=target.schema)


def test_handoff_state_machine_and_reclaim(postgresql_test_target):
    store = _store(postgresql_test_target)
    try:
        store.ensure_session("handoff-1", source="telegram")
        assert store.request_handoff("handoff-1", "discord")
        assert not store.request_handoff("handoff-1", "slack")
        assert store.claim_handoff("handoff-1")
        assert not store.claim_handoff("handoff-1")
        assert store.fail_handoff("handoff-1", "dispatch failed", only_states=("running",))
        assert store.get_handoff_state("handoff-1")["state"] == "failed"
        assert store.request_handoff("handoff-1", "discord")
        assert store.claim_handoff("handoff-1")
        assert store.reclaim_stale_running_handoffs("watcher restarted") == ["handoff-1"]
    finally:
        store.close()


def test_gateway_routing_replace_and_heartbeat_prune(postgresql_test_target):
    store = _store(postgresql_test_target)
    try:
        store.save_gateway_routing_entry("peer-a", '{"session_id":"s-a"}', scope="profile-a")
        assert store.load_gateway_routing_entries(scope="profile-a") == {
            "peer-a": '{"session_id":"s-a"}'
        }
        store.replace_gateway_routing_entries(
            {"peer-b": '{"session_id":"s-b"}'}, scope="profile-a"
        )
        assert store.gateway_routing_entry_for_session("s-a") is None
        assert store.gateway_routing_entry_for_session("s-b")["session_id"] == "s-b"
        store.register_backend_heartbeat(
            backend_id="backend-a", pid=123, started_at=10, last_heartbeat=10
        )
        assert store.list_backend_heartbeats()[0]["backend_id"] == "backend-a"
        assert store.prune_stale_heartbeats(max_age_seconds=1) == ["backend-a"]
    finally:
        store.close()


def test_telegram_topic_binding_and_mode_is_profile_scoped(postgresql_test_target):
    store = _store(postgresql_test_target)
    try:
        store.ensure_session("topic-1", source="telegram", metadata={
            "session_key": "agent:default:telegram:chat",
            "chat_id": "chat", "chat_type": "private", "user_id": "user",
        })
        store.enable_telegram_topic_mode(chat_id="chat", user_id="user", profile_name="default")
        assert store.is_telegram_topic_mode_enabled(chat_id="chat", user_id="user")
        store.bind_telegram_topic(
            chat_id="chat", thread_id="1", user_id="user",
            session_key="agent:default:telegram:chat", session_id="topic-1",
        )
        assert store.get_telegram_topic_binding_by_session(session_id="topic-1")["thread_id"] == "1"
        assert store.is_telegram_session_linked_to_topic(session_id="topic-1")
        assert store.delete_telegram_topic_binding(chat_id="chat", thread_id="1") == 1
        assert not store.is_telegram_topic_mode_enabled(chat_id="chat", user_id="user")
    finally:
        store.close()
