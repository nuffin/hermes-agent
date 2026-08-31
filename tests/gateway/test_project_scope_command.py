"""Gateway trust-boundary tests for /project-scope."""
from unittest.mock import patch

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._session_key_for_source = lambda _source: "scope-session"
    return runner


def _event(platform: Platform, action: str) -> MessageEvent:
    return MessageEvent(
        text=f"/project-scope {action}",
        source=SessionSource(platform=platform, user_id="user-1", chat_id="chat-1", chat_type="dm"),
    )


@pytest.mark.asyncio
async def test_interactive_gateway_source_may_reach_project_scope_control():
    runner = _runner()
    with patch("tools.approval.is_trusted_interactive_approval_context", return_value=True), patch(
        "hermes_cli.project_scope_command.run_project_scope_command", return_value="ok",
    ) as run:
        result = await runner._handle_project_scope_command(_event(Platform.TELEGRAM, "activate release-scope"))

    assert result == "ok"
    assert run.call_args.kwargs["delegated"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", [Platform.WEBHOOK, Platform.API_SERVER])
@pytest.mark.parametrize("action", ["activate release-scope", "confirm token", "revoke"])
async def test_unattended_gateway_sources_are_explicitly_denied_project_scope_control(platform, action):
    runner = _runner()
    with patch("tools.approval.is_trusted_interactive_approval_context", return_value=False), patch(
        "hermes_cli.project_scope_command.run_project_scope_command", return_value="denied",
    ) as run:
        result = await runner._handle_project_scope_command(_event(platform, action))

    assert result == "denied"
    assert run.call_args.kwargs["delegated"] is True


@pytest.mark.asyncio
async def test_delegated_gateway_execution_is_explicitly_denied_project_scope_control():
    runner = _runner()
    with patch("tools.approval.is_trusted_interactive_approval_context", return_value=False), patch(
        "hermes_cli.project_scope_command.run_project_scope_command", return_value="denied",
    ) as run:
        await runner._handle_project_scope_command(_event(Platform.TELEGRAM, "revoke"))

    assert run.call_args.kwargs["delegated"] is True
