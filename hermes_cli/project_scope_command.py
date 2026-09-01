"""Trusted, session-bound slash surface for project scoped approvals."""
from __future__ import annotations

import secrets
import threading
import time

from agent.redact import redact_sensitive_text
from tools.project_scope_approval import (
    ProjectScopeTemplate, activate_project_scope_template, get_active_project_scope,
    grant_delegated_project_scope, load_project_scope_templates,
    project_scope_policy_digest, revoke_project_scope,
)

_PENDING_TTL_SECONDS = 120.0
_lock = threading.RLock()
_pending: dict[str, tuple[str, ProjectScopeTemplate, str, float]] = {}
# session -> token, board-db path, board, card, assignee, immutable activation, expiry
_pending_kanban: dict[str, tuple[str, str, str, str, str | None, object, float]] = {}


def _summary(template: ProjectScopeTemplate) -> str:
    """Render the exact immutable policy snapshot the user is reviewing."""
    summary = {
        "id": template.template_id,
        "repository_roots": [str(path) for path in template.repository_roots],
        "temporary_roots": [str(path) for path in template.temporary_roots],
        "allowed_operations": sorted(template.allowed_operations),
        "git_remotes": [{"name": name, "url_prefixes": list(prefixes)} for name, prefixes in template.git_remotes],
        "git_ref_rules": list(template.git_ref_rules),
        "docker_registry_prefixes": list(template.docker_registry_prefixes),
    }
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
    if action == "grant-delegate" and len(parts) == 3:
        # Exact runtime identifiers are issued by the trusted delegation
        # runtime. This narrow interactive control cannot choose a template;
        # it can only copy this session's already-confirmed immutable snapshot.
        grant = grant_delegated_project_scope(session_key, parts[1], parts[2])
        if grant is None:
            return "Project scope delegate grant was rejected; no child scope was granted."
        return ("Project scope delegate grant created for session "
                f"`{redact_sensitive_text(parts[1], force=True)}` and subagent "
                f"`{redact_sensitive_text(parts[2], force=True)}`.")
    if action == "grant-kanban" and len(parts) == 3:
        # The interactive parent names a concrete board/card; no task text is
        # authority.  Snapshot the active immutable activation for confirmation.
        activation = get_active_project_scope(session_key)
        if activation is None:
            return "Kanban scope grant was rejected: activate a project scope in this parent session first."
        board, card = parts[1], parts[2]
        try:
            from hermes_cli import kanban_db as kb
            conn = kb.connect(board=board)
            try:
                task = kb.get_task(conn, card)
                board_db = str(kb.kanban_db_path(board=board))
            finally:
                conn.close()
        except Exception:
            task = None
            board_db = ""
        if task is None:
            return "Kanban scope grant was rejected: board/card identity was not found."
        token = secrets.token_urlsafe(18)
        with _lock:
            _pending_kanban[session_key] = (token, board_db, board, card, task.assignee, activation,
                                             time.monotonic() + _PENDING_TTL_SECONDS)
        identity = f"board `{redact_sensitive_text(board, force=True)}`, card `{redact_sensitive_text(card, force=True)}`, assignee `{redact_sensitive_text(str(task.assignee or 'unassigned'), force=True)}`"
        return (f"{_summary(activation.template)}\nAuthorize this exact Kanban root: {identity}.\n"
                f"Descendants inherit only dispatcher-verified lineage; workers cannot grant or revoke it.\n"
                f"To confirm, run: /project-scope confirm-kanban {token}")
    if action == "confirm-kanban" and len(parts) == 2:
        with _lock:
            pending_kanban = _pending_kanban.pop(session_key, None)
        if pending_kanban is None or pending_kanban[0] != parts[1] or pending_kanban[6] < time.monotonic():
            return "Kanban scope confirmation is unavailable, expired, or mismatched; no root grant was created."
        token, board_db, board, card, assignee, activation, _expiry = pending_kanban
        # Replacement/revoke between review and confirmation invalidates it.
        live = get_active_project_scope(session_key)
        if live is None or live.activation_id != activation.activation_id:
            return "Kanban scope confirmation is stale; no root grant was created."
        try:
            from hermes_cli.kanban_scope_lineage import grant_root
            grant_root(board_db, board, card, assignee, activation)
        except Exception:
            return "Kanban scope root grant failed closed; no scope was granted."
        return f"Kanban root scope grant created for board `{redact_sensitive_text(board, force=True)}` card `{redact_sensitive_text(card, force=True)}`."
    if action == "revoke" and len(parts) == 1:
        return "Project scope revoked for this session." if revoke_project_scope(session_key) else "No active project scope for this session."
    if action == "activate" and len(parts) == 2 and parts[1] in templates:
        reviewed_template = templates[parts[1]]
        rendered = _summary(reviewed_template)
        token = secrets.token_urlsafe(18)
        with _lock:
            # Store the reviewed immutable policy, not a mutable config key.
            _pending[session_key] = (
                token, reviewed_template, project_scope_policy_digest(reviewed_template),
                time.monotonic() + _PENDING_TTL_SECONDS,
            )
        return f"{rendered}\nTo explicitly activate this exact scope for this session, run: /project-scope confirm {token}"
    if action == "confirm" and len(parts) == 2:
        with _lock:
            pending = _pending.pop(session_key, None)
        if (pending is None or pending[0] != parts[1] or pending[3] < time.monotonic()
                or pending[2] != project_scope_policy_digest(pending[1])):
            return "Project scope confirmation is unavailable, expired, or mismatched; no scope was activated."
        activation = activate_project_scope_template(session_key, pending[1], delegated=False)
        return (f"Project scope `{pending[1].template_id}` activated for this session."
                if activation is not None else "Project scope activation failed closed; no scope was activated.")
    return "Usage: /project-scope [list|activate <template-id>|confirm <token>|grant-kanban <board> <card>|confirm-kanban <token>|grant-delegate <child-session> <subagent-id>|revoke]"
