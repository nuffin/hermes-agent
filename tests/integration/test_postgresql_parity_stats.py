"""Real PostgreSQL coverage for statistics and maintenance parity."""

from __future__ import annotations

import time

import pytest

from state_store import MessageRecord, PostgreSQLStateStoreConfig
from state_store_postgresql import PostgreSQLStateStore
from tests.integration.postgresql_test_target import OwnedPostgreSQLTestTarget


_DSN = "postgresql://hermes_state_store_test@127.0.0.1:5432/hermes_state_store_test"
_SETTINGS = PostgreSQLStateStoreConfig(dsn_env="B5_STATS_TEST_DSN", connect_timeout_seconds=5, pool_max_size=2)


@pytest.fixture
def store(postgresql_test_target: OwnedPostgreSQLTestTarget):
    handle = PostgreSQLStateStore(_SETTINGS, _DSN, schema=postgresql_test_target.schema)
    try:
        yield handle
    finally:
        handle.close()


def _session(store: PostgreSQLStateStore, session_id: str, source: str = "cli", **metadata):
    store.ensure_session(session_id, source, metadata=metadata)
    store.append_message(session_id, role="user", content="hello")
    store.append_message(session_id, role="assistant", content="done")


def test_stats_usage_cron_counts_and_cwds(store, postgresql_test_target):
    _session(store, "root", "cli", cwd="/repo", session_key="chat")
    store.update_token_counts(
        "root", input_tokens=7, output_tokens=3, model="m", billing_provider="p",
        estimated_cost_usd=1.25, api_call_count=1,
    )
    store.record_auxiliary_usage("root", "vision", input_tokens=4, output_tokens=2, model="m")
    assert store.usage_totals() == {"tokens": 10, "cost_usd": 1.25}
    assert store.auxiliary_usage_by_task("root")["vision"]["input_tokens"] == 4
    assert store.session_count_by_source() == {"cli": 1}
    assert store.distinct_session_cwds() == [{"cwd": "/repo", "sessions": 1, "last_active": pytest.approx(store.get_session("root")["started_at"])}]

    for index in range(2):
        sid = f"cron_alpha_{index:08d}"
        _session(store, sid, "cron")
        postgresql_test_target.execute(
            f"UPDATE {postgresql_test_target.schema}.sessions SET started_at=%s WHERE id=%s",
            (1_700_000_000 + index, sid),
        )
    assert [row["id"] for row in store.list_cron_job_runs("alpha")] == [
        "cron_alpha_00000001", "cron_alpha_00000000"
    ]
    assert store.list_cron_job_runs("beta") == []


def test_hygiene_expiry_scope_and_repo_backfill(store):
    _session(store, "scope", "telegram", cwd="/old")
    assert store.increment_hygiene_failure_streak("telegram:1") == 1
    assert store.increment_hygiene_failure_streak("telegram:1") == 2
    store.reset_hygiene_failure_streak("telegram:1")
    assert store.increment_hygiene_failure_streak("telegram:1") == 1
    store.set_expiry_finalized("scope")
    store.backfill_repo_roots({"/old": "/repo"})
    assert store.declared_scope_identity("scope") == (False, "telegram")
    assert store.distinct_session_cwds()[0]["cwd"] == "/old"
    with store._connection() as connection, connection.cursor() as cursor:
        cursor.execute(f"SELECT expiry_finalized, git_repo_root FROM {store._schema}.sessions WHERE id='scope'")
        expiry_finalized, git_repo_root = cursor.fetchone()
    assert expiry_finalized is True
    assert git_repo_root == "/repo"


def test_compression_child_and_orphan_maintenance(store, postgresql_test_target):
    _session(store, "parent")
    store.end_session("parent", "compression")
    _session(store, "child", parent_session_id="parent")
    assert store.find_live_compression_child("parent")["id"] == "child"
    assert store.reopen_orphaned_compression_session("parent") is False

    _session(store, "orphan", parent_session_id="parent")
    postgresql_test_target.execute(
        f"UPDATE {postgresql_test_target.schema}.sessions SET started_at=%s, api_call_count=0 "
        "WHERE id=%s", (time.time() - 604801, "orphan"))
    assert store.finalize_orphaned_compression_sessions() == 1
    assert store.get_session("orphan")["end_reason"] == "orphaned_compression"


def test_auxiliary_usage_follows_compression_lineage(store):
    _session(store, "lineage-root")
    store.record_auxiliary_usage("lineage-root", "compression", input_tokens=3, model="m")
    store.ensure_session("lineage-child", "cli", metadata={"parent_session_id": "lineage-root"})
    store.record_auxiliary_usage("lineage-child", "compression", input_tokens=5, model="m")
    assert store.auxiliary_usage_by_task("lineage-child")["compression"]["input_tokens"] == 8


def test_facade_transmits_stats_methods(store):
    from cli_session_store import PostgreSQLCLISessionStore

    facade = PostgreSQLCLISessionStore(store)
    assert facade.session_count_by_source() == {}
    assert facade.usage_totals() == {"tokens": 0, "cost_usd": 0.0}
    assert facade.auxiliary_usage_by_task("") == {}
