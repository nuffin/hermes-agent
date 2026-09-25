"""Session-topic catalog after the non-topic SQLite-import manifest head.

This topic-owned revision is the sole PostgreSQL production DDL authority for
``session_topics`` and ``messages.topic_id``.  The immutable v25 core and its
non-topic v26 child deliberately do not mention either object.
"""
from __future__ import annotations

from alembic import op

from state_store_alembic.migration_helpers import qualified_name

revision = "state_store_v27_session_topics"
down_revision = "state_store_v26_sqlite_import"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().config.attributes["tenant_schema"]
    q = lambda relation: qualified_name(schema, relation)
    op.execute(
        f"CREATE TABLE {q('session_topics')} ("
        "id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
        f"session_id text NOT NULL REFERENCES {q('sessions')}(id) ON DELETE CASCADE, "
        "title text NOT NULL, summary text, "
        "state text NOT NULL DEFAULT 'active' "
        "CHECK (state IN ('active', 'warm')), "
        "message_count bigint NOT NULL DEFAULT 0 CHECK (message_count >= 0), "
        "created_at double precision NOT NULL, last_active_at double precision NOT NULL)"
    )
    op.execute(f"ALTER TABLE {q('messages')} ADD COLUMN topic_id bigint")
    # A scalar topic FK would allow a message in one session to point at a
    # topic from another. The supporting non-partial unique index is the
    # PostgreSQL FK target; the SET NULL column list keeps messages in place.
    op.execute(
        f"CREATE UNIQUE INDEX session_topics_session_id_id_unique ON {q('session_topics')} "
        "(session_id, id)"
    )
    op.execute(
        f"ALTER TABLE {q('messages')} ADD CONSTRAINT messages_topic_id_fkey "
        f"FOREIGN KEY (session_id, topic_id) REFERENCES {q('session_topics')} (session_id, id) "
        "ON DELETE SET NULL (topic_id)"
    )
    op.execute(
        f"CREATE INDEX session_topics_session_last_active ON {q('session_topics')} "
        "(session_id, last_active_at DESC)"
    )
    op.execute(
        f"CREATE UNIQUE INDEX session_topics_one_active_per_session ON {q('session_topics')} "
        "(session_id) WHERE state = 'active'"
    )
    op.execute(
        f"CREATE INDEX messages_topic_id ON {q('messages')} (session_id, topic_id, id)"
    )


def downgrade() -> None:
    raise RuntimeError("State-store session topics cannot be downgraded")
