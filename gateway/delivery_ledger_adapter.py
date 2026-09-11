"""Direct-test factory only. Gateway consumers remain bound to SQLite functions."""
from __future__ import annotations
from pathlib import Path
from typing import Any


def open_delivery_ledger(*, backend: str = "sqlite", dsn: str | None = None, **kwargs: Any):
    """Open an isolated adapter for conformance tests; never changes gateway routing."""
    if backend == "postgresql":
        if not dsn:
            raise ValueError("PostgreSQL DeliveryLedger requires a DSN")
        from gateway.delivery_ledger_postgresql import PostgreSQLDeliveryLedger
        return PostgreSQLDeliveryLedger(dsn, **kwargs)
    if backend == "sqlite":
        from gateway import delivery_ledger
        return SqliteDeliveryLedger()
    raise ValueError("delivery ledger backend must be sqlite or postgresql")


class SqliteDeliveryLedger:
    """Compatibility object for direct tests over the unchanged module contract."""
    def record_obligation(self, **kwargs):
        from gateway import delivery_ledger
        return delivery_ledger.record_obligation(**kwargs)
    def mark_attempting(self, receipt):
        from gateway import delivery_ledger
        return delivery_ledger.mark_attempting(receipt)
    def mark_delivered(self, receipt):
        from gateway import delivery_ledger
        return delivery_ledger.mark_delivered(receipt)
    def mark_failed(self, receipt, error=""):
        from gateway import delivery_ledger
        return delivery_ledger.mark_failed(receipt, error)
    def release_runtime_claim(self, receipt, error=""):
        from gateway import delivery_ledger
        return delivery_ledger.release_runtime_claim(receipt, error)
    def close(self):
        return None
