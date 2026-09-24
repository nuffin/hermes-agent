"""``hermes -z`` exit-code contract: the outcome is judged from the turn result, not from
whether any text was printed (#111770 — an incomplete or failed run that left an explanation on
stdout used to exit 0, so scripts treated a half-done job as success)."""

from unittest import mock

import hermes_cli.oneshot as oneshot


def _run(monkeypatch, tmp_path, response, result):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    usage = tmp_path / "usage.json"
    with mock.patch.object(oneshot, "_run_agent", return_value=(response, dict(result))):
        code = oneshot.run_oneshot("q", usage_file=str(usage))
    return code, usage


def test_incomplete_run_with_text_exits_nonzero_and_reports_why(monkeypatch, tmp_path):
    import json

    partial = {"final_response": "Got as far as step 2.", "completed": False, "partial": True,
               "turn_exit_reason": "iteration_limit"}
    code, usage = _run(monkeypatch, tmp_path, "Got as far as step 2.", partial)
    assert code == 2
    report = json.loads(usage.read_text(encoding="utf-8"))
    assert report["completed"] is False and report["partial"] is True
    assert report["turn_exit_reason"] == "iteration_limit"

    interrupted = {"final_response": "Stopping.", "completed": False, "interrupted": True}
    code, usage = _run(monkeypatch, tmp_path, "Stopping.", interrupted)
    assert code == 130
    assert json.loads(usage.read_text(encoding="utf-8"))["interrupted"] is True

    failed = {"final_response": "Provider returned 401.", "completed": False, "failed": True}
    code, _ = _run(monkeypatch, tmp_path, "Provider returned 401.", failed)
    assert code == 2


def test_completed_run_still_exits_zero(monkeypatch, tmp_path):
    code, _ = _run(monkeypatch, tmp_path, "Paris.", {"final_response": "Paris.", "completed": True, "failed": False})
    assert code == 0


def test_oneshot_lifecycle_finalization_uses_standard_hook(monkeypatch):
    calls = []

    def fake_finalize_session(**kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr("hermes_cli.lifecycle.finalize_session", fake_finalize_session)
    oneshot._finalize_oneshot_lifecycle("session-1", "cli")

    assert calls == [{"session_id": "session-1", "platform": "cli", "reason": "oneshot_cleanup"}]


def test_close_agent_finalizes_plugins_before_session_store_close(monkeypatch):
    events = []

    class Agent:
        session_id = "session-2"
        platform = "cli"
        _session_messages = None

        def shutdown_memory_provider(self):
            events.append("memory")

        def close(self):
            events.append("agent")

    class SessionDB:
        def close(self):
            events.append("db")

    monkeypatch.setattr(oneshot, "_linger_for_background_completions", lambda: None)
    monkeypatch.setattr(oneshot, "_finalize_oneshot_lifecycle", lambda session_id, platform: events.append("finalize"))
    oneshot._close_agent(Agent(), SessionDB())

    assert events == ["memory", "agent", "finalize", "db"]
