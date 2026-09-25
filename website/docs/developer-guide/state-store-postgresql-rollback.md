# PostgreSQL rollback boundary

PostgreSQL → SQLite data migration is **not implemented** in this branch. After a
PostgreSQL tenant has accepted writes, changing `state_store.backend` to `sqlite`
without an independently verified, topic-aware export/import loses PostgreSQL-era
history. That configuration-only switch is not a supported rollback procedure.

## What is supported

- **Before PostgreSQL accepts writes:** selecting SQLite again is safe because no
  PostgreSQL-era history exists to preserve.
- **PostgreSQL recovery and retention:** use
  `PostgreSQLSandboxOperations.backup(..., quiesced=True)` and
  `restore_and_verify()` for native logical backup/restore evidence. Keep the
  PostgreSQL tenant and its verified backup as the authoritative recovery copy.
- **SQLite and PostgreSQL isolation:** the switch-roundtrip integration test proves
  that ordinary backend selection never copies writes in either direction.

## Fail-closed rule after cutover

Do not claim or perform a reverse rollback until a dedicated tool can atomically
export and import all of the following into a separate SQLite candidate and read
that candidate back before configuration changes:

1. session rows and message rows;
2. `session_topics` IDs, titles, summaries, states, counts, and timestamps;
3. every `messages.topic_id` association; and
4. the conditional invariant that topicless sessions are valid, while every
   nonempty topic set has exactly one active topic.

The current bounded StateStore export/import surface does not carry the complete
topic catalog, so it cannot satisfy that contract. There is no supported
configuration-only fallback, no dual-write, and no silent topic reconstruction.

## Verification boundary

Native doctor validates the PostgreSQL catalog and active-topic invariant. A
future reverse portability feature must add a PostgreSQL→SQLite round-trip test
with a read-back assertion for topic metadata and message associations before this
document can advertise data-preserving rollback.
