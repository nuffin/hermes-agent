"""Backend-neutral state-store configuration and bootstrap boundary.

This module intentionally does not expose database connections.  It is the
first incremental seam for moving SQLite-owned state behind a contract while
keeping existing SQLite installations operational without configuration changes.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SUPPORTED_BACKENDS = frozenset({"sqlite", "postgresql"})


class StateStoreConfigurationError(ValueError):
    """A selected state-store backend has unsafe or unusable configuration."""


@dataclass(frozen=True)
class PostgreSQLStateStoreConfig:
    """Non-secret PostgreSQL connection policy; the DSN is never retained here."""

    dsn_env: str
    connect_timeout_seconds: int
    pool_max_size: int


@dataclass(frozen=True)
class ResolvedStateStoreConfig:
    """Selected backend and non-secret connection policy.

    The DSN stays in the secret resolver and must not be logged, repr'd, or
    propagated as normal configuration data.
    """

    backend: str
    postgresql: PostgreSQLStateStoreConfig | None = None


class StateStore(Protocol):
    """Incremental session/message/title contract; broader SessionDB APIs stay out of scope."""

    def ensure_session(
        self, session_id: str, source: str = "unknown", *, metadata: Mapping[str, Any] | None = None,
    ) -> str: ...

    def append_message(self, session_id: str, *, role: str, content: str | None = None) -> int: ...

    def get_messages(self, session_id: str) -> list[dict[str, Any]]: ...

    def end_session(self, session_id: str, end_reason: str) -> None: ...

    def get_session(self, session_id: str) -> dict[str, Any] | None: ...

    def set_session_hidden(self, session_id: str, hidden: bool) -> bool: ...

    def set_session_title(self, session_id: str, title: str) -> bool: ...

    def set_auto_title(self, session_id: str, title: str, *, source: str) -> bool: ...

    def get_session_title(self, session_id: str) -> str | None: ...

    def get_session_title_source(self, session_id: str) -> str | None: ...

    def set_session_title_source(self, session_id: str, source: str) -> bool: ...

    def get_session_by_title(self, title: str) -> dict[str, Any] | None: ...

    def resolve_session_by_title(self, title: str) -> str | None: ...

    def get_next_title_in_lineage(self, base_title: str) -> str: ...

    def close(self) -> None: ...


def _scoped_secret(name: str) -> str | None:
    """Use Hermes's profile-aware secret scope rather than borrowing another profile's env."""
    try:
        from hermes_cli.config import _env_ref_lookup
    except Exception:
        return os.environ.get(name)
    return _env_ref_lookup(name)


def _positive_int(value: Any, path: str) -> int:
    if isinstance(value, bool):
        raise StateStoreConfigurationError(f"{path} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise StateStoreConfigurationError(f"{path} must be a positive integer") from exc
    if parsed <= 0:
        raise StateStoreConfigurationError(f"{path} must be a positive integer")
    return parsed


def resolve_state_store_config(
    config: Mapping[str, Any], *, secret_lookup: Callable[[str], str | None] | None = None,
) -> ResolvedStateStoreConfig:
    """Resolve and validate state-store policy without retaining the DSN secret."""
    raw = config.get("state_store", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise StateStoreConfigurationError("state_store must be a mapping")
    backend = str(raw.get("backend", "sqlite")).strip().lower()
    if backend not in _SUPPORTED_BACKENDS:
        allowed = ", ".join(sorted(_SUPPORTED_BACKENDS))
        raise StateStoreConfigurationError(f"state_store.backend must be one of: {allowed}")
    if backend == "sqlite":
        return ResolvedStateStoreConfig(backend="sqlite")

    pg = raw.get("postgresql", {})
    if not isinstance(pg, Mapping):
        raise StateStoreConfigurationError("state_store.postgresql must be a mapping")
    dsn_env = str(pg.get("dsn_env", "")).strip()
    if not _ENV_NAME.fullmatch(dsn_env):
        raise StateStoreConfigurationError("state_store.postgresql.dsn_env must name an environment variable")
    settings = PostgreSQLStateStoreConfig(
        dsn_env=dsn_env,
        connect_timeout_seconds=_positive_int(pg.get("connect_timeout_seconds", 10), "state_store.postgresql.connect_timeout_seconds"),
        pool_max_size=_positive_int(pg.get("pool_max_size", 8), "state_store.postgresql.pool_max_size"),
    )
    lookup = secret_lookup or _scoped_secret
    if not str(lookup(settings.dsn_env) or "").strip():
        raise StateStoreConfigurationError(
            f"PostgreSQL state store requires secret {settings.dsn_env}; configure it in the active profile secret scope")
    return ResolvedStateStoreConfig(backend="postgresql", postgresql=settings)


class SqliteStateStore:
    """Narrow adapter over the existing SessionDB compatibility facade."""

    def __init__(self, db_path: Path | None = None) -> None:
        from hermes_state import SessionDB

        self._session_db = SessionDB() if db_path is None else SessionDB(db_path=db_path)

    def ensure_session(
        self, session_id: str, source: str = "unknown", *, metadata: Mapping[str, Any] | None = None,
    ) -> str:
        return self._session_db.ensure_session(session_id, source=source, **dict(metadata or {}))

    def append_message(self, session_id: str, *, role: str, content: str | None = None) -> int:
        if content is None:
            return self._session_db.append_message(session_id, role=role)
        return self._session_db.append_message(session_id, role=role, content=content)

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        return self._session_db.get_messages(session_id)

    def end_session(self, session_id: str, end_reason: str) -> None:
        self._session_db.end_session(session_id, end_reason)

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return self._session_db.get_session(session_id)

    def set_session_hidden(self, session_id: str, hidden: bool) -> bool:
        return self._session_db.set_session_hidden(session_id, hidden)

    def set_session_title(self, session_id: str, title: str) -> bool:
        return self._session_db.set_session_title(session_id, title)

    def set_auto_title(self, session_id: str, title: str, *, source: str) -> bool:
        return self._session_db.set_auto_title(session_id, title, source=source)

    def get_session_title(self, session_id: str) -> str | None:
        return self._session_db.get_session_title(session_id)

    def get_session_title_source(self, session_id: str) -> str | None:
        return self._session_db.get_session_title_source(session_id)

    def set_session_title_source(self, session_id: str, source: str) -> bool:
        return self._session_db.set_session_title_source(session_id, source)

    def get_session_by_title(self, title: str) -> dict[str, Any] | None:
        return self._session_db.get_session_by_title(title)

    def resolve_session_by_title(self, title: str) -> str | None:
        return self._session_db.resolve_session_by_title(title)

    def get_next_title_in_lineage(self, base_title: str) -> str:
        return self._session_db.get_next_title_in_lineage(base_title)

    def close(self) -> None:
        self._session_db.close()


def open_state_store(
    config: Mapping[str, Any], *, db_path: Path | None = None,
    secret_lookup: Callable[[str], str | None] | None = None,
) -> StateStore:
    """Open the selected narrow State Store; PostgreSQL never falls back to SQLite."""
    resolved = resolve_state_store_config(config, secret_lookup=secret_lookup)
    if resolved.backend == "sqlite":
        return SqliteStateStore(db_path=db_path)
    assert resolved.postgresql is not None
    dsn = (secret_lookup or _scoped_secret)(resolved.postgresql.dsn_env)
    if not str(dsn or "").strip():
        raise StateStoreConfigurationError(
            f"PostgreSQL state store requires secret {resolved.postgresql.dsn_env}; configure it in the active profile secret scope")
    from state_store_postgresql import PostgreSQLStateStore

    return PostgreSQLStateStore(resolved.postgresql, str(dsn))
