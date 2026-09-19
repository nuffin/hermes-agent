"""Focused contract tests for the opt-in remote delegation tool."""

import json
from typing import Any

import toolsets
import tools.remote_delegate_tool as remote


def _configured(monkeypatch, *, base_url="https://remote.example/api", secret="test-secret"):
    monkeypatch.setattr(
        remote,
        "_load_remote_delegation_config",
        lambda: {
            "targets": {
                "build-farm": {
                    "base_url": base_url,
                    "api_key_env": "REMOTE_DELEGATION_TEST_KEY",
                    "profile": "isolated",
                }
            }
        },
    )
    monkeypatch.setenv("REMOTE_DELEGATION_TEST_KEY", secret)


def test_remote_delegation_is_opt_in_and_never_in_default_bundles():
    assert toolsets.TOOLSETS["remote_delegation"]["tools"] == ["remote_delegate_task"]
    assert "remote_delegate_task" not in toolsets._HERMES_CORE_TOOLS
    assert {"base_url", "api_key", "api_key_env", "profile", "provider", "toolsets", "headers"}.isdisjoint(
        remote.REMOTE_DELEGATE_TASK_SCHEMA["parameters"]["properties"]
    )
    default_bundles = ["coding", *(name for name in toolsets.TOOLSETS if name.startswith("hermes-"))]
    for bundle in default_bundles:
        assert "remote_delegate_task" not in toolsets.resolve_toolset(bundle)


def test_check_fn_requires_a_complete_configured_target_and_secret(monkeypatch):
    monkeypatch.setattr(remote, "_load_remote_delegation_config", lambda: {"targets": {}})
    assert remote.check_remote_delegate_requirements() is False

    _configured(monkeypatch)
    assert remote.check_remote_delegate_requirements() is True
    monkeypatch.delenv("REMOTE_DELEGATION_TEST_KEY")
    assert remote.check_remote_delegate_requirements() is False


def test_spawn_uses_fixed_config_and_exposes_only_public_fields(monkeypatch):
    _configured(monkeypatch)
    calls = []

    def request(config, path, method, body=None, idempotency_key=None):
        calls.append((config, path, method, body, idempotency_key))
        return {"run_id": "run_123", "status": "queued", "private": "must-not-leak"}

    monkeypatch.setattr(remote, "_request", request)
    result = json.loads(
        remote.remote_delegate_task(
            target="build-farm",
            tasks=[{
                "goal": "compile package",
                "context": "only this context",
                "output_schema": {"type": "object"},
                "request_id": "known-request",
            }],
        )
    )

    assert result == {"target": "build-farm", "runs": [{"target": "build-farm", "remote_id": "run_123", "status": "queued"}]}
    config, path, method, body, key = calls[0]
    assert config["base_url"] == "https://remote.example/api"
    assert path == "/v1/runs"
    assert method == "POST"
    assert body["input"] == "compile package"
    assert "context" not in body["input"]
    assert json.loads(body["instructions"].split("\n", 1)[1]) == {
        "context": "only this context",
        "output_schema": {"type": "object"},
        "profile": "isolated",
    }
    assert key == "remote-delegation-known-request"
    assert "remote.example" not in json.dumps(result)
    assert "test-secret" not in json.dumps(result)


def test_spawn_returns_prior_public_ids_when_later_remote_call_fails(monkeypatch):
    _configured(monkeypatch)
    replies = iter(({"run_id": "run_first", "status": "queued"}, None))
    monkeypatch.setattr(remote, "_request", lambda *args, **kwargs: next(replies))

    result = json.loads(remote.remote_delegate_task(target="build-farm", tasks=[{"goal": "one"}, {"goal": "two"}]))

    assert result == {
        "target": "build-farm",
        "runs": [{"target": "build-farm", "remote_id": "run_first", "status": "queued"}],
        "error": "Remote delegation request failed.",
    }


def test_invalid_target_and_remote_errors_are_redacted(monkeypatch):
    _configured(monkeypatch)
    assert json.loads(remote.remote_delegate_task(target="https://attacker.invalid", tasks=[{"goal": "x"}])) == {
        "error": "Remote delegation request failed."
    }
    monkeypatch.setattr(remote, "_request", lambda *args, **kwargs: None)
    output = remote.remote_delegate_task(action="status", target="build-farm", remote_id="run_123")
    assert json.loads(output) == {"error": "Remote delegation request failed.", "target": "build-farm"}
    assert "test-secret" not in output
    assert "remote.example" not in output


def test_redirect_handler_fails_closed():
    unknown: Any = None
    assert remote._NoRedirect().redirect_request(unknown, unknown, unknown, unknown, unknown, unknown) is None
