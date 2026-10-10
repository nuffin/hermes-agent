"""Profile config and intent-split fallbacks must remain visible and private."""
from __future__ import annotations

import builtins
import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

PLUGIN_PATH = Path(__file__).parents[2] / "plugins" / "skill-graph" / "__init__.py"
SECRET = "private-key-or-query-token"


@pytest.fixture
def graph(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("SKILL_GRAPH_BOUNDARY_KEY", SECRET)
    monkeypatch.setenv("SKILL_GRAPH_BOUNDARY_URL", f"https://{SECRET}.invalid/v1")
    config = {"model": {"provider": "test", "default": "main-model"},
              "skills": {"config": {"skill-graph": {}}}}
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: config)
    monkeypatch.setattr("hermes_cli.auth.PROVIDER_REGISTRY", {
        "test": SimpleNamespace(api_key_env_vars=["SKILL_GRAPH_BOUNDARY_KEY"],
                                base_url_env_var="SKILL_GRAPH_BOUNDARY_URL",
                                inference_base_url=""),
    })
    monkeypatch.setattr("hermes_cli.auth.has_usable_secret", bool)
    spec = importlib.util.spec_from_file_location("test_skill_graph_config_intent_plugin", PLUGIN_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    return module, config, home


def _private(caplog):
    assert SECRET not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_read_only_config_keeps_prior_dirs_when_later_entry_fails(graph, monkeypatch, caplog):
    module, config, home = graph
    good = home / "source"
    good.mkdir()
    config["skills"]["config"]["skill-graph"]["source_dirs"] = [
        str(good) + ":read-only", SECRET + ":read-only",
    ]
    original = module._resolve_config_path

    def resolve(value):
        if value == SECRET:
            raise ValueError(SECRET)
        return original(value)

    monkeypatch.setattr(module, "_resolve_config_path", resolve)
    with caplog.at_level(logging.ERROR, logger=module.__name__):
        assert module._get_read_only_source_dirs() == {good}
    assert "could not resolve read-only source directories" in caplog.text
    assert "resolve:" in caplog.text
    _private(caplog)


@pytest.mark.parametrize("stage", ["import", "load"])
def test_provider_config_failure_is_logged_and_returns_none(graph, monkeypatch, caplog, stage):
    module, _, _ = graph
    if stage == "import":
        original_import = builtins.__import__

        def fail_import(name, *args, **kwargs):
            if name == "hermes_cli.auth":
                raise ImportError(SECRET)
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fail_import)
    else:
        def fail_config():
            raise ValueError(SECRET)

        monkeypatch.setattr("hermes_cli.config.load_config_readonly", fail_config)
    with caplog.at_level(logging.ERROR, logger=module.__name__):
        assert module._resolve_llm_provider() is None
    assert f"could not {stage if stage == 'import' else 'read'} provider configuration" in caplog.text
    _private(caplog)


def test_provider_resolution_keeps_preferred_main_and_registry_fallback(graph, monkeypatch):
    module, config, _ = graph
    registry = __import__("hermes_cli.auth", fromlist=["PROVIDER_REGISTRY"]).PROVIDER_REGISTRY
    registry["other"] = registry["test"]
    config["skills"]["config"]["skill-graph"]["enrichment"] = {"provider": "other"}
    assert module._resolve_llm_provider()[0] == "other"
    config["skills"]["config"]["skill-graph"]["enrichment"] = {}
    assert module._resolve_llm_provider()[0] == "test"
    config["model"]["provider"] = "missing"
    assert module._resolve_llm_provider()[0] == "test"


def test_split_config_failure_keeps_default_model_and_raw_intent_fallback(graph, monkeypatch, caplog):
    module, _, _ = graph
    calls = []
    load_count = 0

    def load():
        nonlocal load_count
        load_count += 1
        if load_count > 1:
            raise ValueError(SECRET)
        return {"model": {"provider": "test"}}

    def post(url, *, headers, json, timeout):
        calls.append((url, headers, json, timeout))
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"choices": [{"message": {"content": "{}"}}]})

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", load)
    monkeypatch.setattr(requests, "post", post)
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        assert module._split_intents("Find a private skill") == ([], None, None)
    assert calls[0][2]["model"] == "deepseek-v4-flash"
    assert "intent split model fallback" in caplog.text
    assert "returned no intents" in caplog.text
    _private(caplog)

    monkeypatch.setattr(module, "_rank_skill_candidates", lambda intents: [
        {"name": "example", "description": "example", "score": 1.0},
    ] if intents == ["Find a private skill"] else [])
    block, intents = module._build_skill_candidates_context("Find a private skill", is_first_turn=True)
    assert block is not None
    assert intents == ["Find a private skill"]


@pytest.mark.parametrize("failure", [
    requests.ConnectionError(SECRET),
    ValueError("invalid-response"),
])
def test_split_transport_or_parse_failure_never_logs_secret(graph, monkeypatch, caplog, failure):
    module, _, _ = graph
    attempts = []

    def post(url, *, headers, json, timeout):
        attempts.append(url)
        if isinstance(failure, requests.RequestException):
            raise failure
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"choices": [{"message": {"content": SECRET}}]})

    monkeypatch.setattr(requests, "post", post)
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        assert module._split_intents("Find a private skill") == ([], None, None)
    assert len(attempts) == 3
    assert "attempt=3/3" in caplog.text
    _private(caplog)


def test_split_empty_response_is_visible_without_leaking_input(graph, monkeypatch, caplog):
    module, _, _ = graph
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"choices": [{"message": {"content": ""}}]},
    ))
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        assert module._split_intents(SECRET) == ([], None, None)
    assert "intent split empty response [attempt=3/3]" in caplog.text
    _private(caplog)
