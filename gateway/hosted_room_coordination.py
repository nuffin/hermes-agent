"""Backend-neutral boundary for durable hosted-room coordination.

The SQLite adapter deliberately delegates to the existing proven SQLite operations.
It is the first extraction step: callers traverse this object, but it does not
select a runtime backend or introduce a PostgreSQL schema/migration.
"""

from __future__ import annotations

from pathlib import Path
from functools import partial
from typing import Any, Callable, Protocol

from gateway import hosted_room_driver as driver
from gateway import hosted_rooms
from gateway.hosted_room_policy_checkpoint import HostedRoomPolicyCheckpoint
from gateway.hosted_rooms_common import DbPath


class HostedRoomCoordination(Protocol):
    """Durable room/peer/driver/policy operations with their existing fence semantics.

    Each mutating method represents the atomic SQLite operation documented in
    ``docs/hosted-room-coordination-protocol.md``.  Callers provide identities,
    authority/lease/attempt fences, and clocks exactly as the current public
    APIs require; an implementation must fail closed on a stale or conflicting
    replay rather than silently overwriting durable state.
    """

    db_path: Path

    def create_room(self, **kwargs: Any) -> dict[str, Any]: ...
    def append_event(self, **kwargs: Any) -> dict[str, Any]: ...
    def read_events(self, **kwargs: Any) -> dict[str, Any]: ...
    def room_state(self, **kwargs: Any) -> dict[str, Any]: ...
    def list_rooms(self, **kwargs: Any) -> list[dict[str, Any]]: ...
    def claim_authority(self, **kwargs: Any) -> dict[str, Any]: ...
    def request_room_stop(self, **kwargs: Any) -> dict[str, Any]: ...
    def disband_room(self, **kwargs: Any) -> dict[str, Any]: ...
    def list_room_link_records(self, **kwargs: Any) -> list[dict[str, Any]]: ...
    def upsert_room_link_record(self, **kwargs: Any) -> None: ...
    def update_room_link_status(self, **kwargs: Any) -> bool: ...
    def delete_room_link_records(self, **kwargs: Any) -> int: ...
    def reserve_peer_room(self, **kwargs: Any) -> dict[str, Any]: ...
    def revoke_room_grant_scope(self, **kwargs: Any) -> dict[str, Any]: ...
    def peer_room_grant_is_current(self, **kwargs: Any) -> bool: ...
    def room_grant_is_revoked(self, **kwargs: Any) -> bool: ...
    def upsert_remote_run_receipt(self, **kwargs: Any) -> dict[str, Any]: ...
    def remote_run_receipt(self, **kwargs: Any) -> dict[str, Any] | None: ...
    def acquire_lease(self, **kwargs: Any) -> driver.DriverLease: ...
    def renew_lease(self, lease: driver.DriverLease, **kwargs: Any) -> driver.DriverLease: ...
    def release_lease(self, lease: driver.DriverLease, **kwargs: Any) -> dict[str, Any]: ...
    def admit_task(self, identity: driver.TaskIdentity, **kwargs: Any) -> dict[str, Any]: ...
    def start_task(self, identity: driver.TaskIdentity, lease: driver.DriverLease, **kwargs: Any) -> driver.TaskAttempt: ...
    def settle_task(self, attempt: driver.TaskAttempt, **kwargs: Any) -> dict[str, Any]: ...
    def recover_room(self, lease: driver.DriverLease, **kwargs: Any) -> dict[str, list[driver.TaskIdentity]]: ...
    def list_tasks(self, **kwargs: Any) -> list[dict[str, Any]]: ...
    def policy_checkpoint(self) -> HostedRoomPolicyCheckpoint: ...


class SqliteHostedRoomCoordination:
    """Production SQLite implementation of :class:`HostedRoomCoordination`.

    No transaction is split here: every delegation stays inside the existing
    SQLite public operation, retaining BEGIN IMMEDIATE, compare-and-set fences,
    exact replay rules, expiry clock handling, and crash recovery behavior.
    """

    def __init__(self, db_path: DbPath) -> None:
        self.db_path = Path(db_path)

    def _room(self, operation: Callable[..., Any], **kwargs: Any) -> Any:
        return operation(self.db_path, **kwargs)

    def create_room(self, **kwargs: Any) -> dict[str, Any]: return self._room(hosted_rooms.create_room, **kwargs)
    def append_event(self, **kwargs: Any) -> dict[str, Any]: return self._room(hosted_rooms.append_event, **kwargs)
    def read_events(self, **kwargs: Any) -> dict[str, Any]: return self._room(hosted_rooms.read_events, **kwargs)
    def room_state(self, **kwargs: Any) -> dict[str, Any]: return self._room(hosted_rooms.room_state, **kwargs)
    def list_rooms(self, **kwargs: Any) -> list[dict[str, Any]]: return self._room(hosted_rooms.list_rooms, **kwargs)
    def claim_authority(self, **kwargs: Any) -> dict[str, Any]: return self._room(hosted_rooms.claim_authority, **kwargs)
    def request_room_stop(self, **kwargs: Any) -> dict[str, Any]: return self._room(hosted_rooms.request_room_stop, **kwargs)
    def disband_room(self, **kwargs: Any) -> dict[str, Any]: return self._room(hosted_rooms.disband_room, **kwargs)
    def list_room_link_records(self, **kwargs: Any) -> list[dict[str, Any]]: return self._room(hosted_rooms.list_room_link_records, **kwargs)
    def upsert_room_link_record(self, **kwargs: Any) -> None: return self._room(hosted_rooms.upsert_room_link_record, **kwargs)
    def update_room_link_status(self, **kwargs: Any) -> bool: return self._room(hosted_rooms.update_room_link_status, **kwargs)
    def delete_room_link_records(self, **kwargs: Any) -> int: return self._room(hosted_rooms.delete_room_link_records, **kwargs)
    def reserve_peer_room(self, **kwargs: Any) -> dict[str, Any]: return self._room(hosted_rooms.reserve_peer_room, **kwargs)
    def revoke_room_grant_scope(self, **kwargs: Any) -> dict[str, Any]: return self._room(hosted_rooms.revoke_room_grant_scope, **kwargs)
    def peer_room_grant_is_current(self, **kwargs: Any) -> bool: return self._room(hosted_rooms.peer_room_grant_is_current, **kwargs)
    def room_grant_is_revoked(self, **kwargs: Any) -> bool: return self._room(hosted_rooms.room_grant_is_revoked, **kwargs)
    def upsert_remote_run_receipt(self, **kwargs: Any) -> dict[str, Any]: return self._room(hosted_rooms.upsert_remote_run_receipt, **kwargs)
    def remote_run_receipt(self, **kwargs: Any) -> dict[str, Any] | None: return self._room(hosted_rooms.remote_run_receipt, **kwargs)

    def acquire_lease(self, **kwargs: Any) -> driver.DriverLease: return self._room(driver.acquire_lease, **kwargs)
    def renew_lease(self, lease: driver.DriverLease, **kwargs: Any) -> driver.DriverLease: return driver.renew_lease(self.db_path, lease, **kwargs)
    def release_lease(self, lease: driver.DriverLease, **kwargs: Any) -> dict[str, Any]: return driver.release_lease(self.db_path, lease, **kwargs)
    def admit_task(self, identity: driver.TaskIdentity, **kwargs: Any) -> dict[str, Any]: return driver.admit_task(self.db_path, identity, **kwargs)
    def start_task(self, identity: driver.TaskIdentity, lease: driver.DriverLease, **kwargs: Any) -> driver.TaskAttempt: return driver.start_task(self.db_path, identity, lease, **kwargs)
    def settle_task(self, attempt: driver.TaskAttempt, **kwargs: Any) -> dict[str, Any]: return driver.settle_task(self.db_path, attempt, **kwargs)
    def recover_room(self, lease: driver.DriverLease, **kwargs: Any) -> dict[str, list[driver.TaskIdentity]]: return driver.recover_room(self.db_path, lease, **kwargs)
    def list_tasks(self, **kwargs: Any) -> list[dict[str, Any]]: return self._room(driver.list_tasks, **kwargs)
    def policy_checkpoint(self) -> HostedRoomPolicyCheckpoint:
        return HostedRoomPolicyCheckpoint(self.db_path, read_events=self.read_events)

    def __getattr__(self, name: str) -> Any:
        """Bind remaining public driver operations without exposing a raw database path.

        The explicit protocol methods above name the core contract.  The driver
        has additional fenced recovery/cancellation transitions; binding them
        here keeps its complete existing state machine on the same adapter while
        avoiding a second, subtly divergent wrapper table.
        """
        operation = getattr(driver, name, None)
        if callable(operation) and not name.startswith("_"):
            return partial(operation, self.db_path)
        raise AttributeError(name)


def sqlite_hosted_room_coordination(db_path: DbPath) -> SqliteHostedRoomCoordination:
    """Construct the only runtime adapter; PostgreSQL selection is intentionally absent."""
    return SqliteHostedRoomCoordination(db_path)
