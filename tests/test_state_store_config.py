"""Contract tests for backend-neutral state-store configuration."""

import pytest

import state_store
from state_store import StateStoreConfigurationError, open_state_store, resolve_state_store_config


def test_default_configuration_keeps_sqlite_backend_without_postgresql_secret():
    resolved = resolve_state_store_config({})

    assert resolved.backend == "sqlite"
    assert resolved.postgresql is None


def test_postgresql_uses_named_secret_without_returning_secret_in_configuration():
    resolved = resolve_state_store_config(
        {
            "state_store": {
                "backend": "postgresql",
                "postgresql": {
                    "dsn_env": "HERMES_STATE_STORE_POSTGRES_DSN",
                    "connect_timeout_seconds": 12,
                    "pool_max_size": 4,
                },
            },
        },
        secret_lookup=lambda name: "postgresql://fixture/only" if name == "HERMES_STATE_STORE_POSTGRES_DSN" else None,
    )

    assert resolved.backend == "postgresql"
    assert resolved.postgresql.dsn_env == "HERMES_STATE_STORE_POSTGRES_DSN"
    assert resolved.postgresql.connect_timeout_seconds == 12
    assert resolved.postgresql.pool_max_size == 4
    assert "fixture/only" not in repr(resolved)


@pytest.mark.parametrize("state_store", [
    {"backend": "postgresql", "postgresql": {"dsn_env": "INVALID-NAME"}},
    {"backend": "postgresql", "postgresql": {"dsn_env": "HERMES_STATE_STORE_POSTGRES_DSN", "pool_max_size": 0}},
    {"backend": "unknown"},
])
def test_invalid_state_store_configuration_fails_closed(state_store):
    with pytest.raises(StateStoreConfigurationError):
        resolve_state_store_config({"state_store": state_store}, secret_lookup=lambda _name: None)


def test_postgresql_without_its_configured_secret_fails_closed():
    with pytest.raises(StateStoreConfigurationError, match="HERMES_STATE_STORE_POSTGRES_DSN"):
        resolve_state_store_config(
            {"state_store": {"backend": "postgresql", "postgresql": {"dsn_env": "HERMES_STATE_STORE_POSTGRES_DSN"}}},
            secret_lookup=lambda _name: None,
        )


def test_selected_postgresql_driver_failure_has_no_public_exception_chain(monkeypatch):
    """The selected-PG public boundary does not retain a DSN-bearing driver error."""
    import traceback
    from types import SimpleNamespace

    secret_marker = "postgres-driver-secret-marker"

    class ExplodingPostgreSQLStore:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError(f"driver rejected postgresql://user:{secret_marker}@db/private")

    monkeypatch.setattr("state_store_postgresql.PostgreSQLStateStore", ExplodingPostgreSQLStore)
    monkeypatch.setattr(state_store, "_resolve_postgresql_tenant_schema", lambda: SimpleNamespace(name="safe_schema"))
    monkeypatch.setattr(state_store, "_is_default_state_store_profile", lambda: False)
    config = {"state_store": {"backend": "postgresql", "postgresql": {"dsn_env": "TEST_PG_DSN"}}}

    with pytest.raises(StateStoreConfigurationError) as raised:
        open_state_store(config, secret_lookup=lambda _name: f"postgresql://user:{secret_marker}@db/private")

    exc = raised.value
    public_cli_payload = {"error": str(exc), "type": type(exc).__name__}
    public = "\n".join((str(exc), repr(exc), "".join(traceback.format_exception(exc)), repr(public_cli_payload)))
    assert str(exc) == "PostgreSQL state store could not open the selected backend"
    assert exc.__cause__ is None and exc.__context__ is None
    assert secret_marker not in public


def test_config_structure_reports_invalid_state_store_shape():
    from hermes_cli.config import validate_config_structure

    issues = validate_config_structure({"state_store": {"backend": "postgresql", "postgresql": "not-a-map"}})

    assert any(issue.severity == "error" and "state_store.postgresql" in issue.message for issue in issues)
