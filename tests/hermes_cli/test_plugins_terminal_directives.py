"""Tests for plugin directives at verification and final-response boundaries."""

from hermes_cli.plugins import (
    PluginManager,
    get_pre_final_response_directive,
    get_pre_verify_continue_message,
)


class TestGetPreVerifyContinueMessage:
    """`pre_verify` directive aggregation — mirrors the pre_tool_call block path."""

    def test_none_when_no_hooks(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda hook_name, **kwargs: [])
        assert get_pre_verify_continue_message() is None

    def test_forwards_scope_signals_to_hooks(self, monkeypatch):
        seen = {}

        def capture(hook_name, **kwargs):
            seen.update(kwargs)
            return []

        monkeypatch.setattr("hermes_cli.plugins.invoke_hook", capture)
        get_pre_verify_continue_message(coding=True, attempt=2, changed_paths=["a.py"])
        assert seen["coding"] is True
        assert seen["attempt"] == 2
        assert seen["changed_paths"] == ["a.py"]


def test_pre_final_response_directive_uses_first_valid_response(monkeypatch):
    from hermes_cli import plugins as plugins_mod

    monkeypatch.setattr(
        plugins_mod,
        "invoke_hook",
        lambda *_args, **_kwargs: [
            {"action": "continue"},
            {"action": "replace", "response": "safe correction"},
            {"action": "continue", "message": "later nudge"},
        ],
    )
    assert get_pre_final_response_directive(
        session_id="s", turn_id="t", candidate_response="I am working"
    ) == ("replace", "safe correction")


def test_pre_final_response_hook_failure_returns_safe_replacement():
    manager = PluginManager()

    def failing_callback(**_kwargs):
        raise RuntimeError("intentional test failure")

    manager._hooks["pre_final_response"] = [failing_callback]
    results = manager.invoke_hook("pre_final_response", session_id="s", turn_id="t", candidate_response="I am working")
    assert results == [{
        "action": "replace",
        "response": "Execution has not been confirmed because the final-response policy hook did not return safely. No execution claim is being made.",
    }]
