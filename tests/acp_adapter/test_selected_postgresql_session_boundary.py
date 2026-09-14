"""ACP refuses selected PostgreSQL before any legacy session side effect."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from acp_adapter.session import SessionManager
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from state_store_runtime_readiness import PostgreSQLRuntimeActivationError, trap_state_db_opens


_PG_CONFIG = (
    "state_store:\n"
    "  backend: postgresql\n"
    "  postgresql:\n"
    "    dsn_env: HERMES_STATE_STORE_TEST_DSN\n"
)


def _selected_pg_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / ".hermes" / "profiles" / "selected-pg"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(_PG_CONFIG, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_STATE_STORE_TEST_DSN", "postgresql://fixture/only")
    return home


def _assert_refusal(exc: PostgreSQLRuntimeActivationError, home: Path) -> None:
    assert exc.report.profile_home == str(home.resolve())
    assert "acp-session-transcript-lifecycle" in exc.report.missing_capabilities
    assert "postgresql://" not in str(exc)


def test_selected_postgresql_manager_refuses_before_agent_or_sqlite_artifacts(tmp_path, monkeypatch):
    home = _selected_pg_home(tmp_path, monkeypatch)
    factory_calls = []
    manager = SessionManager(agent_factory=lambda: factory_calls.append("agent") or SimpleNamespace(model="test"))

    with trap_state_db_opens(home) as opens:
        for operation in (
            lambda: manager.create_session(cwd="/workspace"),
            lambda: manager.get_session("missing"),
            lambda: manager.list_sessions(),
            lambda: manager.update_cwd("missing", "/workspace"),
            lambda: manager.fork_session("missing", "/workspace"),
        ):
            with pytest.raises(PostgreSQLRuntimeActivationError) as caught:
                operation()
            _assert_refusal(caught.value, home)

    assert factory_calls == []
    assert opens == []
    assert not (home / "state.db").exists()
    assert not list(home.rglob("*.json"))


def test_selected_named_profile_refuses_before_root_sqlite_session_reuse(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    manager = SessionManager(agent_factory=lambda: SimpleNamespace(model="test"))
    root_state = manager.create_session()
    root_size = (root / "state.db").stat().st_size

    profile = root / "profiles" / "selected-pg"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(_PG_CONFIG, encoding="utf-8")
    monkeypatch.setenv("HERMES_STATE_STORE_TEST_DSN", "postgresql://fixture/only")
    token = set_hermes_home_override(str(profile))
    try:
        with trap_state_db_opens(root, profile) as opens:
            with pytest.raises(PostgreSQLRuntimeActivationError) as caught:
                manager.get_session(root_state.session_id)
    finally:
        reset_hermes_home_override(token)

    _assert_refusal(caught.value, profile)
    assert opens == []
    assert (root / "state.db").stat().st_size == root_size
    assert not (profile / "state.db").exists()


def test_default_sqlite_acp_session_persists_and_restores(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = SessionManager(agent_factory=lambda: SimpleNamespace(model="test"))
    state = manager.create_session(cwd="/workspace")
    state.history.append({"role": "user", "content": "persist me"})
    manager.save_session(state.session_id)

    restored = SessionManager(agent_factory=lambda: SimpleNamespace(model="test")).get_session(state.session_id)

    assert restored is not None
    assert restored.cwd == "/workspace"
    assert restored.history[0]["content"] == "persist me"
    assert (home / "state.db").exists()


@pytest.mark.asyncio
async def test_selected_postgresql_server_session_operations_refuse_before_protocol_side_effects(tmp_path, monkeypatch):
    pytest.importorskip("acp")
    from acp_adapter.server import HermesACPAgent

    home = _selected_pg_home(tmp_path, monkeypatch)
    factory_calls = []
    manager = SessionManager(agent_factory=lambda: factory_calls.append("agent") or SimpleNamespace(model="test"))
    server = HermesACPAgent(session_manager=manager)
    sent = []
    server._conn = SimpleNamespace(session_update=lambda *args: sent.append(args))

    with trap_state_db_opens(home) as opens:
        for operation in (
            lambda: server.new_session(cwd="/workspace"),
            lambda: server.load_session(cwd="/workspace", session_id="missing"),
            lambda: server.resume_session(cwd="/workspace", session_id="missing"),
            lambda: server.list_sessions(),
            lambda: server.cancel(session_id="missing"),
            lambda: server.set_session_model("test", "missing"),
            lambda: server.set_session_mode("default", "missing"),
            lambda: server.set_config_option("option", "missing", "value"),
        ):
            with pytest.raises(PostgreSQLRuntimeActivationError) as caught:
                await operation()
            _assert_refusal(caught.value, home)

    assert factory_calls == []
    assert sent == []
    assert opens == []
    assert not (home / "state.db").exists()
