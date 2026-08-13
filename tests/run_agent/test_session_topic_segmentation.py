"""Unit tests for session topic segmentation.

Covers the parser, fuzzy matcher, and TopicManager DB methods.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# _parse_topic
# ---------------------------------------------------------------------------


class TestParseTopic:
    """Tests for the standalone _parse_topic function (regex extraction)."""

    def test_extracts_topic_at_end(self):
        from run_agent import _parse_topic

        cleaned, name = _parse_topic("some response\n\nTOPIC: git")
        assert name == "git"
        assert "TOPIC:" not in cleaned

    def test_extracts_topic_with_hyphens(self):
        from run_agent import _parse_topic

        cleaned, name = _parse_topic("text\nTOPIC: docker-compose")
        assert name == "docker-compose"
        assert "TOPIC:" not in cleaned

    def test_extracts_topic_with_spaces_in_name(self):
        from run_agent import _parse_topic

        cleaned, name = _parse_topic("text\nTOPIC: machine learning")
        assert name == "machine learning"

    def test_extracts_topic_multiline_content(self):
        from run_agent import _parse_topic

        cleaned, name = _parse_topic(
            "line one\nline two\n\nTOPIC: python"
        )
        assert name == "python"
        assert "line one" in cleaned
        assert "line two" in cleaned

    def test_no_topic_line_returns_none(self):
        from run_agent import _parse_topic

        cleaned, name = _parse_topic("just a normal response")
        assert name is None
        assert cleaned == "just a normal response"

    def test_strips_only_topic_line(self):
        from run_agent import _parse_topic

        cleaned, name = _parse_topic(
            "The answer is 42.\n\nTOPIC: trivia"
        )
        assert name == "trivia"
        assert "The answer is 42." in cleaned
        # The TOPIC line itself is gone
        assert "\n\nTOPIC:" not in cleaned

    def test_topic_with_colon_in_name(self):
        from run_agent import _parse_topic

        cleaned, name = _parse_topic("text\nTOPIC: C: drive")
        assert name == "C: drive"

    def test_empty_topic_name_not_matched(self):
        from run_agent import _parse_topic

        # "TOPIC:" with only whitespace should still match (agent error)
        # but return empty name — we handle this gracefully
        cleaned, name = _parse_topic("text\nTOPIC:   ")
        assert name == ""

    def test_whitespace_around_name_trimmed(self):
        from run_agent import _parse_topic

        cleaned, name = _parse_topic("text\nTOPIC:   hello world   ")
        assert name == "hello world"


# ---------------------------------------------------------------------------
# TopicManager (SessionDB)
# ---------------------------------------------------------------------------


class TestTopicManager:
    """Tests for TopicManager CRUD methods on SessionDB."""

    @pytest.fixture
    def db(self):
        """Create an isolated in-memory SessionDB with a test session."""
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "state.db"
            sdb = SessionDB(db_path=db_path)
            # Create a session row so foreign keys work
            import time
            sdb._conn.execute(
                "INSERT INTO sessions (id, source, started_at) "
                "VALUES ('test-db', 'cli', ?)",
                (time.time(),),
            )
            sdb._conn.commit()
            yield sdb
            sdb.close()

    def test_create_and_get_topics(self, db):
        sid = "test-db"
        tid = db.create_topic(sid, "git")
        assert tid is not None
        assert tid > 0

        topics = db.get_topics(sid)
        assert len(topics) == 1
        assert topics[0]["title"] == "git"
        assert topics[0]["state"] == "active"
        assert topics[0]["message_count"] == 0

    def test_multiple_topics(self, db):
        sid = "test-db"
        db.create_topic(sid, "python")
        db.create_topic(sid, "docker")

        topics = db.get_topics(sid)
        assert len(topics) == 2
        assert [topic["title"] for topic in topics] == ["python", "docker"]
        assert [topic["state"] for topic in topics] == ["warm", "active"]

    def test_create_topic_atomically_archives_previous_active(self, db):
        sid = "test-db"
        first = db.create_topic(sid, "python")
        second = db.create_topic(sid, "docker")

        active = [topic for topic in db.get_topics(sid) if topic["state"] == "active"]
        assert [topic["id"] for topic in active] == [second]
        assert db.get_active_topic(sid)["id"] == second
        assert first != second

    def test_reconcile_legacy_multiple_active_topics(self, db):
        sid = "test-db"
        first = db.create_topic(sid, "python")
        second = db.create_topic(sid, "docker")
        db._conn.execute(
            "UPDATE session_topics SET state = 'active' WHERE id IN (?, ?)",
            (first, second),
        )
        db._conn.commit()

        kept = db.reconcile_topic_states(sid)

        assert kept == second
        states = {topic["id"]: topic["state"] for topic in db.get_topics(sid)}
        assert states == {first: "warm", second: "active"}

    def test_update_topic_summary_and_chronological_context(self, db):
        sid = "test-db"
        first = db.create_topic(sid, "python")
        second = db.create_topic(sid, "docker")

        assert db.update_topic_summary(first, "Goal: repair topic state") is True
        context = db.get_topic_title_context(sid)

        assert [topic["id"] for topic in context] == [first, second]
        assert context[0]["summary"] == "Goal: repair topic state"
        assert set(context[0]) >= {
            "id", "title", "summary", "state", "message_count",
            "created_at", "last_active_at",
        }

    def test_topic_switch_preserves_archived_history(self, db):
        sid = "test-db"
        first = db.create_topic(sid, "python")
        db.append_message(sid, "user", "old durable message", topic_id=first)
        second = db.create_topic(sid, "docker")

        assert db.get_topic_messages(sid, first) == []
        archived = db.get_topic_messages(sid, first, include_inactive=True)
        assert [message["content"] for message in archived] == ["old durable message"]
        assert db.get_active_topic(sid)["id"] == second

    def test_set_active_topic(self, db):
        sid = "test-db"
        db.create_topic(sid, "git")
        tid2 = db.create_topic(sid, "docker")

        # Archive current, activate tid2
        result = db.set_active_topic(sid, tid2)
        assert result is True

        topics = db.get_topics(sid)
        states = {t["title"]: t["state"] for t in topics}
        assert states["docker"] == "active"
        assert states["git"] == "warm"

    def test_set_active_topic_nonexistent(self, db):
        sid = "test-db"
        db.create_topic(sid, "git")

        # Try to activate a non-existent topic
        result = db.set_active_topic(sid, 999)
        assert result is False

        # Try to activate a non-existent topic — validate-then-archive (#72149 #4):
        # target doesn't exist, so we bail out without archiving the current
        # active topic.  "git" stays active.
        topics = db.get_topics(sid)
        assert topics[0]["state"] == "active"

    def test_update_message_count(self, db):
        sid = "test-db"
        tid = db.create_topic(sid, "git")

        db.update_topic_message_count(tid, 3)
        topics = db.get_topics(sid)
        assert topics[0]["message_count"] == 3

    def test_set_topic_session_title(self, db):
        sid = "test-db"
        db.create_topic(sid, "kubernetes")

        db.set_topic_session_title(sid)
        row = db._conn.execute(
            "SELECT title FROM sessions WHERE id = ?", (sid,)
        ).fetchone()
        assert row["title"] == "kubernetes"
        assert db.get_session_title_source(sid) == db.TITLE_SOURCE_DERIVED

    def test_set_topic_session_title_uses_first_created_and_protects_human_title(self, db):
        sid = "test-db"
        db.create_topic(sid, "first-topic")
        db.create_topic(sid, "newest-topic")

        assert db.set_topic_session_title(sid) == "first-topic (+1 topics)"
        db.set_session_title(sid, "Human title")

        assert db.set_topic_session_title(sid) == "first-topic (+1 topics)"
        assert db.get_session_title(sid) == "Human title"
        assert db.get_session_title_source(sid) == db.TITLE_SOURCE_USER

    def test_topic_scoped_compaction_reuses_summary_and_preserves_history(self, db):
        from agent.context_compressor import COMPRESSED_SUMMARY_METADATA_KEY

        sid = "test-db"
        topic_id = db.create_topic(sid, "python")
        db.append_message(sid, "user", "old message", topic_id=topic_id)
        compacted = [{
            "role": "assistant",
            "content": "Goal: repair topic persistence\nFiles: hermes_state.py",
            COMPRESSED_SUMMARY_METADATA_KEY: True,
        }]

        db.archive_and_compact(sid, compacted, topic_id=topic_id)

        topic = db.get_topic_title_context(sid)[0]
        assert "repair topic persistence" in topic["summary"]
        durable = db.get_topic_messages(sid, topic_id, include_inactive=True)
        assert any(message["content"] == "old message" for message in durable)

    def test_get_topic_messages(self, db):
        sid = "test-db"
        import time
        tid = db.create_topic(sid, "git")

        db._conn.execute(
            "INSERT INTO messages (session_id, role, content, topic_id, timestamp) "
            "VALUES (?, 'user', 'hello', ?, ?)",
            (sid, tid, time.time()),
        )
        db._conn.commit()

        msgs = db.get_topic_messages(sid, tid)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "hello"


# ---------------------------------------------------------------------------
# _process_topic_signals (fuzzy matching)
# ---------------------------------------------------------------------------


class TestProcessTopicSignals:
    """Tests for _process_topic_signals fuzzy matching logic."""

    @pytest.fixture
    def db(self):
        from hermes_state import SessionDB
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "state.db"
            sdb = SessionDB(db_path=db_path)
            import time
            sdb._conn.execute(
                "INSERT INTO sessions (id, source, started_at) "
                "VALUES ('test-session-proc', 'cli', ?)",
                (time.time(),),
            )
            sdb._conn.commit()
            yield sdb
            sdb.close()

    def _make_agent(self, db):
        """Build a minimal agent stub with the method and DB wired up."""
        from run_agent import AIAgent

        agent = MagicMock()
        agent._session_db = db
        agent.session_id = "test-session"
        agent._active_topic_id = None
        agent._topic_drift = None  # bypass hysteresis for non-hysteresis tests
        agent._switch_to_topic = MagicMock()
        agent._create_topic_from_shift = MagicMock()
        agent._invalidate_system_prompt = MagicMock()

        # Bind the real method
        agent._process_topic_signals = (
            AIAgent._process_topic_signals.__get__(agent)
        )
        return agent

    def test_exact_match_switches(self, db):
        """TOPIC: git matches existing 'git' topic."""
        sid = "test-session-proc"
        tid = db.create_topic(sid, "git")

        agent = self._make_agent(db)
        agent.session_id = sid
        agent._active_topic_id = tid + 1  # different from git

        cleaned = agent._process_topic_signals("answer\nTOPIC: git")

        agent._switch_to_topic.assert_called_once_with(tid)
        agent._create_topic_from_shift.assert_not_called()
        assert "TOPIC:" not in cleaned

    def test_fuzzy_match_docker_contains(self, db):
        """TOPIC: docker-networking matches existing 'docker' topic."""
        sid = "test-session-proc"
        tid = db.create_topic(sid, "docker")

        agent = self._make_agent(db)
        agent.session_id = sid
        agent._active_topic_id = None

        agent._process_topic_signals("answer\nTOPIC: docker-networking")

        # Should match 'docker' via substring
        agent._switch_to_topic.assert_called_once_with(tid)
        agent._create_topic_from_shift.assert_not_called()

    def test_fuzzy_match_reverse_contains(self, db):
        """TOPIC: python matches existing 'python-decorators' topic."""
        sid = "test-session-proc"
        tid = db.create_topic(sid, "python-decorators")

        agent = self._make_agent(db)
        agent.session_id = sid
        agent._active_topic_id = None

        agent._process_topic_signals("answer\nTOPIC: python")

        agent._switch_to_topic.assert_called_once_with(tid)
        agent._create_topic_from_shift.assert_not_called()

    def test_no_match_creates_new(self, db):
        """TOPIC: cooking doesn't match any existing topic."""
        sid = "test-session-proc"
        db.create_topic(sid, "git")

        agent = self._make_agent(db)
        agent.session_id = sid

        agent._process_topic_signals("answer\nTOPIC: cooking")

        agent._switch_to_topic.assert_not_called()
        agent._create_topic_from_shift.assert_called_once_with("cooking")

    def test_case_insensitive(self, db):
        """TOPIC: GIT matches existing 'git' topic."""
        sid = "test-session-proc"
        tid = db.create_topic(sid, "git")

        agent = self._make_agent(db)
        agent.session_id = sid

        agent._process_topic_signals("answer\nTOPIC: GIT")

        agent._switch_to_topic.assert_called_once_with(tid)

    def test_same_topic_no_switch(self, db):
        """TOPIC: git when already on git topic — no switch call."""
        sid = "test-session-proc"
        tid = db.create_topic(sid, "git")

        agent = self._make_agent(db)
        agent.session_id = sid
        agent._active_topic_id = tid  # already on git

        agent._process_topic_signals("answer\nTOPIC: git")

        agent._switch_to_topic.assert_not_called()
        agent._create_topic_from_shift.assert_not_called()

    def test_no_topic_line_returns_unchanged(self, db):
        """Content without TOPIC line passes through unchanged."""
        sid = "test-session-proc"
        db.create_topic(sid, "git")

        agent = self._make_agent(db)
        agent.session_id = sid

        cleaned = agent._process_topic_signals("just a response")
        assert cleaned == "just a response"


class TestTopicSummaryLifecycle:
    def test_switch_refreshes_archived_topic_summary(self):
        from run_agent import AIAgent

        db = MagicMock()
        db.set_active_topic.return_value = True
        db.get_topic_messages.return_value = [
            {"role": "user", "content": "Repair hermes_state.py topic state"},
            {"role": "assistant", "content": "Implemented atomic transition"},
        ]
        agent = MagicMock()
        agent._session_db = db
        agent.session_id = "session"
        agent._active_topic_id = 3
        agent._session_messages = []
        agent._refresh_active_topic_summary = (
            AIAgent._refresh_active_topic_summary.__get__(agent)
        )
        agent._switch_to_topic = AIAgent._switch_to_topic.__get__(agent)

        agent._switch_to_topic(4)

        db.update_topic_summary.assert_called_once()
        assert "hermes_state.py" in db.update_topic_summary.call_args.args[1]
        db.set_active_topic.assert_called_once_with("session", 4)

    def test_finalize_refreshes_active_topic_summary(self):
        source = Path("agent/turn_finalizer.py").read_text(encoding="utf-8")
        refresh = source.index("agent._refresh_active_topic_summary()")
        persist = source.index("agent._persist_session(messages, conversation_history)")
        assert refresh < persist


# ---------------------------------------------------------------------------
# _TopicDriftTracker (hysteresis)
# ---------------------------------------------------------------------------


class TestTopicDriftTracker:
    """Tests for the _TopicDriftTracker hysteresis mechanism."""

    def test_single_signal_no_switch(self):
        """A single stray TOPIC signal does not trigger a switch."""
        from run_agent import _TopicDriftTracker

        tracker = _TopicDriftTracker(threshold=2)
        assert tracker.feed("cooking") is None
        assert tracker.feed("cooking") == "cooking"

    def test_interleaved_signals_no_switch(self):
        """Interleaved TOPIC signals reset the counter each time."""
        from run_agent import _TopicDriftTracker

        tracker = _TopicDriftTracker(threshold=2)
        assert tracker.feed("cooking") is None   # 1
        assert tracker.feed("git") is None        # reset → 1
        assert tracker.feed("cooking") is None    # reset → 1
        assert tracker.feed("cooking") == "cooking"  # 2 → confirm

    def test_reset_after_confirm(self):
        """After confirming a shift, the tracker resets."""
        from run_agent import _TopicDriftTracker

        tracker = _TopicDriftTracker(threshold=2)
        tracker.feed("cooking")  # 1
        result = tracker.feed("cooking")  # 2 → confirm
        assert result == "cooking"
        # Now the tracker should be fresh
        assert tracker.feed("docker") is None  # 1

    def test_reset_explicit(self):
        from run_agent import _TopicDriftTracker

        tracker = _TopicDriftTracker(threshold=2)
        tracker.feed("cooking")
        tracker.reset()
        assert tracker.feed("cooking") is None  # starts from 1 again


class TestProcessTopicSignalsWithHysteresis:
    """Tests for _process_topic_signals with hysteresis enabled."""

    @pytest.fixture
    def db(self):
        from hermes_state import SessionDB
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "state.db"
            sdb = SessionDB(db_path=db_path)
            import time
            sdb._conn.execute(
                "INSERT INTO sessions (id, source, started_at) "
                "VALUES ('test-hyst', 'cli', ?)",
                (time.time(),),
            )
            sdb._conn.commit()
            yield sdb
            sdb.close()

    def _make_agent(self, db):
        """Same as TestProcessTopicSignals but with _topic_drift wired."""
        from run_agent import AIAgent, _TopicDriftTracker

        agent = MagicMock()
        agent._session_db = db
        agent.session_id = "test-hyst"
        agent._active_topic_id = None
        agent._topic_drift = _TopicDriftTracker(threshold=2)
        agent._switch_to_topic = MagicMock()
        agent._create_topic_from_shift = MagicMock()
        agent._invalidate_system_prompt = MagicMock()

        agent._process_topic_signals = (
            AIAgent._process_topic_signals.__get__(agent)
        )
        return agent

    def test_two_consecutive_new_topic_creates(self, db):
        """2 consecutive TOPIC: cooking → creates new topic."""
        sid = "test-hyst"
        db.create_topic(sid, "git")

        agent = self._make_agent(db)
        agent.session_id = sid

        # First: no switch
        cleaned = agent._process_topic_signals("answer\nTOPIC: cooking")
        agent._switch_to_topic.assert_not_called()
        agent._create_topic_from_shift.assert_not_called()
        assert "TOPIC:" not in cleaned

        # Second: creates
        cleaned = agent._process_topic_signals("answer\nTOPIC: cooking")
        agent._create_topic_from_shift.assert_called_once_with("cooking")
        assert "TOPIC:" not in cleaned

    def test_two_consecutive_existing_topic_switches(self, db):
        """2 consecutive TOPIC: docker → switches to existing 'docker'."""
        sid = "test-hyst"
        tid = db.create_topic(sid, "docker")
        db.create_topic(sid, "git")

        agent = self._make_agent(db)
        agent.session_id = sid
        agent._active_topic_id = tid + 1  # on a different topic

        # First: no switch
        agent._process_topic_signals("answer\nTOPIC: docker")
        agent._switch_to_topic.assert_not_called()

        # Second: switches
        agent._process_topic_signals("answer\nTOPIC: docker")
        agent._switch_to_topic.assert_called_once_with(tid)

    def test_interleaved_signals_no_create(self, db):
        """TOPIC: cooking → TOPIC: git → TOPIC: cooking: still no create."""
        sid = "test-hyst"
        db.create_topic(sid, "git")

        agent = self._make_agent(db)
        agent.session_id = sid

        agent._process_topic_signals("answer\nTOPIC: cooking")
        agent._process_topic_signals("answer\nTOPIC: git")    # interleaving
        agent._process_topic_signals("answer\nTOPIC: cooking")  # reset to 1

        agent._switch_to_topic.assert_not_called()
        agent._create_topic_from_shift.assert_not_called()

    def test_fuzzy_match_still_works_with_hysteresis(self, db):
        """Fuzzy matching still applies after hysteresis confirms."""
        sid = "test-hyst"
        tid = db.create_topic(sid, "docker")

        agent = self._make_agent(db)
        agent.session_id = sid

        # docker-networking should match 'docker' after hysteresis
        agent._process_topic_signals("answer\nTOPIC: docker-networking")
        agent._switch_to_topic.assert_not_called()

        agent._process_topic_signals("answer\nTOPIC: docker-networking")
        agent._switch_to_topic.assert_called_once_with(tid)
