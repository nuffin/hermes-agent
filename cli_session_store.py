"""CLI-only session-store acquisition; PostgreSQL never falls back to SQLite."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from state_store import MessageRecord, open_state_store, resolve_state_store_config


class PostgreSQLCLISessionCapabilityError(RuntimeError):
    """A CLI path requested a SessionDB feature not yet ported to PostgreSQL."""


class PostgreSQLCLISessionStore:
    """Strict CLI compatibility facade over the PostgreSQL StateStore contract.

    This is deliberately not a general SessionDB replacement.  Every method is
    either implemented with PostgreSQL semantics or rejected before a fallback
    can open SQLite state.
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    @staticmethod
    def _record(message: Mapping[str, Any]) -> MessageRecord:
        fields = MessageRecord.__dataclass_fields__
        return MessageRecord(**{name: message[name] for name in fields if name in message})

    def create_session(self, session_id: str, source: str, **kwargs: Any) -> str:
        model_config = kwargs.get("model_config")
        if isinstance(model_config, str):
            try:
                model_config = json.loads(model_config)
            except json.JSONDecodeError as exc:
                raise ValueError("CLI session model_config must be JSON") from exc
        metadata = {
            key: value for key, value in {
                "model": kwargs.get("model"), "model_config": model_config,
                "user_id": kwargs.get("user_id"), "session_key": kwargs.get("session_key"),
                "chat_id": kwargs.get("chat_id"), "chat_type": kwargs.get("chat_type"),
                "thread_id": kwargs.get("thread_id"), "display_name": kwargs.get("display_name"),
                "origin_json": kwargs.get("origin_json"), "parent_session_id": kwargs.get("parent_session_id"),
                "cwd": kwargs.get("cwd"), "profile_name": kwargs.get("profile_name"),
            }.items() if value is not None
        }
        created = self._store.ensure_session(session_id, source, metadata=metadata)
        if "system_prompt" in kwargs:
            self._store.set_system_prompt(session_id, kwargs["system_prompt"])
        return created

    ensure_session = create_session

    def append_message(
        self, session_id: str, role: str, content: Any = None, tool_name: str | None = None,
        tool_calls: Any = None, tool_call_id: str | None = None, token_count: int | None = None,
        finish_reason: str | None = None, reasoning: str | None = None,
        reasoning_content: str | None = None, reasoning_details: Any = None,
        codex_reasoning_items: Any = None, codex_message_items: Any = None,
        platform_message_id: str | None = None, observed: bool = False,
        effect_disposition: str | None = None, _compressed_summary: bool = False,
        timestamp: Any = None, api_content: str | None = None,
        display_kind: str | None = None, display_metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> int:
        """Persist the SessionDB-compatible single-message CLI surface.

        The facade accepts only fields representable by ``MessageRecord``.  In
        particular, SQLite-specific write-lock controls are rejected before a
        database write rather than being silently ignored or falling back.
        """
        if kwargs:
            raise PostgreSQLCLISessionCapabilityError(
                "PostgreSQL CLI persistence does not support single-message controls: "
                + ", ".join(sorted(kwargs)))
        return self._store.append_message_record(session_id, MessageRecord(
            role=role, content=content, tool_name=tool_name, tool_calls=tool_calls,
            tool_call_id=tool_call_id, token_count=token_count,
            finish_reason=finish_reason, reasoning=reasoning,
            reasoning_content=reasoning_content, reasoning_details=reasoning_details,
            codex_reasoning_items=codex_reasoning_items,
            codex_message_items=codex_message_items,
            platform_message_id=platform_message_id, observed=observed,
            effect_disposition=effect_disposition,
            _compressed_summary=_compressed_summary, timestamp=timestamp,
            api_content=api_content, display_kind=display_kind,
            display_metadata=display_metadata,
        ))

    def append_messages_batch(self, session_id: str, messages: list[Mapping[str, Any]], **kwargs: Any) -> int:
        # The agent's append path carries the holder which owns the surrounding
        # turn/compression lease. PostgreSQL coordinates those leases separately
        # from append-only rows; accepting the metadata here preserves the public
        # SessionDB call shape without pretending that it is an SQLite mutex.
        for name in ("compression_lock_holder", "turn_lease_holder", "turn_lease_ttl_seconds", "chunk_rows"):
            kwargs.pop(name, None)
        if kwargs:
            raise PostgreSQLCLISessionCapabilityError(
                "PostgreSQL CLI persistence does not support batch controls: " + ", ".join(sorted(kwargs)))
        return self._store.append_message_records(session_id, [self._record(message) for message in messages])

    def get_messages_as_conversation(self, session_id: str, *, include_ancestors: bool = False,
                                     repair_alternation: bool = False, **kwargs: Any) -> list[dict[str, Any]]:
        if kwargs:
            raise PostgreSQLCLISessionCapabilityError("PostgreSQL CLI resume does not support: " + ", ".join(sorted(kwargs)))
        restored, _display = self._store.get_resume_conversations(session_id)
        if not include_ancestors:
            # The adapter's resume projection is already the only safe model-fed view.
            return restored
        return restored

    def get_resume_conversations(self, session_id: str):
        return self._store.get_resume_conversations(session_id)

    # Observation and cooldown coordination are durable PostgreSQL operations.
    # Deliberate compression publication remains unavailable through this facade
    # until its parent/child transaction is ported as one contract.
    def touch_session_activity(self, session_id: str, ts: float | None = None, **kwargs: Any) -> None:
        return self._store.touch_session_activity(session_id, ts, **kwargs)
    def clear_session_activity_labels(self, session_id: str) -> None:
        return self._store.clear_session_activity_labels(session_id)
    def get_compression_failure_cooldown(self, session_id: str):
        return self._store.get_compression_failure_cooldown(session_id)
    def record_compression_failure_cooldown(self, session_id: str, cooldown_until: float, error: str | None = None) -> None:
        return self._store.record_compression_failure_cooldown(session_id, cooldown_until, error)
    def clear_compression_failure_cooldown(self, session_id: str) -> None:
        return self._store.clear_compression_failure_cooldown(session_id)
    def get_compression_failure_cooldown_row(self, session_id: str):
        return self._store.get_compression_failure_cooldown_row(session_id)
    def restore_compression_failure_cooldown_row(self, session_id: str, snapshot: Mapping[str, Any]) -> None:
        return self._store.restore_compression_failure_cooldown_row(session_id, snapshot)

    # Compression coordination is an all-or-nothing PostgreSQL contract. These
    # wrappers deliberately preserve the production adapter's arguments and
    # return values rather than emulating SQLite or offering a partial fallback.
    @property
    def capabilities(self): return self._store.capabilities
    def get_compression_fallback_streak(self, session_id: str): return self._store.get_compression_fallback_streak(session_id)
    def set_compression_fallback_streak(self, session_id: str, streak: int): return self._store.set_compression_fallback_streak(session_id, streak)
    def get_compression_ineffective_count(self, session_id: str): return self._store.get_compression_ineffective_count(session_id)
    def set_compression_ineffective_count(self, session_id: str, count: int): return self._store.set_compression_ineffective_count(session_id, count)
    def get_compression_recovery_deadline(self, session_id: str): return self._store.get_compression_recovery_deadline(session_id)
    def set_compression_recovery_deadline(self, session_id: str, deadline: float): return self._store.set_compression_recovery_deadline(session_id, deadline)
    def get_session_model_config_value(self, session_id: str, key: str, default: Any = None): return self._store.get_session_model_config_value(session_id, key, default)
    def patch_session_model_config(self, session_id: str, patch: Mapping[str, Any]): return self._store.patch_session_model_config(session_id, patch)
    def try_acquire_compression_lock(self, session_id: str, holder: str, ttl_seconds: float = 300.0): return self._store.try_acquire_compression_lock(session_id, holder, ttl_seconds)
    def refresh_compression_lock(self, session_id: str, holder: str, ttl_seconds: float = 300.0): return self._store.refresh_compression_lock(session_id, holder, ttl_seconds)
    def release_compression_lock(self, session_id: str, holder: str): return self._store.release_compression_lock(session_id, holder)
    def get_compression_lock_holder(self, session_id: str): return self._store.get_compression_lock_holder(session_id)
    def try_acquire_session_turn_lease(self, session_id: str, holder: str, *, ttl_seconds: float = 300.0, **kwargs: Any): return self._store.try_acquire_session_turn_lease(session_id, holder, ttl_seconds=ttl_seconds, **kwargs)
    def refresh_session_turn_lease(self, session_id: str, holder: str, *, ttl_seconds: float = 300.0): return self._store.refresh_session_turn_lease(session_id, holder, ttl_seconds=ttl_seconds)
    def release_session_turn_lease(self, session_id: str, holder: str): return self._store.release_session_turn_lease(session_id, holder)
    def get_active_message_watermark(self, session_id: str): return self._store.get_active_message_watermark(session_id)
    def get_compression_publication_receipt(self, request_id: str): return self._store.get_compression_publication_receipt(request_id)
    def publish_compression_child(self, **kwargs: Any): return self._store.publish_compression_child(**kwargs)

    def get_session(self, session_id: str): return self._store.get_session(session_id)
    def get_compression_tip(self, session_id: str): return self._store.get_compression_tip(session_id)
    def get_conversation_root(self, session_id: str): return self._store.get_conversation_root(session_id)
    def get_compression_lineage(self, session_id: str): return self._store.get_compression_lineage(session_id)
    def assert_resume_safe(self, session_id: str, *args: Any, **kwargs: Any): return self._store.assert_resume_safe(session_id, *args, **kwargs)
    def get_session_title(self, session_id: str): return self._store.get_session_title(session_id)
    def get_session_title_source(self, session_id: str): return self._store.get_session_title_source(session_id)
    def set_session_title_source(self, session_id: str, source: str): return self._store.set_session_title_source(session_id, source)
    def set_session_title(self, session_id: str, title: str): return self._store.set_session_title(session_id, title)
    def get_session_by_title(self, title: str): return self._store.get_session_by_title(title)
    def resolve_session_by_title(self, title: str): return self._store.resolve_session_by_title(title)
    def get_next_title_in_lineage(self, title: str): return self._store.get_next_title_in_lineage(title)
    def update_session_billing_route(self, session_id: str, **kwargs: Any): return self._store.update_session_billing_route(session_id, **kwargs)
    def queue_token_counts(self, session_id: str, **kwargs: Any): return self._store.queue_token_counts(session_id, **kwargs)
    def flush_token_counts(self, timeout: float = 5.0): return self._store.flush_token_counts(timeout)
    def record_auxiliary_usage(self, session_id: str, task: str, **kwargs: Any): return self._store.record_auxiliary_usage(session_id, task, **kwargs)
    def update_system_prompt(self, session_id: str, prompt: str | None): return self._store.set_system_prompt(session_id, prompt)
    def reopen_session(self, session_id: str) -> None:
        with self._store._connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"UPDATE {self._store._schema}.sessions SET ended_at=NULL, end_reason=NULL WHERE id=%s", (session_id,))
    def end_session(self, session_id: str, reason: str): return self._store.end_session(session_id, reason)
    def search_sessions(self, source: str | None = None, limit: int = 20, offset: int = 0,
                        workspace_key: str | None = None, **kwargs: Any):
        if kwargs:
            raise PostgreSQLCLISessionCapabilityError("PostgreSQL CLI session search does not support: " + ", ".join(sorted(kwargs)))
        rows = self._store.list_session_summaries(
            source=source, limit=max(limit + offset, 1000), offset=0,
            include_archived=True, include_hidden=True, include_pinned=True)
        if workspace_key:
            # CLI's resume scoping is an exact workspace identity (git root or CWD),
            # not a path-prefix search.  Resolve it in PostgreSQL before slicing.
            with self._store._connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT id FROM {self._store._schema}.sessions WHERE git_repo_root=%s OR cwd=%s",
                    (workspace_key, workspace_key))
                allowed = {row[0] for row in cursor.fetchall()}
            rows = [row for row in rows if row["id"] in allowed]
        return rows[offset:offset + limit]
    def delete_session_if_empty(self, session_id: str, **kwargs: Any) -> bool:
        if kwargs:
            raise PostgreSQLCLISessionCapabilityError("PostgreSQL CLI deletion does not manage session files")
        with self._store._connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM {self._store._schema}.sessions s WHERE s.id=%s AND s.title IS NULL AND NOT EXISTS (SELECT 1 FROM {self._store._schema}.messages m WHERE m.session_id=s.id) AND NOT EXISTS (SELECT 1 FROM {self._store._schema}.sessions c WHERE c.parent_session_id=s.id)", (session_id,))
            return cursor.rowcount > 0
    def delete_session(self, session_id: str, **kwargs: Any) -> bool:
        if set(kwargs) - {"sessions_dir"}:
            raise PostgreSQLCLISessionCapabilityError("PostgreSQL CLI deletion does not support these controls")
        with self._store._connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM {self._store._schema}.messages WHERE session_id=%s", (session_id,))
            cursor.execute(f"UPDATE {self._store._schema}.sessions SET parent_session_id=NULL WHERE parent_session_id=%s", (session_id,))
            cursor.execute(f"DELETE FROM {self._store._schema}.sessions WHERE id=%s", (session_id,))
            return cursor.rowcount > 0
    def close(self): self._store.close()

    def __getattr__(self, name: str):
        raise PostgreSQLCLISessionCapabilityError(
            f"PostgreSQL CLI session store does not implement '{name}'; no SQLite fallback is permitted")


def open_cli_session_store(config: Mapping[str, Any], *, read_only: bool = False) -> Any:
    """Acquire the selected CLI store without changing SQLite's legacy path."""
    resolved = resolve_state_store_config(config)
    if resolved.backend == "sqlite":
        from hermes_state import SessionDB
        if read_only:
            return SessionDB(read_only=True)
        # CLI's normal writer shares the registry handle with its REPL follow-up
        # paths, preserving the SQLite runtime's single-open behavior.
        from hermes_state_registry import acquire
        return acquire()
    return PostgreSQLCLISessionStore(open_state_store(config))
