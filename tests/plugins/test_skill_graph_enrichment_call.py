"""Isolated LLM enrichment request, retry and privacy contracts."""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests


PLUGIN_PATH = Path(__file__).parents[2] / "plugins" / "skill-graph" / "__init__.py"
SECRET = "SECRET-enrichment-prompt-or-credential"


class Response:
    def __init__(self, content, *, status: object = 200, body=SECRET, error=None):
        self.content = content
        self.status_code = status
        self.text = body
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise requests.HTTPError(self.error)

    def json(self):
        if isinstance(self.content, Exception):
            raise self.content
        return {"choices": [{"message": {"content": self.content}}]}


@pytest.fixture(params=["bare", "package"])
def request_env(monkeypatch, tmp_path, request):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.setenv("TEST_ENRICH_KEY", SECRET)
    monkeypatch.setenv("TEST_ENRICH_URL", f"https://{SECRET}.invalid/v1/")
    config = {"model": {"provider": "main", "default": "main-model"},
              "skills": {"config": {"skill-graph": {"enrichment": {}}}}}
    registry = {
        "main": SimpleNamespace(api_key_env_vars=["TEST_ENRICH_KEY"],
                                base_url_env_var="TEST_ENRICH_URL", inference_base_url=""),
        "fallback": SimpleNamespace(api_key_env_vars=["TEST_ENRICH_KEY"],
                                    base_url_env_var="TEST_ENRICH_URL", inference_base_url=""),
    }
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: config)
    monkeypatch.setattr("hermes_cli.auth.PROVIDER_REGISTRY", registry)
    monkeypatch.setattr("hermes_cli.auth.has_usable_secret", bool)
    name = "test_skill_graph_enrichment_plugin"
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_PATH,
        submodule_search_locations=[str(PLUGIN_PATH.parent)] if request.param == "package" else None,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    if request.param == "package":
        monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    sleeps = []
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    return module, config, registry, sleeps


def _responses(monkeypatch, sequence):
    calls = []
    remaining = iter(sequence)

    def post(url, *, headers, json, timeout):
        calls.append((url, headers, json, timeout))
        item = next(remaining)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(requests, "post", post)
    return calls


def _assert_private(caplog):
    assert SECRET not in caplog.text
    assert "body=" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_success_fenced_json_and_main_provider(request_env, monkeypatch, caplog):
    module, config, _, sleeps = request_env
    result = {"tags": ["python"], "scenes": ["coding"], "suggestions": []}
    calls = _responses(monkeypatch, [Response(f"```json\n{json.dumps(result)}\n```")])
    with caplog.at_level(logging.DEBUG, logger=module.__name__):
        assert module._call_llm_for_enrichment(SECRET) == result
    assert len(calls) == 1
    assert calls[0][0] == f"https://{SECRET}.invalid/v1/chat/completions"
    assert calls[0][1]["Authorization"] == f"Bearer {SECRET}"
    assert calls[0][2]["messages"] == [{"role": "user", "content": SECRET}]
    assert calls[0][2]["model"] == config["model"]["default"]
    assert calls[0][3] == 30
    assert sleeps == []
    _assert_private(caplog)


def test_preferred_provider_and_model_override(request_env, monkeypatch):
    module, config, _, _ = request_env
    config["skills"]["config"]["skill-graph"]["enrichment"] = {
        "provider": "fallback", "model": "fast-model",
    }
    calls = _responses(monkeypatch, [Response("{\"tags\": []}")])
    assert module._call_llm_for_enrichment(SECRET) == {"tags": []}
    assert calls[0][2]["model"] == "fast-model"


def test_fallback_provider_and_default_model(request_env, monkeypatch):
    module, config, registry, _ = request_env
    config["model"] = {"provider": "missing"}
    registry["copilot"] = registry.pop("main")
    registry["lmstudio"] = registry.pop("fallback")
    registry["usable"] = SimpleNamespace(api_key_env_vars=["TEST_ENRICH_KEY"],
                                          base_url_env_var="TEST_ENRICH_URL", inference_base_url="")
    calls = _responses(monkeypatch, [Response("{}")])
    assert module._call_llm_for_enrichment(SECRET) == {}
    assert calls[0][2]["model"] == "deepseek-v4-flash"


def test_preferred_provider_unusable_does_not_fallback(request_env, monkeypatch, caplog):
    module, config, registry, sleeps = request_env
    config["skills"]["config"]["skill-graph"]["enrichment"]["provider"] = SECRET
    registry[SECRET] = SimpleNamespace(api_key_env_vars=["UNSET_ENRICH_KEY"],
                                       base_url_env_var=None, inference_base_url="")
    calls = _responses(monkeypatch, [])
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        assert module._call_llm_for_enrichment(SECRET) is None
    assert calls == [] and sleeps == []
    assert "location=enrichment.provider" in caplog.text
    _assert_private(caplog)


def test_no_provider_returns_none_without_network(request_env, monkeypatch):
    module, _, _, _ = request_env
    monkeypatch.delenv("TEST_ENRICH_KEY")
    calls = _responses(monkeypatch, [])
    assert module._call_llm_for_enrichment(SECRET) is None
    assert calls == []


@pytest.mark.parametrize("first", [
    requests.ConnectionError(SECRET),
    Response("", body=SECRET),
    Response("{}", status=503, error=SECRET),
    Response("not-json " + SECRET),
])
def test_first_failure_then_success_redacts_and_backs_off(
    request_env, monkeypatch, caplog, first,
):
    module, _, _, sleeps = request_env
    calls = _responses(monkeypatch, [first, Response('{"tags": ["ok"]}')])
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        assert module._call_llm_for_enrichment(SECRET) == {"tags": ["ok"]}
    assert len(calls) == 2
    assert sleeps == [2]
    assert "attempt=1/3" in caplog.text
    if isinstance(first, Response) and first.status_code == 503:
        assert "status=503" in caplog.text
    _assert_private(caplog)


@pytest.mark.parametrize("failure", [
    requests.ConnectionError(SECRET),
    Response("", body=SECRET, status=SECRET),
    Response("{invalid " + SECRET),
])
def test_three_failures_return_none_with_bounded_retry(request_env, monkeypatch, caplog, failure):
    module, _, _, sleeps = request_env
    calls = _responses(monkeypatch, [failure, failure, failure])
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        assert module._call_llm_for_enrichment(SECRET) is None
    assert len(calls) == 3
    assert sleeps == [2, 4]
    assert "attempt=3/3" in caplog.text
    _assert_private(caplog)


@pytest.mark.parametrize("content", ['["not-a-dict"]', '```json\n[1]\n```'])
def test_non_dict_json_returns_none_without_retry(request_env, monkeypatch, content):
    module, _, _, sleeps = request_env
    calls = _responses(monkeypatch, [Response(content)])
    assert module._call_llm_for_enrichment(SECRET) is None
    assert len(calls) == 1 and sleeps == []


def test_setup_and_model_fallback_redact_config_errors(request_env, monkeypatch, caplog):
    module, config, _, _ = request_env
    config["model"] = None
    calls = _responses(monkeypatch, [Response("{}")])
    config["skills"]["config"]["skill-graph"]["enrichment"]["provider"] = "main"
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        assert module._call_llm_for_enrichment(SECRET) == {}
    assert calls[0][2]["model"] == "deepseek-v4-flash"
    assert "location=enrichment.model" in caplog.text
    caplog.clear()

    def fail():
        raise ValueError(SECRET)

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", fail)
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        assert module._call_llm_for_enrichment(SECRET) is None
    assert len(calls) == 1
    assert "location=enrichment.setup" in caplog.text
    _assert_private(caplog)


def test_intent_split_continuation_and_model_override(request_env, monkeypatch, caplog):
    module, config, _, sleeps = request_env
    config["skills"]["config"]["skill-graph"]["intent_split_model"] = "split-model"
    calls = _responses(monkeypatch, [Response(""), Response(
        '```json\n{"intents": ["  Review the change.  ", "Run its tests."], '
        '"scene": "coding", "topic_continuation": "YES"}\n```'
    )])
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        result = module._split_intents(SECRET, ["Earlier task"])
    assert result == (["Review the change.", "Run its tests."], "coding", True)
    assert len(calls) == 2 and sleeps == [1.5]
    assert calls[0][2]["model"] == "split-model"
    assert calls[0][2]["temperature"] == 0.1
    assert calls[0][2]["max_tokens"] == 1024
    assert "Earlier task" in calls[0][2]["messages"][0]["content"]
    assert calls[0][0] == f"https://{SECRET}.invalid/v1/chat/completions"
    _assert_private(caplog)


def test_intent_split_no_provider_and_exhausted_transport(request_env, monkeypatch, caplog):
    module, _, _, sleeps = request_env
    monkeypatch.delenv("TEST_ENRICH_KEY")
    calls = _responses(monkeypatch, [])
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        assert module._split_intents(SECRET) == ([], None, None)
    assert calls == [] and sleeps == []
    _assert_private(caplog)

    monkeypatch.setenv("TEST_ENRICH_KEY", SECRET)
    caplog.clear()
    calls = _responses(monkeypatch, [requests.ConnectionError(SECRET)] * 3)
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        assert module._split_intents(SECRET) == ([], None, None)
    assert len(calls) == 3 and sleeps == [1.5, 3.0]
    _assert_private(caplog)


def test_facade_seams_remain_late_bound(request_env, monkeypatch):
    module, _, _, _ = request_env
    monkeypatch.setattr(module, "_request_enrichment_with_retries", lambda *a: {"injected": True})
    assert module._call_llm_for_enrichment(SECRET) == {"injected": True}
    monkeypatch.setattr(module, "_resolve_llm_provider", lambda: None)
    assert module._split_intents(SECRET) == ([], None, None)
    monkeypatch.setattr(module, "_split_intents", lambda *a: (["patched"], "coding", None))
    assert module._split_intents(SECRET) == (["patched"], "coding", None)


def test_prompt_scene_vocabulary_and_response_contract(request_env):
    module, _, _, _ = request_env
    assert module._needs_enrichment({"tags": ["t"], "scenes": []})
    prompt = module._build_enrichment_prompt("sample", "x" * 4100)
    assert "sample" in prompt and "coding:" in prompt and "x" * 4000 in prompt
    assert "x" * 4001 not in prompt
    assert module._parse_enrichment_response({"choices": [{"message": {"content": ""}}]}) == (True, None)
    assert module._parse_enrichment_response({"choices": [{"message": {"content": "[]"}}]}) == (False, None)
