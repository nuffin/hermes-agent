"""Regression coverage for selected-PG legacy-maintenance boundaries."""
from argparse import Namespace

import pytest

from state_store_maintenance import StateStoreMaintenanceCapabilityError
from state_store_runtime_readiness import trap_state_db_opens


def _pg_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "state_store:\n  backend: postgresql\n  postgresql:\n    dsn_env: HERMES_STATE_STORE_TEST_DSN\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_STATE_STORE_TEST_DSN", "postgresql://fixture/only")
    return home


@pytest.mark.parametrize("action", ["repair", "recover", "import", "repair-profiles"])
def test_selected_pg_sessions_pre_dispatch_refuses_before_state_db(tmp_path, monkeypatch, capsys, action):
    home = _pg_home(tmp_path, monkeypatch)
    from hermes_cli.sessions_cmd import cmd_sessions

    with trap_state_db_opens(home) as events:
        result = cmd_sessions(Namespace(sessions_action=action))

    assert result == 2
    assert events == []
    assert not (home / "state.db").exists()
    message = capsys.readouterr().out
    assert "PostgreSQL" in message
    assert "no SQLite fallback" in message


@pytest.mark.parametrize("operation", ["backup", "archive-import"])
def test_selected_pg_archive_operations_refuse_before_state_db(tmp_path, monkeypatch, operation):
    home = _pg_home(tmp_path, monkeypatch)
    from hermes_cli import backup

    call = (lambda: backup.run_backup(Namespace(output=None))) if operation == "backup" else (
        lambda: backup.run_import(Namespace(zipfile=tmp_path / "missing.zip", force=True))
    )
    with trap_state_db_opens(home) as events:
        with pytest.raises(StateStoreMaintenanceCapabilityError, match="PostgreSQL.*no SQLite fallback"):
            call()
    assert events == []
    assert not (home / "state.db").exists()


@pytest.mark.parametrize("operation", ["list", "create", "restore"])
def test_selected_pg_snapshot_operations_refuse_before_state_db(tmp_path, monkeypatch, operation):
    home = _pg_home(tmp_path, monkeypatch)
    from hermes_cli import backup

    call = {
        "list": lambda: backup.list_quick_snapshots(),
        "create": lambda: backup.create_quick_snapshot(),
        "restore": lambda: backup.restore_quick_snapshot("snapshot-id"),
    }[operation]
    with trap_state_db_opens(home) as events:
        with pytest.raises(StateStoreMaintenanceCapabilityError, match="PostgreSQL.*no SQLite fallback"):
            call()
    assert events == []
    assert not (home / "state.db").exists()


def test_selected_pg_direct_legacy_helpers_refuse_before_state_db(tmp_path, monkeypatch):
    home = _pg_home(tmp_path, monkeypatch)
    from hermes_cli.foreign_sessions import import_foreign_session
    from hermes_cli.sessions_cmd_repair_profiles import cmd_repair_profiles

    with trap_state_db_opens(home) as events:
        with pytest.raises(StateStoreMaintenanceCapabilityError):
            import_foreign_session("claude", tmp_path / "missing.jsonl")
        with pytest.raises(StateStoreMaintenanceCapabilityError):
            cmd_repair_profiles(Namespace(apply=False, json=False, legacy_main="report"))
    assert events == []
    assert not (home / "state.db").exists()


def test_invalid_selected_pg_configuration_refuses_without_sqlite_fallback(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("state_store:\n  backend: postgresql\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    from state_store_maintenance import StateStoreMaintenanceConfigurationError
    from hermes_cli.backup import create_quick_snapshot

    with trap_state_db_opens(home) as events:
        with pytest.raises(StateStoreMaintenanceConfigurationError, match="PostgreSQL.*no SQLite fallback"):
            create_quick_snapshot()
    assert events == []
    assert not (home / "state.db").exists()


@pytest.mark.parametrize("operation", ["update", "claw"])
def test_selected_pg_automatic_backup_hooks_refuse_before_state_db(tmp_path, monkeypatch, operation):
    home = _pg_home(tmp_path, monkeypatch)
    from hermes_cli import backup

    call = backup.create_pre_update_backup if operation == "update" else backup.create_pre_migration_backup
    with trap_state_db_opens(home) as events:
        with pytest.raises(StateStoreMaintenanceCapabilityError, match="PostgreSQL.*no SQLite fallback"):
            call()
    assert events == []
    assert not (home / "state.db").exists()


def test_sqlite_snapshot_create_remains_available(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli.backup import create_quick_snapshot

    assert create_quick_snapshot(label="sqlite") is None
