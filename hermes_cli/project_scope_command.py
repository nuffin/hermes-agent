"""Trusted, session-bound slash surface for project scoped approvals."""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

from agent.redact import redact_sensitive_text
from tools.project_scope_approval import (
    activate_project_scope, get_active_project_scope, load_project_scope_templates,
    project_scope_summary, revoke_project_scope,
)

_PENDING_TTL_SECONDS = 120.0
_lock = threading.RLock()
_pending: dict[str, tuple[str, str, float]] = {}


def _summary(template_id: str) -> str | None:
    summary = project_scope_summary(template_id)
    if summary is None:
        return None
    # This is deliberately a field allowlist, never raw YAML or command text.
    clean = lambda value: redact_sensitive_text(str(value), force=True)
    lines = [f"Project scope `{clean(summary['id'])}` (session-only):"]
    lines += ["Repository roots:", *[f"- {clean(x)}" for x in summary["repository_roots"]]]
    lines += ["Temporary roots:", *[f"- {clean(x)}" for x in summary["temporary_roots"]]]
    lines += ["Allowed operations:", *[f"- {clean(x)}" for x in summary["allowed_operations"]]]
    lines += ["Git remote prefixes:", *[f"- {clean(entry['name'])}: {clean(prefix)}" for entry in summary["git_remotes"] for prefix in entry["url_prefixes"]]]
    lines += ["Git ref rules:", *[f"- {clean(x)}" for x in summary["git_ref_rules"]]]
    lines += ["Docker registry prefixes:", *[f"- {clean(x)}" for x in summary["docker_registry_prefixes"]]]
    lines.append("Expires: session; hardline and user-deny rules remain enforced.")
    return "\n".join(lines)


def run_project_scope_command(raw_args: str, *, session_key: str, delegated: bool = False) -> str:
    """Run /project-scope list|activate|confirm|revoke; fail closed by default."""
    if delegated or not session_key:
        return "Project scope activation and revocation are unavailable to delegated or unattended callers."
    parts = (raw_args or "").strip().split()
    action = parts[0].lower() if parts else "list"
    templates = load_project_scope_templates()
    if action == "list":
        ids = sorted(templates)
        return "Configured project scopes: " + (", ".join(ids) if ids else "none")
    if action == "revoke" and len(parts) == 1:
        return "Project scope revoked for this session." if revoke_project_scope(session_key) else "No active project scope for this session."
    if action == "activate" and len(parts) == 2 and parts[1] in templates:
        rendered = _summary(parts[1])
        token = secrets.token_urlsafe(18)
        with _lock:
            _pending[session_key] = (token, parts[1], time.monotonic() + _PENDING_TTL_SECONDS)
        return f"{rendered}\nTo explicitly activate this exact scope for this session, run: /project-scope confirm {token}"
    if action == "confirm" and len(parts) == 2:
        with _lock:
            pending = _pending.pop(session_key, None)
        if pending is None or pending[0] != parts[1] or pending[2] < time.monotonic():
            return "Project scope confirmation is unavailable, expired, or mismatched; no scope was activated."
        activation = activate_project_scope(session_key, pending[1], delegated=False)
        return (f"Project scope `{pending[1]}` activated for this session."
                if activation is not None else "Project scope activation failed closed; no scope was activated.")
    return "Usage: /project-scope [list|activate <template-id>|confirm <token>|revoke]"
