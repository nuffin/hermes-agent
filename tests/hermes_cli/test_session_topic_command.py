"""CLI routing and lifecycle tests for /session-topic."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hermes_cli.cli_commands_mixin import CLICommandsMixin
from hermes_cli.commands import resolve_command
from hermes_state import SessionDB


class _TestCLI(CLICommandsMixin):
    def __init__(self, db: SessionDB):
        self._session_db = db
        self.session_id = "session"
        self.agent = SimpleNamespace(
            _ensure_db_session=lambda: None,
            _session_init_model_config={},
            _topic_segmentation_enabled=False,
            _active_topic_id=None,
            context_compressor=SimpleNamespace(),
        )


def _cli(db: SessionDB) -> _TestCLI:
    return _TestCLI(db)


def test_registry_keeps_telegram_topic_and_adds_cli_only_session_topic():
    telegram = resolve_command("topic")
    session_topic = resolve_command("session-topic")

    assert telegram is not None and telegram.gateway_only is True
    assert session_topic is not None and session_topic.cli_only is True
    assert resolve_command("session-topics") is session_topic


def test_cli_new_switch_off_persist_override_without_renaming(tmp_path, capsys):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("session", source="cli", model="test")
        db.set_session_title("session", "User title")
        git = db.ensure_session_topic("session", "git")
        cli = _cli(db)

        cli._handle_session_topic_command("/session-topic new cooking")
        cooking = db.get_active_topic("session")
        assert cooking is not None and cooking["title"] == "cooking"
        assert cli.agent._topic_segmentation_enabled is True
        assert cli.agent.context_compressor._active_topic_id == cooking["id"]

        cli._handle_session_topic_command(f"/session-topic switch {git['id']}")
        active = db.get_active_topic("session")
        assert active is not None and active["id"] == git["id"]

        cli._handle_session_topic_command("/session-topic off")
        assert cli.agent._topic_segmentation_enabled is False
        assert cli.agent._active_topic_id is None
        assert db.get_session_model_config_value(
            "session", "topic_segmentation_enabled"
        ) is False
        assert db.get_session_title("session") == "User title"
        assert "Session topic segmentation disabled" in capsys.readouterr().out
    finally:
        db.close()


def test_cli_invalid_switch_preserves_active_topic(tmp_path, capsys):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("session", source="cli", model="test")
        topic = db.ensure_session_topic("session", "git")
        cli = _cli(db)

        cli._handle_session_topic_command("/session-topic switch 999999")

        active = db.get_active_topic("session")
        assert active is not None and active["id"] == topic["id"]
        assert "does not belong" in capsys.readouterr().out
    finally:
        db.close()


def test_off_after_rejected_dynamic_surface_cannot_flush_its_snapshot(tmp_path, monkeypatch):
    """A real command and real ``_ensure_db_session`` keep a rejected tool surface off disk."""
    from run_agent import AIAgent

    db_path = tmp_path / "state.db"
    session_id = "rejected-topic-command"
    prior_prompt = "Model: prior-model\nProvider: openrouter\nprior prompt bytes"
    prior_pin = {
        "version": 1,
        "tools": [{"type": "function", "function": {"name": "prior_tool", "parameters": {}}}],
    }
    db = SessionDB(db_path)
    try:
        db.create_session(session_id, source="cli", model="test")
        db.update_system_prompt(session_id, prior_prompt)
        db.update_session_tool_names(session_id, prior_pin)
        before = db.get_session(session_id)

        with (
            patch("model_tools.get_tool_definitions", return_value=[]),
            patch("model_tools.check_toolset_requirements", return_value={}),
            patch("agent.process_bootstrap.OpenAI"),
        ):
            agent = AIAgent(
                api_key="test-key", base_url="http://127.0.0.1/offline", model="test-model",
                quiet_mode=True, skip_context_files=True, skip_memory=True,
                session_id=session_id, session_db=db,
            )
        agent.client = MagicMock()
        agent.tools = []
        agent.valid_tool_names = set()
        agent._topic_segmentation_enabled = True
        agent._session_db_created = False

        def inject_mcp(target):
            target.tools = [{"type": "function", "function": {"name": "mcp_late_tool"}}]
            target.valid_tool_names = {"mcp_late_tool"}

        monkeypatch.setattr("agent.turn_context._refresh_mcp_tools_between_turns", inject_mcp)
        monkeypatch.setattr("tools.bot_mode_dm.ensure_message_agent_tool", lambda _agent: False)
        refused = agent.run_conversation("must not persist")
        assert refused["pre_admission_failure"] is True
        assert agent.tools == [] and agent.valid_tool_names == set()

        cli = _cli(db)
        cli.agent = agent
        cli._handle_session_topic_command("/session-topic off")
        after_command = db.get_session(session_id)
        assert after_command["system_prompt"] == before["system_prompt"]
        assert after_command["tool_names"] == before["tool_names"]
    finally:
        db.close()

    reopened = SessionDB(db_path)
    try:
        restored = reopened.get_session(session_id)
        assert restored["system_prompt"] == prior_prompt
        assert json.loads(restored["tool_names"]) == prior_pin
    finally:
        reopened.close()
