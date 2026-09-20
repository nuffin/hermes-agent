"""Selected-state-store boundary for legacy SQLite maintenance operations.

Maintenance commands historically operate on ``state.db`` files.  This facade
resolves the selected store before callers inspect or open those files, and
makes every unported PostgreSQL operation an explicit refusal instead of a
SQLite fallback.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from state_store import StateStoreConfigurationError
from state_store_runtime_readiness import inspect_runtime_activation


@dataclass(frozen=True)
class StateStoreMaintenanceOperations:
    """Backend-selected maintenance capability registry.

    Only SQLite file maintenance is implemented today.  PostgreSQL operations
    must be added here with a native implementation before a command may route
    to it; a capability name alone never authorizes a fallback.
    """

    selected_backend: str
    profile_home: Path
    _sqlite_capabilities = frozenset({
        "sessions-repair", "sessions-recover", "sessions-import", "sessions-repair-profiles",
        "backup", "backup-quick", "archive-import", "snapshot-list", "snapshot-create", "snapshot-restore",
        "snapshot-prune", "update-snapshot", "claw-snapshot",
    })

    @classmethod
    def resolve(
        cls, config: Mapping[str, Any] | None = None, *, home: Path | None = None,
    ) -> "StateStoreMaintenanceOperations":
        try:
            report = inspect_runtime_activation(config, home=home or _current_home(), include_inventory=False)
        except StateStoreConfigurationError as exc:
            # Preserve legacy SQLite maintenance for malformed unrelated config
            # fixtures.  A config that actually declares state_store is an
            # attempted backend selection and must fail closed instead.
            if _declares_state_store(config, home or _current_home()):
                raise StateStoreMaintenanceConfigurationError(
                    "PostgreSQL state-store configuration could not be resolved safely; "
                    f"no SQLite fallback is permitted: {exc}"
                ) from exc
            return cls("sqlite", (home or _current_home()).expanduser().resolve())
        return cls(report.selected_backend, Path(report.profile_home))

    def require(self, capability: str) -> None:
        if capability not in self._sqlite_capabilities:
            raise ValueError(f"Unknown state-store maintenance capability: {capability}")
        if self.selected_backend == "postgresql":
            raise StateStoreMaintenanceCapabilityError(capability, self.profile_home)


class StateStoreMaintenanceError(RuntimeError):
    """Maintenance cannot safely use the selected state store."""


class StateStoreMaintenanceConfigurationError(StateStoreMaintenanceError):
    """Selection itself was invalid, so falling through to SQLite is unsafe."""


class StateStoreMaintenanceCapabilityError(StateStoreMaintenanceError):
    """The selected PostgreSQL store lacks this native maintenance capability."""

    def __init__(self, capability: str, home: Path) -> None:
        self.capability = capability
        self.home = home
        command = capability.replace("-", " ")
        super().__init__(
            f"PostgreSQL state store does not support {command} yet; "
            "no SQLite fallback is permitted."
        )


def require_state_store_maintenance(
    capability: str, config: Mapping[str, Any] | None = None, *, home: Path | None = None,
) -> StateStoreMaintenanceOperations:
    """Resolve selection and refuse unported maintenance before ``state.db`` access."""
    operations = StateStoreMaintenanceOperations.resolve(config, home=home)
    operations.require(capability)
    return operations


def _current_home() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _declares_state_store(config: Mapping[str, Any] | None, home: Path) -> bool:
    if isinstance(config, Mapping):
        return "state_store" in config
    try:
        import yaml
    except ImportError:
        return False
    try:
        raw = yaml.safe_load((home.expanduser() / "config.yaml").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        return False
    return isinstance(raw, Mapping) and "state_store" in raw
