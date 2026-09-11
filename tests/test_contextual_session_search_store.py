"""Contract tests for the complete backend-neutral contextual recall capability."""

import json

import pytest

from hermes_state import SessionDB
from state_store import (
    ContextualSessionSearchUnavailable,
    SqliteContextualSessionSearchStore,
    contextual_session_search_store,
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
