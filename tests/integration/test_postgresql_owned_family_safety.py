"""Sentinel and source-inventory proof for migrated PostgreSQL fixture families."""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

from gateway.delivery_ledger_postgresql import PostgreSQLDeliveryLedger
from state_store import open_state_store
from tests.integration.postgresql_test_target import TEST_DSN, OwnedPostgreSQLTestTarget


_FAMILY = (
    "test_postgresql_state_store_slice.py",
    "test_postgresql_session_runtime_ownership.py",
    "test_postgresql_delivery_ledger.py",
    "test_postgresql_cli_session_store.py",
)
# Store-pool search_path poisoning is a deliberate lifecycle assertion: the facade
# must reset it on checkout. Catalog/data reads are also safe; direct DDL/DML is not.
_SAFE_DIRECT_EXECUTE_EXEMPTIONS = ("SET search_path TO public", "SHOW search_path", "SELECT ")
_DANGEROUS_SQL = ("CREATE ", "ALTER ", "DROP ", "INSERT ", "UPDATE ", "DELETE ", "REINDEX ", "TRUNCATE ")


def _psycopg():
    return importlib.import_module("psycopg")


def test_owned_family_migrations_do_not_touch_preexisting_shared_sentinel(monkeypatch):
    """CLI/runtime/session/ledger constructors migrate only marker-owned UUID schemas."""
    shared = "hermes_owned_family_shared_sentinel"
    with _psycopg().connect(TEST_DSN, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(f'CREATE SCHEMA "{shared}"')
        cursor.execute(f'CREATE TABLE "{shared}".catalog_sentinel (id integer PRIMARY KEY, value text NOT NULL)')
        cursor.execute(f'INSERT INTO "{shared}".catalog_sentinel VALUES (1, %s)', ("untouched",))
    target = OwnedPostgreSQLTestTarget(TEST_DSN).allocate()
    delivery = OwnedPostgreSQLTestTarget(TEST_DSN, prefix="hermes_delivery_ledger_tenant_").allocate()
    try:
        import state_store
        monkeypatch.setenv("HERMES_STATE_STORE_TEST_DSN", TEST_DSN)
        monkeypatch.setattr(state_store, "postgresql_tenant_schema", lambda *_args, **_kwargs: target.schema)
        config = {"state_store": {"backend": "postgresql", "postgresql": {"dsn_env": "HERMES_STATE_STORE_TEST_DSN"}}}
        store = open_state_store(config)
        store.close()
        # Session-runtime migrations live in the StateStore catalog and are covered by the same factory path.
        runtime_store = open_state_store(config)
        assert getattr(runtime_store, "_schema") == target.schema
        runtime_store.close()
        ledger = PostgreSQLDeliveryLedger(TEST_DSN, schema=delivery.schema)
        ledger.close()
        target.verify()
        delivery.verify()
        with _psycopg().connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute(f'SELECT id, value FROM "{shared}".catalog_sentinel')
            assert cursor.fetchall() == [(1, "untouched")]
            cursor.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = %s", (shared,))
            assert cursor.fetchone() == (1,)
    finally:
        delivery.drop()
        target.drop()
        with _psycopg().connect(TEST_DSN, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA "{shared}" CASCADE')


def test_migrated_family_has_no_direct_dangerous_postgresql_execute_calls():
    """Regression inventory: mutations require OwnedPostgreSQLTestTarget.execute."""
    root = Path(__file__).parent
    violations: list[str] = []
    for filename in _FAMILY:
        source = (root / filename).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=filename)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "execute":
                continue
            if not isinstance(node.func.value, ast.Name) or node.func.value.id != "cursor":
                continue
            statement = ast.get_source_segment(source, node) or ""
            normalized = statement.upper()
            if any(token in normalized for token in _DANGEROUS_SQL):
                violations.append(f"{filename}:{node.lineno}: {statement}")
            elif not any(exemption.upper() in normalized for exemption in _SAFE_DIRECT_EXECUTE_EXEMPTIONS):
                violations.append(f"{filename}:{node.lineno}: unlisted direct execute exemption: {statement}")
    assert not violations, "\n".join(violations)
