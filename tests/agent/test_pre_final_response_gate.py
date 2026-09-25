"""Regression tests for plugin-controlled terminal response gating."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


def _response(content: str):
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="test/model",
        usage=None,
    )


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        instance = AIAgent(
            session_id="pre-final-response-test",
            api_key="test-key",
            base_url="https://example.invalid/v1",
            provider="openai-compat",
            model="test/model",
            max_iterations=2,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    instance._cached_system_prompt = "stable test prompt"
    instance._session_db = None
    instance.save_trajectories = False
    instance.compression_enabled = False
    instance._cleanup_task_resources = lambda *_a, **_kw: None
    instance._save_trajectory = lambda *_a, **_kw: None
    return instance


def test_unsupported_candidate_is_never_interim_or_durable_before_safe_replacement(agent):
    answers = iter([_response("I am working on the fix."), _response("I am still working on the fix.")])
    agent._interruptible_api_call = lambda _kwargs: next(answers)
    interim = []
    streamed = []
    agent.interim_assistant_callback = lambda text, **kwargs: interim.append((text, kwargs))
    agent.stream_delta_callback = streamed.append

    def directive(**kwargs):
        if kwargs["attempt"] == 0:
            return "continue", "Use a real tool, admit a delegation, downgrade to a plan, or report a blocker."
        return "replace", "Not started: no verified execution evidence exists for this turn."

    with (
        patch("hermes_cli.plugins.has_hook", side_effect=lambda name: name == "pre_final_response"),
        patch("hermes_cli.plugins.get_pre_final_response_directive", side_effect=directive),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("fix the issue")

    assert result["final_response"] == "Not started: no verified execution evidence exists for this turn."
    assert interim == []
    assert streamed == ["Not started: no verified execution evidence exists for this turn."]
    durable_text = [message.get("content") for message in result["messages"] if message.get("role") == "assistant"]
    assert durable_text == ["Not started: no verified execution evidence exists for this turn."]
    assert all(not message.get("_pre_final_response_candidate") for message in result["messages"])
    assert all(not message.get("_pre_final_response_synthetic") for message in result["messages"])


def test_no_plugin_keeps_terminal_delivery_unbuffered(agent):
    agent._interruptible_api_call = lambda _kwargs: _response("ordinary final response")

    with (
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("answer normally")

    assert result["final_response"] == "ordinary final response"
    assert agent._defer_final_response_stream_delivery is False
