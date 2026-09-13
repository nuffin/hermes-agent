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
- `test_postgresql_state_store_slice.py` (28 tests)
  - Every StateStore factory is routed to a fixture-owned UUID schema; root and named profile acquisitions obtain separately owned UUID targets through the same helper.
  - Fixture reset, historical seed, catalog drift, index rebuild, and data fault injection use marker-validated target operations.
  - Database-wide optional-extension mutation was removed from this family; it never changes shared named-volume state.
- `test_postgresql_session_runtime_ownership.py` (5 tests)
  - Runtime ownership stores are constructed against the owned schema; expiry, invalid-row rollback, and index-drift injection use the target helper.
- `test_postgresql_delivery_ledger.py` (6 tests)
  - The ledger uses `postgresql_delivery_target`; dedicated migration/catalog and index-drift coverage use its marker-validated target.
- `test_postgresql_cli_session_store.py` (3 PostgreSQL lifecycle tests; 1 legacy SQLite control)
  - CLI and AIAgent lifecycle factories receive the owned schema at construction; direct post-close schema deletion is removed.
- `test_postgresql_owned_family_safety.py` (2 tests)
  - A pre-existing shared sentinel schema/catalog record survives StateStore, runtime, and delivery migration construction unchanged.
  - AST inventory rejects direct dangerous `cursor.execute` DDL/DML in the four migrated files. Explicit exemptions are catalog/data reads and the store-pool `SET/SHOW search_path` reset contract.

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
