"""Classic CLI hot-reloads compression config into its existing agent on the next turn."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import cli
from agent.context_compressor import ContextCompressor
from hermes_cli.config import get_config_path


class _StopAfterCompressionSync(Exception):
    pass


def _agent_with_compressor():
    compressor = ContextCompressor(
        model="gpt-5.6-sol",
        threshold_percent=0.85,
        config_context_length=272_000,
        quiet_mode=True,
    )
    client = object()
    return SimpleNamespace(
        model="gpt-5.6-sol",
        provider="openai-codex",
        base_url="",
        client=client,
        context_compressor=compressor,
        compression_enabled=True,
        compression_idle_compact_after_seconds=0,
        codex_responses_native_compaction=False,
        codex_responses_compact_threshold=200_000,
    ), compressor, client


def _chat_turn(monkeypatch, shell, config_text: str) -> None:
    """Drive a real CLI turn only through the pre-user-message config sync."""
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config_text, encoding="utf-8")
    monkeypatch.setattr(shell, "_ensure_runtime_credentials", lambda: True)
    monkeypatch.setattr(
        shell,
        "_resolve_turn_agent_config",
        lambda message: {"signature": shell._active_agent_route_signature, "model": None, "runtime": None},
    )
    monkeypatch.setattr(shell, "_init_agent", lambda **kw: True)
    monkeypatch.setattr(shell, "_sync_fallback_chain_with_config", lambda agent: None)

    def stop(message, images):
        raise _StopAfterCompressionSync()

    monkeypatch.setattr(shell, "_chat_route_images", stop)
    with pytest.raises(_StopAfterCompressionSync):
        shell.chat("hello")


def _shell_with_live_agent():
    shell = cli.HermesCLI(compact=True, max_turns=1)
    agent, compressor, client = _agent_with_compressor()
    shell.agent = agent
    return shell, agent, compressor, client


def _config(compression: str) -> str:
    return (
        "model:\n"
        "  default: gpt-5.6-sol\n"
        "  provider: openai-codex\n"
        "  context_length: 272000\n"
        f"compression:\n{compression}"
    )


def test_cli_turn_adopts_compression_change_without_replacing_agent_or_client(monkeypatch):
    shell, agent, compressor, client = _shell_with_live_agent()
    stale = compressor.threshold_tokens
    assert stale > 100_000

    _chat_turn(monkeypatch, shell, _config("  threshold_tokens: 100000\n"))

    assert shell.agent is agent
    assert agent.client is client
    assert compressor.threshold_tokens == 100_000


def test_cli_turn_skips_unchanged_compression_config(monkeypatch):
    shell, _agent, compressor, _client = _shell_with_live_agent()
    config_text = _config("  threshold_tokens: 100000\n")
    _chat_turn(monkeypatch, shell, config_text)
    compressor.threshold_tokens = 99_999

    _chat_turn(monkeypatch, shell, config_text)

    assert compressor.threshold_tokens == 99_999


def test_cli_turn_keeps_last_good_compression_when_config_is_torn(monkeypatch):
    shell, agent, compressor, client = _shell_with_live_agent()
    _chat_turn(monkeypatch, shell, _config("  threshold_tokens: 100000\n"))

    _chat_turn(monkeypatch, shell, "compression: [\n  threshold_tokens: {{{\n")

    assert shell.agent is agent
    assert agent.client is client
    assert compressor.threshold_tokens == 100_000


def test_cli_turn_removing_threshold_tokens_restores_construction_default(monkeypatch):
    shell, _agent, compressor, _client = _shell_with_live_agent()
    _chat_turn(monkeypatch, shell, _config("  threshold_tokens: 100000\n"))
    assert compressor.threshold_tokens_cap == 100_000

    _chat_turn(monkeypatch, shell, _config(""))

    assert compressor.threshold_tokens_cap == 256_000
    assert compressor.threshold_tokens > 100_000
