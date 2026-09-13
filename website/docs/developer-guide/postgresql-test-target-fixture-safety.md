# PostgreSQL test-target fixture safety inventory

`tests/integration/postgresql_test_target.py` is the only allowed implementation
of target allocation, ownership-marker validation, or schema teardown. Its
fixture allocates a UUID-derived schema, stores an opaque marker capability, and
revalidates that marker immediately before destructive teardown.

## Migrated family

- `test_postgresql_compression_rotation_acceptance.py`
  - `rotation_schema` now obtains the schema from `postgresql_test_target`.
  - StateStore receives that schema at construction.
  - v20 catalog fault injection goes through `OwnedPostgreSQLTestTarget.execute`.
  - PG v19 acceptance and v20 rejection remain covered by the rotation gate.

## Remaining migration inventory (not yet covered by this commit)

The following modules still contain direct destructive PostgreSQL fixture SQL
and must not be described as fixture-safe until moved behind the central target:

- `test_postgresql_state_store_slice.py`
- `test_postgresql_session_runtime_ownership.py`
- `test_postgresql_delivery_ledger.py`
- `test_postgresql_cli_session_store.py`
- `test_postgresql_compression_coordination.py`
- `test_postgresql_phase12_fault_harness.py`
- `test_postgresql_state_store_sqlite_import.py`
- `test_postgresql_state_store_operations.py`

This is an explicit coverage boundary, not a compatibility fallback. Production
routing and compression rotation behavior are unchanged.
