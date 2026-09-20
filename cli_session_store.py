"""CLI-only session-store acquisition; PostgreSQL never falls back to SQLite."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from state_store import MessageRecord, open_state_store, resolve_state_store_config


_LOCAL_FILTER_CANDIDATE_LIMIT = 1000


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
    def resolve_session_id(self, session_id_or_prefix: str) -> str | None:
        """Resolve an exact ID or one unambiguous PostgreSQL-local prefix."""
        exact = self._store.get_session(session_id_or_prefix)
        if exact:
            return str(exact["id"])
        with self._store._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT id FROM {self._store._schema}.sessions WHERE id LIKE %s ORDER BY started_at DESC LIMIT 2",
                (f"{session_id_or_prefix}%",),
            )
            matches = cursor.fetchall()
        return str(matches[0][0]) if len(matches) == 1 else None

    def session_count(self, source: str | None = None) -> int:
        """Return the complete PostgreSQL session count for CLI statistics."""
        with self._store._connection() as connection, connection.cursor() as cursor:
            if source is None:
                cursor.execute(f"SELECT COUNT(*) FROM {self._store._schema}.sessions")
            else:
                cursor.execute(f"SELECT COUNT(*) FROM {self._store._schema}.sessions WHERE source=%s", (source,))
            row = cursor.fetchone()
        return int(row[0]) if row else 0

    def message_count(self) -> int:
        """Return the complete physical PostgreSQL message count for CLI statistics."""
        with self._store._connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM {self._store._schema}.messages")
            row = cursor.fetchone()
        return int(row[0]) if row else 0

    def export_session(self, session_id: str) -> dict[str, Any] | None:
        """Project one complete active PostgreSQL segment into SessionDB export shape."""
        session = self._store.get_session(session_id)
        if session is None:
            return None
        messages = self._store.get_message_records(session_id)
        from hermes_state_portability import _export_timings
        return {**session, "message_count": len(messages), "messages": messages, "timings": _export_timings(messages, session_id)}

    def export_session_lineage(self, session_id: str) -> dict[str, Any] | None:
        """Export the complete compression lineage when every segment is available."""
        lineage_ids = self._store.get_compression_lineage(session_id)
        if not lineage_ids:
            return None
        segments = [segment for segment in (self.export_session(item) for item in lineage_ids) if segment]
        if not segments:
            return None
        messages = [message for segment in segments for message in segment["messages"]]
        from hermes_state_portability import _export_timings
        return {
            **segments[-1], "segments": segments, "lineage_session_ids": [segment["id"] for segment in segments],
            "message_count": len(messages), "messages": messages,
            "timings": _export_timings(messages, session_id),
        }
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
    # The inline public session_search consumer receives this CLI facade from
    # AIAgent.  These are the complete read-only contextual primitives already
    # implemented by its selected PostgreSQL store, not SQLite emulation.
    def get_messages(self, session_id: str): return self._store.get_messages(session_id)
    def get_messages_around(self, session_id: str, around_message_id: int, *, window: int = 5): return self._store.get_messages_around(session_id, around_message_id, window=window)
    def get_anchored_view(self, session_id: str, around_message_id: int, *, window: int, bookend: int): return self._store.get_anchored_view(session_id, around_message_id, window=window, bookend=bookend)
    def get_message_storage_state(self, message_id: int): return self._store.get_message_storage_state(message_id)
    def search_messages(self, *args: Any, **kwargs: Any): return self._store.search_messages(*args, **kwargs)
    def list_recent_sessions_bounded(self, *, limit: int, exclude_sources: list[str], timeout_seconds: float): return self._store.list_recent_sessions_bounded(limit=limit, exclude_sources=exclude_sources, timeout_seconds=timeout_seconds)
    def search_index_status(self): return self._store.search_index_status()
    def rebuild_search_index(self): return self._store.rebuild_search_index()
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
    def list_sessions_rich(
        self, source: str | None = None, sources: list[str] | None = None,
        exclude_sources: list[str] | None = None, cwd_prefix: str | None = None,
        limit: int = 20, offset: int = 0, include_children: bool = False,
        min_message_count: int = 0, project_compression_tips: bool = True,
        order_by_last_active: bool = False, include_archived: bool = False,
        archived_only: bool = False, id_query: str | None = None,
        search_query: str | None = None, compact_rows: bool = False,
        include_pinned: bool = False, session_key: str | None = None,
        include_hidden: bool = False,
    ) -> list[dict[str, Any]]:
        """Project PostgreSQL summary rows into the bounded CLI listing contract.

        The state-store owns the canonical source/archive/hidden filters and MRU
        order.  This facade adds only SessionDB's presentation and local CLI
        filters, then resolves compression heads to their resumable descendants.
        """
        if sources is not None or cwd_prefix is not None or min_message_count or id_query is not None:
            raise PostgreSQLCLISessionCapabilityError(
                "PostgreSQL CLI session listing does not support these filters")
        if limit < 0 or offset < 0:
            raise ValueError("session list limit and offset must be non-negative")
        search = (search_query or "").strip().lower()
        # Source/archive/hidden/pinned filters are backend predicates, so they
        # must be applied before pagination.  Bare /resume has no local filter:
        # fetch only its requested page and hydrate only those rows.  Title/ID
        # and workspace filters require post-fetch inspection, which stays
        # explicitly bounded rather than imposing that scan on every listing.
        local_filter = bool(search or session_key)
        candidate_limit = limit + offset
        if local_filter:
            candidate_limit = max(candidate_limit, _LOCAL_FILTER_CANDIDATE_LIMIT)
        rows = self._store.list_session_summaries(
            source=source, exclude_sources=tuple(exclude_sources or ()),
            limit=candidate_limit, offset=0,
            include_archived=include_archived, archived_only=archived_only,
            include_hidden=include_hidden, include_pinned=include_pinned,
        )
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for summary in rows:
            original_id = str(summary["id"])
            session_id = self._store.get_compression_tip(original_id) if project_compression_tips and not include_children else original_id
            if session_id in seen:
                continue
            session = self._store.get_session(session_id)
            if session is None:
                continue
            title = str(session.get("title") or "")
            if search and search not in session_id.lower() and search not in title.lower():
                continue
            if session_key and session.get("git_repo_root") != session_key and session.get("cwd") != session_key:
                continue
            messages = self._store.get_message_records(session_id)
            preview_raw = next((row.get("content") for row in messages if row.get("role") == "user" and row.get("content") is not None), "")
            from hermes_state_sessions import _shape_preview
            preview = _shape_preview(preview_raw)
            row = {**session, **summary, "id": session_id, "preview": preview,
                   "last_active": session.get("last_active", summary.get("last_active")),
                   "message_count": len(messages)}
            seen.add(session_id)
            result.append(row)
        if order_by_last_active or search:
            result.sort(key=lambda row: (row.get("last_active") or 0, row.get("started_at") or 0, row["id"]), reverse=True)
        else:
            result.sort(key=lambda row: (row.get("started_at") or 0, row["id"]), reverse=True)
        return result[offset:offset + limit]

    def resolve_resume_session_id(self, session_id: str) -> str:
        """Return the PostgreSQL compression tip that existing resume APIs read."""
        return self._store.get_compression_tip(session_id)

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
    def _delegate_delete_ids(self, cursor: Any, session_id: str) -> list[str]:
        """Return recursively tagged delegate descendants, excluding *session_id*.

        The recursive CTE uses ``UNION`` deliberately: malformed delegation cycles
        terminate, and the root can never become its own descendant.  Untagged
        branch/compression children remain outside this list and are orphaned.
        """
        schema = self._store._schema
        cursor.execute(
            f"WITH RECURSIVE delegates(id) AS ("
            f"SELECT id FROM {schema}.sessions WHERE id=%s "
            "UNION "
            f"SELECT child.id FROM {schema}.sessions child "
            "JOIN delegates parent ON ("
            "child.model_config ->> '_delegate_from' = parent.id "
            "OR (child.parent_session_id = parent.id "
            "AND child.model_config ? '_delegate_from'))"
            ") SELECT id FROM delegates WHERE id <> %s ORDER BY id",
            (session_id, session_id),
        )
        return [str(row[0]) for row in cursor.fetchall()]

    def get_session_delete_targets(self, session_id: str) -> list[str]:
        """Rows an explicit delete would remove, matching SessionDB semantics."""
        with self._store._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT id FROM {self._store._schema}.sessions WHERE id=%s",
                (session_id,),
            )
            if cursor.fetchone() is None:
                return []
            return [session_id, *self._delegate_delete_ids(cursor, session_id)]

    def _delete_session_ids(self, cursor: Any, root_ids: list[str]) -> list[str]:
        """Delete existing roots and their delegates in the caller's transaction."""
        schema = self._store._schema
        cursor.execute(
            f"SELECT id FROM {schema}.sessions WHERE id = ANY(%s) FOR UPDATE",
            (root_ids,),
        )
        existing_roots = [str(row[0]) for row in cursor.fetchall()]
        if not existing_roots:
            return []
        doomed = set(existing_roots)
        for root_id in existing_roots:
            doomed.update(self._delegate_delete_ids(cursor, root_id))
        doomed_ids = sorted(doomed)
        # Lock the entire doomed set before changing dependencies. A concurrent
        # child insertion needs a conflicting FK lock on its parent.
        cursor.execute(f"SELECT id FROM {schema}.sessions WHERE id = ANY(%s) FOR UPDATE", (doomed_ids,))
        cursor.execute(
            f"DELETE FROM {schema}.compression_rotation_receipts "
            "WHERE parent_session_id = ANY(%s) OR child_session_id = ANY(%s)",
            (doomed_ids, doomed_ids),
        )
        cursor.execute(f"DELETE FROM {schema}.messages WHERE session_id = ANY(%s)", (doomed_ids,))
        cursor.execute(
            f"UPDATE {schema}.sessions SET parent_session_id=NULL "
            "WHERE parent_session_id = ANY(%s) AND NOT (id = ANY(%s))",
            (doomed_ids, doomed_ids),
        )
        cursor.execute(f"DELETE FROM {schema}.sessions WHERE id = ANY(%s)", (doomed_ids,))
        cursor.execute(
            f"DELETE FROM {schema}.system_prompts prompt WHERE NOT EXISTS ("
            f"SELECT 1 FROM {schema}.sessions session "
            "WHERE session.system_prompt_hash = prompt.hash)"
        )
        return existing_roots

    def delete_session_if_empty(self, session_id: str, **kwargs: Any) -> bool:
        if kwargs:
            raise PostgreSQLCLISessionCapabilityError("PostgreSQL CLI deletion does not manage session files")
        with self._store._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT id FROM {self._store._schema}.sessions session WHERE id=%s "
                "AND title IS NULL AND NOT EXISTS (SELECT 1 FROM "
                f"{self._store._schema}.messages message WHERE message.session_id=session.id) "
                "AND NOT EXISTS (SELECT 1 FROM "
                f"{self._store._schema}.sessions child WHERE child.parent_session_id=session.id) FOR UPDATE",
                (session_id,),
            )
            if cursor.fetchone() is None:
                return False
            return bool(self._delete_session_ids(cursor, [session_id]))

    def delete_session(self, session_id: str, **kwargs: Any) -> bool:
        unsupported = set(kwargs) - {"sessions_dir", "expected_delete_ids"}
        if unsupported:
            raise PostgreSQLCLISessionCapabilityError("PostgreSQL CLI deletion does not support these controls")
        expected = kwargs.get("expected_delete_ids")
        if expected is not None and not isinstance(expected, list):
            raise PostgreSQLCLISessionCapabilityError("expected_delete_ids must be a list")
        # PostgreSQL owns all durable session data; sessions_dir is a strict
        # compatibility no-op and no filesystem data is removed in this mode.
        with self._store._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT id FROM {self._store._schema}.sessions WHERE id=%s FOR UPDATE",
                (session_id,),
            )
            if cursor.fetchone() is None:
                return False
            current = {session_id, *self._delegate_delete_ids(cursor, session_id)}
            if expected is not None and current != set(expected):
                return False
            return bool(self._delete_session_ids(cursor, [session_id]))
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
