"""Dedicated PostgreSQL delivery-ledger adapter; gateway runtime still uses SQLite."""
from __future__ import annotations

import hashlib
import importlib
import re
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from gateway import delivery_ledger as common
from hermes_constants import get_hermes_home, profile_name_for_home

_ROOT_SCHEMA = "hermes_delivery_ledger"
_SCHEMA_RE = re.compile(r"^hermes_delivery_ledger_tenant_[0-9a-f]{32}$")
_VERSION = 1


class DeliveryLedgerConfigurationError(ValueError):
    pass


def postgresql_delivery_ledger_schema() -> str:
    """Derive tenant only from canonical resolved home/profile, never adapter metadata."""
    home = get_hermes_home().resolve()
    profile = profile_name_for_home(home)
    if profile == "default":
        return _ROOT_SCHEMA
    digest = hashlib.sha256(f"{home}\0{profile}".encode()).hexdigest()[:32]
    return f"hermes_delivery_ledger_tenant_{digest}"


@dataclass(frozen=True)
class DeliveryLedgerPostgreSQLConfig:
    connect_timeout_seconds: int = 5
    pool_max_size: int = 4
    lease_seconds: float = float(common.STALE_AFTER_SECONDS)


class PostgreSQLDeliveryLedger:
    """Tenant-scoped, receipt-fenced PostgreSQL implementation of the ledger contract.

    Each operation owns its transaction and connection; no database handle escapes this adapter.
    """
    def __init__(self, dsn: str, *, schema: str | None = None, settings: DeliveryLedgerPostgreSQLConfig | None = None,
                 owner_identity: tuple[str, str, str] | None = None) -> None:
        try:
            self._psycopg = importlib.import_module("psycopg")
        except ImportError as exc:
            raise DeliveryLedgerConfigurationError("PostgreSQL DeliveryLedger requires psycopg") from exc
        self._schema = schema or postgresql_delivery_ledger_schema()
        if self._schema != _ROOT_SCHEMA and not _SCHEMA_RE.fullmatch(self._schema):
            raise DeliveryLedgerConfigurationError("PostgreSQL DeliveryLedger received an invalid trusted tenant schema")
        self._dsn, self._settings = dsn, settings or DeliveryLedgerPostgreSQLConfig()
        self._identity = owner_identity or (str(uuid.uuid4()), socket.gethostname() or "unknown-host", str(uuid.uuid4()))
        self._closed = False
        self._lock = threading.Lock()
        connection = self._connect()
        try:
            self._migrate(connection)
        finally:
            connection.close()

    def _connect(self):
        if self._closed:
            raise RuntimeError("PostgreSQL DeliveryLedger is closed")
        return self._psycopg.connect(self._dsn, connect_timeout=self._settings.connect_timeout_seconds, autocommit=False)

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'SET LOCAL search_path TO "{self._schema}", pg_catalog')
                yield cursor
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _migrate(self, connection: Any) -> None:
        try:
            with connection.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"{self._schema}:delivery-migration",))
                cur.execute("SHOW server_version_num")
                if int(cur.fetchone()[0]) < 180000:
                    raise DeliveryLedgerConfigurationError("PostgreSQL DeliveryLedger requires PostgreSQL 18 or newer")
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {self._schema}")
                cur.execute(f"CREATE TABLE IF NOT EXISTS {self._schema}.delivery_schema_migrations (version integer PRIMARY KEY, applied_at double precision NOT NULL)")
                cur.execute(f"SELECT version FROM {self._schema}.delivery_schema_migrations")
                versions = {int(row[0]) for row in cur.fetchall()}
                if versions - {_VERSION}:
                    raise DeliveryLedgerConfigurationError("unsupported PostgreSQL DeliveryLedger migration version")
                if 1 not in versions:
                    cur.execute(f"""CREATE TABLE {self._schema}.delivery_obligations (
                      obligation_id text PRIMARY KEY, session_key text NOT NULL, platform text NOT NULL, chat_id text NOT NULL,
                      thread_id text, content text NOT NULL, state text NOT NULL CHECK (state IN ('pending','attempting','failed','delivered','abandoned')),
                      attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0), created_at double precision NOT NULL,
                      updated_at double precision NOT NULL, owner_pid integer, owner_started_at bigint, last_error text,
                      adapter_profile text NOT NULL DEFAULT 'default', delivery_fence bigint NOT NULL DEFAULT 0 CHECK (delivery_fence >= 0),
                      owner_installation_id text, owner_host text, owner_generation text, lease_expires_at double precision)""")
                    cur.execute(f"CREATE INDEX delivery_claim_idx ON {self._schema}.delivery_obligations (state, platform, adapter_profile, lease_expires_at, created_at)")
                    cur.execute(f"INSERT INTO {self._schema}.delivery_schema_migrations VALUES (1, extract(epoch from clock_timestamp()))")
                self._validate(cur)
            connection.commit()
        except Exception:
            connection.rollback(); raise

    def _validate(self, cur: Any) -> None:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name='delivery_obligations'", (self._schema,))
        required = {'obligation_id','session_key','platform','chat_id','thread_id','content','state','attempts','created_at','updated_at','owner_pid','owner_started_at','last_error','adapter_profile','delivery_fence','owner_installation_id','owner_host','owner_generation','lease_expires_at'}
        missing = required - {row[0] for row in cur.fetchall()}
        if missing: raise DeliveryLedgerConfigurationError(f"PostgreSQL DeliveryLedger schema drift: missing columns {sorted(missing)}")
        cur.execute("SELECT 1 FROM pg_indexes WHERE schemaname=%s AND indexname='delivery_claim_idx'", (self._schema,))
        if cur.fetchone() is None: raise DeliveryLedgerConfigurationError("PostgreSQL DeliveryLedger schema drift: missing delivery_claim_idx")

    @staticmethod
    def compute_obligation_id(session_key: str, message_ref: str, content: str) -> str:
        return common.compute_obligation_id(session_key, message_ref, content)

    def _owner(self) -> tuple[str,str,str,int,int | None]:
        pid, started = common._owner_stamp()
        return (*self._identity, pid, started)

    def record_obligation(self, *, obligation_id: str, session_key: str, platform: str, chat_id: str, thread_id: str | None, content: str, adapter_profile: str | None = None) -> common.DeliveryReceipt:
        installation, host, generation, pid, started = self._owner()
        with self._transaction() as cur:
            cur.execute("""INSERT INTO delivery_obligations (obligation_id,session_key,platform,chat_id,thread_id,content,state,attempts,created_at,updated_at,owner_pid,owner_started_at,adapter_profile,delivery_fence,owner_installation_id,owner_host,owner_generation)
            VALUES (%s,%s,%s,%s,%s,%s,'pending',0,extract(epoch from clock_timestamp()),extract(epoch from clock_timestamp()),%s,%s,%s,0,%s,%s,%s) ON CONFLICT (obligation_id) DO NOTHING""",
            (obligation_id,session_key,platform,str(chat_id),thread_id,content,pid,started,(adapter_profile or 'default').strip() or 'default',installation,host,generation))
            cur.execute("SELECT delivery_fence FROM delivery_obligations WHERE obligation_id=%s", (obligation_id,))
            return common.DeliveryReceipt(obligation_id, int(cur.fetchone()[0]))

    def mark_attempting(self, receipt: common.DeliveryReceipt) -> common.DeliveryReceipt | None:
        installation,host,generation,pid,started = self._owner()
        with self._transaction() as cur:
            cur.execute("""UPDATE delivery_obligations SET state='attempting',delivery_fence=delivery_fence+1,updated_at=extract(epoch from clock_timestamp()),owner_installation_id=%s,owner_host=%s,owner_generation=%s,owner_pid=%s,owner_started_at=%s,lease_expires_at=extract(epoch from clock_timestamp())+%s
            WHERE obligation_id=%s AND delivery_fence=%s AND state='pending' RETURNING delivery_fence""", (installation,host,generation,pid,started,self._settings.lease_seconds,receipt.obligation_id,receipt.fence))
            row=cur.fetchone(); return common.DeliveryReceipt(receipt.obligation_id,int(row[0])) if row else None

    def renew_claim(self, receipt: common.DeliveryReceipt) -> bool:
        installation,host,generation,pid,started=self._owner()
        with self._transaction() as cur:
            cur.execute("""UPDATE delivery_obligations SET lease_expires_at=extract(epoch from clock_timestamp())+%s,updated_at=extract(epoch from clock_timestamp()) WHERE obligation_id=%s AND delivery_fence=%s AND state='attempting' AND owner_installation_id=%s AND owner_host=%s AND owner_generation=%s AND owner_pid=%s AND owner_started_at IS NOT DISTINCT FROM %s AND lease_expires_at >= extract(epoch from clock_timestamp())""", (self._settings.lease_seconds,receipt.obligation_id,receipt.fence,installation,host,generation,pid,started))
            return bool(cur.rowcount)

    def _finish(self, receipt: common.DeliveryReceipt, state: str, error: str = '', release: bool = False) -> bool:
        installation,host,generation,pid,started=self._owner()
        with self._transaction() as cur:
            cur.execute(f"""UPDATE delivery_obligations SET state=%s, updated_at=extract(epoch from clock_timestamp()), last_error=%s, lease_expires_at=NULL {", attempts=GREATEST(attempts-1,0)" if release else ""}
            WHERE obligation_id=%s AND delivery_fence=%s AND state='attempting' AND owner_installation_id=%s AND owner_host=%s AND owner_generation=%s AND owner_pid=%s AND owner_started_at IS NOT DISTINCT FROM %s AND lease_expires_at >= extract(epoch from clock_timestamp())""", (state,error[:500] or None,receipt.obligation_id,receipt.fence,installation,host,generation,pid,started))
            return bool(cur.rowcount)

    def mark_delivered(self, receipt: common.DeliveryReceipt) -> bool: return self._finish(receipt,'delivered')
    def mark_failed(self, receipt: common.DeliveryReceipt, error: str='') -> bool: return self._finish(receipt,'failed',error)
    def release_runtime_claim(self, receipt: common.DeliveryReceipt, error: str='') -> bool: return self._finish(receipt,'failed',error,True)

    def sweep_recoverable(self, now: float | None=None, *, deliverable_platforms: set | None=None, deliverable_targets: set | None=None) -> list[dict[str,Any]]:
        """Claim expired leases atomically; server clock governs eligibility."""
        installation,host,generation,pid,started=self._owner(); claimed=[]
        with self._transaction() as cur:
            cur.execute("""SELECT obligation_id,session_key,platform,chat_id,thread_id,content,state,attempts,adapter_profile,delivery_fence FROM delivery_obligations
            WHERE state IN ('pending','attempting','failed') AND (lease_expires_at IS NULL OR lease_expires_at < extract(epoch from clock_timestamp())) FOR UPDATE SKIP LOCKED""")
            for oid,sk,platform,chat,thread,content,state,attempts,profile,fence in cur.fetchall():
                if deliverable_platforms is not None and platform not in deliverable_platforms: continue
                if deliverable_targets is not None and (platform,profile) not in deliverable_targets: continue
                if attempts >= common.MAX_ATTEMPTS:
                    cur.execute("UPDATE delivery_obligations SET state='abandoned',updated_at=extract(epoch from clock_timestamp()) WHERE obligation_id=%s AND delivery_fence=%s",(oid,fence)); continue
                cur.execute("""UPDATE delivery_obligations SET state='attempting',attempts=attempts+1,delivery_fence=delivery_fence+1,updated_at=extract(epoch from clock_timestamp()),owner_installation_id=%s,owner_host=%s,owner_generation=%s,owner_pid=%s,owner_started_at=%s,lease_expires_at=extract(epoch from clock_timestamp())+%s WHERE obligation_id=%s AND delivery_fence=%s RETURNING delivery_fence""",(installation,host,generation,pid,started,self._settings.lease_seconds,oid,fence))
                new=cur.fetchone()
                if new: claimed.append({'obligation_id':oid,'receipt':common.DeliveryReceipt(oid,int(new[0])),'session_key':sk,'platform':platform,'chat_id':chat,'thread_id':thread,'content':content,'profile':profile,'attempts':attempts+1,'needs_marker':state!='pending'})
        return claimed

    def prune(self, now: float | None=None) -> None:
        with self._transaction() as cur:
            cur.execute("DELETE FROM delivery_obligations WHERE state IN ('delivered','abandoned') AND updated_at < extract(epoch from clock_timestamp())-%s", (common._RETENTION_SECONDS,))
            cur.execute("SELECT count(*) FROM delivery_obligations"); excess=max(0,int(cur.fetchone()[0])-common._MAX_ROWS)
            if excess: cur.execute("DELETE FROM delivery_obligations WHERE obligation_id IN (SELECT obligation_id FROM delivery_obligations ORDER BY CASE state WHEN 'delivered' THEN 0 WHEN 'abandoned' THEN 1 ELSE 2 END,updated_at LIMIT %s)",(excess,))

    def debug_rows(self, limit: int=20) -> list[dict[str,Any]]:
        with self._transaction() as cur:
            cur.execute("SELECT obligation_id,state,attempts,last_error FROM delivery_obligations ORDER BY updated_at DESC LIMIT %s",(limit,))
            return [dict(zip(('obligation_id','state','attempts','last_error'), row)) for row in cur.fetchall()]

    def close(self) -> None: self._closed=True
