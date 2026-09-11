"""Contract tests for the complete backend-neutral contextual recall capability."""

import json
from pathlib import Path

import pytest

import state_store
from hermes_constants import get_hermes_home
from hermes_state import SessionDB
from state_store import (
    ContextualSessionSearchUnavailable,
    SqliteContextualSessionSearchStore,
    contextual_session_search_store,
    resolve_contextual_session_search_store,
)
from tools.session_search_tool import session_search


def _seed(db):
    db.create_session("older", source="cli")
    first = db.append_message("older", role="user", content="contextual needle opening")
    anchor = db.append_message("older", role="assistant", content="contextual needle anchor")
    db.append_message("older", role="tool", content="contextual needle tool output")
    db._conn.execute("UPDATE sessions SET title = ? WHERE id = ?", ("Contextual contract", "older"))
    db._conn.commit()
    return first, anchor


def test_sqlite_contextual_contract_preserves_sessiondb_primitives(tmp_path):
    raw = SessionDB(tmp_path / "state.db")
    first, anchor = _seed(raw)
    store = contextual_session_search_store(raw)

    assert isinstance(store, SqliteContextualSessionSearchStore)
    assert store.get_session("older") == raw.get_session("older")
    assert store.get_messages("older") == raw.get_messages("older")
    assert store.get_messages_around("older", anchor, window=1) == raw.get_messages_around("older", anchor, window=1)
    assert store.get_anchored_view("older", anchor, window=1, bookend=1) == raw.get_anchored_view(
        "older", anchor, window=1, bookend=1)
    assert store.get_message_storage_state(first) == {"session_id": "older", "active": 1, "compacted": 0}
    assert store.resolve_session_by_title("Contextual contract") == "older"
    assert store.list_recent_sessions_bounded(limit=5, exclude_sources=[], timeout_seconds=3.0)
    assert store.search_index_status() is None


def test_tool_uses_contextual_store_for_all_public_shapes_and_index_status(tmp_path, monkeypatch):
    raw = SessionDB(tmp_path / "state.db")
    _, anchor = _seed(raw)
    store = contextual_session_search_store(raw)
    monkeypatch.setattr(raw, "fts_rebuild_status", lambda: {"percent": 50, "indexed": 1, "total": 2})

    discover = json.loads(session_search(query="contextual needle", db=store, detail="full"))
    assert discover["success"] is True
    assert discover["index_rebuild"]["percent"] == 50
    result_anchor = discover["results"][0]["match_message_id"]
    assert result_anchor in {anchor - 1, anchor}
    assert json.loads(session_search(session_id="older", around_message_id=result_anchor, db=store))["mode"] == "scroll"
    assert json.loads(session_search(session_id="older", db=store))["mode"] == "read"
    assert json.loads(session_search(db=store))["mode"] == "browse"


def test_postgresql_contextual_search_is_explicitly_fail_closed():
    with pytest.raises(ContextualSessionSearchUnavailable, match="does not implement contextual session search"):
        contextual_session_search_store(backend="postgresql")


def _profile_layout(tmp_path: Path, monkeypatch):
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root, root / "profiles"


def test_profile_resolver_uses_each_profiles_sqlite_config_and_preserves_isolation(tmp_path, monkeypatch):
    root, profiles = _profile_layout(tmp_path, monkeypatch)
    alice, bob = profiles / "alice", profiles / "bob"
    alice.mkdir(parents=True)
    bob.mkdir(parents=True)
    (root / "config.yaml").write_text("state_store:\n  backend: sqlite\n", encoding="utf-8")
    (alice / "config.yaml").write_text("state_store:\n  backend: sqlite\n", encoding="utf-8")
    (bob / "config.yaml").write_text("state_store:\n  backend: sqlite\n", encoding="utf-8")
    for home, session_id, content in ((root, "root", "root-only"), (alice, "alice", "alice-only"), (bob, "bob", "bob-only")):
        db = SessionDB(home / "state.db")
        db.create_session(session_id, source="test")
        db.append_message(session_id, role="user", content=content)
        db.close()

    root_db = SessionDB(root / "state.db")
    try:
        assert json.loads(session_search(session_id="root", db=root_db))["success"] is True
    finally:
        root_db.close()
    alice_result = json.loads(session_search(session_id="alice", profile="alice"))
    assert alice_result["success"] is True
    assert "alice-only" in json.dumps(alice_result)
    assert json.loads(session_search(session_id="bob", profile="alice"))["success"] is False
    assert "bob-only" not in json.dumps(alice_result)


def test_profile_resolver_routes_postgresql_through_canonical_tenant_then_fails_closed(tmp_path, monkeypatch):
    root, profiles = _profile_layout(tmp_path, monkeypatch)
    pg_home = profiles / "pgtenant"
    pg_home.mkdir(parents=True)
    (pg_home / "config.yaml").write_text(
        "state_store:\n  backend: postgresql\n  postgresql:\n    dsn_env: PROFILE_PG_DSN\n",
        encoding="utf-8",
    )
    (pg_home / ".env").write_text("PROFILE_PG_DSN=postgresql://fixture-only\n", encoding="utf-8")
    seen = {}

    class _PartialPostgreSQLStore:
        def close(self):
            seen["closed"] = True

    def fake_open(config, *, secret_lookup):
        seen["home"] = get_hermes_home().resolve()
        seen["schema"] = state_store.postgresql_tenant_schema()
        seen["secret"] = secret_lookup("PROFILE_PG_DSN")
        return _PartialPostgreSQLStore()

    monkeypatch.setattr(state_store, "open_state_store", fake_open)
    with pytest.raises(ContextualSessionSearchUnavailable):
        resolve_contextual_session_search_store(profile="pgtenant", read_only=True)

    caller_db = SessionDB(tmp_path / "caller.db")
    try:
        tool_result = json.loads(session_search(db=caller_db, profile="pgtenant"))
    finally:
        caller_db.close()

    assert tool_result["success"] is False
    assert "does not implement contextual session search" in tool_result["error"]
    assert seen == {
        "home": pg_home.resolve(),
        "schema": postgresql_tenant_schema_for(pg_home),
        "secret": "postgresql://fixture-only",
        "closed": True,
    }
    assert not (pg_home / "state.db").exists()
    assert get_hermes_home().resolve() == root.resolve()


def postgresql_tenant_schema_for(home: Path) -> str:
    """Expected named tenant schema without feeding a profile value into production SQL."""
    import hashlib

    return "hermes_state_store_tenant_" + hashlib.sha256(
        f"{home.resolve()}\0pgtenant".encode("utf-8")
    ).hexdigest()[:32]


def test_profile_resolver_rejects_invalid_backend_and_injection_shaped_profile_without_sqlite_fallback(tmp_path, monkeypatch):
    _root, profiles = _profile_layout(tmp_path, monkeypatch)
    invalid = profiles / "invalid"
    invalid.mkdir(parents=True)
    (invalid / "config.yaml").write_text("state_store:\n  backend: not-a-backend\n", encoding="utf-8")
    (invalid / "state.db").write_text("must never be opened as sqlite", encoding="utf-8")

    with pytest.raises(state_store.StateStoreConfigurationError):
        resolve_contextual_session_search_store(profile="invalid", read_only=True)
    with pytest.raises(ValueError):
        resolve_contextual_session_search_store(profile="../../invalid", read_only=True)
