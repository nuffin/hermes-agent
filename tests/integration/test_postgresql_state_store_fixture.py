"""Real PostgreSQL 18 + pgvector Compose-fixture contract."""

import subprocess


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=True, text=True, capture_output=True)


def test_postgresql_state_store_fixture_is_live_with_named_pgdata_and_vector():
    inspect = _docker(
        "inspect", "hermes-agent-postgresql-state-store-dev",
        "--format", "{{range .Mounts}}{{printf \"%s|%s|%s\\n\" .Name .Destination .Type}}{{end}}",
    )
    mounts = inspect.stdout.strip().splitlines()
    assert mounts == ["hermes-agent-postgresql-state-store-pgdata|/var/lib/postgresql|volume"]

    ready = _docker(
        "exec", "hermes-agent-postgresql-state-store-dev", "pg_isready",
        "-U", "hermes_state_store_test", "-d", "hermes_state_store_test",
    )
    assert "accepting connections" in ready.stdout

    extensions = _docker(
        "exec", "hermes-agent-postgresql-state-store-dev", "psql", "-At", "-v", "ON_ERROR_STOP=1",
        "-U", "hermes_state_store_test", "-d", "hermes_state_store_test",
        "-c", "CREATE EXTENSION IF NOT EXISTS vector; SELECT extversion FROM pg_extension WHERE extname = 'vector'",
    )
    assert extensions.stdout.strip()

    trgm = _docker(
        "exec", "hermes-agent-postgresql-state-store-dev", "psql", "-At", "-v", "ON_ERROR_STOP=1",
        "-U", "hermes_state_store_test", "-d", "hermes_state_store_test",
        "-c", "CREATE EXTENSION IF NOT EXISTS pg_trgm; SELECT extname FROM pg_extension WHERE extname = 'pg_trgm'",
    )
    assert trgm.stdout.strip().splitlines()[-1] == "pg_trgm"
