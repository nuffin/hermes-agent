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
import math
import queue
import re
import threading
import time
from collections.abc import Iterator, Mapping
from typing import Any, Collection

from hermes_cli.timefmt import coerce_epoch
from agent.session_activity import bound_activity_description, normalize_activity_provenance
from hermes_state_common import _RECOVERABLE_END_REASONS, _RESET_END_REASONS
from hermes_state_runtime_ownership import RuntimeOwner, RuntimeOwnershipReceipt, SessionRuntimeOwnershipMixin, TurnState
from state_store import MessageRecord, PostgreSQLStateStoreConfig, StateStoreConfigurationError
from state_store_postgresql_search import compile_postgresql_search_expression
from token_usage_transport import TokenUsageTransport

_LEGACY_ROOT_SCHEMA = "hermes_state_store_slice"
_SCHEMA_PATTERN = re.compile(r"^hermes_state_store_tenant_[0-9a-f]{32}$")
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
_CONVERSATION_GENERATION_SCHEMA_VERSION = 11
_MODEL_CONFIG_LIFECYCLE_SCHEMA_VERSION = 12
_GIT_METADATA_GENERATION_SCHEMA_VERSION = 13
_SEARCH_DOCUMENT_SCHEMA_VERSION = 14
_SEARCH_INDEX_SCHEMA_VERSION = 15
_BOUNDED_BROWSE_SCHEMA_VERSION = 16
_SEARCH_HEALTH_SCHEMA_VERSION = 17
_SESSION_RUNTIME_OWNERSHIP_SCHEMA_VERSION = 18
_COMPRESSION_COORDINATION_SCHEMA_VERSION = 19
_SEARCH_INDEX_NAME = "messages_search_document_gin"
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
_SEARCH_RESULT_FIELDS = (
    "id", "session_id", "role", "snippet", "timestamp", "tool_name", "source", "model", "session_started", "context",
)
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


class PostgreSQLStateStore(SessionRuntimeOwnershipMixin):
    """Thread-safe bounded psycopg connection pool for session/message persistence."""

    def __init__(self, settings: PostgreSQLStateStoreConfig, dsn: str, *, schema: str) -> None:
        try:
            self._psycopg = importlib.import_module("psycopg")
        except ImportError as exc:
            raise StateStoreConfigurationError(
                "PostgreSQL State Store requires the optional dependency: pip install 'hermes-agent[state-store]'"
            ) from exc
        if schema != _LEGACY_ROOT_SCHEMA and not _SCHEMA_PATTERN.fullmatch(schema):
            raise StateStoreConfigurationError("PostgreSQL State Store received an invalid trusted tenant schema")
        self._settings = settings
        self._dsn = dsn
        self._schema = schema
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
            self._set_connection_search_path(connection)
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

    def _set_connection_search_path(self, connection: Any) -> None:
        """Reset the pooled connection to this store's trusted tenant schema."""
        with connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{self._schema}", pg_catalog')
        connection.commit()

    def _probe_and_migrate(self, connection: Any) -> None:
        """Apply and validate every migration in order in one locked transaction.

        A ledger row is evidence only after its corresponding catalog contract has
        been validated. This rejects drift instead of silently treating a marker as
        proof that an older or manually modified schema is usable.
        """
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"{self._schema}:migration",))
                cursor.execute("SHOW server_version_num")
                version = int(cursor.fetchone()[0])
                if version < 180000:
                    raise StateStoreConfigurationError("PostgreSQL State Store requires PostgreSQL 18 or newer")

                cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {self._schema}")
                cursor.execute(f"CREATE TABLE IF NOT EXISTS {self._schema}.schema_migrations (version integer PRIMARY KEY, applied_at double precision NOT NULL)")
                cursor.execute(f"SELECT version FROM {self._schema}.schema_migrations ORDER BY version")
                applied = {int(row[0]) for row in cursor.fetchall()}
                unsupported = sorted(version for version in applied if version < 1 or version > _COMPRESSION_COORDINATION_SCHEMA_VERSION)
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
                    (_CONVERSATION_GENERATION_SCHEMA_VERSION, self._apply_v11, self._validate_v11),
                    (_MODEL_CONFIG_LIFECYCLE_SCHEMA_VERSION, self._apply_v12, self._validate_v12),
                    (_GIT_METADATA_GENERATION_SCHEMA_VERSION, self._apply_v13, self._validate_v13),
                    (_SEARCH_DOCUMENT_SCHEMA_VERSION, self._apply_v14, self._validate_v14),
                    (_SEARCH_INDEX_SCHEMA_VERSION, self._apply_v15, self._validate_v15),
                    (_BOUNDED_BROWSE_SCHEMA_VERSION, self._apply_v16, self._validate_v16),
                    (_SEARCH_HEALTH_SCHEMA_VERSION, self._apply_v17, self._validate_v17),
                    (_SESSION_RUNTIME_OWNERSHIP_SCHEMA_VERSION, self._apply_v18, self._validate_v18),
                    (_COMPRESSION_COORDINATION_SCHEMA_VERSION, self._apply_v19, self._validate_v19),
                )
                for migration_version, apply, validate in migrations:
                    if migration_version not in applied:
                        apply(cursor)
                        validate(cursor)
                        cursor.execute(
                            f"INSERT INTO {self._schema}.schema_migrations (version, applied_at) VALUES (%s, %s)",
                            (migration_version, time.time()),
                        )
                    else:
                        validate(cursor)
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _required_columns(self, cursor: Any, table: str, columns: set[str]) -> None:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s",
            (self._schema, table),
        )
        missing = columns - {str(row[0]) for row in cursor.fetchall()}
        if missing:
            raise StateStoreConfigurationError(
                f"PostgreSQL State Store schema drift: {self._schema}.{table} is missing columns {sorted(missing)}"
            )

    def _require_index(self, cursor: Any, name: str) -> None:
        cursor.execute("SELECT 1 FROM pg_class WHERE relkind = 'i' AND relname = %s AND relnamespace = %s::regnamespace", (name, self._schema))
        if cursor.fetchone() is None:
            raise StateStoreConfigurationError(f"PostgreSQL State Store schema drift: missing index {self._schema}.{name}")

    def _require_foreign_key(self, cursor: Any, name: str, table: str, target: str) -> None:
        cursor.execute(
            "SELECT 1 FROM pg_constraint WHERE conname = %s AND conrelid = %s::regclass "
            "AND contype = 'f' AND confrelid = %s::regclass",
            (name, f"{self._schema}.{table}", f"{self._schema}.{target}"),
        )
        if cursor.fetchone() is None:
            raise StateStoreConfigurationError(f"PostgreSQL State Store schema drift: missing or invalid foreign key {self._schema}.{name}")

    def _apply_v1(self, cursor: Any) -> None:
        cursor.execute(
            f"CREATE TABLE IF NOT EXISTS {self._schema}.sessions ("
            "id text PRIMARY KEY, source text NOT NULL, started_at double precision NOT NULL, "
            "ended_at double precision, end_reason text)"
        )
        cursor.execute(
            f"CREATE TABLE IF NOT EXISTS {self._schema}.messages ("
            "id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, session_id text NOT NULL "
            f"REFERENCES {self._schema}.sessions(id), role text NOT NULL, content text, created_at double precision NOT NULL)"
        )
        cursor.execute(f"CREATE INDEX IF NOT EXISTS messages_session_id_id ON {self._schema}.messages (session_id, id)")

    def _validate_v1(self, cursor: Any) -> None:
        self._required_columns(cursor, "sessions", {"id", "source", "started_at", "ended_at", "end_reason"})
        self._required_columns(cursor, "messages", {"id", "session_id", "role", "content", "created_at"})
        self._require_index(cursor, "messages_session_id_id")
        self._require_foreign_key(cursor, "messages_session_id_fkey", "messages", "sessions")

    def _apply_v2(self, cursor: Any) -> None:
        for column in _SESSION_METADATA_COLUMNS:
            cursor.execute(f"ALTER TABLE {self._schema}.sessions ADD COLUMN IF NOT EXISTS {column} {_SESSION_METADATA_TYPES[column]}")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS sessions_source_session_key ON {self._schema}.sessions (source, session_key)")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS sessions_parent_session_id ON {self._schema}.sessions (parent_session_id)")

    def _validate_v2(self, cursor: Any) -> None:
        self._required_columns(cursor, "sessions", set(_SESSION_METADATA_COLUMNS))
        self._require_index(cursor, "sessions_source_session_key")
        self._require_index(cursor, "sessions_parent_session_id")

    def _apply_v3(self, cursor: Any) -> None:
        cursor.execute(
            "SELECT 1 FROM pg_constraint WHERE conname = %s AND conrelid = %s::regclass "
            "AND contype = 'f' AND confrelid = %s::regclass",
            ("sessions_parent_session_id_fkey", f"{self._schema}.sessions", f"{self._schema}.sessions"),
        )
        if cursor.fetchone() is None:
            cursor.execute(
                f"ALTER TABLE {self._schema}.sessions ADD CONSTRAINT sessions_parent_session_id_fkey "
                f"FOREIGN KEY (parent_session_id) REFERENCES {self._schema}.sessions(id) NOT VALID"
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
        cursor.execute(f"ALTER TABLE {self._schema}.sessions ADD COLUMN IF NOT EXISTS title text")
        cursor.execute(f"ALTER TABLE {self._schema}.sessions ADD COLUMN IF NOT EXISTS title_source text")
        cursor.execute(f"ALTER TABLE {self._schema}.sessions ADD COLUMN IF NOT EXISTS hidden boolean NOT NULL DEFAULT false")
        cursor.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS sessions_title_unique ON {self._schema}.sessions (title) WHERE title IS NOT NULL")

    def _validate_v5(self, cursor: Any) -> None:
        self._required_columns(cursor, "sessions", {"title", "title_source", "hidden"})
        self._require_index(cursor, "sessions_title_unique")

    def _apply_v6(self, cursor: Any) -> None:
        cursor.execute(f"ALTER TABLE {self._schema}.sessions ADD COLUMN IF NOT EXISTS archived boolean NOT NULL DEFAULT false")
        cursor.execute(f"ALTER TABLE {self._schema}.sessions ADD COLUMN IF NOT EXISTS pinned boolean NOT NULL DEFAULT false")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS sessions_visibility_started_at ON {self._schema}.sessions (archived, hidden, started_at DESC)")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS sessions_pinned_started_at ON {self._schema}.sessions (pinned, started_at DESC) WHERE pinned")

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
            cursor.execute(f"ALTER TABLE {self._schema}.messages ADD COLUMN IF NOT EXISTS {column} {types[column]}")

    def _validate_v7(self, cursor: Any) -> None:
        self._required_columns(cursor, "messages", set(_MESSAGE_RECORD_COLUMNS))

    def _apply_v8(self, cursor: Any) -> None:
        cursor.execute(f"CREATE INDEX IF NOT EXISTS messages_resume_projection ON {self._schema}.messages (session_id, active, id)")

    def _validate_v8(self, cursor: Any) -> None:
        self._validate_v7(cursor)
        self._require_index(cursor, "messages_resume_projection")

    def _apply_v9(self, cursor: Any) -> None:
        cursor.execute(f"CREATE TABLE IF NOT EXISTS {self._schema}.system_prompts (hash text PRIMARY KEY, prompt text NOT NULL)")
        cursor.execute(f"ALTER TABLE {self._schema}.sessions ADD COLUMN IF NOT EXISTS system_prompt_hash text")
        cursor.execute(
            "SELECT 1 FROM pg_constraint WHERE conname = %s AND conrelid = %s::regclass "
            "AND contype = 'f' AND confrelid = %s::regclass",
            ("sessions_system_prompt_hash_fkey", f"{self._schema}.sessions", f"{self._schema}.system_prompts"),
        )
        if cursor.fetchone() is None:
            cursor.execute(
                f"ALTER TABLE {self._schema}.sessions ADD CONSTRAINT sessions_system_prompt_hash_fkey "
                f"FOREIGN KEY (system_prompt_hash) REFERENCES {self._schema}.system_prompts(hash)"
            )

    def _validate_v9(self, cursor: Any) -> None:
        self._required_columns(cursor, "sessions", {"system_prompt_hash"})
        self._required_columns(cursor, "system_prompts", {"hash", "prompt"})
        self._require_foreign_key(cursor, "sessions_system_prompt_hash_fkey", "sessions", "system_prompts")

    def _apply_v10(self, cursor: Any) -> None:
        for column in _USAGE_SESSION_COLUMNS:
            type_name = "bigint NOT NULL DEFAULT 0" if column in {*_USAGE_COUNTERS, "api_call_count"} else ("double precision" if column in {"estimated_cost_usd", "actual_cost_usd"} else "text")
            cursor.execute(f"ALTER TABLE {self._schema}.sessions ADD COLUMN IF NOT EXISTS {column} {type_name}")
        cursor.execute(f'''CREATE TABLE IF NOT EXISTS {self._schema}.session_model_usage (
            session_id text NOT NULL REFERENCES {self._schema}.sessions(id) ON DELETE CASCADE,
            model text NOT NULL, billing_provider text NOT NULL DEFAULT '', billing_base_url text NOT NULL DEFAULT '',
            billing_mode text NOT NULL DEFAULT '', task text NOT NULL DEFAULT '', api_call_count bigint NOT NULL DEFAULT 0,
            input_tokens bigint NOT NULL DEFAULT 0, output_tokens bigint NOT NULL DEFAULT 0,
            cache_read_tokens bigint NOT NULL DEFAULT 0, cache_write_tokens bigint NOT NULL DEFAULT 0,
            reasoning_tokens bigint NOT NULL DEFAULT 0, estimated_cost_usd double precision NOT NULL DEFAULT 0,
            actual_cost_usd double precision NOT NULL DEFAULT 0, cost_status text, cost_source text,
            first_seen double precision, last_seen double precision,
            PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task))''')
        cursor.execute(f"CREATE INDEX IF NOT EXISTS session_model_usage_session ON {self._schema}.session_model_usage (session_id)")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS session_model_usage_model ON {self._schema}.session_model_usage (model)")

    def _validate_v10(self, cursor: Any) -> None:
        self._required_columns(cursor, "sessions", set(_USAGE_SESSION_COLUMNS))
        self._required_columns(cursor, "session_model_usage", {"session_id", "model", "billing_provider", "billing_base_url", "billing_mode", "task", "api_call_count", *_USAGE_COUNTERS, "estimated_cost_usd", "actual_cost_usd", "cost_status", "cost_source", "first_seen", "last_seen"})
        self._require_index(cursor, "session_model_usage_session")
        self._require_index(cursor, "session_model_usage_model")
        self._require_foreign_key(cursor, "session_model_usage_session_id_fkey", "session_model_usage", "sessions")

    def _apply_v11(self, cursor: Any) -> None:
        """Create the non-prunable peer generation ledger.

        This intentionally has no foreign key to sessions: deleting or pruning
        session history must never reissue an affinity generation (ABA).
        """
        cursor.execute(f"""CREATE TABLE IF NOT EXISTS {self._schema}.conversation_generations (
            source text NOT NULL,
            session_key text NOT NULL,
            generation bigint NOT NULL DEFAULT 0,
            PRIMARY KEY (source, session_key))""")

    def _validate_v11(self, cursor: Any) -> None:
        self._required_columns(cursor, "conversation_generations", {"source", "session_key", "generation"})
        cursor.execute(
            "SELECT a.attname FROM pg_constraint c "
            "JOIN unnest(c.conkey) WITH ORDINALITY AS key(attnum, ordinal) ON true "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = key.attnum "
            "WHERE c.conname = %s AND c.conrelid = %s::regclass AND c.contype = 'p' "
            "ORDER BY key.ordinal",
            ("conversation_generations_pkey", f"{self._schema}.conversation_generations"),
        )
        if [row[0] for row in cursor.fetchall()] != ["source", "session_key"]:
            raise StateStoreConfigurationError(
                f"PostgreSQL State Store schema drift: {self._schema}.conversation_generations primary key must be (source, session_key)")
        cursor.execute(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_schema = %s AND table_name = %s",
            (self._schema, "conversation_generations"),
        )
        columns = {row[0]: row[1:] for row in cursor.fetchall()}
        expected = {
            "source": ("text", "NO"), "session_key": ("text", "NO"), "generation": ("bigint", "NO"),
        }
        if any(columns.get(name, (None, None))[:2] != contract for name, contract in expected.items()):
            raise StateStoreConfigurationError(
                f"PostgreSQL State Store schema drift: {self._schema}.conversation_generations has invalid column contract")
        if "0" not in str(columns["generation"][2] or ""):
            raise StateStoreConfigurationError(
                f"PostgreSQL State Store schema drift: {self._schema}.conversation_generations.generation must default to 0")
        cursor.execute(
            "SELECT 1 FROM pg_constraint WHERE conrelid = %s::regclass AND contype = 'f'",
            (f"{self._schema}.conversation_generations",),
        )
        if cursor.fetchone() is not None:
            raise StateStoreConfigurationError(
                f"PostgreSQL State Store schema drift: {self._schema}.conversation_generations must not reference prunable session rows")

    def _apply_v12(self, cursor: Any) -> None:
        """Record the validated model/config mutation lifecycle contract; no new DDL."""
        return None

    def _validate_v12(self, cursor: Any) -> None:
        self._validate_v2(cursor)
        self._validate_v9(cursor)
        self._validate_v10(cursor)
        cursor.execute(
            "SELECT data_type FROM information_schema.columns WHERE table_schema = %s "
            "AND table_name = 'sessions' AND column_name = 'model_config'",
            (self._schema,),
        )
        row = cursor.fetchone()
        if row is None or row[0] != "jsonb":
            raise StateStoreConfigurationError(
                f"PostgreSQL State Store schema drift: {self._schema}.sessions.model_config must be jsonb")

    def _apply_v13(self, cursor: Any) -> None:
        cursor.execute(f"ALTER TABLE {self._schema}.sessions ADD COLUMN IF NOT EXISTS git_branch text")
        cursor.execute(
            f"ALTER TABLE {self._schema}.sessions ADD COLUMN IF NOT EXISTS "
            "git_metadata_generation bigint NOT NULL DEFAULT 0"
        )

    def _validate_v13(self, cursor: Any) -> None:
        self._required_columns(cursor, "sessions", {"git_branch", "git_metadata_generation"})
        cursor.execute(
            "SELECT branch.data_type, branch.is_nullable, branch.column_default, generation.data_type, generation.is_nullable, generation.column_default "
            "FROM information_schema.columns AS branch JOIN information_schema.columns AS generation "
            "ON generation.table_schema = branch.table_schema AND generation.table_name = branch.table_name "
            "WHERE branch.table_schema = %s AND branch.table_name = 'sessions' "
            "AND branch.column_name = 'git_branch' AND generation.column_name = 'git_metadata_generation'",
            (self._schema,),
        )
        row = cursor.fetchone()
        if row is None or row[0] != "text" or row[1] != "YES" or row[2] is not None or row[3] != "bigint" or row[4] != "NO" or str(row[5] or "").strip() != "0":
            raise StateStoreConfigurationError(
                f"PostgreSQL State Store schema drift: {self._schema}.sessions Git metadata columns "
                "must be git_branch text and git_metadata_generation bigint NOT NULL DEFAULT 0")

    def _apply_v14(self, cursor: Any) -> None:
        # ``simple`` is built into PostgreSQL.  Do not substitute vector similarity or
        # require pg_trgm: neither is a lexical full-text contract.
        cursor.execute(
            f"ALTER TABLE {self._schema}.messages ADD COLUMN IF NOT EXISTS search_document tsvector "
            "GENERATED ALWAYS AS (to_tsvector('simple', "
            "coalesce(content, '') || ' ' || coalesce(tool_name, '') || ' ' || coalesce(tool_calls::text, ''))) STORED"
        )

    def _validate_v14(self, cursor: Any) -> None:
        self._required_columns(cursor, "messages", {"search_document"})
        cursor.execute(
            "SELECT data_type, is_generated FROM information_schema.columns WHERE table_schema = %s "
            "AND table_name = 'messages' AND column_name = 'search_document'",
            (self._schema,),
        )
        row = cursor.fetchone()
        if row is None or row[0] != "tsvector" or row[1] != "ALWAYS":
            raise StateStoreConfigurationError(
                f"PostgreSQL State Store schema drift: {self._schema}.messages.search_document must be a generated tsvector")

    def _apply_v15(self, cursor: Any) -> None:
        cursor.execute(f"CREATE INDEX IF NOT EXISTS {_SEARCH_INDEX_NAME} ON {self._schema}.messages USING GIN (search_document)")

    def _validate_v15(self, cursor: Any) -> None:
        self._validate_v14(cursor)
        self._require_index(cursor, _SEARCH_INDEX_NAME)

    def _apply_v16(self, cursor: Any) -> None:
        """Add the durable activity projection needed by bounded contextual browse.

        Existing stores are backfilled from persisted message timestamps rather
        than wall clock so an upgrade cannot reorder historical sessions.
        """
        cursor.execute(f"ALTER TABLE {self._schema}.sessions ADD COLUMN IF NOT EXISTS last_activity_at double precision")
        cursor.execute(
            f"UPDATE {self._schema}.sessions AS s SET last_activity_at = COALESCE("
            f"(SELECT MAX(m.created_at) FROM {self._schema}.messages AS m WHERE m.session_id = s.id), s.started_at) "
            "WHERE s.last_activity_at IS NULL"
        )
        cursor.execute(
            f"CREATE INDEX IF NOT EXISTS sessions_effective_activity ON {self._schema}.sessions "
            "(archived, hidden, last_activity_at DESC, started_at DESC, id DESC)"
        )

    def _validate_v16(self, cursor: Any) -> None:
        self._required_columns(cursor, "sessions", {"last_activity_at"})
        self._require_index(cursor, "sessions_effective_activity")

    def _apply_v17(self, cursor: Any) -> None:
        """Keep only maintenance outcomes; PostgreSQL has no deferred FTS backfill state."""
        cursor.execute(
            f"CREATE TABLE IF NOT EXISTS {self._schema}.search_index_maintenance ("
            "singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton), "
            "last_success_at double precision, last_error text)"
        )
        cursor.execute(
            f"INSERT INTO {self._schema}.search_index_maintenance (singleton) VALUES (true) "
            "ON CONFLICT (singleton) DO NOTHING"
        )

    def _validate_v17(self, cursor: Any) -> None:
        self._validate_v15(cursor)
        self._required_columns(cursor, "search_index_maintenance", {"singleton", "last_success_at", "last_error"})

    def _apply_v18(self, cursor: Any) -> None:
        """Add the no-route, fenced runtime handoff evidence tables.

        These tables intentionally have no foreign keys to prunable sessions and
        no relationship to the StateStore migration ledger beyond this tenant's
        catalog validation.  They are direct-test-only until a consumer can carry
        the receipt through every effect boundary.
        """
        cursor.execute(
            f"CREATE TABLE IF NOT EXISTS {self._schema}.session_runtime_owners ("
            "namespace text NOT NULL DEFAULT '', session_id text NOT NULL, "
            "installation_id text NOT NULL, host text NOT NULL, process_generation text NOT NULL, "
            "fence bigint NOT NULL CHECK (fence > 0), expires_at double precision NOT NULL, "
            "updated_at double precision NOT NULL, PRIMARY KEY (namespace, session_id))"
        )
        cursor.execute(
            f"CREATE TABLE IF NOT EXISTS {self._schema}.session_runtime_turns ("
            "namespace text NOT NULL DEFAULT '', session_id text NOT NULL, turn_id text NOT NULL, "
            "state text NOT NULL CHECK (state IN ('running', 'indeterminate', 'settled')), "
            "owner_fence bigint NOT NULL CHECK (owner_fence > 0), receipt_json jsonb, "
            "created_at double precision NOT NULL, updated_at double precision NOT NULL, "
            "PRIMARY KEY (namespace, session_id, turn_id))"
        )
        cursor.execute(f"CREATE INDEX IF NOT EXISTS session_runtime_owners_expires ON {self._schema}.session_runtime_owners (expires_at)")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS session_runtime_turns_state ON {self._schema}.session_runtime_turns (namespace, session_id, state)")

    def _validate_v18(self, cursor: Any) -> None:
        self._required_columns(cursor, "session_runtime_owners", {
            "namespace", "session_id", "installation_id", "host", "process_generation", "fence", "expires_at", "updated_at",
        })
        self._required_columns(cursor, "session_runtime_turns", {
            "namespace", "session_id", "turn_id", "state", "owner_fence", "receipt_json", "created_at", "updated_at",
        })
        self._require_index(cursor, "session_runtime_owners_expires")
        self._require_index(cursor, "session_runtime_turns_state")

    def _apply_v19(self, cursor: Any) -> None:
        """Persist the non-destructive compression coordination state.

        This migration deliberately does *not* advertise compression rotation:
        a lease/cooldown without atomic parent/child publication would make a
        lineage fork easier to create, not safer.  These rows support durable
        observation and a future all-or-nothing publication transaction only.
        """
        session_columns = {
            "last_activity_description": "text NOT NULL DEFAULT ''",
            "last_activity_provenance": "text NOT NULL DEFAULT 'unknown'",
            "compression_failure_cooldown_until": "double precision",
            "compression_failure_error": "text",
            "compression_fallback_streak": "bigint NOT NULL DEFAULT 0",
            "compression_ineffective_count": "bigint NOT NULL DEFAULT 0",
            "compression_recovery_deadline": "double precision",
        }
        for column, type_name in session_columns.items():
            cursor.execute(f"ALTER TABLE {self._schema}.sessions ADD COLUMN IF NOT EXISTS {column} {type_name}")
        cursor.execute(
            f"CREATE TABLE IF NOT EXISTS {self._schema}.compression_locks ("
            f"session_id text PRIMARY KEY REFERENCES {self._schema}.sessions(id) ON DELETE CASCADE, "
            "holder text NOT NULL, fence bigint NOT NULL CHECK (fence > 0), "
            "expires_at double precision NOT NULL, updated_at double precision NOT NULL)"
        )
        cursor.execute(
            f"CREATE TABLE IF NOT EXISTS {self._schema}.session_turn_leases ("
            "conversation_id text PRIMARY KEY, holder text NOT NULL, "
            "fence bigint NOT NULL CHECK (fence > 0), expires_at double precision NOT NULL, "
            "updated_at double precision NOT NULL)"
        )
        cursor.execute(f"CREATE INDEX IF NOT EXISTS compression_locks_expires ON {self._schema}.compression_locks (expires_at)")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS session_turn_leases_expires ON {self._schema}.session_turn_leases (expires_at)")

    def _validate_v19(self, cursor: Any) -> None:
        self._required_columns(cursor, "sessions", {
            "last_activity_at", "last_activity_description", "last_activity_provenance",
            "compression_failure_cooldown_until", "compression_failure_error",
            "compression_fallback_streak", "compression_ineffective_count", "compression_recovery_deadline",
        })
        self._required_columns(cursor, "compression_locks", {"session_id", "holder", "fence", "expires_at", "updated_at"})
        self._required_columns(cursor, "session_turn_leases", {"conversation_id", "holder", "fence", "expires_at", "updated_at"})
        self._require_index(cursor, "compression_locks_expires")
        self._require_index(cursor, "session_turn_leases_expires")

    @staticmethod
    def _runtime_namespace(namespace: str | None) -> str:
        return (namespace or "").strip()

    @staticmethod
    def _runtime_ttl_seconds(ttl_seconds: float) -> float:
        ttl = float(ttl_seconds)
        if not math.isfinite(ttl):
            raise ValueError("runtime ownership ttl_seconds must be finite")
        return max(0.1, ttl)

    def _ownership_lock_and_clock(self, cursor: Any, namespace: str, session_id: str) -> float:
        """Serialize one owner key, including absent-row claims, then read server time."""
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"{self._schema}:runtime-owner:{namespace}:{session_id}",),
        )
        cursor.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp())")
        row = cursor.fetchone()
        return float(next(iter(row.values())) if isinstance(row, Mapping) else row[0])

    @staticmethod
    def _receipt_matches(row: Mapping[Any, Any], receipt: RuntimeOwnershipReceipt, now: float) -> bool:
        owner = receipt.owner
        return (
            int(row["fence"]) == receipt.fence and float(row["expires_at"]) > now
            and row["installation_id"] == owner.installation_id and row["host"] == owner.host
            and row["process_generation"] == owner.process_generation
        )

    def acquire_session_runtime_ownership(
        self, session_id: str, owner: RuntimeOwner, *, ttl_seconds: float = 300.0, namespace: str | None = None,
    ) -> RuntimeOwnershipReceipt | None:
        if not session_id:
            return None
        installation_id, host, process_generation = self._runtime_owner_columns(owner)
        namespace = self._runtime_namespace(namespace)
        ttl = self._runtime_ttl_seconds(ttl_seconds)
        with self._connection() as connection, connection.cursor() as cursor:
            now = self._ownership_lock_and_clock(cursor, namespace, session_id)
            expires_at = now + ttl
            cursor.execute(
                "SELECT installation_id, host, process_generation, fence, expires_at "
                "FROM session_runtime_owners WHERE namespace=%s AND session_id=%s FOR UPDATE",
                (namespace, session_id),
            )
            raw_row = cursor.fetchone()
            row = None if raw_row is None else dict(zip(
                ("installation_id", "host", "process_generation", "fence", "expires_at"), raw_row))
            if row is None:
                fence = 1
                cursor.execute(
                    "INSERT INTO session_runtime_owners (namespace, session_id, installation_id, host, process_generation, fence, expires_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (namespace, session_id, installation_id, host, process_generation, fence, expires_at, now),
                )
            elif (row["installation_id"], row["host"], row["process_generation"]) == (installation_id, host, process_generation):
                fence = int(row["fence"])
                cursor.execute(
                    "UPDATE session_runtime_owners SET expires_at=%s, updated_at=%s WHERE namespace=%s AND session_id=%s AND fence=%s",
                    (expires_at, now, namespace, session_id, fence),
                )
            elif float(row["expires_at"]) > now:
                return None
            else:
                fence = int(row["fence"]) + 1
                cursor.execute(
                    "UPDATE session_runtime_owners SET installation_id=%s, host=%s, process_generation=%s, fence=%s, expires_at=%s, updated_at=%s "
                    "WHERE namespace=%s AND session_id=%s AND fence=%s",
                    (installation_id, host, process_generation, fence, expires_at, now, namespace, session_id, int(row["fence"])),
                )
                cursor.execute(
                    "UPDATE session_runtime_turns SET state='indeterminate', updated_at=%s "
                    "WHERE namespace=%s AND session_id=%s AND state='running' AND owner_fence < %s",
                    (now, namespace, session_id, fence),
                )
        return RuntimeOwnershipReceipt(namespace, session_id, owner, fence, expires_at)

    def renew_session_runtime_ownership(self, receipt: RuntimeOwnershipReceipt, *, ttl_seconds: float = 300.0) -> RuntimeOwnershipReceipt | None:
        installation_id, host, process_generation = self._runtime_owner_columns(receipt.owner)
        ttl = self._runtime_ttl_seconds(ttl_seconds)
        with self._connection() as connection, connection.cursor() as cursor:
            now = self._ownership_lock_and_clock(cursor, receipt.namespace, receipt.session_id)
            expires_at = now + ttl
            cursor.execute(
                "UPDATE session_runtime_owners SET expires_at=%s, updated_at=%s WHERE namespace=%s AND session_id=%s "
                "AND installation_id=%s AND host=%s AND process_generation=%s AND fence=%s AND expires_at > %s",
                (expires_at, now, receipt.namespace, receipt.session_id, installation_id, host, process_generation, receipt.fence, now),
            )
            if cursor.rowcount != 1:
                return None
        return RuntimeOwnershipReceipt(receipt.namespace, receipt.session_id, receipt.owner, receipt.fence, expires_at)

    def release_session_runtime_ownership(self, receipt: RuntimeOwnershipReceipt) -> bool:
        installation_id, host, process_generation = self._runtime_owner_columns(receipt.owner)
        with self._connection() as connection, connection.cursor() as cursor:
            now = self._ownership_lock_and_clock(cursor, receipt.namespace, receipt.session_id)
            # Keep the row as a durable fence tombstone; a later owner can never reuse a fence.
            cursor.execute(
                "UPDATE session_runtime_owners SET expires_at=%s, updated_at=%s WHERE namespace=%s AND session_id=%s "
                "AND installation_id=%s AND host=%s AND process_generation=%s AND fence=%s",
                (now, now, receipt.namespace, receipt.session_id, installation_id, host, process_generation, receipt.fence),
            )
            return cursor.rowcount == 1

    def begin_session_runtime_turn(self, receipt: RuntimeOwnershipReceipt, turn_id: str) -> bool:
        if not turn_id:
            return False
        self._runtime_owner_columns(receipt.owner)
        with self._connection() as connection, connection.cursor() as cursor:
            now = self._ownership_lock_and_clock(cursor, receipt.namespace, receipt.session_id)
            cursor.execute("SELECT installation_id, host, process_generation, fence, expires_at FROM session_runtime_owners "
                           "WHERE namespace=%s AND session_id=%s FOR UPDATE", (receipt.namespace, receipt.session_id))
            raw_owner = cursor.fetchone()
            owner = None if raw_owner is None else dict(zip(
                ("installation_id", "host", "process_generation", "fence", "expires_at"), raw_owner))
            if owner is None or not self._receipt_matches(owner, receipt, now):
                return False
            cursor.execute("SELECT state, owner_fence FROM session_runtime_turns WHERE namespace=%s AND session_id=%s AND turn_id=%s FOR UPDATE",
                           (receipt.namespace, receipt.session_id, turn_id))
            raw_row = cursor.fetchone()
            row = None if raw_row is None else dict(zip(("state", "owner_fence"), raw_row))
            if row is not None:
                return row["state"] == "running" and int(row["owner_fence"]) == receipt.fence
            cursor.execute(
                "INSERT INTO session_runtime_turns (namespace, session_id, turn_id, state, owner_fence, receipt_json, created_at, updated_at) "
                "VALUES (%s, %s, %s, 'running', %s, NULL, %s, %s)",
                (receipt.namespace, receipt.session_id, turn_id, receipt.fence, now, now),
            )
            return True

    def resolve_session_runtime_turn(self, receipt: RuntimeOwnershipReceipt, turn_id: str, *, state: TurnState, receipt_data: dict | None = None) -> bool:
        if state not in {"settled", "indeterminate"} or not turn_id:
            return False
        if state == "settled" and receipt_data is None:
            raise ValueError("settled turn requires a verified receipt")
        self._runtime_owner_columns(receipt.owner)
        with self._connection() as connection, connection.cursor() as cursor:
            now = self._ownership_lock_and_clock(cursor, receipt.namespace, receipt.session_id)
            cursor.execute("SELECT installation_id, host, process_generation, fence, expires_at FROM session_runtime_owners "
                           "WHERE namespace=%s AND session_id=%s FOR UPDATE", (receipt.namespace, receipt.session_id))
            raw_owner = cursor.fetchone()
            owner = None if raw_owner is None else dict(zip(
                ("installation_id", "host", "process_generation", "fence", "expires_at"), raw_owner))
            if owner is None or not self._receipt_matches(owner, receipt, now):
                return False
            cursor.execute("SELECT state, receipt_json FROM session_runtime_turns WHERE namespace=%s AND session_id=%s AND turn_id=%s FOR UPDATE",
                           (receipt.namespace, receipt.session_id, turn_id))
            raw_row = cursor.fetchone()
            row = None if raw_row is None else dict(zip(("state", "receipt_json"), raw_row))
            if row is None or row["state"] == "settled":
                return False
            payload = json.dumps(receipt_data, sort_keys=True) if receipt_data is not None else None
            cursor.execute("UPDATE session_runtime_turns SET state=%s, receipt_json=%s::jsonb, updated_at=%s "
                           "WHERE namespace=%s AND session_id=%s AND turn_id=%s AND state IN ('running', 'indeterminate')",
                           (state, payload, now, receipt.namespace, receipt.session_id, turn_id))
            return cursor.rowcount == 1

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
            self._set_connection_search_path(connection)
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

    def _search_maintenance_lock_name(self) -> str:
        return f"{self._schema}:search-index-maintenance"

    def _search_index_catalog(self, cursor: Any) -> tuple[str, str]:
        """Return generated-document and GIN catalog health without trusting an index name alone."""
        self._validate_v14(cursor)
        cursor.execute(
            "SELECT i.indisvalid, i.indisready, i.indislive, am.amname, "
            "array_agg(a.attname ORDER BY key.ordinality) "
            "FROM pg_class AS c JOIN pg_index AS i ON i.indexrelid = c.oid "
            "JOIN pg_am AS am ON am.oid = c.relam "
            "JOIN unnest(i.indkey) WITH ORDINALITY AS key(attnum, ordinality) ON true "
            "JOIN pg_attribute AS a ON a.attrelid = i.indrelid AND a.attnum = key.attnum "
            "WHERE c.relnamespace = %s::regnamespace AND c.relname = %s "
            "GROUP BY i.indisvalid, i.indisready, i.indislive, am.amname",
            (self._schema, _SEARCH_INDEX_NAME),
        )
        row = cursor.fetchone()
        if row is None:
            return "valid", "missing"
        valid, ready, live, access_method, columns = row
        if bool(valid) and bool(ready) and bool(live) and access_method == "gin" and list(columns) == ["search_document"]:
            return "valid", "valid"
        return "valid", "invalid"

    def search_index_status(self) -> dict[str, Any]:
        """PostgreSQL generated-search health, deliberately unlike SQLite FTS rebuild progress.

        Canonical rows synchronously derive ``search_document``; PostgreSQL therefore has no
        detached-corruption fallback, deferred high-water backfill, retry, or quarantine state.
        A missing/invalid GIN catalog entry makes contextual routing unavailable rather than
        silently claiming SQLite's canonical-LIKE fallback semantics.
        """
        with self._connection() as connection, connection.cursor() as cursor:
            document, gin_index = self._search_index_catalog(cursor)
            cursor.execute(
                f"SELECT last_success_at, last_error FROM {self._schema}.search_index_maintenance WHERE singleton"
            )
            maintenance = cursor.fetchone() or (None, None)
            cursor.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (self._search_maintenance_lock_name(),))
            acquired = bool(cursor.fetchone()[0])
            if acquired:
                cursor.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (self._search_maintenance_lock_name(),))
        available = document == "valid" and gin_index == "valid"
        return {
            "backend": "postgresql",
            "available": available,
            "query_path_available": available,
            "generated_document": document,
            "gin_index": gin_index,
            "rebuild": {"supported": True, "operation": "reindex_or_create", "in_progress": not acquired},
            "last_successful_rebuild_at": maintenance[0],
            "last_error": maintenance[1],
            "sqlite_fts_semantics": {
                "corruption_detach": False, "canonical_like_fallback": False,
                "deferred_backfill": False, "high_water": False, "retry_quarantine": False,
            },
        }

    def rebuild_search_index(self) -> dict[str, Any]:
        """Repair this trusted tenant's GIN catalog entry under one cross-process advisory lock.

        ``REINDEX ... CONCURRENTLY`` and ``CREATE INDEX CONCURRENTLY`` require autocommit;
        this deliberately uses a dedicated connection, never a pooled transaction connection.
        """
        connection = self._new_connection()
        connection.autocommit = True
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{self._schema}", pg_catalog')
                cursor.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (self._search_maintenance_lock_name(),))
                acquired = bool(cursor.fetchone()[0])
                if not acquired:
                    result = self.search_index_status()
                    result["rebuild"] = {**result["rebuild"], "operation": "already_running"}
                    return result
                try:
                    _document, gin_index = self._search_index_catalog(cursor)
                    if gin_index == "missing":
                        cursor.execute(f"CREATE INDEX CONCURRENTLY {_SEARCH_INDEX_NAME} ON {self._schema}.messages USING GIN (search_document)")
                        operation = "create"
                    elif gin_index == "invalid":
                        cursor.execute(f"DROP INDEX CONCURRENTLY {self._schema}.{_SEARCH_INDEX_NAME}")
                        cursor.execute(f"CREATE INDEX CONCURRENTLY {_SEARCH_INDEX_NAME} ON {self._schema}.messages USING GIN (search_document)")
                        operation = "replace_invalid"
                    else:
                        cursor.execute(f"REINDEX INDEX CONCURRENTLY {self._schema}.{_SEARCH_INDEX_NAME}")
                        operation = "reindex"
                    _document, repaired = self._search_index_catalog(cursor)
                    if repaired != "valid":
                        raise StateStoreConfigurationError(f"PostgreSQL State Store search-index repair did not restore {self._schema}.{_SEARCH_INDEX_NAME}")
                    cursor.execute(
                        f"UPDATE {self._schema}.search_index_maintenance SET last_success_at = %s, last_error = NULL WHERE singleton",
                        (time.time(),),
                    )
                except Exception as exc:
                    cursor.execute(
                        f"UPDATE {self._schema}.search_index_maintenance SET last_error = %s WHERE singleton",
                        (str(exc)[:1000],),
                    )
                    raise
                finally:
                    cursor.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (self._search_maintenance_lock_name(),))
            result = self.search_index_status()
            result["rebuild"] = {**result["rebuild"], "operation": operation}
            return result
        finally:
            connection.close()

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
                f"INSERT INTO {self._schema}.sessions ({', '.join(columns)}) VALUES ({placeholders}) "
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
            cursor.execute(
                f"UPDATE {self._schema}.sessions AS child SET "
                "cwd = COALESCE(child.cwd, parent.cwd), "
                "git_branch = COALESCE(child.git_branch, parent.git_branch), "
                "git_repo_root = COALESCE(child.git_repo_root, parent.git_repo_root) "
                f"FROM {self._schema}.sessions AS parent "
                "WHERE child.id = %s AND child.parent_session_id IS NOT NULL AND parent.id = child.parent_session_id",
                (session_id,),
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
                f"INSERT INTO {self._schema}.messages (session_id, role, content, created_at, {', '.join(_MESSAGE_RECORD_WRITE_COLUMNS)}) "
                f"VALUES ({', '.join('%s' for _ in range(21))}) RETURNING id, created_at",
                self._record_params(session_id, record),
            )
            message_id, created_at = cursor.fetchone()
            cursor.execute(
                f"UPDATE {self._schema}.sessions SET last_activity_at = GREATEST("
                "COALESCE(last_activity_at, started_at), %s) WHERE id = %s",
                (created_at, session_id),
            )
            return int(message_id)

    def append_message_records(self, session_id: str, records: list[MessageRecord]) -> int:
        if not records:
            return 0
        with self._connection() as connection, connection.cursor() as cursor:
            for record in records:
                cursor.execute(
                    f"INSERT INTO {self._schema}.messages (session_id, role, content, created_at, {', '.join(_MESSAGE_RECORD_WRITE_COLUMNS)}) "
                    f"VALUES ({', '.join('%s' for _ in range(21))}) RETURNING created_at",
                    self._record_params(session_id, record),
                )
                created_at = cursor.fetchone()[0]
                cursor.execute(
                    f"UPDATE {self._schema}.sessions SET last_activity_at = GREATEST("
                    "COALESCE(last_activity_at, started_at), %s) WHERE id = %s",
                    (created_at, session_id),
                )
        return len(records)

    @staticmethod
    def _coordination_ttl(ttl_seconds: float) -> float:
        ttl = float(ttl_seconds)
        if not math.isfinite(ttl):
            raise ValueError("compression coordination ttl_seconds must be finite")
        return max(0.1, ttl)

    def _coordination_clock_and_lock(self, cursor: Any, kind: str, key: str) -> float:
        """Serialize an extant or absent lease key and use PostgreSQL's clock.

        Row locks alone cannot protect a missing lease row.  The transaction
        advisory lock is scoped by the trusted tenant schema so two profiles
        never coordinate accidentally.
        """
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"{self._schema}:{kind}:{key}",))
        cursor.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp())")
        return float(cursor.fetchone()[0])

    def touch_session_activity(self, session_id: str, ts: float | None = None, *, description: str | None = None,
                               provenance: Any = None) -> None:
        """Monotonically publish the current activity observation and its labels."""
        if not session_id:
            return
        when = float(ts if ts is not None else time.time())
        label = bound_activity_description(description)
        source = normalize_activity_provenance(provenance).value
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {self._schema}.sessions SET "
                "last_activity_at = GREATEST(COALESCE(last_activity_at, started_at), %s), "
                "last_activity_description = CASE WHEN last_activity_at IS NULL OR last_activity_at <= %s THEN %s ELSE last_activity_description END, "
                "last_activity_provenance = CASE WHEN last_activity_at IS NULL OR last_activity_at <= %s THEN %s ELSE last_activity_provenance END "
                "WHERE id = %s",
                (when, when, label, when, source, session_id),
            )

    def clear_session_activity_labels(self, session_id: str) -> None:
        """Clear only transient labels; keep the durable last-activity clock."""
        if not session_id:
            return
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {self._schema}.sessions SET last_activity_description='', last_activity_provenance='unknown' "
                "WHERE id=%s AND (last_activity_description <> '' OR last_activity_provenance <> 'unknown')",
                (session_id,),
            )

    def record_compression_failure_cooldown(self, session_id: str, cooldown_until: float, error: str | None = None) -> None:
        """Merge-max a durable retry deadline; a later short failure cannot reopen it."""
        if not session_id:
            return
        deadline = float(cooldown_until)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {self._schema}.sessions SET compression_failure_cooldown_until=GREATEST("
                "COALESCE(compression_failure_cooldown_until, '-Infinity'::float8), %s), "
                "compression_failure_error=%s WHERE id=%s",
                (deadline, error, session_id),
            )

    def get_compression_failure_cooldown(self, session_id: str) -> dict[str, Any] | None:
        if not session_id:
            return None
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT compression_failure_cooldown_until, compression_failure_error, "
                "EXTRACT(EPOCH FROM clock_timestamp()) FROM " + f"{self._schema}.sessions WHERE id=%s",
                (session_id,),
            )
            row = cursor.fetchone()
        if row is None or row[0] is None or float(row[0]) <= float(row[2]):
            return None
        return {"cooldown_until": float(row[0]), "remaining_seconds": float(row[0]) - float(row[2]), "error": row[1]}

    def get_compression_failure_cooldown_row(self, session_id: str) -> dict[str, Any]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT compression_failure_cooldown_until, compression_failure_error FROM {self._schema}.sessions WHERE id=%s", (session_id,))
            row = cursor.fetchone()
        return {"session_exists": row is not None, "cooldown_until": None if row is None else row[0], "error": None if row is None else row[1]}

    def restore_compression_failure_cooldown_row(self, session_id: str, snapshot: Mapping[str, Any]) -> None:
        if not snapshot.get("session_exists", False):
            if self.get_compression_failure_cooldown_row(session_id)["session_exists"]:
                raise RuntimeError("cannot restore absent compression cooldown row: session now exists")
            return
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"UPDATE {self._schema}.sessions SET compression_failure_cooldown_until=%s, compression_failure_error=%s WHERE id=%s", (snapshot.get("cooldown_until"), snapshot.get("error"), session_id))
            if cursor.rowcount != 1:
                return
        if self.get_compression_failure_cooldown_row(session_id) != {"session_exists": True, "cooldown_until": snapshot.get("cooldown_until"), "error": snapshot.get("error")}:
            raise RuntimeError("compression cooldown rollback verification failed")

    def clear_compression_failure_cooldown(self, session_id: str) -> None:
        if not session_id:
            return
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"UPDATE {self._schema}.sessions SET compression_failure_cooldown_until=NULL, compression_failure_error=NULL WHERE id=%s", (session_id,))

    def _compression_counter(self, session_id: str, column: str, *, decimal: bool = False) -> int | float:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT {column} FROM {self._schema}.sessions WHERE id=%s", (session_id,))
            row = cursor.fetchone()
        value = 0.0 if row is None or row[0] is None else float(row[0])
        return max(0.0, value) if decimal else max(0, int(value))

    def _set_compression_counter(self, session_id: str, column: str, value: int | float, *, decimal: bool = False) -> None:
        if not session_id:
            return
        normalized = max(0.0, float(value or 0)) if decimal else max(0, int(value or 0))
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"UPDATE {self._schema}.sessions SET {column}=%s WHERE id=%s", (normalized or None if decimal else normalized, session_id))

    def get_compression_fallback_streak(self, session_id: str) -> int: return int(self._compression_counter(session_id, "compression_fallback_streak"))
    def set_compression_fallback_streak(self, session_id: str, streak: int) -> None: self._set_compression_counter(session_id, "compression_fallback_streak", streak)
    def get_compression_ineffective_count(self, session_id: str) -> int: return int(self._compression_counter(session_id, "compression_ineffective_count"))
    def set_compression_ineffective_count(self, session_id: str, count: int) -> None: self._set_compression_counter(session_id, "compression_ineffective_count", count)
    def get_compression_recovery_deadline(self, session_id: str) -> float: return float(self._compression_counter(session_id, "compression_recovery_deadline", decimal=True))
    def set_compression_recovery_deadline(self, session_id: str, deadline: float) -> None: self._set_compression_counter(session_id, "compression_recovery_deadline", deadline, decimal=True)

    def _try_coordination_lease(self, cursor: Any, *, table: str, key_column: str, key: str,
                                holder: str, ttl_seconds: float) -> bool:
        if not key or not holder:
            return False
        now = self._coordination_clock_and_lock(cursor, table, key)
        cursor.execute(f"SELECT holder, fence, expires_at FROM {self._schema}.{table} WHERE {key_column}=%s FOR UPDATE", (key,))
        row = cursor.fetchone()
        expires_at = now + self._coordination_ttl(ttl_seconds)
        if row is None:
            cursor.execute(f"INSERT INTO {self._schema}.{table} ({key_column}, holder, fence, expires_at, updated_at) VALUES (%s, %s, 1, %s, %s)", (key, holder, expires_at, now))
            return True
        current_holder, fence, old_expiry = str(row[0]), int(row[1]), float(row[2])
        if current_holder != holder and old_expiry > now:
            return False
        next_fence = fence if current_holder == holder else fence + 1
        cursor.execute(f"UPDATE {self._schema}.{table} SET holder=%s, fence=%s, expires_at=%s, updated_at=%s WHERE {key_column}=%s AND fence=%s", (holder, next_fence, expires_at, now, key, fence))
        return cursor.rowcount == 1

    def _compression_turn_lease_key_on_cursor(self, cursor: Any, session_id: str) -> str:
        """Resolve a compression lineage root in the same lease transaction."""
        current, seen = session_id, {session_id}
        while current:
            cursor.execute(f"SELECT parent_session_id, end_reason, model_config FROM {self._schema}.sessions WHERE id=%s", (current,))
            row = cursor.fetchone()
            if row is None or row[0] is None:
                return current
            parent_id, _reason, config = str(row[0]), row[1], row[2]
            if parent_id in seen:
                return current
            cursor.execute(f"SELECT end_reason FROM {self._schema}.sessions WHERE id=%s", (parent_id,))
            parent = cursor.fetchone()
            if parent is None or parent[0] != "compression" or self._is_explicit_branch({"model_config": config}):
                return current
            seen.add(parent_id)
            current = parent_id
        return session_id

    def try_acquire_compression_lock(self, session_id: str, holder: str, ttl_seconds: float = 300.0) -> bool:
        with self._connection() as connection, connection.cursor() as cursor:
            return self._try_coordination_lease(cursor, table="compression_locks", key_column="session_id", key=session_id, holder=holder, ttl_seconds=ttl_seconds)

    def refresh_compression_lock(self, session_id: str, holder: str, ttl_seconds: float = 300.0) -> bool:
        if not session_id or not holder:
            return False
        with self._connection() as connection, connection.cursor() as cursor:
            now = self._coordination_clock_and_lock(cursor, "compression_locks", session_id)
            cursor.execute(f"UPDATE {self._schema}.compression_locks SET expires_at=%s, updated_at=%s WHERE session_id=%s AND holder=%s", (now + self._coordination_ttl(ttl_seconds), now, session_id, holder))
            return cursor.rowcount == 1

    def release_compression_lock(self, session_id: str, holder: str) -> None:
        if not session_id or not holder:
            return
        with self._connection() as connection, connection.cursor() as cursor:
            self._coordination_clock_and_lock(cursor, "compression_locks", session_id)
            cursor.execute(f"DELETE FROM {self._schema}.compression_locks WHERE session_id=%s AND holder=%s", (session_id, holder))

    def get_compression_lock_holder(self, session_id: str) -> str | None:
        if not session_id:
            return None
        with self._connection() as connection, connection.cursor() as cursor:
            now = self._coordination_clock_and_lock(cursor, "compression_locks", session_id)
            cursor.execute(f"SELECT holder FROM {self._schema}.compression_locks WHERE session_id=%s AND expires_at > %s", (session_id, now))
            row = cursor.fetchone()
        return None if row is None else str(row[0])

    def try_acquire_session_turn_lease(self, session_id: str, holder: str, *, ttl_seconds: float = 300.0, **_ignored: Any) -> bool:
        if not session_id or not holder:
            return False
        with self._connection() as connection, connection.cursor() as cursor:
            key = self._compression_turn_lease_key_on_cursor(cursor, session_id)
            return self._try_coordination_lease(cursor, table="session_turn_leases", key_column="conversation_id", key=key, holder=holder, ttl_seconds=ttl_seconds)

    def refresh_session_turn_lease(self, session_id: str, holder: str, *, ttl_seconds: float = 300.0) -> bool:
        if not session_id or not holder:
            return False
        with self._connection() as connection, connection.cursor() as cursor:
            key = self._compression_turn_lease_key_on_cursor(cursor, session_id)
            now = self._coordination_clock_and_lock(cursor, "session_turn_leases", key)
            cursor.execute(f"UPDATE {self._schema}.session_turn_leases SET expires_at=%s, updated_at=%s WHERE conversation_id=%s AND holder=%s", (now + self._coordination_ttl(ttl_seconds), now, key, holder))
            return cursor.rowcount == 1

    def release_session_turn_lease(self, session_id: str, holder: str) -> None:
        if not session_id or not holder:
            return
        with self._connection() as connection, connection.cursor() as cursor:
            key = self._compression_turn_lease_key_on_cursor(cursor, session_id)
            self._coordination_clock_and_lock(cursor, "session_turn_leases", key)
            cursor.execute(f"DELETE FROM {self._schema}.session_turn_leases WHERE conversation_id=%s AND holder=%s", (key, holder))

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
            cursor.execute(f"SELECT {columns} FROM {self._schema}.messages WHERE session_id = %s AND active ORDER BY id", (session_id,))
            records = list(cursor.fetchall())
        for record in records:
            record["content"] = self._decode_content(record["content"])
        return records

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            cursor.execute(
                f"SELECT id, session_id, role, content, created_at FROM {self._schema}.messages WHERE session_id = %s ORDER BY id",
                (session_id,),
            )
            return list(cursor.fetchall())

    def _contextual_message_rows(self, cursor: Any, session_id: str, ids: list[int]) -> list[dict[str, Any]]:
        """Hydrate bounded contextual rows in the same snapshot as their seek.

        Contextual browsing is physical transcript order: ids, rather than wall
        timestamps, preserve tool-call adjacency when clocks tie or regress.
        """
        if not ids:
            return []
        columns = (
            "id, session_id, role, content, tool_call_id, tool_calls, tool_name, effect_disposition, "
            "created_at AS timestamp, token_count, finish_reason, reasoning, reasoning_content, reasoning_details, "
            "codex_reasoning_items, codex_message_items, platform_message_id, observed, _compressed_summary, "
            "active, compacted, api_content, display_kind, display_metadata"
        )
        cursor.execute(
            f"SELECT {columns} FROM {self._schema}.messages "
            "WHERE session_id = %s AND id = ANY(%s) ORDER BY id",
            (session_id, ids),
        )
        rows = list(cursor.fetchall())
        for row in rows:
            row["content"] = self._decode_content(row["content"])
        return rows

    def get_messages_around(self, session_id: str, around_message_id: int, *, window: int = 5) -> dict[str, Any]:
        """Return SQLite-compatible physical transcript neighbours around one anchor.

        A foreign anchor deliberately yields an empty view.  The anchor is included
        in the backward seek so re-anchoring at a page boundary repeats it.
        """
        window = max(int(window), 0)
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            cursor.execute(
                f"SELECT id FROM {self._schema}.messages WHERE id = %s AND session_id = %s",
                (around_message_id, session_id),
            )
            if cursor.fetchone() is None:
                return {"window": [], "messages_before": 0, "messages_after": 0}
            cursor.execute(
                f"SELECT id FROM {self._schema}.messages WHERE session_id = %s AND id <= %s "
                "ORDER BY id DESC LIMIT %s",
                (session_id, around_message_id, window + 1),
            )
            before_ids = [row["id"] for row in cursor.fetchall()]
            cursor.execute(
                f"SELECT id FROM {self._schema}.messages WHERE session_id = %s AND id > %s "
                "ORDER BY id ASC LIMIT %s",
                (session_id, around_message_id, window),
            )
            after_ids = [row["id"] for row in cursor.fetchall()]
            rows = self._contextual_message_rows(cursor, session_id, list(reversed(before_ids)) + after_ids)
        return {"window": rows, "messages_before": max(0, len(before_ids) - 1), "messages_after": len(after_ids)}

    def get_anchored_view(
        self, session_id: str, around_message_id: int, *, window: int = 5, bookend: int = 3,
        keep_roles: tuple[str, ...] | None = ("user", "assistant"),
    ) -> dict[str, Any]:
        """Return the filtered anchor view and non-overlapping same-session bookends."""
        bookend = max(int(bookend), 0)
        primitive = self.get_messages_around(session_id, around_message_id, window=window)
        physical_window = primitive["window"]
        if not physical_window:
            return {"window": [], "messages_before": 0, "messages_after": 0, "bookend_start": [], "bookend_end": []}
        filtered_window = physical_window
        if keep_roles is not None:
            keep_set = set(keep_roles)
            filtered_window = [row for row in physical_window if row["id"] == around_message_id or row["role"] in keep_set]
        start_rows: list[dict[str, Any]] = []
        end_rows: list[dict[str, Any]] = []
        if bookend:
            role_clause, role_params = "", []
            if keep_roles is not None:
                role_clause, role_params = " AND role = ANY(%s)", [list(keep_roles)]
            with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
                cursor.execute(
                    f"SELECT id FROM {self._schema}.messages WHERE session_id = %s AND id < %s{role_clause} "
                    "AND length(content) > 0 ORDER BY id ASC LIMIT %s",
                    [session_id, physical_window[0]["id"], *role_params, bookend],
                )
                start_rows = self._contextual_message_rows(cursor, session_id, [row["id"] for row in cursor.fetchall()])
                cursor.execute(
                    f"SELECT id FROM {self._schema}.messages WHERE session_id = %s AND id > %s{role_clause} "
                    "AND length(content) > 0 ORDER BY id DESC LIMIT %s",
                    [session_id, physical_window[-1]["id"], *role_params, bookend],
                )
                end_rows = self._contextual_message_rows(cursor, session_id, list(reversed([row["id"] for row in cursor.fetchall()])))
        return {
            "window": filtered_window, "messages_before": primitive["messages_before"],
            "messages_after": primitive["messages_after"], "bookend_start": start_rows, "bookend_end": end_rows,
        }

    @staticmethod
    def _flatten_search_context(content: Any) -> str:
        """Match SessionDB's contextual text projection for decoded content."""
        if isinstance(content, list):
            parts = [part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"]
            return " ".join(part for part in parts if part).strip() or "[multimodal content]"
        return content if isinstance(content, str) else ""

    def _search_contexts(self, message_ids: Collection[int]) -> dict[int, list[dict[str, str]]]:
        """Batch same-session timestamp/identity neighbours for canonical candidates."""
        contexts = {int(message_id): [] for message_id in message_ids}
        if not contexts:
            return contexts
        sql = f"""
            WITH target AS (
                SELECT id, session_id, created_at FROM {self._schema}.messages WHERE id = ANY(%s)
            )
            SELECT t.id AS match_id, m.role, m.content
            FROM target AS t JOIN {self._schema}.messages AS m ON m.id IN (
                t.id,
                (SELECT p.id FROM {self._schema}.messages AS p
                 WHERE p.session_id = t.session_id AND (p.created_at, p.id) < (t.created_at, t.id)
                 ORDER BY p.created_at DESC, p.id DESC LIMIT 1),
                (SELECT n.id FROM {self._schema}.messages AS n
                 WHERE n.session_id = t.session_id AND (n.created_at, n.id) > (t.created_at, t.id)
                 ORDER BY n.created_at, n.id LIMIT 1)
            )
            ORDER BY t.id, m.created_at, m.id
        """
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            cursor.execute(sql, (list(contexts),))
            for row in cursor.fetchall():
                contexts[int(row["match_id"])].append({
                    "role": row["role"],
                    "content": self._flatten_search_context(self._decode_content(row["content"]))[:200],
                })
        return contexts

    def search_messages(
        self, query: str, source_filter: list[str] | None = None, exclude_sources: list[str] | None = None,
        role_filter: list[str] | None = None, limit: int = 20, offset: int = 0, sort: str | None = None,
        include_inactive: bool = False, fields: Collection[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Bounded lexical search, deliberately narrower than SQLite FTS5.

        Latin terms use PostgreSQL's built-in ``simple`` tsvector/tsquery. CJK
        uses a parameterized canonical-row substring fallback because PostgreSQL
        ships no CJK tokenizer here. Neither route uses pgvector or pg_trgm.
        """
        if not isinstance(query, str) or not query.strip() or limit <= 0 or offset < 0:
            return []
        result_fields: tuple[str, ...] | None = None
        if fields is not None:
            if isinstance(fields, str):
                raise TypeError("search fields must be a collection of field names, not a string")
            unknown = set(fields).difference(_SEARCH_RESULT_FIELDS)
            if unknown:
                raise ValueError(f"unsupported PostgreSQL search result field(s): {', '.join(sorted(unknown))}")
            result_fields = tuple(field for field in _SEARCH_RESULT_FIELDS if field in fields)
        predicates = ["COALESCE(m.display_kind, '') <> 'hidden'"]
        params: list[Any] = []
        if not include_inactive:
            predicates.append("(m.active OR m.compacted)")
        if source_filter is not None:
            if not source_filter:
                return []
            predicates.append("s.source = ANY(%s)"); params.append(source_filter)
        if exclude_sources:
            predicates.append("NOT (s.source = ANY(%s))"); params.append(exclude_sources)
        if role_filter:
            predicates.append("m.role = ANY(%s)"); params.append(role_filter)
        expression = compile_postgresql_search_expression(query)
        if expression.is_cjk_literal:
            searchable = "(coalesce(m.content, '') || ' ' || coalesce(m.tool_name, '') || ' ' || coalesce(m.tool_calls::text, ''))"
            predicates.extend(f"position(%s in {searchable}) > 0" for _ in expression.params)
            params.extend(expression.params)
            rank, snippet, order = "0.0", "left(coalesce(m.content, m.tool_name, ''), 240)", "m.created_at DESC, m.id DESC"
        else:
            query_sql, query_params = expression.sql, list(expression.params)
            predicates.append(f"m.search_document @@ {query_sql}")
            rank = f"ts_rank_cd(m.search_document, {query_sql})"
            snippet = f"ts_headline('simple', coalesce(m.content, m.tool_name, ''), {query_sql}, 'StartSel=>>>, StopSel=<<<, MaxWords=40, MinWords=1')"
            # SELECT placeholders precede filters/WHERE. The expression is static SQL plus
            # separately-bound literals, including the parser-generated prefix marker.
            params = [*query_params, *query_params, *params, *query_params]
            order = "m.created_at DESC, m.id DESC" if sort == "newest" else "m.created_at ASC, m.id ASC" if sort == "oldest" else "rank DESC, m.id DESC"
        params.extend([limit, offset])
        sql = (
            f"SELECT m.id, m.session_id, m.role, {snippet} AS snippet, m.created_at AS timestamp, m.tool_name, "
            f"s.source, s.model, s.started_at AS session_started, {rank} AS rank "
            f"FROM {self._schema}.messages m JOIN {self._schema}.sessions s ON s.id = m.session_id "
            f"WHERE {' AND '.join(predicates)} ORDER BY {order} LIMIT %s OFFSET %s"
        )
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            cursor.execute(sql, params)
            rows = list(cursor.fetchall())
        for row in rows:
            row.pop("rank", None)
        if result_fields is None or "context" in result_fields:
            try:
                contexts = self._search_contexts([int(row["id"]) for row in rows])
            except Exception:  # Context is best-effort; lexical candidates remain usable on enrichment failure.
                contexts = {}
            for row in rows:
                row["context"] = contexts.get(row["id"], [])
        if result_fields is not None:
            rows = [{field: row[field] for field in result_fields if field in row} for row in rows]
        return rows

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
                    f"SELECT id, parent_session_id, end_reason, model_config FROM {self._schema}.sessions "
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
            cursor.execute(f"SELECT {columns} FROM {self._schema}.messages WHERE session_id IN ({placeholders}) AND {predicate} ORDER BY id", session_ids)
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
            cursor.execute(f"SELECT model, billing_provider, api_call_count FROM {self._schema}.sessions WHERE id=%s FOR UPDATE", (session_id,))
            row = cursor.fetchone() or {}
            if int(row.get("api_call_count") or 0) == 0 and accounted and model and billing_provider and (row.get("model") != model or row.get("billing_provider") != billing_provider):
                cursor.execute(f"UPDATE {self._schema}.sessions SET model=%s, billing_provider=%s, billing_base_url=%s, billing_mode=%s WHERE id=%s", (model, billing_provider, billing_base_url, billing_mode, session_id))
            additions = not absolute
            set_counters = ", ".join(f"{field} = {'%s' if not additions else field + ' + %s'}" for field in _USAGE_COUNTERS)
            estimated = "COALESCE(%s, 0)" if absolute else "COALESCE(estimated_cost_usd, 0) + COALESCE(%s, 0)"
            actual = "CASE WHEN %s::double precision IS NULL THEN actual_cost_usd ELSE %s::double precision END" if absolute else "CASE WHEN %s::double precision IS NULL THEN actual_cost_usd ELSE COALESCE(actual_cost_usd, 0) + %s::double precision END"
            calls = "%s" if absolute else "api_call_count + %s"
            route = (billing_provider if accounted else None, billing_base_url if accounted else None, billing_mode if accounted else None, model if accounted else None)
            cursor.execute(f"UPDATE {self._schema}.sessions SET {set_counters}, estimated_cost_usd={estimated}, actual_cost_usd={actual}, cost_status=COALESCE(%s,cost_status), cost_source=COALESCE(%s,cost_source), pricing_version=COALESCE(%s,pricing_version), billing_provider=COALESCE(billing_provider,%s), billing_base_url=COALESCE(billing_base_url,%s), billing_mode=COALESCE(billing_mode,%s), model=COALESCE(model,%s), api_call_count={calls} WHERE id=%s", (*counters, estimated_cost_usd, actual_cost_usd, actual_cost_usd, cost_status, cost_source, pricing_version, *route, api_call_count, session_id))
            if not absolute and has_usage:
                self._record_model_usage(cursor, session_id, model=model, billing_provider=billing_provider, billing_base_url=billing_base_url, billing_mode=billing_mode, input_tokens=input_tokens, output_tokens=output_tokens, cache_read_tokens=cache_read_tokens, cache_write_tokens=cache_write_tokens, reasoning_tokens=reasoning_tokens, estimated_cost_usd=estimated_cost_usd, actual_cost_usd=actual_cost_usd, cost_status=cost_status, cost_source=cost_source, api_call_count=api_call_count)

    def _record_model_usage(self, cursor: Any, session_id: str, *, model: str | None = None, billing_provider: str | None = None, billing_base_url: str | None = None, billing_mode: str | None = None, input_tokens: int = 0, output_tokens: int = 0, cache_read_tokens: int = 0, cache_write_tokens: int = 0, reasoning_tokens: int = 0, estimated_cost_usd: float | None = None, actual_cost_usd: float | None = None, cost_status: str | None = None, cost_source: str | None = None, api_call_count: int = 0, task: str = "") -> None:
        session = {}
        if not task:
            cursor.execute(f"SELECT model, billing_provider, billing_base_url, billing_mode FROM {self._schema}.sessions WHERE id=%s", (session_id,))
            session = cursor.fetchone() or {}
        now = time.time()
        cursor.execute(f"""INSERT INTO {self._schema}.session_model_usage (session_id,model,billing_provider,billing_base_url,billing_mode,task,api_call_count,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,reasoning_tokens,estimated_cost_usd,actual_cost_usd,cost_status,cost_source,first_seen,last_seen) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (session_id,model,billing_provider,billing_base_url,billing_mode,task) DO UPDATE SET api_call_count=session_model_usage.api_call_count+EXCLUDED.api_call_count,input_tokens=session_model_usage.input_tokens+EXCLUDED.input_tokens,output_tokens=session_model_usage.output_tokens+EXCLUDED.output_tokens,cache_read_tokens=session_model_usage.cache_read_tokens+EXCLUDED.cache_read_tokens,cache_write_tokens=session_model_usage.cache_write_tokens+EXCLUDED.cache_write_tokens,reasoning_tokens=session_model_usage.reasoning_tokens+EXCLUDED.reasoning_tokens,estimated_cost_usd=session_model_usage.estimated_cost_usd+EXCLUDED.estimated_cost_usd,actual_cost_usd=session_model_usage.actual_cost_usd+EXCLUDED.actual_cost_usd,cost_status=COALESCE(EXCLUDED.cost_status,session_model_usage.cost_status),cost_source=COALESCE(EXCLUDED.cost_source,session_model_usage.cost_source),last_seen=EXCLUDED.last_seen""", (session_id,model or session.get("model") or "unknown",billing_provider or session.get("billing_provider") or "",billing_base_url or session.get("billing_base_url") or "",billing_mode or session.get("billing_mode") or "",task or "",api_call_count or 0,input_tokens or 0,output_tokens or 0,cache_read_tokens or 0,cache_write_tokens or 0,reasoning_tokens or 0,float(estimated_cost_usd or 0),float(actual_cost_usd or 0),cost_status,cost_source,now,now))

    def record_auxiliary_usage(self, session_id: str, task: str, **kwargs: Any) -> None:
        if not session_id or not task:
            return
        self.ensure_session(session_id)
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            self._record_model_usage(cursor, session_id, task=task, api_call_count=int(kwargs.pop("api_call_count", 1) if kwargs.get("api_call_count") is not None else 1), **kwargs)

    def end_session(self, session_id: str, end_reason: str) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {self._schema}.sessions SET ended_at = %s, end_reason = %s "
                "WHERE id = %s AND ended_at IS NULL RETURNING source, session_key",
                (time.time(), end_reason, session_id),
            )
            self._bump_conversation_generation(cursor, cursor.fetchone(), end_reason)

    def _bump_conversation_generation(self, cursor: Any, row: Any, end_reason: str) -> None:
        """Advance only for the row this transaction newly marked as a reset.

        The row-returning update makes first-end-reason and generation increment
        one commit. The durable table has no session FK and is never pruned.
        """
        if row is None or end_reason not in _RESET_END_REASONS:
            return
        source, session_key = (str(value or "").strip() for value in row)
        if source and session_key:
            cursor.execute(
                f"INSERT INTO {self._schema}.conversation_generations (source, session_key, generation) VALUES (%s, %s, 1) "
                "ON CONFLICT (source, session_key) DO UPDATE "
                "SET generation = conversation_generations.generation + 1",
                (source, session_key),
            )

    def promote_to_session_reset(self, session_id: str, reason: str = "session_reset") -> bool:
        """Promote a live/recoverably closed row atomically, matching SQLite.

        Explicitly ended rows are immutable; only a successful promotion may
        advance the peer generation.
        """
        if not session_id:
            return False
        try:
            with self._connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {self._schema}.sessions SET ended_at = %s, end_reason = %s "
                    "WHERE id = %s AND (ended_at IS NULL OR end_reason = ANY(%s)) "
                    "RETURNING source, session_key",
                    (time.time(), reason, session_id, list(_RECOVERABLE_END_REASONS)),
                )
                row = cursor.fetchone()
                self._bump_conversation_generation(cursor, row, reason)
                return row is not None
        except Exception:
            return False

    def latest_conversation_boundary(self, session_key: str, source: str) -> int | None:
        """Return the durable source-qualified generation, never an aggregate."""
        if not session_key or not source:
            return None
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT generation FROM {self._schema}.conversation_generations WHERE source = %s AND session_key = %s",
                (source, session_key),
            )
            row = cursor.fetchone()
        generation = int(row[0]) if row is not None and row[0] is not None else 0
        return generation if generation > 0 else None

    @staticmethod
    def _model_config_object(value: Any) -> dict[str, Any]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                return {}
        return dict(value) if isinstance(value, Mapping) else {}

    def update_session_meta(self, session_id: str, model_config_json: str, model: str | None = None) -> None:
        """Replace model config and optionally fill a missing model after queued usage is durable."""
        self.flush_token_counts()
        config = self._model_config_object(model_config_json)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {self._schema}.sessions SET model_config = %s, model = COALESCE(%s, model) WHERE id = %s",
                (self._psycopg.types.json.Jsonb(config) if config else None, model, session_id),
            )

    def patch_session_model_config(self, session_id: str, patch: Mapping[str, Any]) -> None:
        """Atomically shallow-merge config; a ``None`` value removes its key."""
        if not session_id or not patch:
            return
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            cursor.execute(f"SELECT model_config FROM {self._schema}.sessions WHERE id = %s FOR UPDATE", (session_id,))
            row = cursor.fetchone()
            if row is None:
                return
            config = self._model_config_object(row.get("model_config"))
            for key, value in patch.items():
                if value is None:
                    config.pop(key, None)
                else:
                    config[key] = value
            cursor.execute(
                f"UPDATE {self._schema}.sessions SET model_config = %s WHERE id = %s",
                (self._psycopg.types.json.Jsonb(config) if config else None, session_id),
            )

    def get_session_model_config_value(self, session_id: str, key: str, default: Any = None) -> Any:
        session = self.get_session(session_id) or {}
        return self._model_config_object(session.get("model_config")).get(key, default)

    def update_session_model(self, session_id: str, model: str, provider: str | None = None) -> None:
        """Switch the persisted route after queued pre-switch usage has drained."""
        self.flush_token_counts()
        patch: dict[str, Any] = {"browser_model_lock": None}
        if model:
            patch["model"] = model
        if provider:
            patch["provider"] = provider
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            cursor.execute(f"SELECT model_config FROM {self._schema}.sessions WHERE id = %s FOR UPDATE", (session_id,))
            row = cursor.fetchone()
            if row is None:
                return
            config = self._model_config_object(row.get("model_config"))
            for key, value in patch.items():
                if value is None:
                    config.pop(key, None)
                else:
                    config[key] = value
            cursor.execute(
                f"UPDATE {self._schema}.sessions SET model = %s, model_config = %s, system_prompt_hash = NULL WHERE id = %s",
                (model, self._psycopg.types.json.Jsonb(config) if config else None, session_id),
            )
            cursor.execute(
                f"DELETE FROM {self._schema}.system_prompts p WHERE NOT EXISTS "
                f"(SELECT 1 FROM {self._schema}.sessions s WHERE s.system_prompt_hash = p.hash)"
            )

    def update_session_billing_route(
        self, session_id: str, *, provider: str, base_url: str, billing_mode: str | None = None,
    ) -> None:
        """Persist the latest billable route after queued pre-switch usage has drained."""
        self.flush_token_counts()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {self._schema}.sessions SET billing_provider = %s, billing_base_url = %s, "
                "billing_mode = COALESCE(%s, billing_mode), system_prompt_hash = NULL WHERE id = %s",
                (provider, base_url, billing_mode, session_id),
            )
            cursor.execute(
                f"DELETE FROM {self._schema}.system_prompts p WHERE NOT EXISTS "
                f"(SELECT 1 FROM {self._schema}.sessions s WHERE s.system_prompt_hash = p.hash)"
            )

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        self.flush_token_counts()
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            cursor.execute(
                f"SELECT s.id, s.source, s.started_at, s.ended_at, s.end_reason, s.title, s.title_source, s.hidden, s.archived, s.pinned, "
                f"s.system_prompt_hash, s.git_branch, s.git_metadata_generation, s.last_activity_at, s.last_activity_description, s.last_activity_provenance, "
                f"s.compression_failure_cooldown_until, s.compression_failure_error, s.compression_fallback_streak, s.compression_ineffective_count, s.compression_recovery_deadline, p.prompt AS system_prompt, {', '.join('s.' + column for column in _SESSION_METADATA_COLUMNS)} "
                f"FROM {self._schema}.sessions s LEFT JOIN {self._schema}.system_prompts p ON p.hash = s.system_prompt_hash WHERE s.id = %s", (session_id,),
            )
            return cursor.fetchone()

    def get_message_storage_state(self, message_id: int) -> dict[str, Any] | None:
        """Return only the tenant-local visibility state required by contextual recall."""
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            cursor.execute(
                f"SELECT session_id, active, compacted FROM {self._schema}.messages WHERE id = %s",
                (message_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return {"session_id": row["session_id"], "active": int(row["active"]), "compacted": int(row["compacted"])}

    def update_session_cwd(
        self, session_id: str, cwd: str, git_branch: str | None = None,
        git_repo_root: str | None = None, replace_git_meta: bool = False,
    ) -> int | None:
        """Claim Git enrichment authority atomically, matching SessionDB's A→B→A fence."""
        if not session_id or not cwd:
            return None
        branch, repo_root = (git_branch or "").strip(), (git_repo_root or "").strip()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {self._schema}.sessions SET cwd = %s, "
                "git_metadata_generation = git_metadata_generation + 1, "
                "git_branch = CASE WHEN cwd IS DISTINCT FROM %s OR %s THEN %s "
                "WHEN %s <> '' THEN %s ELSE git_branch END, "
                "git_repo_root = CASE WHEN cwd IS DISTINCT FROM %s OR %s THEN %s "
                "WHEN %s <> '' THEN %s ELSE git_repo_root END "
                "WHERE id = %s RETURNING git_metadata_generation",
                (cwd, cwd, replace_git_meta, branch or None, branch, branch or None,
                 cwd, replace_git_meta, repo_root or None, repo_root, repo_root or None, session_id),
            )
            row = cursor.fetchone()
            return None if row is None else int(row[0])

    def publish_session_git_metadata(
        self, session_id: str, cwd: str, generation: int, git_branch: str | None = None,
        git_repo_root: str | None = None,
    ) -> bool:
        """Publish an async Git probe only if its claim has not been superseded."""
        if not session_id or not cwd or not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            return False
        fields = [("git_branch", (git_branch or "").strip()), ("git_repo_root", (git_repo_root or "").strip())]
        fields = [(column, value) for column, value in fields if value]
        if not fields:
            return False
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {self._schema}.sessions SET {', '.join(f'{column} = %s' for column, _ in fields)} "
                "WHERE id = %s AND cwd = %s AND git_metadata_generation = %s RETURNING id",
                [*(value for _, value in fields), session_id, cwd, generation],
            )
            return cursor.fetchone() is not None

    def set_system_prompt(self, session_id: str, system_prompt: str | None) -> None:
        prompt_hash = None if system_prompt is None else hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
        with self._connection() as connection, connection.cursor() as cursor:
            if prompt_hash is not None:
                cursor.execute(
                    f"INSERT INTO {self._schema}.system_prompts (hash, prompt) VALUES (%s, %s) ON CONFLICT (hash) DO NOTHING",
                    (prompt_hash, system_prompt),
                )
            cursor.execute(f"UPDATE {self._schema}.sessions SET system_prompt_hash = %s WHERE id = %s", (prompt_hash, session_id))
            cursor.execute(
                f"DELETE FROM {self._schema}.system_prompts p WHERE NOT EXISTS "
                f"(SELECT 1 FROM {self._schema}.sessions s WHERE s.system_prompt_hash = p.hash)"
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
                        JOIN {self._schema}.sessions child ON child.id = a.id
                        JOIN {self._schema}.sessions parent ON parent.id = child.parent_session_id
                        WHERE parent.end_reason = 'compression'
                    ),
                    descendants(id) AS (
                        SELECT %s
                        UNION
                        SELECT child.id FROM descendants d
                        JOIN {self._schema}.sessions parent ON parent.id = d.id
                        JOIN {self._schema}.sessions child ON child.parent_session_id = parent.id
                        WHERE parent.end_reason = 'compression'
                    ), lineage(id) AS (
                        SELECT id FROM ancestors UNION SELECT id FROM descendants
                    )
                    UPDATE {self._schema}.sessions SET {column} = %s WHERE id IN (SELECT id FROM lineage)""",
                (session_id, session_id, value),
            )
            return cursor.rowcount > 0

    def set_session_archived(self, session_id: str, archived: bool) -> bool:
        return self._set_lineage_column("archived", session_id, archived)

    def set_session_pinned(self, session_id: str, pinned: bool) -> bool:
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            cursor.execute(f"SELECT title, hidden FROM {self._schema}.sessions WHERE id = %s FOR UPDATE", (session_id,))
            row = cursor.fetchone()
            if row is None:
                return False
            cursor.execute(
                f"""WITH RECURSIVE
                    ancestors(id) AS (SELECT %s UNION SELECT parent.id FROM ancestors a JOIN {self._schema}.sessions child ON child.id = a.id JOIN {self._schema}.sessions parent ON parent.id = child.parent_session_id WHERE parent.end_reason = 'compression'),
                    descendants(id) AS (SELECT %s UNION SELECT child.id FROM descendants d JOIN {self._schema}.sessions parent ON parent.id = d.id JOIN {self._schema}.sessions child ON child.parent_session_id = parent.id WHERE parent.end_reason = 'compression'),
                    lineage(id) AS (SELECT id FROM ancestors UNION SELECT id FROM descendants)
                    UPDATE {self._schema}.sessions SET pinned = %s WHERE id IN (SELECT id FROM lineage)""",
                (session_id, session_id, pinned),
            )
            changed = cursor.rowcount > 0
            if pinned and not (row["hidden"] and row["title"] == _CANONICAL_BOT_CHAT_TITLE):
                cursor.execute(
                    f"""WITH RECURSIVE
                        ancestors(id) AS (SELECT %s UNION SELECT parent.id FROM ancestors a JOIN {self._schema}.sessions child ON child.id = a.id JOIN {self._schema}.sessions parent ON parent.id = child.parent_session_id WHERE parent.end_reason = 'compression'),
                        descendants(id) AS (SELECT %s UNION SELECT child.id FROM descendants d JOIN {self._schema}.sessions parent ON parent.id = d.id JOIN {self._schema}.sessions child ON child.parent_session_id = parent.id WHERE parent.end_reason = 'compression'),
                        lineage(id) AS (SELECT id FROM ancestors UNION SELECT id FROM descendants)
                        UPDATE {self._schema}.sessions SET hidden = false WHERE id IN (SELECT id FROM lineage)""",
                    (session_id, session_id),
                )
            return changed

    def list_recent_sessions_bounded(
        self, *, limit: int = 20, exclude_sources: list[str] | None = None,
        timeout_seconds: float = 3.0, candidate_limit: int | None = None,
        lineage_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return the bounded, compression-aware contextual browse projection.

        This is intentionally a direct StateStore operation, not an opt-in to
        ``ContextualSessionSearchStore``: PostgreSQL still lacks the other
        contextual shapes.  The CTE follows only compression-continuation
        edges, rejects incomplete/cyclic/cap-exhausted lineages, and projects a
        visible root to its freshest terminal tip in one tenant-scoped snapshot.
        """
        limit = max(1, int(limit))
        timeout_seconds = max(0.0, float(timeout_seconds))
        if candidate_limit is None:
            candidate_limit = max(128, limit * 8)
        candidate_limit = max(limit, min(int(candidate_limit), 2048))
        if lineage_limit is None:
            lineage_limit = min(8192, candidate_limit * 8)
        lineage_limit = max(candidate_limit, min(int(lineage_limit), 8192))
        excluded = list(exclude_sources or [])
        candidate_filter = "NOT s.archived AND NOT s.hidden AND NOT (COALESCE(s.model_config, '{}'::jsonb) ? '_delegate_from')"
        params: list[Any] = []
        if excluded:
            candidate_filter += " AND NOT (s.source = ANY(%s))"
            params.append(excluded)
        edge = (
            "parent.end_reason = 'compression' AND child.parent_session_id = parent.id "
            "AND NOT (COALESCE(child.model_config, '{}'::jsonb) ? '_branched_from') "
            "AND NOT (COALESCE(child.model_config, '{}'::jsonb) ? '_delegate_from') "
            "AND COALESCE(child.source, '') <> 'tool'"
        )
        query = f"""
            WITH RECURSIVE
            recent_candidates(id) AS (
                SELECT s.id FROM {self._schema}.sessions AS s
                WHERE {candidate_filter}
                ORDER BY COALESCE(s.last_activity_at, s.started_at) DESC, s.started_at DESC, s.id DESC
                LIMIT %s
            ),
            ancestors(candidate_id, cur_id, depth, path) AS (
                SELECT id, id, 1, ARRAY[id]::text[] FROM recent_candidates
                UNION ALL
                SELECT a.candidate_id, parent.id, a.depth + 1, a.path || parent.id
                FROM ancestors AS a
                JOIN {self._schema}.sessions AS child ON child.id = a.cur_id
                JOIN {self._schema}.sessions AS parent ON {edge}
                WHERE a.depth < %s AND NOT parent.id = ANY(a.path)
            ),
            candidate_roots(root_id) AS (
                SELECT DISTINCT a.cur_id
                FROM ancestors AS a
                JOIN {self._schema}.sessions AS child ON child.id = a.cur_id
                WHERE NOT EXISTS (
                    SELECT 1 FROM {self._schema}.sessions AS parent WHERE {edge}
                )
                  AND NOT EXISTS (
                    SELECT 1 FROM ancestors AS clipped
                    WHERE clipped.candidate_id = a.candidate_id AND clipped.depth >= %s
                )
            ),
            chain(root_id, cur_id, depth, path) AS (
                SELECT root_id, root_id, 1, ARRAY[root_id]::text[] FROM candidate_roots
                UNION ALL
                SELECT c.root_id, child.id, c.depth + 1, c.path || child.id
                FROM chain AS c
                JOIN {self._schema}.sessions AS parent ON parent.id = c.cur_id
                JOIN {self._schema}.sessions AS child ON {edge}
                WHERE c.depth < %s AND NOT child.id = ANY(c.path)
            ),
            valid_roots(root_id) AS (
                SELECT root_id FROM chain GROUP BY root_id
                HAVING MAX(depth) < %s AND COUNT(DISTINCT cur_id) < %s
            ),
            ranked_tips AS (
                SELECT c.root_id, c.cur_id,
                       COALESCE((SELECT MAX(m.created_at) FROM {self._schema}.messages AS m WHERE m.session_id = tip.id), tip.last_activity_at, tip.started_at) AS activity,
                       ROW_NUMBER() OVER (PARTITION BY c.root_id ORDER BY COALESCE((SELECT MAX(m.created_at) FROM {self._schema}.messages AS m WHERE m.session_id = tip.id), tip.last_activity_at, tip.started_at) DESC, tip.id DESC) AS rank_in_root
                FROM chain AS c
                JOIN valid_roots AS valid ON valid.root_id = c.root_id
                JOIN {self._schema}.sessions AS tip ON tip.id = c.cur_id
                WHERE NOT EXISTS (
                    SELECT 1 FROM {self._schema}.sessions AS parent
                    JOIN {self._schema}.sessions AS child ON {edge}
                    WHERE parent.id = c.cur_id
                )
            )
            SELECT tip.id, tip.source, tip.model, COALESCE(tip.title, root.title) AS title,
                   root.started_at, tip.ended_at, tip.end_reason, rt.activity AS last_active,
                   COALESCE((
                       SELECT m.content FROM {self._schema}.messages AS m
                       WHERE m.session_id = tip.id AND m.role = 'user' AND m.content IS NOT NULL
                         AND (m.active OR m.compacted) AND COALESCE(m.display_kind, '') <> 'hidden'
                       ORDER BY m.created_at, m.id LIMIT 1
                   ), '') AS preview,
                   CASE WHEN root.id <> tip.id THEN root.id ELSE NULL END AS _lineage_root_id
            FROM ranked_tips AS rt
            JOIN {self._schema}.sessions AS root ON root.id = rt.root_id
            JOIN {self._schema}.sessions AS tip ON tip.id = rt.cur_id
            WHERE rt.rank_in_root = 1 AND NOT root.archived AND NOT root.hidden
              AND NOT (COALESCE(root.model_config, '{{}}'::jsonb) ? '_delegate_from')
            ORDER BY rt.activity DESC, root.started_at DESC, tip.id DESC
            LIMIT %s
        """
        params.extend([candidate_limit, lineage_limit, lineage_limit, lineage_limit, lineage_limit, lineage_limit, limit])
        try:
            with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
                cursor.execute("SELECT set_config('statement_timeout', %s, true)", (f"{max(1, int(timeout_seconds * 1000))}ms",))
                cursor.execute(query, params)
                rows = list(cursor.fetchall())
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "57014":
                raise TimeoutError(f"recent-session browse exceeded {timeout_seconds:g}s deadline") from exc
            raise
        for row in rows:
            row["preview"] = self._decode_content(row["preview"])
        return rows

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
        grouped = f" FROM {self._schema}.sessions s LEFT JOIN {self._schema}.messages m ON m.session_id = s.id{where} GROUP BY s.id"
        order = " ORDER BY last_active DESC, s.started_at DESC, s.id DESC"
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            cursor.execute(f"SELECT {projection}{grouped}{order} LIMIT %s OFFSET %s", [*params, limit, offset])
            rows = list(cursor.fetchall())
            if include_pinned:
                seen = {row["id"] for row in rows}
                pinned_where = where + (" AND s.pinned" if where else " WHERE s.pinned")
                cursor.execute(f"SELECT {projection} FROM {self._schema}.sessions s LEFT JOIN {self._schema}.messages m ON m.session_id = s.id{pinned_where} GROUP BY s.id{order}", params)
                rows.extend(row for row in cursor.fetchall() if row["id"] not in seen)
            return rows

    def _set_session_title(self, session_id: str, title: str, *, source: str) -> bool:
        cleaned_title = _sanitize_title(title)
        is_user = source == "user"
        if not is_user and source not in {"derived", "llm"}:
            raise ValueError(f"invalid automatic title source: {source!r}")
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            cursor.execute(f"SELECT title, title_source, hidden FROM {self._schema}.sessions WHERE id = %s FOR UPDATE", (session_id,))
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
                cursor.execute(f"SELECT id FROM {self._schema}.sessions WHERE title = %s AND id != %s FOR UPDATE", (cleaned_title, session_id))
                conflict = cursor.fetchone()
                if conflict:
                    conflict_id = conflict["id"]
                    cursor.execute(
                        f"WITH RECURSIVE ancestors(id) AS (SELECT %s UNION SELECT parent.id FROM ancestors a JOIN {self._schema}.sessions child ON child.id = a.id JOIN {self._schema}.sessions parent ON parent.id = child.parent_session_id WHERE parent.end_reason = 'compression') SELECT 1 FROM ancestors WHERE id = %s AND id != %s LIMIT 1",
                        (session_id, conflict_id, session_id),
                    )
                    if cursor.fetchone() is None:
                        raise ValueError(f"Title '{cleaned_title}' is already in use by session {conflict_id}")
                    cursor.execute(f"UPDATE {self._schema}.sessions SET title = NULL WHERE id = %s", (conflict_id,))
            cursor.execute(f"UPDATE {self._schema}.sessions SET title = %s, title_source = %s WHERE id = %s", (cleaned_title, source if cleaned_title else None, session_id))
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
            cursor.execute(f"UPDATE {self._schema}.sessions SET title_source = %s WHERE id = %s AND title IS NOT NULL", (source, session_id))
            return cursor.rowcount > 0

    def get_session_by_title(self, title: str) -> dict[str, Any] | None:
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            cursor.execute(f"SELECT id, source, started_at, ended_at, end_reason, title, title_source, hidden, archived, pinned, git_branch, git_metadata_generation, {', '.join(_SESSION_METADATA_COLUMNS)} FROM {self._schema}.sessions WHERE title = %s", (title,))
            return cursor.fetchone()

    def resolve_session_by_title(self, title: str) -> str | None:
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            cursor.execute(f"SELECT id FROM {self._schema}.sessions WHERE title LIKE %s ESCAPE '\\' ORDER BY started_at DESC", (_escape_like(title) + " #%",))
            row = cursor.fetchone()
            if row:
                return str(row["id"])
            cursor.execute(f"SELECT id FROM {self._schema}.sessions WHERE title = %s", (title,))
            row = cursor.fetchone()
            return None if row is None else str(row["id"])

    def get_next_title_in_lineage(self, base_title: str) -> str:
        match = _NUMBERED_TITLE_RE.match(base_title)
        base = match.group(1) if match else base_title
        with self._connection() as connection, connection.cursor(row_factory=self._psycopg.rows.dict_row) as cursor:
            cursor.execute(f"SELECT title FROM {self._schema}.sessions WHERE title = %s OR title LIKE %s ESCAPE '\\'", (base, _escape_like(base) + " #%"))
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
