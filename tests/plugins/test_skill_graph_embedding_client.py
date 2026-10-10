"""Failure and privacy contracts for the bundled skill-graph embedding client."""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from unittest.mock import Mock

import pytest


CLIENT_PATH = Path(__file__).parents[2] / "plugins" / "skill-graph" / "embedding_client.py"
PRIVATE = "https://user:password@example.invalid/secret-user-text"


@pytest.fixture
def client_module():
    spec = importlib.util.spec_from_file_location("test_skill_graph_embedding_client", CLIENT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_redacted(caplog):
    assert len(caplog.records) == 1
    assert caplog.records[0].exc_info is not None
    assert "password" not in caplog.text
    assert "example.invalid" not in caplog.text
    assert "secret-user-text" not in caplog.text


def test_config_reader_failure_uses_defaults_and_logs_safely(client_module, caplog):
    def fail():
        raise Exception(PRIVATE)

    client = client_module.EmbeddingClient(fail)
    with caplog.at_level(logging.WARNING, logger="skill-graph.embedding"):
        assert client.backend() == "auto"
        assert client.endpoint() == "http://localhost:8081"
        assert client.model_name() == "bge-m3"
    _assert_redacted(caplog)
    assert "Embedding configuration unavailable; using defaults" in caplog.text


def test_non_dict_config_uses_defaults(client_module):
    assert client_module.EmbeddingClient(lambda: None).backend() == "auto"


@pytest.mark.parametrize("failure", [RuntimeError(PRIVATE), Exception(PRIVATE)])
def test_gpu_failure_falls_back_to_cpu_without_leaking(client_module, monkeypatch, caplog, failure):
    cpu = Mock(return_value=[[0.25, 0.75]])
    monkeypatch.setattr(client_module, "gpu_health_check", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(client_module, "_detect_protocol", lambda _ep: "tei")
    monkeypatch.setattr(client_module, "_embed_tei", Mock(side_effect=failure))
    monkeypatch.setattr(client_module, "_embed_cpu", cpu)
    client = client_module.EmbeddingClient(lambda: {"embedding_api_url": PRIVATE})
    with caplog.at_level(logging.INFO, logger="skill-graph.embedding"):
        assert client.embed(["secret-user-text"]) == [[0.25, 0.75]]
    cpu.assert_called_once_with(["secret-user-text"])
    _assert_redacted(caplog)
    assert "falling back to CPU" in caplog.text


@pytest.mark.parametrize("gpu_result", [[], [[]], [[1.0]]])
def test_empty_or_partial_gpu_result_is_not_a_success(client_module, monkeypatch, caplog, gpu_result):
    cpu = Mock(return_value=[[0.25], [0.75]])
    monkeypatch.setattr(client_module, "gpu_health_check", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(client_module, "_detect_protocol", lambda _ep: "openai")
    monkeypatch.setattr(client_module, "_embed_openai", Mock(return_value=gpu_result))
    monkeypatch.setattr(client_module, "_embed_cpu", cpu)
    with caplog.at_level(logging.WARNING, logger="skill-graph.embedding"):
        assert client_module.EmbeddingClient(lambda: {}).embed(["a", "b"]) == [[0.25], [0.75]]
    cpu.assert_called_once_with(["a", "b"])
    assert "falling back to CPU" in caplog.text


def test_gpu_success_does_not_log_user_preview(client_module, monkeypatch, caplog):
    monkeypatch.setattr(client_module, "gpu_health_check", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(client_module, "_detect_protocol", lambda _ep: "tei")
    monkeypatch.setattr(client_module, "_embed_tei", lambda *_args: [[1.0]])
    with caplog.at_level(logging.INFO, logger="skill-graph.embedding"):
        assert client_module.EmbeddingClient(lambda: {}).embed(["secret-user-text"]) == [[1.0]]
    assert "Embed [TEI] 1 texts" in caplog.text
    assert "secret-user-text" not in caplog.text


def test_failed_cpu_fallback_propagates_instead_of_returning_fake_embeddings(client_module, monkeypatch):
    monkeypatch.setattr(client_module, "gpu_health_check", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(client_module, "_detect_protocol", lambda _ep: "tei")
    monkeypatch.setattr(client_module, "_embed_tei", Mock(side_effect=RuntimeError(PRIVATE)))
    monkeypatch.setattr(client_module, "_embed_cpu", Mock(side_effect=ImportError("CPU unavailable")))
    with pytest.raises(ImportError, match="CPU unavailable"):
        client_module.EmbeddingClient(lambda: {}).embed(["some text"])


def test_unknown_protocol_does_not_log_endpoint(client_module, monkeypatch, caplog):
    monkeypatch.setattr(client_module, "gpu_health_check", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(client_module, "_detect_protocol", lambda _ep: "unknown")
    monkeypatch.setattr(client_module, "_embed_cpu", lambda _texts: [[1.0]])
    with caplog.at_level(logging.WARNING, logger="skill-graph.embedding"):
        assert client_module.EmbeddingClient(lambda: {"embedding_api_url": PRIVATE}).embed(["a"]) == [[1.0]]
    assert "Unknown embedding protocol" in caplog.text
    assert PRIVATE not in caplog.text
