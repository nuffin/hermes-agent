"""Selected PostgreSQL must not create gateway SQLite/JSON routing artifacts."""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore
from state_store_runtime_readiness import PostgreSQLRuntimeActivationError, trap_state_db_opens


_PG_CONFIG = (
    "state_store:\n"
    "  backend: postgresql\n"
    "  postgresql:\n"
    "    dsn_env: HERMES_STATE_STORE_TEST_DSN\n"
)


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="safe-route-chat",
        chat_name="Safe route",
        chat_type="dm",
        user_id="safe-route-user",
    )


def _selected_pg_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / ".hermes" / "profiles" / "selected-pg"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(_PG_CONFIG, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_STATE_STORE_TEST_DSN", "postgresql://fixture/only")
    return home


def _assert_no_gateway_artifacts(home: Path, sessions_dir: Path) -> None:
    assert not (home / "state.db").exists()
    assert not (sessions_dir / "sessions.json").exists()
    assert list(sessions_dir.glob("*.jsonl")) == [] if sessions_dir.exists() else True


def test_selected_postgresql_session_store_refuses_before_route_or_durable_artifacts(tmp_path, monkeypatch):
    home = _selected_pg_home(tmp_path, monkeypatch)
    sessions_dir = home / "sessions"

    with trap_state_db_opens(home) as opens:
        with pytest.raises(PostgreSQLRuntimeActivationError) as caught:
            SessionStore(sessions_dir, GatewayConfig())

    assert "gateway-session-routing-transcript" in caught.value.report.missing_capabilities
    assert caught.value.report.profile_home == str(home.resolve())
    assert "postgresql://" not in str(caught.value)
    assert opens == []
    _assert_no_gateway_artifacts(home, sessions_dir)


def test_selected_postgresql_gateway_runner_refuses_before_transport_or_route_artifacts(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner
    from gateway.platforms.base import BasePlatformAdapter

    home = _selected_pg_home(tmp_path, monkeypatch)
    sender_calls = []
    sessions_dir = home / "sessions"

    async def fake_send(*args, **kwargs):
        sender_calls.append((args, kwargs))

    monkeypatch.setattr(BasePlatformAdapter, "send", fake_send)

    with trap_state_db_opens(home) as opens:
        with pytest.raises(PostgreSQLRuntimeActivationError):
            GatewayRunner(GatewayConfig(sessions_dir=sessions_dir))

    assert sender_calls == []
    assert opens == []
    _assert_no_gateway_artifacts(home, sessions_dir)


def test_selected_named_profile_blocks_a_root_sqlite_store_before_route_creation(tmp_path, monkeypatch):
    """A store created at root must re-check the active named profile per route."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    root = tmp_path / ".hermes"
    root.mkdir()
    profile = root / "profiles" / "selected-pg"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(_PG_CONFIG, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_STATE_STORE_TEST_DSN", "postgresql://fixture/only")
    sessions_dir = root / "sessions"
    store = SessionStore(sessions_dir, GatewayConfig())
    root_db_size = (root / "state.db").stat().st_size

    token = set_hermes_home_override(str(profile))
    try:
        with trap_state_db_opens(root, profile) as opens:
            with pytest.raises(PostgreSQLRuntimeActivationError) as caught:
                store.get_or_create_session(_source())
            with pytest.raises(PostgreSQLRuntimeActivationError):
                store.append_to_transcript("must-not-spool", {"role": "user", "content": "blocked"})
            with pytest.raises(PostgreSQLRuntimeActivationError):
                store.rewrite_transcript("must-not-rewrite", [])
            with pytest.raises(PostgreSQLRuntimeActivationError):
                store.rewind_session("must-not-rewind")
            with pytest.raises(PostgreSQLRuntimeActivationError):
                store.recover_interrupted_turns()
    finally:
        reset_hermes_home_override(token)

    assert caught.value.report.profile_home == str(profile.resolve())
    assert opens == []
    assert (root / "state.db").stat().st_size == root_db_size
    assert not (sessions_dir / "sessions.json").exists()
    assert not (profile / "state.db").exists()


def test_default_sqlite_session_store_still_routes_and_persists(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    sessions_dir = home / "sessions"

    store = SessionStore(sessions_dir, GatewayConfig())
    entry = store.get_or_create_session(_source())

    assert entry.session_id
    assert (home / "state.db").exists()
    assert (sessions_dir / "sessions.json").exists()
