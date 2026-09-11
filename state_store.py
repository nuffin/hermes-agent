"""Backend-neutral state-store configuration and bootstrap boundary.

This module intentionally does not expose database connections.  It is the
first incremental seam for moving SQLite-owned state behind a contract while
keeping existing SQLite installations operational without configuration changes.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
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
    """Minimal lifecycle contract; operational methods migrate here in later slices."""

    def close(self) -> None:
        """Release backend-owned resources."""


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
