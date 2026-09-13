"""PG18 acceptance gate for a future atomic compression-rotation adapter.

The adapter deliberately does not exist in this slice.  Each prospective
publication test is a strict runtime xfail while it is absent; exposing the
method without extending this harness turns that xfail into an actionable
failure, never a weak pass.
"""
from __future__ import annotations

import importlib
import uuid
from typing import Any

import pytest

from state_store import PostgreSQLStateStoreConfig, StateStoreConfigurationError
from state_store_postgresql import PostgreSQLStateStore

pytestmark = pytest.mark.integration

_DSN = "postgresql://hermes_state_store_test@127.0.0.1:5432/hermes_state_store_test"
_SETTINGS = PostgreSQLStateStoreConfig(dsn_env="PG_ROTATION_GATE_DSN", connect_timeout_seconds=5, pool_max_size=2)
_CAPABILITY = "atomic-compression-rotation-v1"
_PHASES = (
    "lease-revalidated", "child-row-inserted", "handoff-inserted",
    "parent-close-issued", "commit-returned",
)


def _psycopg():
    return importlib.import_module("psycopg")


def _store(schema: str) -> PostgreSQLStateStore:
    return PostgreSQLStateStore(_SETTINGS, _DSN, schema=schema)


def _drop_schema(schema: str) -> None:
    with _psycopg().connect(_DSN, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


@pytest.fixture(autouse=True)
def requires_postgresql_18(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PG_ROTATION_GATE_DSN", _DSN)
    with _psycopg().connect(_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("SHOW server_version_num")
        assert int(cursor.fetchone()[0]) >= 180000


@pytest.fixture
def rotation_schema():
    schema = f"hermes_state_store_tenant_{uuid.uuid4().hex}"
    try:
        yield schema
    finally:
        _drop_schema(schema)


def _require_adapter(store: PostgreSQLStateStore) -> Any:
    """Keep absence explicit; never silently substitute a fake publisher."""
    publisher = getattr(store, "publish_compression_child", None)
    if publisher is None:
        pytest.xfail(
            "atomic-compression-rotation-v1 is deliberately unsupported; "
            "selected PG must retain its fail-closed CLI rejection"
        )
    # A future implementation must first make the capability name explicit.
    capabilities = getattr(store, "capabilities", ())
    assert _CAPABILITY in capabilities, "publisher exists without capability-scoped report"
    return publisher


def _seed_parent(store: PostgreSQLStateStore) -> str:
    parent = f"parent-{uuid.uuid4().hex}"
    store.ensure_session(parent, source="rotation-gate", metadata={
        "model": "rotation-model", "model_config": {"provider": "deterministic"},
        "system_prompt": "rotation prompt", "title": "rotation title",
        "cwd": "/rotation/repo", "git_repo_root": "/rotation/repo",
        "git_branch": "main", "profile_name": "rotation-profile",
    })
    store.append_message(parent, role="user", content="persisted parent")
    store.touch_session_activity(parent, 100.0, description="rotation activity", provenance="tool")
    store.record_compression_failure_cooldown(parent, 200.0, "rotation cooldown")
    store.set_compression_fallback_streak(parent, 3)
    store.set_compression_ineffective_count(parent, 2)
    store.set_compression_recovery_deadline(parent, 300.0)
    return parent


def _assert_no_partial_publication(store: PostgreSQLStateStore, parent: str) -> None:
    """Adapter-level audit used after every injected crash/failure cell."""
    parent_row = store.get_session(parent)
    assert parent_row is not None
    children = [row for row in store.list_session_summaries(include_archived=True, include_hidden=True, limit=1000) if row.get("parent_session_id") == parent]
    if parent_row.get("end_reason") == "compression":
        assert len(children) == 1, "closed parent requires exactly one visible child"
        assert children[0].get("ended_at") is None
    else:
        assert not children, "failed pre-commit publication must not leak a child"


def test_pg18_rotation_capability_is_not_advertised_or_implied(rotation_schema):
    """GREEN today: direct store does not claim a destructive adapter exists."""
    store = _store(rotation_schema)
    try:
        assert getattr(store, "publish_compression_child", None) is None
        assert _CAPABILITY not in getattr(store, "capabilities", ())
    finally:
        store.close()


def test_pg18_unknown_catalog_version_is_rejected_fail_closed(rotation_schema):
    """A disposable fixture proves v20 drift is rejected without touching root."""
    store = _store(rotation_schema)
    store.close()
    with _psycopg().connect(_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {rotation_schema}.schema_migrations (version, applied_at) VALUES (20, 0)"
        )
    with pytest.raises(
        StateStoreConfigurationError,
        match=r"Unsupported PostgreSQL State Store schema migration versions: \[20\]",
    ):
        _store(rotation_schema)


@pytest.mark.parametrize("phase", _PHASES)
def test_future_phase_faults_are_all_or_nothing_and_reopenable(rotation_schema, phase):
    """Future adapter must inject real SQL-boundary faults, not mocked returns."""
    store = _store(rotation_schema)
    try:
        publisher = _require_adapter(store)
        parent = _seed_parent(store)
        pytest.fail(
            f"{phase}: adapter exposed publisher but this gate must be extended with "
            "a real transaction-boundary observer, server error, terminate-backend, "
            "SIGKILL worker, fresh-adapter audit, and post-commit acknowledgement check"
        )
    finally:
        store.close()


def test_future_two_owner_stale_fence_retry_and_namespace_isolation(rotation_schema):
    """Reserved hard gate for spawned real adapters and UUID schema isolation."""
    store = _store(rotation_schema)
    try:
        _require_adapter(store)
        pytest.fail(
            "adapter exposed publisher but this gate must be extended with two spawned "
            "real pools, barrier/queue coordination, stale-fence rejection, exactly-one "
            "commit, successful next-lease retry, and independent-schema audit"
        )
    finally:
        store.close()
