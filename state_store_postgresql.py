"""PostgreSQL implementation of the first State Store session/message slice.

This deliberately owns only the narrow compatibility contract in ``state_store``.
It uses a fixed internal schema name, never interpolates caller data into SQL, and
is not yet a replacement for the full SessionDB state surface.
"""

from __future__ import annotations

import contextlib
import importlib
import queue
import threading
import time
from collections.abc import Iterator
from typing import Any

from state_store import PostgreSQLStateStoreConfig, StateStoreConfigurationError

_SCHEMA = "hermes_state_store_slice"
_SCHEMA_VERSION = 1


class PostgreSQLStateStore:
    """Thread-safe bounded psycopg connection pool for session/message persistence."""

    def __init__(self, settings: PostgreSQLStateStoreConfig, dsn: str) -> None:
        try:
            self._psycopg = importlib.import_module("psycopg")
        except ImportError as exc:
            raise StateStoreConfigurationError(
                "PostgreSQL State Store requires the optional dependency: pip install 'hermes-agent[state-store]'"
            ) from exc
        self._settings = settings
        self._dsn = dsn
        self._idle: queue.LifoQueue[Any] = queue.LifoQueue(maxsize=settings.pool_max_size)
        self._created = 1
        self._lock = threading.Lock()
        self._closed = False
        connection = self._new_connection()
        try:
            self._probe_and_migrate(connection)
        except Exception:
            connection.close()
            raise
        self._idle.put(connection)

    def _new_connection(self) -> Any:
        return self._psycopg.connect(
            self._dsn,
            connect_timeout=self._settings.connect_timeout_seconds,
            autocommit=False,
        )

    def _probe_and_migrate(self, connection: Any) -> None:
        with connection.cursor() as cursor:
            cursor.execute("SHOW server_version_num")
            version = int(cursor.fetchone()[0])
            if version < 180000:
                raise StateStoreConfigurationError("PostgreSQL State Store requires PostgreSQL 18 or newer")
            cursor.execute("SELECT extname FROM pg_extension WHERE extname IN ('vector', 'pg_trgm')")
            extensions = {row[0] for row in cursor.fetchall()}
            missing = {"vector", "pg_trgm"} - extensions
            if missing:
                raise StateStoreConfigurationError(
                    f"PostgreSQL State Store requires capabilities: {', '.join(sorted(missing))}"
                )
            cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {_SCHEMA}")
            cursor.execute(f"CREATE TABLE IF NOT EXISTS {_SCHEMA}.schema_migrations (version integer PRIMARY KEY, applied_at double precision NOT NULL)")
            cursor.execute(f"SELECT version FROM {_SCHEMA}.schema_migrations WHERE version = %s", (_SCHEMA_VERSION,))
            if cursor.fetchone() is None:
                cursor.execute(
                    f"CREATE TABLE {_SCHEMA}.sessions ("
                    "id text PRIMARY KEY, source text NOT NULL, started_at double precision NOT NULL, "
                    "ended_at double precision, end_reason text)"
                )
                cursor.execute(
                    f"CREATE TABLE {_SCHEMA}.messages ("
                    "id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, session_id text NOT NULL "
                    f"REFERENCES {_SCHEMA}.sessions(id), role text NOT NULL, content text, created_at double precision NOT NULL)"
                )
                cursor.execute(f"CREATE INDEX messages_session_id_id ON {_SCHEMA}.messages (session_id, id)")
                cursor.execute(
                    f"INSERT INTO {_SCHEMA}.schema_migrations (version, applied_at) VALUES (%s, %s)",
                    (_SCHEMA_VERSION, time.time()),
                )
        connection.commit()

    @contextlib.contextmanager
    def _connection(self) -> Iterator[Any]:
        with self._lock:
            if self._closed:
                raise RuntimeError("PostgreSQL State Store is closed")
            try:
                connection = self._idle.get_nowait()
            except queue.Empty:
                if self._created < self._settings.pool_max_size:
                    self._created += 1
                    connection = self._new_connection()
                else:
                    connection = None
        if connection is None:
            connection = self._idle.get()
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            with self._lock:
                if self._closed:
                    connection.close()
                else:
                    self._idle.put(connection)

    def ensure_session(self, session_id: str, source: str = "unknown") -> str:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {_SCHEMA}.sessions (id, source, started_at) VALUES (%s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (session_id, source, time.time()),
            )
        return session_id

    def append_message(self, session_id: str, *, role: str, content: str | None = None) -> int:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {_SCHEMA}.messages (session_id, role, content, created_at) VALUES (%s, %s, %s, %s) RETURNING id",
                (session_id, role, content, time.time()),
            )
            return int(cursor.fetchone()[0])

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            cursor.execute(
                f"SELECT id, session_id, role, content, created_at FROM {_SCHEMA}.messages WHERE session_id = %s ORDER BY id",
                (session_id,),
            )
            return list(cursor.fetchall())

    def end_session(self, session_id: str, end_reason: str) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {_SCHEMA}.sessions SET ended_at = %s, end_reason = %s "
                "WHERE id = %s AND ended_at IS NULL",
                (time.time(), end_reason, session_id),
            )

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            cursor.execute(f"SELECT id, source, started_at, ended_at, end_reason FROM {_SCHEMA}.sessions WHERE id = %s", (session_id,))
            return cursor.fetchone()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            connections = []
            while True:
                try:
                    connections.append(self._idle.get_nowait())
                except queue.Empty:
                    break
        for connection in connections:
            connection.close()
