"""Terminal-response plugin directives for verification and final-response gates."""

from __future__ import annotations

from typing import List, Optional


def get_pre_verify_continue_message(
    *, session_id: str = "", platform: str = "", model: str = "", coding: bool = False,
    attempt: int = 0, final_response: str = "", changed_paths: Optional[List[str]] = None,
) -> Optional[str]:
    """Check ``pre_verify`` hooks for ``{"action": "continue", "message"}`` (or Claude-Code Stop
    ``{"decision": "block", "reason"}``) to keep the turn going; first non-empty message wins, any
    other return lets the turn finish. ``coding``/``attempt`` let hooks scope and self-throttle."""
    from hermes_cli.plugins import invoke_hook

    hook_results = invoke_hook(
        "pre_verify", session_id=session_id, platform=platform, model=model, coding=coding,
        attempt=attempt, final_response=final_response, changed_paths=list(changed_paths or []),
    )
    for result in hook_results:
        if not isinstance(result, dict):
            continue
        action = str(result.get("action") or result.get("decision") or "").strip().lower()
        message = result.get("message") or result.get("reason")
        if action in ("continue", "block") and isinstance(message, str) and message.strip():
            return message.strip()
    return None


def get_pre_final_response_directive(
    *, session_id: str = "", turn_id: str = "", task_id: str = "", platform: str = "",
    model: str = "", provider: str = "", api_call_count: int = 0, finish_reason: str = "",
    attempt: int = 0, candidate_response: str = "",
) -> tuple[Optional[str], Optional[str]]:
    """Return the first valid terminal-response directive from ``pre_final_response`` hooks.

    A ``continue`` directive supplies a non-empty synthetic user nudge. A ``replace``
    directive supplies a non-empty safe terminal response. Plugin errors, malformed
    values, and unsupported actions are ignored so this opt-in seam is fail-open.
    """
    from hermes_cli.plugins import invoke_hook

    hook_results = invoke_hook(
        "pre_final_response", session_id=session_id, turn_id=turn_id, task_id=task_id,
        platform=platform, model=model, provider=provider, api_call_count=api_call_count,
        finish_reason=finish_reason, attempt=attempt, candidate_response=candidate_response,
    )
    for result in hook_results:
        if not isinstance(result, dict):
            continue
        action = str(result.get("action") or "").strip().lower()
        if action == "continue":
            message = result.get("message")
            if isinstance(message, str) and message.strip():
                return "continue", message.strip()
        elif action == "replace":
            response = result.get("response")
            if isinstance(response, str) and response.strip():
                return "replace", response.strip()
    return None, None
