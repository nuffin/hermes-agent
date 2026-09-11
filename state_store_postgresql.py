"""PostgreSQL implementation of the first State Store session/message slice.

This deliberately owns only the narrow compatibility contract in ``state_store``.
It uses a fixed internal schema name, never interpolates caller data into SQL, and
is not yet a replacement for the full SessionDB state surface.
"""

from __future__ import annotations

import contextlib
import importlib
import queue
import re
import threading
import time
from collections.abc import Iterator, Mapping
from typing import Any

from state_store import PostgreSQLStateStoreConfig, StateStoreConfigurationError

_SCHEMA = "hermes_state_store_slice"
_SESSION_METADATA_SCHEMA_VERSION = 2
_PARENT_SESSION_FOREIGN_KEY_SCHEMA_VERSION = 3
# Version 4 is a deliberately recorded compatibility checkpoint. It has no DDL
# because it only establishes a durable, validated ledger boundary for the v1-v3
# contract after releases that wrote the parent key outside the migration ledger.
_COMPATIBILITY_CHECKPOINT_SCHEMA_VERSION = 4
_SCHEMA_VERSION = 5
_VISIBILITY_SCHEMA_VERSION = 6
_TITLE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TITLE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
_TITLE_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff\ufffc\ufff9-\ufffb]")
_NUMBERED_TITLE_RE = re.compile(r"^(.*?) #(\d+)$")
_TITLE_SOURCE_RANK = {"derived": 0, "llm": 1, "user": 2}
_CANONICAL_BOT_CHAT_TITLE = "Bot Chat"
_MAX_TITLE_LENGTH = 100
_SESSION_METADATA_COLUMNS = (
    "user_id", "session_key", "chat_id", "chat_type", "thread_id", "display_name", "origin_json",
    "model", "model_config", "parent_session_id", "cwd", "profile_name", "git_repo_root",
)
_SESSION_METADATA_TYPES = {
    "user_id": "text", "session_key": "text", "chat_id": "text", "chat_type": "text", "thread_id": "text",
    "display_name": "text", "origin_json": "text", "model": "text", "model_config": "jsonb",
    "parent_session_id": "text", "cwd": "text", "profile_name": "text", "git_repo_root": "text",
}


def _sanitize_title(title: str | None) -> str | None:
    if not title:
        return None
    cleaned = _TITLE_INVISIBLE_RE.sub("", _TITLE_CONTROL_RE.sub("", _TITLE_SURROGATE_RE.sub("\ufffd", str(title))))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None
    if len(cleaned) > _MAX_TITLE_LENGTH:
        raise ValueError(f"Title too long ({len(cleaned)} chars, max {_MAX_TITLE_LENGTH})")
    return cleaned


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
        """Apply and validate every migration in order in one locked transaction.

        A ledger row is evidence only after its corresponding catalog contract has
        been validated. This rejects drift instead of silently treating a marker as
        proof that an older or manually modified schema is usable.
        """
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("hermes_state_store_slice_migration",))
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
                cursor.execute(f"SELECT version FROM {_SCHEMA}.schema_migrations ORDER BY version")
                applied = {int(row[0]) for row in cursor.fetchall()}
                unsupported = sorted(version for version in applied if version < 1 or version > _VISIBILITY_SCHEMA_VERSION)
                if unsupported:
                    raise StateStoreConfigurationError(f"Unsupported PostgreSQL State Store schema migration versions: {unsupported}")
                migrations = (
                    (1, self._apply_v1, self._validate_v1),
                    (_SESSION_METADATA_SCHEMA_VERSION, self._apply_v2, self._validate_v2),
                    (_PARENT_SESSION_FOREIGN_KEY_SCHEMA_VERSION, self._apply_v3, self._validate_v3),
                    (_COMPATIBILITY_CHECKPOINT_SCHEMA_VERSION, self._apply_v4, self._validate_v4),
                    (_SCHEMA_VERSION, self._apply_v5, self._validate_v5),
                    (_VISIBILITY_SCHEMA_VERSION, self._apply_v6, self._validate_v6),
                )
                for migration_version, apply, validate in migrations:
                    if migration_version not in applied:
                        apply(cursor)
                        validate(cursor)
                        cursor.execute(
                            f"INSERT INTO {_SCHEMA}.schema_migrations (version, applied_at) VALUES (%s, %s)",
                            (migration_version, time.time()),
                        )
                    else:
                        validate(cursor)
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _required_columns(cursor: Any, table: str, columns: set[str]) -> None:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s",
            (_SCHEMA, table),
        )
        missing = columns - {str(row[0]) for row in cursor.fetchall()}
        if missing:
            raise StateStoreConfigurationError(
                f"PostgreSQL State Store schema drift: {_SCHEMA}.{table} is missing columns {sorted(missing)}"
            )

    @staticmethod
    def _require_index(cursor: Any, name: str) -> None:
        cursor.execute("SELECT 1 FROM pg_class WHERE relkind = 'i' AND relname = %s AND relnamespace = %s::regnamespace", (name, _SCHEMA))
        if cursor.fetchone() is None:
            raise StateStoreConfigurationError(f"PostgreSQL State Store schema drift: missing index {_SCHEMA}.{name}")

    @staticmethod
    def _require_foreign_key(cursor: Any, name: str, table: str, target: str) -> None:
        cursor.execute(
            "SELECT 1 FROM pg_constraint WHERE conname = %s AND conrelid = %s::regclass "
            "AND contype = 'f' AND confrelid = %s::regclass",
            (name, f"{_SCHEMA}.{table}", f"{_SCHEMA}.{target}"),
        )
        if cursor.fetchone() is None:
            raise StateStoreConfigurationError(f"PostgreSQL State Store schema drift: missing or invalid foreign key {_SCHEMA}.{name}")

    def _apply_v1(self, cursor: Any) -> None:
        cursor.execute(
            f"CREATE TABLE IF NOT EXISTS {_SCHEMA}.sessions ("
            "id text PRIMARY KEY, source text NOT NULL, started_at double precision NOT NULL, "
            "ended_at double precision, end_reason text)"
        )
        cursor.execute(
            f"CREATE TABLE IF NOT EXISTS {_SCHEMA}.messages ("
            "id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, session_id text NOT NULL "
            f"REFERENCES {_SCHEMA}.sessions(id), role text NOT NULL, content text, created_at double precision NOT NULL)"
        )
        cursor.execute(f"CREATE INDEX IF NOT EXISTS messages_session_id_id ON {_SCHEMA}.messages (session_id, id)")

    def _validate_v1(self, cursor: Any) -> None:
        self._required_columns(cursor, "sessions", {"id", "source", "started_at", "ended_at", "end_reason"})
        self._required_columns(cursor, "messages", {"id", "session_id", "role", "content", "created_at"})
        self._require_index(cursor, "messages_session_id_id")
        self._require_foreign_key(cursor, "messages_session_id_fkey", "messages", "sessions")

    def _apply_v2(self, cursor: Any) -> None:
        for column in _SESSION_METADATA_COLUMNS:
            cursor.execute(f"ALTER TABLE {_SCHEMA}.sessions ADD COLUMN IF NOT EXISTS {column} {_SESSION_METADATA_TYPES[column]}")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS sessions_source_session_key ON {_SCHEMA}.sessions (source, session_key)")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS sessions_parent_session_id ON {_SCHEMA}.sessions (parent_session_id)")

    def _validate_v2(self, cursor: Any) -> None:
        self._required_columns(cursor, "sessions", set(_SESSION_METADATA_COLUMNS))
        self._require_index(cursor, "sessions_source_session_key")
        self._require_index(cursor, "sessions_parent_session_id")

    def _apply_v3(self, cursor: Any) -> None:
        cursor.execute(
            "SELECT 1 FROM pg_constraint WHERE conname = %s AND conrelid = %s::regclass "
            "AND contype = 'f' AND confrelid = %s::regclass",
            ("sessions_parent_session_id_fkey", f"{_SCHEMA}.sessions", f"{_SCHEMA}.sessions"),
        )
        if cursor.fetchone() is None:
            cursor.execute(
                f"ALTER TABLE {_SCHEMA}.sessions ADD CONSTRAINT sessions_parent_session_id_fkey "
                f"FOREIGN KEY (parent_session_id) REFERENCES {_SCHEMA}.sessions(id) NOT VALID"
            )

    def _validate_v3(self, cursor: Any) -> None:
        self._require_foreign_key(cursor, "sessions_parent_session_id_fkey", "sessions", "sessions")

    def _apply_v4(self, cursor: Any) -> None:
        # See the module-level constant: this records validation of the legacy,
        # previously unledgered parent-key transition and intentionally has no DDL.
        return None

    def _validate_v4(self, cursor: Any) -> None:
        self._validate_v1(cursor)
        self._validate_v2(cursor)
        self._validate_v3(cursor)

    def _apply_v5(self, cursor: Any) -> None:
        cursor.execute(f"ALTER TABLE {_SCHEMA}.sessions ADD COLUMN IF NOT EXISTS title text")
        cursor.execute(f"ALTER TABLE {_SCHEMA}.sessions ADD COLUMN IF NOT EXISTS title_source text")
        cursor.execute(f"ALTER TABLE {_SCHEMA}.sessions ADD COLUMN IF NOT EXISTS hidden boolean NOT NULL DEFAULT false")
        cursor.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS sessions_title_unique ON {_SCHEMA}.sessions (title) WHERE title IS NOT NULL")

    def _validate_v5(self, cursor: Any) -> None:
        self._required_columns(cursor, "sessions", {"title", "title_source", "hidden"})
        self._require_index(cursor, "sessions_title_unique")

    def _apply_v6(self, cursor: Any) -> None:
        cursor.execute(f"ALTER TABLE {_SCHEMA}.sessions ADD COLUMN IF NOT EXISTS archived boolean NOT NULL DEFAULT false")
        cursor.execute(f"ALTER TABLE {_SCHEMA}.sessions ADD COLUMN IF NOT EXISTS pinned boolean NOT NULL DEFAULT false")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS sessions_visibility_started_at ON {_SCHEMA}.sessions (archived, hidden, started_at DESC)")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS sessions_pinned_started_at ON {_SCHEMA}.sessions (pinned, started_at DESC) WHERE pinned")

    def _validate_v6(self, cursor: Any) -> None:
        self._required_columns(cursor, "sessions", {"archived", "pinned"})
        self._require_index(cursor, "sessions_visibility_started_at")
        self._require_index(cursor, "sessions_pinned_started_at")

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

    def ensure_session(
        self, session_id: str, source: str = "unknown", *, metadata: Mapping[str, Any] | None = None,
    ) -> str:
        metadata = dict(metadata or {})
        unknown = set(metadata) - set(_SESSION_METADATA_COLUMNS)
        if unknown:
            raise ValueError(f"Unsupported session metadata fields: {', '.join(sorted(unknown))}")
        metadata_columns = tuple(column for column in _SESSION_METADATA_COLUMNS if column in metadata)
        columns = ("id", "source", "started_at", *metadata_columns)
        placeholders = ", ".join("%s" for _ in columns)
        values = [session_id, source, time.time()]
        for column in metadata_columns:
            value = metadata[column]
            if column == "model_config":
                value = self._psycopg.types.json.Jsonb(value) if value else None
            values.append(value)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {_SCHEMA}.sessions ({', '.join(columns)}) VALUES ({placeholders}) "
                "ON CONFLICT (id) DO UPDATE SET "
                "model = COALESCE(sessions.model, EXCLUDED.model), "
                "model_config = COALESCE(sessions.model_config, EXCLUDED.model_config), "
                "session_key = COALESCE(sessions.session_key, EXCLUDED.session_key), "
                "chat_id = COALESCE(sessions.chat_id, EXCLUDED.chat_id), "
                "chat_type = COALESCE(sessions.chat_type, EXCLUDED.chat_type), "
                "thread_id = COALESCE(sessions.thread_id, EXCLUDED.thread_id), "
                "parent_session_id = COALESCE(sessions.parent_session_id, EXCLUDED.parent_session_id), "
                "cwd = COALESCE(sessions.cwd, EXCLUDED.cwd), "
                "profile_name = COALESCE(sessions.profile_name, EXCLUDED.profile_name), "
                "git_repo_root = COALESCE(sessions.git_repo_root, EXCLUDED.git_repo_root), "
                "origin_json = COALESCE(sessions.origin_json, EXCLUDED.origin_json), "
                "display_name = COALESCE(sessions.display_name, EXCLUDED.display_name)",
                values,
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
            cursor.execute(
                f"SELECT id, source, started_at, ended_at, end_reason, title, title_source, hidden, archived, pinned, {', '.join(_SESSION_METADATA_COLUMNS)} "
                f"FROM {_SCHEMA}.sessions WHERE id = %s", (session_id,),
            )
            return cursor.fetchone()

    def set_session_hidden(self, session_id: str, hidden: bool) -> bool:
        return self._set_lineage_column("hidden", session_id, hidden)

    def _set_lineage_column(self, column: str, session_id: str, value: bool) -> bool:
        """Apply a visibility flag to the whole compression lineage in one transaction."""
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""WITH RECURSIVE
                    ancestors(id) AS (
                        SELECT %s
                        UNION
                        SELECT parent.id FROM ancestors a
                        JOIN {_SCHEMA}.sessions child ON child.id = a.id
                        JOIN {_SCHEMA}.sessions parent ON parent.id = child.parent_session_id
                        WHERE parent.end_reason = 'compression'
                    ),
                    descendants(id) AS (
                        SELECT %s
                        UNION
                        SELECT child.id FROM descendants d
                        JOIN {_SCHEMA}.sessions parent ON parent.id = d.id
                        JOIN {_SCHEMA}.sessions child ON child.parent_session_id = parent.id
                        WHERE parent.end_reason = 'compression'
                    ), lineage(id) AS (
                        SELECT id FROM ancestors UNION SELECT id FROM descendants
                    )
                    UPDATE {_SCHEMA}.sessions SET {column} = %s WHERE id IN (SELECT id FROM lineage)""",
                (session_id, session_id, value),
            )
            return cursor.rowcount > 0

    def set_session_archived(self, session_id: str, archived: bool) -> bool:
        return self._set_lineage_column("archived", session_id, archived)

    def set_session_pinned(self, session_id: str, pinned: bool) -> bool:
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            cursor.execute(f"SELECT title, hidden FROM {_SCHEMA}.sessions WHERE id = %s FOR UPDATE", (session_id,))
            row = cursor.fetchone()
            if row is None:
                return False
            cursor.execute(
                f"""WITH RECURSIVE
                    ancestors(id) AS (SELECT %s UNION SELECT parent.id FROM ancestors a JOIN {_SCHEMA}.sessions child ON child.id = a.id JOIN {_SCHEMA}.sessions parent ON parent.id = child.parent_session_id WHERE parent.end_reason = 'compression'),
                    descendants(id) AS (SELECT %s UNION SELECT child.id FROM descendants d JOIN {_SCHEMA}.sessions parent ON parent.id = d.id JOIN {_SCHEMA}.sessions child ON child.parent_session_id = parent.id WHERE parent.end_reason = 'compression'),
                    lineage(id) AS (SELECT id FROM ancestors UNION SELECT id FROM descendants)
                    UPDATE {_SCHEMA}.sessions SET pinned = %s WHERE id IN (SELECT id FROM lineage)""",
                (session_id, session_id, pinned),
            )
            changed = cursor.rowcount > 0
            if pinned and not (row["hidden"] and row["title"] == _CANONICAL_BOT_CHAT_TITLE):
                cursor.execute(
                    f"""WITH RECURSIVE
                        ancestors(id) AS (SELECT %s UNION SELECT parent.id FROM ancestors a JOIN {_SCHEMA}.sessions child ON child.id = a.id JOIN {_SCHEMA}.sessions parent ON parent.id = child.parent_session_id WHERE parent.end_reason = 'compression'),
                        descendants(id) AS (SELECT %s UNION SELECT child.id FROM descendants d JOIN {_SCHEMA}.sessions parent ON parent.id = d.id JOIN {_SCHEMA}.sessions child ON child.parent_session_id = parent.id WHERE parent.end_reason = 'compression'),
                        lineage(id) AS (SELECT id FROM ancestors UNION SELECT id FROM descendants)
                        UPDATE {_SCHEMA}.sessions SET hidden = false WHERE id IN (SELECT id FROM lineage)""",
                    (session_id, session_id),
                )
            return changed

    def list_session_summaries(
        self, *, source: str | None = None, exclude_sources: tuple[str, ...] = (),
        limit: int = 20, offset: int = 0, include_archived: bool = False,
        archived_only: bool = False, include_hidden: bool = False,
        include_pinned: bool = False,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if source is not None:
            clauses.append("s.source = %s"); params.append(source)
        if exclude_sources:
            clauses.append("NOT (s.source = ANY(%s))"); params.append(list(exclude_sources))
        if archived_only:
            clauses.append("s.archived")
        elif not include_archived:
            clauses.append("NOT s.archived")
        if not include_hidden:
            clauses.append("NOT s.hidden")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        projection = (
            "s.id, s.source, s.started_at, s.ended_at, s.end_reason, s.parent_session_id, s.title, "
            "s.title_source, s.hidden, s.archived, s.pinned, "
            "COALESCE(MAX(m.created_at), s.started_at) AS last_active, COUNT(m.id)::integer AS message_count"
        )
        grouped = f" FROM {_SCHEMA}.sessions s LEFT JOIN {_SCHEMA}.messages m ON m.session_id = s.id{where} GROUP BY s.id"
        order = " ORDER BY last_active DESC, s.started_at DESC, s.id DESC"
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            cursor.execute(f"SELECT {projection}{grouped}{order} LIMIT %s OFFSET %s", [*params, limit, offset])
            rows = list(cursor.fetchall())
            if include_pinned:
                seen = {row["id"] for row in rows}
                pinned_where = where + (" AND s.pinned" if where else " WHERE s.pinned")
                cursor.execute(f"SELECT {projection} FROM {_SCHEMA}.sessions s LEFT JOIN {_SCHEMA}.messages m ON m.session_id = s.id{pinned_where} GROUP BY s.id{order}", params)
                rows.extend(row for row in cursor.fetchall() if row["id"] not in seen)
            return rows

    def _set_session_title(self, session_id: str, title: str, *, source: str) -> bool:
        cleaned_title = _sanitize_title(title)
        is_user = source == "user"
        if not is_user and source not in {"derived", "llm"}:
            raise ValueError(f"invalid automatic title source: {source!r}")
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            cursor.execute(f"SELECT title, title_source, hidden FROM {_SCHEMA}.sessions WHERE id = %s FOR UPDATE", (session_id,))
            current = cursor.fetchone()
            if current is None:
                return False
            if current["title"] == _CANONICAL_BOT_CHAT_TITLE and current["hidden"] and cleaned_title != _CANONICAL_BOT_CHAT_TITLE:
                if is_user:
                    raise ValueError("This is the bot's canonical Bot Chat — its name is its identity, and renaming it would orphan the conversation. To start fresh, create a new bot instead.")
                return False
            rank = _TITLE_SOURCE_RANK.get(current["title_source"], 2 if current["title_source"] is None else 0)
            if not is_user and current["title"] is not None and rank >= _TITLE_SOURCE_RANK[source]:
                return False
            if cleaned_title:
                cursor.execute(f"SELECT id FROM {_SCHEMA}.sessions WHERE title = %s AND id != %s FOR UPDATE", (cleaned_title, session_id))
                conflict = cursor.fetchone()
                if conflict:
                    conflict_id = conflict["id"]
                    cursor.execute(
                        f"WITH RECURSIVE ancestors(id) AS (SELECT %s UNION SELECT parent.id FROM ancestors a JOIN {_SCHEMA}.sessions child ON child.id = a.id JOIN {_SCHEMA}.sessions parent ON parent.id = child.parent_session_id WHERE parent.end_reason = 'compression') SELECT 1 FROM ancestors WHERE id = %s AND id != %s LIMIT 1",
                        (session_id, conflict_id, session_id),
                    )
                    if cursor.fetchone() is None:
                        raise ValueError(f"Title '{cleaned_title}' is already in use by session {conflict_id}")
                    cursor.execute(f"UPDATE {_SCHEMA}.sessions SET title = NULL WHERE id = %s", (conflict_id,))
            cursor.execute(f"UPDATE {_SCHEMA}.sessions SET title = %s, title_source = %s WHERE id = %s", (cleaned_title, source if cleaned_title else None, session_id))
            return cursor.rowcount > 0

    def set_session_title(self, session_id: str, title: str) -> bool:
        return self._set_session_title(session_id, title, source="user")

    def set_auto_title(self, session_id: str, title: str, *, source: str) -> bool:
        return self._set_session_title(session_id, title, source=source)

    def get_session_title(self, session_id: str) -> str | None:
        row = self.get_session(session_id)
        return None if row is None else row["title"]

    def get_session_title_source(self, session_id: str) -> str | None:
        row = self.get_session(session_id)
        return None if row is None or row["title"] is None else row["title_source"]

    def set_session_title_source(self, session_id: str, source: str) -> bool:
        if source not in _TITLE_SOURCE_RANK:
            raise ValueError(f"invalid title source: {source!r}")
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"UPDATE {_SCHEMA}.sessions SET title_source = %s WHERE id = %s AND title IS NOT NULL", (source, session_id))
            return cursor.rowcount > 0

    def get_session_by_title(self, title: str) -> dict[str, Any] | None:
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            cursor.execute(f"SELECT id, source, started_at, ended_at, end_reason, title, title_source, hidden, archived, pinned, {', '.join(_SESSION_METADATA_COLUMNS)} FROM {_SCHEMA}.sessions WHERE title = %s", (title,))
            return cursor.fetchone()

    def resolve_session_by_title(self, title: str) -> str | None:
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            cursor.execute(f"SELECT id FROM {_SCHEMA}.sessions WHERE title LIKE %s ESCAPE '\\' ORDER BY started_at DESC", (_escape_like(title) + " #%",))
            row = cursor.fetchone()
            if row:
                return str(row["id"])
            cursor.execute(f"SELECT id FROM {_SCHEMA}.sessions WHERE title = %s", (title,))
            row = cursor.fetchone()
            return None if row is None else str(row["id"])

    def get_next_title_in_lineage(self, base_title: str) -> str:
        match = _NUMBERED_TITLE_RE.match(base_title)
        base = match.group(1) if match else base_title
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            cursor.execute(f"SELECT title FROM {_SCHEMA}.sessions WHERE title = %s OR title LIKE %s ESCAPE '\\'", (base, _escape_like(base) + " #%"))
            rows = cursor.fetchall()
        if not rows:
            return base
        numbers = [int(match.group(2)) for row in rows if (match := _NUMBERED_TITLE_RE.match(row["title"]))]
        return f"{base} #{max([1, *numbers]) + 1}"

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
