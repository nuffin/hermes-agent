"""SQLite extraction contract for the backend-neutral hosted-room protocol.

This is deliberately an adapter-level conformance harness over current public
SQLite APIs. It records behavior a future PostgreSQL implementation must match;
it neither selects a backend nor proves external delivery.
"""

from __future__ import annotations

import sqlite3

import pytest

from gateway import hosted_room_driver as driver
from gateway import hosted_rooms as rooms
from gateway.hosted_room_policy_checkpoint import HostedRoomPolicyCheckpoint


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def create_room(db, *, room_id: str = "room-1"):
    return rooms.create_room(
        db,
        room_id=room_id,
        name="Protocol room",
        members=[{"profile": "ops", "handle": "ops"}],
        authority_gateway_id="gateway-a",
        now=10,
    )


def append_user_event(db, *, event_id: str, text: str, now: float):
    return rooms.append_event(
        db,
        room_id="room-1",
        event_id=event_id,
        kind="message.user",
        actor={"kind": "user", "id": "user-1"},
        payload={"text": text, "thread_id": "thread-1"},
        authority_gateway_id="gateway-a",
        authority_epoch=1,
        now=now,
    )


def test_room_event_contract_is_ordered_idempotent_and_fenced(tmp_path):
    db = tmp_path / "state.db"
    created = create_room(db)
    first = append_user_event(db, event_id="event-1", text="first", now=11)
    repeated = append_user_event(db, event_id="event-1", text="first", now=12)
    second = append_user_event(db, event_id="event-2", text="second", now=13)

    assert created["authority_epoch"] == 1
    assert (first["seq"], repeated["seq"], second["seq"]) == (1, 1, 2)
    assert repeated["idempotent"] is True
    assert [event["event_id"] for event in rooms.read_events(db, room_id="room-1")["events"]] == [
        "event-1",
        "event-2",
    ]

    rooms.claim_authority(
        db,
        room_id="room-1",
        expected_gateway_id="gateway-a",
        expected_epoch=1,
        new_gateway_id="gateway-b",
        event_id="claim-2",
        now=14,
    )
    with pytest.raises(rooms.AuthorityConflictError, match="stale"):
        append_user_event(db, event_id="stale-event", text="must not append", now=15)


def test_driver_lease_contract_reclaims_after_expiry_and_fences_old_owner(tmp_path):
    db = tmp_path / "state.db"
    create_room(db)
    clock = FakeClock()
    old = driver.acquire_lease(
        db,
        room_id="room-1",
        gateway_id="gateway-a",
        authority_epoch=1,
        process_generation="process-a",
        ttl_seconds=5,
        clock=clock,
    )

    clock.advance(5)
    new = driver.acquire_lease(
        db,
        room_id="room-1",
        gateway_id="gateway-a",
        authority_epoch=1,
        process_generation="process-b",
        ttl_seconds=30,
        clock=clock,
    )

    assert new.reclaimed is True
    assert new.lease_generation == old.lease_generation + 1
    with pytest.raises(driver.StaleLeaseError):
        driver.renew_lease(db, old, ttl_seconds=30, clock=clock)
    with pytest.raises(driver.StaleLeaseError):
        driver.release_lease(db, old, clock=clock)


def test_remote_receipt_is_exactly_idempotent_not_delivery_proof(tmp_path):
    db = tmp_path / "state.db"
    record = {
        "room_id": "room-1",
        "home_install_id": "install-home",
        "authority_gateway_id": "gateway-a",
        "authority_epoch": 1,
        "member_id": "member-1",
        "target_install_id": "install-peer",
        "target_profile": "reviewer",
        "task_id": "task-1",
        "execution_generation": 1,
        "run_id": "run-1",
        "session_id": "session-1",
    }

    rooms.upsert_remote_run_receipt(db, record=record, now=20)
    rooms.upsert_remote_run_receipt(db, record=record, now=21)
    stored = rooms.remote_run_receipt(db, record=record)

    assert stored is not None
    assert (stored["run_id"], stored["session_id"]) == ("run-1", "session-1")
    with pytest.raises(rooms.HostedRoomError, match="conflicts"):
        rooms.upsert_remote_run_receipt(db, record={**record, "run_id": "other-run"}, now=22)


def test_peer_reservation_revocation_and_expiry_are_fenced(tmp_path):
    db = tmp_path / "state.db"
    claims = {
        "room_id": "room-peer",
        "home_install_id": "install-home",
        "member_id": "member-1",
        "target_install_id": "install-peer",
        "target_profile": "reviewer",
        "authority_gateway_id": "gateway-a",
        "authority_epoch": 1,
        "issued_at": 100,
    }

    rooms.reserve_peer_room(db, claims=claims, expires_at=200, now=100)
    assert rooms.peer_room_grant_is_current(db, claims=claims, now=101)
    assert rooms.room_grant_is_revoked(db, claims=claims, now=101) is False

    rooms.revoke_room_grant_scope(db, claims=claims, expires_at=300, now=110)
    assert rooms.room_grant_is_revoked(db, claims=claims, now=111) is True
    assert rooms.peer_room_grant_is_current(db, claims=claims, now=111) is False
    assert rooms.room_grant_is_revoked(db, claims={**claims, "issued_at": 111}, now=111) is False
    assert rooms.room_grant_is_revoked(db, claims=claims, now=301) is False


def test_policy_cursor_and_watermark_only_advance_from_committed_log_order(tmp_path):
    db = tmp_path / "state.db"
    create_room(db)
    user = append_user_event(db, event_id="user-1", text="question", now=11)
    member = rooms.append_event(
        db,
        room_id="room-1",
        event_id="member-1",
        kind="message.member",
        actor={"kind": "member", "id": "ops"},
        payload={"text": "answer", "thread_id": "thread-1", "discussion_event_id": "user-1"},
        authority_gateway_id="gateway-a",
        authority_epoch=1,
        now=12,
    )
    terminal = rooms.append_event(
        db,
        room_id="room-1",
        event_id="settled-1",
        kind="turn.settled",
        actor={"kind": "gateway", "id": "gateway-a"},
        payload={
            "task_id": "task-1",
            "thread_id": "thread-1",
            "discussion_event_id": "user-1",
            "message_event_id": "member-1",
            "member_id": "ops",
            "seen_through_seq": user["seq"],
        },
        authority_gateway_id="gateway-a",
        authority_epoch=1,
        now=13,
    )

    checkpoint = HostedRoomPolicyCheckpoint(db)
    snapshot = checkpoint.snapshot(room_id="room-1", latest_seq=terminal["seq"])
    repeated = checkpoint.snapshot(room_id="room-1", latest_seq=terminal["seq"])

    assert (user["seq"], member["seq"], terminal["seq"]) == (1, 2, 3)
    assert snapshot.through_seq == repeated.through_seq == terminal["seq"]
    assert snapshot.watermarks[("thread-1", "ops")] == member["seq"]
    with sqlite3.connect(db) as conn:
        cursor = conn.execute(
            "SELECT through_seq FROM hosted_room_policy_cursors WHERE room_id=?", ("room-1",)
        ).fetchone()[0]
    assert cursor == terminal["seq"]
