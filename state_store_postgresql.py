"""PostgreSQL implementation of the first State Store session/message slice.

This deliberately owns only the narrow compatibility contract in ``state_store``.
It uses a fixed internal schema name, never interpolates caller data into SQL, and
is not yet a replacement for the full SessionDB state surface.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import json
import queue
import re
import threading
import time
from collections.abc import Iterator, Mapping
from typing import Any

from hermes_cli.timefmt import coerce_epoch
from state_store import MessageRecord, PostgreSQLStateStoreConfig, StateStoreConfigurationError
from token_usage_transport import TokenUsageTransport

_SCHEMA = "hermes_state_store_slice"
_SESSION_METADATA_SCHEMA_VERSION = 2
_PARENT_SESSION_FOREIGN_KEY_SCHEMA_VERSION = 3
# Version 4 is a deliberately recorded compatibility checkpoint. It has no DDL
# because it only establishes a durable, validated ledger boundary for the v1-v3
# contract after releases that wrote the parent key outside the migration ledger.
_COMPATIBILITY_CHECKPOINT_SCHEMA_VERSION = 4
_SCHEMA_VERSION = 5
_VISIBILITY_SCHEMA_VERSION = 6
_MESSAGE_RECORD_SCHEMA_VERSION = 7
_RESUME_PROJECTION_SCHEMA_VERSION = 8
_SYSTEM_PROMPT_SCHEMA_VERSION = 9
_MODEL_USAGE_SCHEMA_VERSION = 10
_USAGE_COUNTERS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
_USAGE_SUM_FIELDS = (*_USAGE_COUNTERS, "api_call_count")
_USAGE_ROUTE_FIELDS = ("model", "cost_status", "cost_source", "pricing_version", "billing_provider", "billing_base_url", "billing_mode")
_USAGE_SESSION_COLUMNS = (*_USAGE_COUNTERS, "estimated_cost_usd", "actual_cost_usd", "cost_status", "cost_source", "pricing_version", "billing_provider", "billing_base_url", "billing_mode", "api_call_count")
_MESSAGE_RECORD_COLUMNS = (
    "tool_call_id", "tool_calls", "tool_name", "effect_disposition", "token_count", "finish_reason",
    "reasoning", "reasoning_content", "reasoning_details", "codex_reasoning_items", "codex_message_items",
    "platform_message_id", "observed", "_compressed_summary", "active", "compacted", "api_content",
    "display_kind", "display_metadata", "display_identity",
)
_MESSAGE_RECORD_WRITE_COLUMNS = tuple(
    column for column in _MESSAGE_RECORD_COLUMNS if column not in {"active", "compacted", "display_identity"}
)
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
        self._token_usage_transport = TokenUsageTransport(
            self._persist_token_usage_delta, sum_fields=_USAGE_SUM_FIELDS,
            cost_fields=("estimated_cost_usd", "actual_cost_usd"), route_fields=_USAGE_ROUTE_FIELDS,
            idle_seconds=lambda: 1.0,
        )
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
                unsupported = sorted(version for version in applied if version < 1 or version > _MODEL_USAGE_SCHEMA_VERSION)
                if unsupported:
                    raise StateStoreConfigurationError(f"Unsupported PostgreSQL State Store schema migration versions: {unsupported}")
                migrations = (
                    (1, self._apply_v1, self._validate_v1),
                    (_SESSION_METADATA_SCHEMA_VERSION, self._apply_v2, self._validate_v2),
                    (_PARENT_SESSION_FOREIGN_KEY_SCHEMA_VERSION, self._apply_v3, self._validate_v3),
                    (_COMPATIBILITY_CHECKPOINT_SCHEMA_VERSION, self._apply_v4, self._validate_v4),
                    (_SCHEMA_VERSION, self._apply_v5, self._validate_v5),
                    (_VISIBILITY_SCHEMA_VERSION, self._apply_v6, self._validate_v6),
                    (_MESSAGE_RECORD_SCHEMA_VERSION, self._apply_v7, self._validate_v7),
                    (_RESUME_PROJECTION_SCHEMA_VERSION, self._apply_v8, self._validate_v8),
                    (_SYSTEM_PROMPT_SCHEMA_VERSION, self._apply_v9, self._validate_v9),
                    (_MODEL_USAGE_SCHEMA_VERSION, self._apply_v10, self._validate_v10),
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

    def _apply_v7(self, cursor: Any) -> None:
        types = {
            "tool_call_id": "text", "tool_calls": "jsonb", "tool_name": "text", "effect_disposition": "text",
            "token_count": "bigint", "finish_reason": "text", "reasoning": "text", "reasoning_content": "text",
            "reasoning_details": "text", "codex_reasoning_items": "text", "codex_message_items": "text",
            "platform_message_id": "text", "observed": "boolean NOT NULL DEFAULT false",
            "_compressed_summary": "boolean NOT NULL DEFAULT false", "active": "boolean NOT NULL DEFAULT true",
            "compacted": "boolean NOT NULL DEFAULT false", "api_content": "text", "display_kind": "text",
            "display_metadata": "jsonb", "display_identity": "text",
        }
        for column in _MESSAGE_RECORD_COLUMNS:
            cursor.execute(f"ALTER TABLE {_SCHEMA}.messages ADD COLUMN IF NOT EXISTS {column} {types[column]}")

    def _validate_v7(self, cursor: Any) -> None:
        self._required_columns(cursor, "messages", set(_MESSAGE_RECORD_COLUMNS))

    def _apply_v8(self, cursor: Any) -> None:
        cursor.execute(f"CREATE INDEX IF NOT EXISTS messages_resume_projection ON {_SCHEMA}.messages (session_id, active, id)")

    def _validate_v8(self, cursor: Any) -> None:
        self._validate_v7(cursor)
        self._require_index(cursor, "messages_resume_projection")

    def _apply_v9(self, cursor: Any) -> None:
        cursor.execute(f"CREATE TABLE IF NOT EXISTS {_SCHEMA}.system_prompts (hash text PRIMARY KEY, prompt text NOT NULL)")
        cursor.execute(f"ALTER TABLE {_SCHEMA}.sessions ADD COLUMN IF NOT EXISTS system_prompt_hash text")
        cursor.execute(
            "SELECT 1 FROM pg_constraint WHERE conname = %s AND conrelid = %s::regclass "
            "AND contype = 'f' AND confrelid = %s::regclass",
            ("sessions_system_prompt_hash_fkey", f"{_SCHEMA}.sessions", f"{_SCHEMA}.system_prompts"),
        )
        if cursor.fetchone() is None:
            cursor.execute(
                f"ALTER TABLE {_SCHEMA}.sessions ADD CONSTRAINT sessions_system_prompt_hash_fkey "
                f"FOREIGN KEY (system_prompt_hash) REFERENCES {_SCHEMA}.system_prompts(hash)"
            )

    def _validate_v9(self, cursor: Any) -> None:
        self._required_columns(cursor, "sessions", {"system_prompt_hash"})
        self._required_columns(cursor, "system_prompts", {"hash", "prompt"})
        self._require_foreign_key(cursor, "sessions_system_prompt_hash_fkey", "sessions", "system_prompts")

    def _apply_v10(self, cursor: Any) -> None:
        for column in _USAGE_SESSION_COLUMNS:
            type_name = "bigint NOT NULL DEFAULT 0" if column in {*_USAGE_COUNTERS, "api_call_count"} else ("double precision" if column in {"estimated_cost_usd", "actual_cost_usd"} else "text")
            cursor.execute(f"ALTER TABLE {_SCHEMA}.sessions ADD COLUMN IF NOT EXISTS {column} {type_name}")
        cursor.execute(f'''CREATE TABLE IF NOT EXISTS {_SCHEMA}.session_model_usage (
            session_id text NOT NULL REFERENCES {_SCHEMA}.sessions(id) ON DELETE CASCADE,
            model text NOT NULL, billing_provider text NOT NULL DEFAULT '', billing_base_url text NOT NULL DEFAULT '',
            billing_mode text NOT NULL DEFAULT '', task text NOT NULL DEFAULT '', api_call_count bigint NOT NULL DEFAULT 0,
            input_tokens bigint NOT NULL DEFAULT 0, output_tokens bigint NOT NULL DEFAULT 0,
            cache_read_tokens bigint NOT NULL DEFAULT 0, cache_write_tokens bigint NOT NULL DEFAULT 0,
            reasoning_tokens bigint NOT NULL DEFAULT 0, estimated_cost_usd double precision NOT NULL DEFAULT 0,
            actual_cost_usd double precision NOT NULL DEFAULT 0, cost_status text, cost_source text,
            first_seen double precision, last_seen double precision,
            PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task))''')
        cursor.execute(f"CREATE INDEX IF NOT EXISTS session_model_usage_session ON {_SCHEMA}.session_model_usage (session_id)")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS session_model_usage_model ON {_SCHEMA}.session_model_usage (model)")

    def _validate_v10(self, cursor: Any) -> None:
        self._required_columns(cursor, "sessions", set(_USAGE_SESSION_COLUMNS))
        self._required_columns(cursor, "session_model_usage", {"session_id", "model", "billing_provider", "billing_base_url", "billing_mode", "task", "api_call_count", *_USAGE_COUNTERS, "estimated_cost_usd", "actual_cost_usd", "cost_status", "cost_source", "first_seen", "last_seen"})
        self._require_index(cursor, "session_model_usage_session")
        self._require_index(cursor, "session_model_usage_model")
        self._require_foreign_key(cursor, "session_model_usage_session_id_fkey", "session_model_usage", "sessions")

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

    @staticmethod
    def _encode_content(content: Any) -> Any:
        if isinstance(content, str) or content is None or isinstance(content, (bytes, int, float)):
            return content
        try:
            return "__hermes_state_json__:" + json.dumps(content)
        except (TypeError, ValueError):
            return str(content)

    @staticmethod
    def _record_json(value: Any, *, object_only: bool = False) -> Any:
        if not value:
            return None
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                return None if object_only else []
        if object_only and not isinstance(value, dict):
            return None
        return value

    @staticmethod
    def _record_json_text(value: Any) -> str | None:
        return None if not value else (value if isinstance(value, str) else json.dumps(value))

    def _record_params(self, session_id: str, record: MessageRecord) -> tuple[Any, ...]:
        timestamp = coerce_epoch(record.timestamp, field="message timestamp")
        tool_calls = self._record_json(record.tool_calls)
        display_metadata = self._record_json(record.display_metadata, object_only=True)
        return (
            session_id, record.role, self._encode_content(record.content),
            timestamp if timestamp is not None else time.time(), record.tool_call_id,
            self._psycopg.types.json.Jsonb(tool_calls) if tool_calls else None, record.tool_name,
            record.effect_disposition, record.token_count,
            record.finish_reason, record.reasoning, record.reasoning_content,
            self._record_json_text(record.reasoning_details), self._record_json_text(record.codex_reasoning_items),
            self._record_json_text(record.codex_message_items), record.platform_message_id, bool(record.observed),
            bool(record._compressed_summary), record.api_content, record.display_kind,
            self._psycopg.types.json.Jsonb(display_metadata) if display_metadata else None,
        )

    def append_message(self, session_id: str, *, role: str, content: str | None = None) -> int:
        return self.append_message_record(session_id, MessageRecord(role=role, content=content))

    def append_message_record(self, session_id: str, record: MessageRecord) -> int:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {_SCHEMA}.messages (session_id, role, content, created_at, {', '.join(_MESSAGE_RECORD_WRITE_COLUMNS)}) "
                f"VALUES ({', '.join('%s' for _ in range(21))}) RETURNING id",
                self._record_params(session_id, record),
            )
            return int(cursor.fetchone()[0])

    def append_message_records(self, session_id: str, records: list[MessageRecord]) -> int:
        if not records:
            return 0
        with self._connection() as connection, connection.cursor() as cursor:
            for record in records:
                cursor.execute(
                    f"INSERT INTO {_SCHEMA}.messages (session_id, role, content, created_at, {', '.join(_MESSAGE_RECORD_WRITE_COLUMNS)}) "
                    f"VALUES ({', '.join('%s' for _ in range(21))})",
                    self._record_params(session_id, record),
                )
        return len(records)

    @staticmethod
    def _decode_content(content: Any) -> Any:
        prefix = "__hermes_state_json__:"
        if isinstance(content, str) and content.startswith(prefix):
            try:
                return json.loads(content[len(prefix):])
            except json.JSONDecodeError:
                return content
        return content

    def get_message_records(self, session_id: str) -> list[dict[str, Any]]:
        columns = (
            "id, session_id, role, content, tool_call_id, tool_calls, tool_name, effect_disposition, "
            "created_at AS timestamp, token_count, finish_reason, reasoning, reasoning_content, reasoning_details, "
            "codex_reasoning_items, codex_message_items, platform_message_id, observed, _compressed_summary, "
            "active, compacted, api_content, display_kind, display_metadata"
        )
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            cursor.execute(f"SELECT {columns} FROM {_SCHEMA}.messages WHERE session_id = %s AND active ORDER BY id", (session_id,))
            records = list(cursor.fetchall())
        for record in records:
            record["content"] = self._decode_content(record["content"])
        return records

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            cursor.execute(
                f"SELECT id, session_id, role, content, created_at FROM {_SCHEMA}.messages WHERE session_id = %s ORDER BY id",
                (session_id,),
            )
            return list(cursor.fetchall())

    @staticmethod
    def _is_explicit_branch(session: Mapping[str, Any]) -> bool:
        config = session.get("model_config") or {}
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except json.JSONDecodeError:
                config = {}
        return isinstance(config, Mapping) and "_branched_from" in config

    def get_compression_lineage(self, session_id: str) -> list[str]:
        session = self.get_session(session_id)
        if session is None or self._is_explicit_branch(session):
            return [session_id] if session else []
        root, seen = session, {session_id}
        while root.get("parent_session_id"):
            parent = self.get_session(str(root["parent_session_id"]))
            if parent is None or str(parent["id"]) in seen or parent.get("end_reason") != "compression" or self._is_explicit_branch(root):
                break
            root = parent
            seen.add(str(root["id"]))
        lineage, current = [str(root["id"])], root
        seen = {str(root["id"])}
        while current.get("end_reason") == "compression":
            with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
                cursor.execute(
                    f"SELECT id, parent_session_id, end_reason, model_config FROM {_SCHEMA}.sessions "
                    "WHERE parent_session_id = %s ORDER BY started_at ASC, id ASC", (current["id"],))
                children = cursor.fetchall()
            next_child = next((child for child in children if not self._is_explicit_branch(child)), None)
            if next_child is None or next_child["id"] in seen:
                break
            current = next_child
            lineage.append(str(current["id"]))
            seen.add(str(current["id"]))
        return lineage if session_id in lineage else [session_id]

    def get_compression_tip(self, session_id: str) -> str | None:
        lineage = self.get_compression_lineage(session_id)
        return lineage[-1] if lineage else session_id

    def get_conversation_root(self, session_id: str) -> str:
        lineage = self.get_compression_lineage(session_id)
        return lineage[0] if lineage else session_id

    def _resume_lineage_ids(self, session_id: str) -> list[str]:
        session = self.get_session(session_id)
        return [session_id] if session is None or self._is_explicit_branch(session) else self.get_compression_lineage(session_id)

    def _projection_rows(self, session_ids: list[str], *, display: bool) -> list[dict[str, Any]]:
        if not session_ids:
            return []
        predicate = "(active OR compacted)" if display else "active"
        placeholders = ", ".join("%s" for _ in session_ids)
        columns = (
            "id, session_id, role, content, created_at AS timestamp, tool_call_id, tool_calls, tool_name, "
            "effect_disposition, finish_reason, reasoning, reasoning_content, reasoning_details, codex_reasoning_items, "
            "codex_message_items, platform_message_id, observed, _compressed_summary, active, compacted, api_content, "
            "display_kind, display_metadata"
        )
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            cursor.execute(f"SELECT {columns} FROM {_SCHEMA}.messages WHERE session_id IN ({placeholders}) AND {predicate} ORDER BY id", session_ids)
            rows = list(cursor.fetchall())
        for row in rows:
            row["content"] = self._decode_content(row["content"])
        if not display:
            return rows
        chosen, first = {}, {}
        for row in rows:
            key = (row["role"], json.dumps(row["content"], sort_keys=True, default=str), row["timestamp"], row["tool_call_id"], json.dumps(row["tool_calls"], sort_keys=True, default=str), row["tool_name"])
            previous = chosen.get(key)
            if previous is None or (bool(row["active"]), row["id"]) > (bool(previous["active"]), previous["id"]):
                chosen[key] = row
            first[key] = min(first.get(key, row["id"]), row["id"])
        return [chosen[key] for key in sorted(chosen, key=first.__getitem__)]

    @staticmethod
    def _conversation(rows: list[dict[str, Any]], *, row_ids: bool = True) -> list[dict[str, Any]]:
        messages = []
        for row in rows:
            message = {"role": row["role"], "content": row["content"]}
            if row_ids:
                message["_row_id"] = row["id"]
            for key in ("timestamp", "tool_call_id", "tool_name", "effect_disposition", "api_content", "display_kind"):
                if row.get(key):
                    message[key] = row[key]
            if row.get("tool_calls"):
                message["tool_calls"] = row["tool_calls"]
            if row.get("display_metadata"):
                message["display_metadata"] = row["display_metadata"]
            if row.get("observed"):
                message["observed"] = True
            if row["role"] == "assistant":
                for key in ("finish_reason", "reasoning", "reasoning_content", "reasoning_details", "codex_reasoning_items", "codex_message_items"):
                    if row.get(key) is not None:
                        message[key] = row[key]
            messages.append(message)
        return messages

    def get_resume_conversations(self, session_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        lineage = self._resume_lineage_ids(session_id)
        display_rows = self._projection_rows(lineage, display=True)
        model_rows = [row for row in self._projection_rows([session_id], display=False) if row["active"]]
        return self._conversation(model_rows), self._conversation(display_rows)

    def get_ancestor_display_prefix(self, session_id: str) -> list[dict[str, Any]]:
        lineage = self._resume_lineage_ids(session_id)
        rows = self._projection_rows(lineage, display=True)
        return [
            {key: value for key, value in message.items() if key != "_row_id"}
            for row, message in zip(rows, self._conversation(rows))
            if row["session_id"] != session_id
        ]

    def get_resume_message_count(self, session_id: str, *, tip_only: bool = False) -> int:
        return len(self._projection_rows([session_id] if tip_only else self._resume_lineage_ids(session_id), display=not tip_only))

    def assert_resume_safe(self, session_id: str, max_messages: int | None = None, *, tip_only: bool = False) -> int:
        if max_messages is None:
            from hermes_state import resolved_max_resume_messages
            max_messages = resolved_max_resume_messages()
        if max_messages < 0:
            raise ValueError("max_messages must be non-negative")
        if max_messages == 0:
            return 0
        count = self.get_resume_message_count(session_id, tip_only=tip_only)
        if count > max_messages:
            from hermes_state import SessionResumeTooLargeError
            raise SessionResumeTooLargeError(count, max_messages, scope="in its tip segment" if tip_only else "across its lineage")
        return count

    def queue_token_counts(self, session_id: str, **kwargs: Any) -> None:
        self._token_usage_transport.queue_delta(session_id, kwargs)

    def flush_token_counts(self, timeout: float = 5.0) -> bool:
        return self._token_usage_transport.flush(timeout)

    def _persist_token_usage_delta(self, session_id: str, **kwargs: Any) -> None:
        self.update_token_counts(session_id, **kwargs)

    def update_token_counts(self, session_id: str, input_tokens: int = 0, output_tokens: int = 0, model: str | None = None, cache_read_tokens: int = 0, cache_write_tokens: int = 0, reasoning_tokens: int = 0, estimated_cost_usd: float | None = None, actual_cost_usd: float | None = None, cost_status: str | None = None, cost_source: str | None = None, pricing_version: str | None = None, billing_provider: str | None = None, billing_base_url: str | None = None, billing_mode: str | None = None, api_call_count: int = 0, absolute: bool = False) -> None:
        counters = (input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens)
        has_usage = bool(any(counters) or api_call_count or estimated_cost_usd)
        accounted = bool(has_usage or actual_cost_usd is not None)
        self.ensure_session(session_id)
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            cursor.execute(f"SELECT model, billing_provider, api_call_count FROM {_SCHEMA}.sessions WHERE id=%s FOR UPDATE", (session_id,))
            row = cursor.fetchone() or {}
            if int(row.get("api_call_count") or 0) == 0 and accounted and model and billing_provider and (row.get("model") != model or row.get("billing_provider") != billing_provider):
                cursor.execute(f"UPDATE {_SCHEMA}.sessions SET model=%s, billing_provider=%s, billing_base_url=%s, billing_mode=%s WHERE id=%s", (model, billing_provider, billing_base_url, billing_mode, session_id))
            additions = not absolute
            set_counters = ", ".join(f"{field} = {'%s' if not additions else field + ' + %s'}" for field in _USAGE_COUNTERS)
            estimated = "COALESCE(%s, 0)" if absolute else "COALESCE(estimated_cost_usd, 0) + COALESCE(%s, 0)"
            actual = "CASE WHEN %s::double precision IS NULL THEN actual_cost_usd ELSE %s::double precision END" if absolute else "CASE WHEN %s::double precision IS NULL THEN actual_cost_usd ELSE COALESCE(actual_cost_usd, 0) + %s::double precision END"
            calls = "%s" if absolute else "api_call_count + %s"
            route = (billing_provider if accounted else None, billing_base_url if accounted else None, billing_mode if accounted else None, model if accounted else None)
            cursor.execute(f"UPDATE {_SCHEMA}.sessions SET {set_counters}, estimated_cost_usd={estimated}, actual_cost_usd={actual}, cost_status=COALESCE(%s,cost_status), cost_source=COALESCE(%s,cost_source), pricing_version=COALESCE(%s,pricing_version), billing_provider=COALESCE(billing_provider,%s), billing_base_url=COALESCE(billing_base_url,%s), billing_mode=COALESCE(billing_mode,%s), model=COALESCE(model,%s), api_call_count={calls} WHERE id=%s", (*counters, estimated_cost_usd, actual_cost_usd, actual_cost_usd, cost_status, cost_source, pricing_version, *route, api_call_count, session_id))
            if not absolute and has_usage:
                self._record_model_usage(cursor, session_id, model=model, billing_provider=billing_provider, billing_base_url=billing_base_url, billing_mode=billing_mode, input_tokens=input_tokens, output_tokens=output_tokens, cache_read_tokens=cache_read_tokens, cache_write_tokens=cache_write_tokens, reasoning_tokens=reasoning_tokens, estimated_cost_usd=estimated_cost_usd, actual_cost_usd=actual_cost_usd, cost_status=cost_status, cost_source=cost_source, api_call_count=api_call_count)

    def _record_model_usage(self, cursor: Any, session_id: str, *, model: str | None = None, billing_provider: str | None = None, billing_base_url: str | None = None, billing_mode: str | None = None, input_tokens: int = 0, output_tokens: int = 0, cache_read_tokens: int = 0, cache_write_tokens: int = 0, reasoning_tokens: int = 0, estimated_cost_usd: float | None = None, actual_cost_usd: float | None = None, cost_status: str | None = None, cost_source: str | None = None, api_call_count: int = 0, task: str = "") -> None:
        session = {}
        if not task:
            cursor.execute(f"SELECT model, billing_provider, billing_base_url, billing_mode FROM {_SCHEMA}.sessions WHERE id=%s", (session_id,))
            session = cursor.fetchone() or {}
        now = time.time()
        cursor.execute(f"""INSERT INTO {_SCHEMA}.session_model_usage (session_id,model,billing_provider,billing_base_url,billing_mode,task,api_call_count,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,reasoning_tokens,estimated_cost_usd,actual_cost_usd,cost_status,cost_source,first_seen,last_seen) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (session_id,model,billing_provider,billing_base_url,billing_mode,task) DO UPDATE SET api_call_count=session_model_usage.api_call_count+EXCLUDED.api_call_count,input_tokens=session_model_usage.input_tokens+EXCLUDED.input_tokens,output_tokens=session_model_usage.output_tokens+EXCLUDED.output_tokens,cache_read_tokens=session_model_usage.cache_read_tokens+EXCLUDED.cache_read_tokens,cache_write_tokens=session_model_usage.cache_write_tokens+EXCLUDED.cache_write_tokens,reasoning_tokens=session_model_usage.reasoning_tokens+EXCLUDED.reasoning_tokens,estimated_cost_usd=session_model_usage.estimated_cost_usd+EXCLUDED.estimated_cost_usd,actual_cost_usd=session_model_usage.actual_cost_usd+EXCLUDED.actual_cost_usd,cost_status=COALESCE(EXCLUDED.cost_status,session_model_usage.cost_status),cost_source=COALESCE(EXCLUDED.cost_source,session_model_usage.cost_source),last_seen=EXCLUDED.last_seen""", (session_id,model or session.get("model") or "unknown",billing_provider or session.get("billing_provider") or "",billing_base_url or session.get("billing_base_url") or "",billing_mode or session.get("billing_mode") or "",task or "",api_call_count or 0,input_tokens or 0,output_tokens or 0,cache_read_tokens or 0,cache_write_tokens or 0,reasoning_tokens or 0,float(estimated_cost_usd or 0),float(actual_cost_usd or 0),cost_status,cost_source,now,now))

    def record_auxiliary_usage(self, session_id: str, task: str, **kwargs: Any) -> None:
        if not session_id or not task:
            return
        self.ensure_session(session_id)
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            self._record_model_usage(cursor, session_id, task=task, api_call_count=int(kwargs.pop("api_call_count", 1) if kwargs.get("api_call_count") is not None else 1), **kwargs)

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
                f"SELECT s.id, s.source, s.started_at, s.ended_at, s.end_reason, s.title, s.title_source, s.hidden, s.archived, s.pinned, "
                f"s.system_prompt_hash, p.prompt AS system_prompt, {', '.join('s.' + column for column in _SESSION_METADATA_COLUMNS)} "
                f"FROM {_SCHEMA}.sessions s LEFT JOIN {_SCHEMA}.system_prompts p ON p.hash = s.system_prompt_hash WHERE s.id = %s", (session_id,),
            )
            return cursor.fetchone()

    def set_system_prompt(self, session_id: str, system_prompt: str | None) -> None:
        prompt_hash = None if system_prompt is None else hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
        with self._connection() as connection, connection.cursor() as cursor:
            if prompt_hash is not None:
                cursor.execute(
                    f"INSERT INTO {_SCHEMA}.system_prompts (hash, prompt) VALUES (%s, %s) ON CONFLICT (hash) DO NOTHING",
                    (prompt_hash, system_prompt),
                )
            cursor.execute(f"UPDATE {_SCHEMA}.sessions SET system_prompt_hash = %s WHERE id = %s", (prompt_hash, session_id))
            cursor.execute(
                f"DELETE FROM {_SCHEMA}.system_prompts p WHERE NOT EXISTS "
                f"(SELECT 1 FROM {_SCHEMA}.sessions s WHERE s.system_prompt_hash = p.hash)"
            )

    def get_system_prompt(self, session_id: str) -> str | None:
        row = self.get_session(session_id)
        return None if row is None else row.get("system_prompt")

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
        self._token_usage_transport.close()
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
