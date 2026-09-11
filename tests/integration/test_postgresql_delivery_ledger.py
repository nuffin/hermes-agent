"""Live PG18 / SQLite receipt-fence conformance for the standalone ledger adapter."""
from __future__ import annotations
import importlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from gateway import delivery_ledger as sqlite
from gateway.delivery_ledger_postgresql import DeliveryLedgerPostgreSQLConfig, PostgreSQLDeliveryLedger

_DSN = "postgresql://hermes_state_store_test@127.0.0.1:5432/hermes_state_store_test"
_SCHEMA = "hermes_delivery_ledger_tenant_0123456789abcdef0123456789abcdef"


def _reset():
    with importlib.import_module("psycopg").connect(_DSN, autocommit=True) as con, con.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")


@pytest.fixture
def ledger():
    _reset()
    value = PostgreSQLDeliveryLedger(_DSN, schema=_SCHEMA, settings=DeliveryLedgerPostgreSQLConfig(lease_seconds=.01))
    try: yield value
    finally: value.close(); _reset()


def _record(ledger, oid="o1"):
    return ledger.record_obligation(obligation_id=oid, session_key="s", platform="slack", chat_id="c", thread_id=None, content="body", adapter_profile="metadata-not-tenant")


def test_fresh_catalog_is_dedicated_and_validated(ledger):
    assert ledger.debug_rows() == []
    with importlib.import_module("psycopg").connect(_DSN) as con, con.cursor() as cur:
        cur.execute(f"SELECT version FROM {_SCHEMA}.delivery_schema_migrations")
        assert cur.fetchall() == [(1,)]
        cur.execute(f"DROP INDEX {_SCHEMA}.delivery_claim_idx")
    with pytest.raises(Exception, match="schema drift"):
        PostgreSQLDeliveryLedger(_DSN, schema=_SCHEMA)


def test_idempotent_record_fence_and_ack(ledger):
    first = _record(ledger)
    assert _record(ledger) == first
    claim = ledger.mark_attempting(first)
    assert claim and claim.fence == first.fence + 1
    assert ledger.mark_delivered(claim)
    assert not ledger.mark_failed(claim, "stale")
    assert ledger.debug_rows()[0]["state"] == "delivered"


def test_concurrent_claim_only_one_receipt_wins(ledger):
    receipt = _record(ledger); barrier = Barrier(2)
    def claim(): barrier.wait(); return ledger.mark_attempting(receipt)
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(lambda _: claim(), range(2)))
    assert sum(item is not None for item in results) == 1


def test_expired_lease_steal_rejects_stale_receipt_and_owner_guard(ledger):
    original=_record(ledger); first=ledger.mark_attempting(original); assert first
    import time; time.sleep(.02)
    thief=PostgreSQLDeliveryLedger(_DSN,schema=_SCHEMA,settings=DeliveryLedgerPostgreSQLConfig(lease_seconds=1))
    try:
        stolen=thief.sweep_recoverable(); assert len(stolen)==1
        assert not ledger.mark_delivered(first)
        assert thief.mark_delivered(stolen[0]['receipt'])
    finally: thief.close()


def test_transaction_rollback_preserves_no_partial_obligation(ledger, monkeypatch):
    original = ledger._validate
    monkeypatch.setattr(ledger, '_validate', lambda cursor: (_ for _ in ()).throw(RuntimeError('inject')))
    with pytest.raises(RuntimeError):
        ledger._migrate(ledger._connect())
    monkeypatch.setattr(ledger, '_validate', original)
    assert ledger.debug_rows() == []


def test_retention_prune_and_adapter_profile_is_not_schema(ledger):
    claim=ledger.mark_attempting(_record(ledger)); assert claim and ledger.mark_delivered(claim)
    ledger.prune(); assert ledger.debug_rows()[0]['obligation_id']=='o1'
    assert ledger._schema == _SCHEMA
