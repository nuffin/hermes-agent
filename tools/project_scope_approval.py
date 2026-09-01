"""In-memory, explicitly activated project-scoped terminal approvals.

This module deliberately has no command execution or prompt/UI surface.  A
configured template is inert until a trusted approval surface explicitly calls
:func:`activate_project_scope` for the current session.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from typing import Any, Mapping

_OPERATION_IDS = frozenset({
    "git.worktree.create", "git.worktree.prune", "git.worktree.remove",
    "git.commit.signed", "git.push.configured_remote", "docker.build",
    "docker.push.configured_registry",
})
_ID_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_REF_RULE_RE = re.compile(r"refs/heads/[A-Za-z0-9._/-]*\*?\Z")
_FULL_REF_RE = re.compile(r"refs/heads/[A-Za-z0-9._/-]+\Z")
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_FORBIDDEN_TEXT = re.compile(r"[\n\r;&|<>`$*?\[{}~]")
_CARRIERS = frozenset({"sh", "bash", "zsh", "fish", "dash", "python", "python3", "node", "ruby", "perl", "pwsh", "powershell", "env", "xargs", "find", "sudo", "command", "eval", "source", "."})


@dataclass(frozen=True)
class TerminalApprovalContext:
    raw_command: str
    backend_type: str
    session_key: str
    supplied_workdir: str | None
    effective_cwd: str
    background: bool
    has_host_access: bool
    execution_identity: str | None = None


@dataclass(frozen=True)
class ProjectScopeTemplate:
    template_id: str
    repository_roots: tuple[Path, ...]
    temporary_roots: tuple[Path, ...]
    git_remotes: tuple[tuple[str, tuple[str, ...]], ...]
    git_ref_rules: tuple[str, ...]
    docker_registry_prefixes: tuple[str, ...]
    allowed_operations: frozenset[str]


@dataclass(frozen=True)
class ActivatedProjectScope:
    template_id: str
    session_key: str
    activation_id: str
    issued_at: float
    template: ProjectScopeTemplate
    policy_digest: str
    confirmation_id: str | None = None


@dataclass(frozen=True)
class TrustedConfirmationReceipt:
    """Trusted interactive confirmation, bound to one exact session/template."""
    receipt_id: str
    session_key: str
    template: ProjectScopeTemplate
    policy_digest: str
    issued_at: float


@dataclass(frozen=True)
class TrustedKanbanGrantReceipt:
    """One trusted `/project-scope` confirmation bound to one card grant."""
    receipt_id: str
    session_key: str
    activation_id: str
    board: str
    task_id: str
    assignee: str | None
    issued_at: float


@dataclass(frozen=True)
class DelegatedProjectScopeGrant:
    """Immutable, runtime-identity-bound parent authorization edge."""
    grant_id: str
    root_activation_id: str
    root_session_key: str
    recipient_session_key: str
    recipient_subagent_id: str
    parent_grant_id: str | None
    parent_subagent_id: str | None
    template: ProjectScopeTemplate
    policy_digest: str
    depth: int
    issued_at: float


@dataclass(frozen=True)
class ScopeDecision:
    status: str  # not_applicable | denied | approved
    operation: str | None = None
    reason: str | None = None
    template_id: str | None = None
    activation_id: str | None = None
    policy_digest: str | None = None
    session_key: str | None = None
    timestamp: float | None = None
    matched_root_label: str | None = None


_lock = threading.RLock()
_active: dict[str, ActivatedProjectScope] = {}
_confirmations: dict[str, TrustedConfirmationReceipt] = {}
_kanban_confirmations: dict[str, TrustedKanbanGrantReceipt] = {}
_delegated_grants: dict[str, DelegatedProjectScopeGrant] = {}
_grant_by_recipient: dict[str, str] = {}
_MAX_LINEAGE_DEPTH = 8


def _policy_digest(template: ProjectScopeTemplate) -> str:
    """Hash a canonical, non-secret representation of an activated policy."""
    payload = {
        "id": template.template_id,
        "repository_roots": [str(path) for path in template.repository_roots],
        "temporary_roots": [str(path) for path in template.temporary_roots],
        "git_remotes": [[name, list(prefixes)] for name, prefixes in template.git_remotes],
        "git_ref_rules": list(template.git_ref_rules),
        "docker_registry_prefixes": list(template.docker_registry_prefixes),
        "allowed_operations": sorted(template.allowed_operations),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def project_scope_policy_digest(template: ProjectScopeTemplate) -> str:
    """Return the canonical digest used to bind a reviewed template snapshot."""
    return _policy_digest(template)


def _observe(event: str, activation: ActivatedProjectScope, *, operation: str | None = None) -> None:
    """Emit allowlisted scope metadata only; never raw command/config values."""
    try:
        from tools.approval import _fire_approval_hook
        _fire_approval_hook("post_approval_response", project_scope={
            "event": event,
            "template_id": activation.template_id,
            "activation_id": activation.activation_id,
            "session_id": activation.session_key,
            "policy_digest": activation.policy_digest,
            "timestamp": time.time(),
            "operation": operation,
            "decision": "approved" if event != "project_scope_revoked" else "revoked",
            "decision_reason": "explicit activation" if event == "project_scope_activated" else "explicit/session revocation",
            "matched_root_label": None,
        })
    except Exception:
        pass


def _existing_directory(value: object) -> Path | None:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        return None
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return resolved if resolved.is_dir() else None


def _string_list(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        return None
    return tuple(value)


def _normal_registry_prefix(value: str) -> str | None:
    value = value.strip().lower()
    if not value or not value.endswith("/") or "://" in value or any(c.isspace() for c in value):
        return None
    return value


def _parse_template(value: object) -> ProjectScopeTemplate | None:
    if not isinstance(value, Mapping):
        return None
    template_id = value.get("id")
    if not isinstance(template_id, str) or not _ID_RE.fullmatch(template_id):
        return None
    repo_values = _string_list(value.get("repository_roots"))
    temp_values = _string_list(value.get("temporary_roots"))
    operations = _string_list(value.get("allowed_operations"))
    refs = _string_list(value.get("git_ref_rules"))
    registries = _string_list(value.get("docker_registry_prefixes"))
    if (not repo_values or not temp_values or not operations or not refs or not registries
            or value.get("activation") != "explicit-session-approval" or value.get("expires") != "session"):
        return None
    repos_untrusted = tuple(_existing_directory(item) for item in repo_values)
    temps_untrusted = tuple(_existing_directory(item) for item in temp_values)
    if any(item is None for item in repos_untrusted + temps_untrusted):
        return None
    repos = tuple(item for item in repos_untrusted if item is not None)
    temps = tuple(item for item in temps_untrusted if item is not None)
    if len(set(repos)) != len(repos) or len(set(temps)) != len(temps):
        return None
    if not set(operations) <= _OPERATION_IDS or len(set(operations)) != len(operations):
        return None
    if any(not _REF_RULE_RE.fullmatch(ref) or ref in {"refs/heads/", "refs/heads/*"} for ref in refs):
        return None
    normalized_untrusted = tuple(_normal_registry_prefix(item) for item in registries)
    if any(item is None for item in normalized_untrusted):
        return None
    normalized_registries = tuple(item for item in normalized_untrusted if item is not None)
    if len(set(normalized_registries)) != len(normalized_registries):
        return None
    remote_rules = value.get("git_remotes")
    if not isinstance(remote_rules, list) or not remote_rules:
        return None
    remotes: list[tuple[str, tuple[str, ...]]] = []
    for rule in remote_rules:
        if not isinstance(rule, Mapping) or not isinstance(rule.get("name"), str) or not rule["name"]:
            return None
        prefixes = _string_list(rule.get("url_prefixes"))
        if not prefixes or any("://" not in prefix and not prefix.startswith("git@") for prefix in prefixes):
            return None
        remotes.append((rule["name"], tuple(prefix.rstrip("/") + "/" for prefix in prefixes)))
    if len({name for name, _ in remotes}) != len(remotes):
        return None
    return ProjectScopeTemplate(template_id, repos, temps, tuple(remotes), tuple(refs), tuple(normalized_registries), frozenset(operations))


def load_project_scope_templates(approvals_config: Mapping[str, Any] | None = None) -> dict[str, ProjectScopeTemplate]:
    """Load only individually valid templates; malformed entries fail closed."""
    if approvals_config is None:
        try:
            # Reuse the established approval-config seam so template loading
            # has the same readonly/cache semantics as command guards.
            from tools.approval import _get_approval_config
            approvals_config = _get_approval_config()
        except Exception:
            return {}
    raw = approvals_config.get("project_scope_templates") if isinstance(approvals_config, Mapping) else None
    if raw is None:
        return {}
    if not isinstance(raw, list):
        return {}
    parsed: dict[str, ProjectScopeTemplate] = {}
    duplicates: set[str] = set()
    for item in raw:
        template = _parse_template(item)
        if template is None:
            continue
        if template.template_id in parsed:
            duplicates.add(template.template_id)
        else:
            parsed[template.template_id] = template
    for template_id in duplicates:
        parsed.pop(template_id, None)
    return parsed


def project_scope_summary(template_id: str) -> dict[str, object] | None:
    template = load_project_scope_templates().get(template_id)
    if template is None:
        return None
    return {"id": template.template_id, "repository_roots": [str(p) for p in template.repository_roots], "temporary_roots": [str(p) for p in template.temporary_roots], "allowed_operations": sorted(template.allowed_operations), "git_remotes": [{"name": name, "url_prefixes": list(prefixes)} for name, prefixes in template.git_remotes], "git_ref_rules": list(template.git_ref_rules), "docker_registry_prefixes": list(template.docker_registry_prefixes), "expires": "session"}


def activate_project_scope_template(*args: object, **kwargs: object) -> None:
    """Removed raw activation surface; trusted confirmations mint receipts."""
    return None


def activate_project_scope(*args: object, **kwargs: object) -> None:
    """Removed raw activation surface; callers cannot assert consent."""
    return None


def _issue_trusted_confirmation(session_key: str, template: ProjectScopeTemplate) -> TrustedConfirmationReceipt:
    """Create a receipt only after the interactive command verified its token."""
    receipt = TrustedConfirmationReceipt(
        uuid.uuid4().hex, session_key, template, _policy_digest(template), time.time(),
    )
    with _lock:
        _confirmations[receipt.receipt_id] = receipt
    return receipt


def _activate_confirmed_project_scope(receipt: TrustedConfirmationReceipt) -> ActivatedProjectScope | None:
    """Consume an immutable trusted receipt; no bool/token/object can substitute."""
    if not isinstance(receipt, TrustedConfirmationReceipt):
        return None
    with _lock:
        live = _confirmations.pop(receipt.receipt_id, None)
        if live != receipt or live.policy_digest != _policy_digest(live.template):
            return None
        activation = ActivatedProjectScope(
            live.template.template_id, live.session_key, uuid.uuid4().hex, time.time(),
            live.template, live.policy_digest, live.receipt_id,
        )
        _active[live.session_key] = activation
    _observe("project_scope_activated", activation)
    return activation


def _issue_trusted_kanban_grant(
    session_key: str, activation: ActivatedProjectScope, board: str, task_id: str,
    assignee: str | None,
) -> TrustedKanbanGrantReceipt | None:
    """Bind a confirmed card grant to the still-live exact session activation."""
    if not isinstance(activation, ActivatedProjectScope):
        return None
    with _lock:
        live = _active.get(session_key)
        if live != activation or live.activation_id != activation.activation_id:
            return None
        receipt = TrustedKanbanGrantReceipt(
            uuid.uuid4().hex, session_key, activation.activation_id, board, task_id,
            assignee, time.time(),
        )
        _kanban_confirmations[receipt.receipt_id] = receipt
        return receipt


def validate_trusted_kanban_grant(
    receipt: object, *, board: str, task_id: str, assignee: str | None,
) -> ActivatedProjectScope | None:
    """Validate live activation and exact receipt binding at root mint time."""
    if not isinstance(receipt, TrustedKanbanGrantReceipt):
        return None
    with _lock:
        live_receipt = _kanban_confirmations.pop(receipt.receipt_id, None)
        live = _active.get(receipt.session_key)
        if (live_receipt != receipt or live is None
                or live.activation_id != receipt.activation_id
                or (receipt.board, receipt.task_id, receipt.assignee) != (board, task_id, assignee)):
            return None
        return live


def _kanban_activation_snapshot(activation: ActivatedProjectScope) -> str:
    """Canonical policy payload a Kanban registry may bind, never restore."""
    template = activation.template
    return json.dumps({
        "template_id": template.template_id,
        "repository_roots": [str(path) for path in template.repository_roots],
        "temporary_roots": [str(path) for path in template.temporary_roots],
        "git_remotes": [[name, list(prefixes)] for name, prefixes in template.git_remotes],
        "git_ref_rules": list(template.git_ref_rules),
        "docker_registry_prefixes": list(template.docker_registry_prefixes),
        "allowed_operations": sorted(template.allowed_operations),
    }, sort_keys=True, separators=(",", ":"))


def validate_live_kanban_activation(
    session_key: object, activation_id: object, policy_digest: object, template_json: object,
) -> ActivatedProjectScope | None:
    """Validate a durable Kanban row against current session-only authority.

    Registry metadata is never an activation source: missing, replaced, or
    modified in-memory authority is deliberately indistinguishable from denial.
    """
    if not (isinstance(session_key, str) and session_key
            and isinstance(activation_id, str) and activation_id
            and isinstance(policy_digest, str) and policy_digest
            and isinstance(template_json, str) and template_json):
        return None
    with _lock:
        live = _active.get(session_key)
        if (live is None or live.session_key != session_key
                or live.activation_id != activation_id
                or live.policy_digest != policy_digest
                or _policy_digest(live.template) != policy_digest
                or _kanban_activation_snapshot(live) != template_json):
            return None
        return live


def revoke_project_scope(session_key: str) -> bool:
    with _lock:
        activation = _active.pop(session_key, None)
    if activation is not None:
        try:
            from hermes_cli.kanban_scope_lineage import revoke_activation_everywhere
            revoke_activation_everywhere(activation.activation_id)
        except Exception:
            pass
        _observe("project_scope_revoked", activation)
        return True
    return False


def clear_project_scope_session(session_key: str) -> None:
    revoke_project_scope(session_key)


def get_active_project_scope(session_key: str) -> ActivatedProjectScope | None:
    with _lock:
        return _active.get(session_key)


def _grant_delegated_project_scope(parent_session_key: str, recipient_session_key: str,
                                  recipient_subagent_id: str, *,
                                  parent_subagent_id: str | None = None) -> DelegatedProjectScopeGrant | None:
    """Create a root edge only from the delegate dispatcher lifecycle."""
    if not all(isinstance(value, str) and value for value in
               (parent_session_key, recipient_session_key, recipient_subagent_id)):
        return None
    if parent_session_key == recipient_session_key:
        return None
    with _lock:
        root = _active.get(parent_session_key)
        if root is None or recipient_session_key in _grant_by_recipient:
            return None
        grant = DelegatedProjectScopeGrant(
            uuid.uuid4().hex, root.activation_id, root.session_key, recipient_session_key,
            recipient_subagent_id, None, parent_subagent_id, root.template,
            root.policy_digest, 1, time.time(),
        )
        _delegated_grants[grant.grant_id] = grant
        _grant_by_recipient[recipient_session_key] = grant.grant_id
    _observe("project_scope_root_grant", root)
    return grant


def _derive_delegated_project_scope(parent_session_key: str, recipient_session_key: str,
                                   recipient_subagent_id: str, *, parent_grant_id: str) -> DelegatedProjectScopeGrant | None:
    """Derive an exact immutable descendant; never a new root or template."""
    if not all(isinstance(value, str) and value for value in
               (parent_session_key, recipient_session_key, recipient_subagent_id, parent_grant_id)):
        return None
    if parent_session_key == recipient_session_key:
        return None
    with _lock:
        parent = _delegated_grants.get(parent_grant_id)
        root = _active.get(parent.root_session_key) if parent else None
        if (parent is None or root is None or parent.recipient_session_key != parent_session_key
                or parent.depth >= _MAX_LINEAGE_DEPTH or recipient_session_key in _grant_by_recipient
                or recipient_session_key == parent.root_session_key
                or recipient_session_key in _active):
            return None
        # A recipient cannot be the root or any live ancestor, even if a stale
        # registry mapping was cleared. Walk only trusted immutable grant edges.
        ancestor = parent
        for _ in range(_MAX_LINEAGE_DEPTH):
            if recipient_session_key == ancestor.recipient_session_key:
                return None
            if ancestor.parent_grant_id is None:
                break
            ancestor = _delegated_grants.get(ancestor.parent_grant_id)
            if ancestor is None:
                return None
        grant = DelegatedProjectScopeGrant(
            uuid.uuid4().hex, parent.root_activation_id, parent.root_session_key,
            recipient_session_key, recipient_subagent_id, parent.grant_id,
            parent.recipient_subagent_id, parent.template, parent.policy_digest,
            parent.depth + 1, time.time(),
        )
        _delegated_grants[grant.grant_id] = grant
        _grant_by_recipient[recipient_session_key] = grant.grant_id
    _observe("project_scope_derived_grant", root)
    return grant


def _mint_dispatcher_child_scope(parent_session_key: str, recipient_session_key: str,
                                 recipient_subagent_id: str, *, parent_subagent_id: str | None = None,
                                 parent_grant_id: str | None = None) -> DelegatedProjectScopeGrant | None:
    """Mint only from runtime-issued parent/child identities."""
    if parent_grant_id:
        return _derive_delegated_project_scope(parent_session_key, recipient_session_key, recipient_subagent_id, parent_grant_id=parent_grant_id)
    return _grant_delegated_project_scope(parent_session_key, recipient_session_key, recipient_subagent_id, parent_subagent_id=parent_subagent_id)


def grant_delegated_project_scope(*args: object, **kwargs: object) -> None:
    """Removed raw grant-minting API; dispatcher lifecycle owns this transition."""
    return None


def derive_delegated_project_scope(*args: object, **kwargs: object) -> None:
    """Removed raw grant-minting API; dispatcher lifecycle owns this transition."""
    return None


def clear_delegated_project_scope(session_key: str, recipient_subagent_id: str) -> None:
    with _lock:
        grant = _delegated_grants.get(_grant_by_recipient.get(session_key, ""))
        if grant is not None and grant.recipient_subagent_id == recipient_subagent_id:
            _grant_by_recipient.pop(session_key, None)
            _delegated_grants.pop(grant.grant_id, None)


def get_delegated_project_scope(session_key: str) -> DelegatedProjectScopeGrant | None:
    """Live lookup: revoked/replaced roots make all descendant grants inert."""
    with _lock:
        grant = _delegated_grants.get(_grant_by_recipient.get(session_key, ""))
        root = _active.get(grant.root_session_key) if grant else None
        if grant is None or root is None or root.activation_id != grant.root_activation_id:
            return None
        return grant


def _parse_argv(command: str) -> list[str] | None:
    if not command or _FORBIDDEN_TEXT.search(command):
        return None
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not argv or _ASSIGNMENT_RE.match(argv[0]):
        return None
    basename = os.path.basename(argv[0]).lower()
    if basename in _CARRIERS or basename not in {"git", "docker"}:
        return None
    # Interpreter evaluation flags are blocked by the carrier executable check;
    # ``-m`` is also Git's ordinary commit-message flag and must be parsed by
    # the operation-specific grammar rather than rejected globally.
    if any(arg in {"-c", "-lc", "--command", "--eval", "-e"} for arg in argv[1:]):
        return None
    return [basename, *argv[1:]]


def _contained(candidate: Path, roots: tuple[Path, ...]) -> Path | None:
    for root in roots:
        try:
            candidate.relative_to(root)
            return root
        except ValueError:
            pass
    return None


def _canonical_path(value: str, cwd: Path, *, planned: bool = False) -> Path | None:
    if not value or "\x00" in value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = cwd / path
    try:
        if path.exists():
            return path.resolve(strict=True)
        if not planned:
            return None
        suffix: list[str] = []
        cursor = path
        while not cursor.exists():
            if cursor.name in {"", ".", ".."}:
                return None
            suffix.append(cursor.name)
            cursor = cursor.parent
        base = cursor.resolve(strict=True)
        return base.joinpath(*reversed(suffix))
    except (OSError, RuntimeError):
        return None


def _ref_allowed(ref: str, rules: tuple[str, ...]) -> bool:
    return bool(_FULL_REF_RE.fullmatch(ref)) and any(fnmatch.fnmatchcase(ref, rule) for rule in rules)


def _remote_destinations(repo: Path, remote_name: str) -> tuple[str, ...] | None:
    """Ask Git for every effective push target without contacting a remote.

    ``git remote get-url --push --all`` is the only supported resolver here:
    it applies Git's own include/includeIf, repeated pushurl, and
    ``url.<base>.pushInsteadOf`` rules.  Parsing config ourselves is unsafe
    because a later Git config feature could silently create an approval gap.
    The subcommand is metadata-only (no transport or hooks); a non-zero,
    malformed, overlong, or timed-out response is deliberately ineligible.
    """
    if not re.fullmatch(r"[A-Za-z0-9._-]+", remote_name):
        return None
    # Config injection through inherited GIT_CONFIG_* variables would make
    # evaluation differ from the scope-approved execution.  Normal on-disk
    # system/global/local config and their Git-managed includes remain active.
    if any(key == "GIT_CONFIG_COUNT" or key.startswith("GIT_CONFIG_KEY_")
           or key.startswith("GIT_CONFIG_VALUE_") for key in os.environ):
        return None
    try:
        result = subprocess.run(
            ["git", "-C", os.fspath(repo), "remote", "get-url", "--push", "--all", remote_name],
            cwd=os.fspath(repo), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, timeout=2, check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode or len(result.stdout) > 16_384:
        return None
    destinations = tuple(line.strip() for line in result.stdout.splitlines() if line.strip())
    return destinations or None


def _repo_from_c(argv: list[str], template: ProjectScopeTemplate, cwd: Path) -> tuple[Path, list[str]] | None:
    if len(argv) < 3 or argv[1] != "-C":
        return None
    repo = _canonical_path(argv[2], cwd)
    if repo is None or repo != cwd or repo not in template.repository_roots:
        return None
    return repo, argv[3:]


def _evaluate_git(argv: list[str], template: ProjectScopeTemplate, cwd: Path) -> ScopeDecision:
    parsed = _repo_from_c(argv, template, cwd)
    if parsed is None:
        return ScopeDecision("denied", reason="repository/cwd does not match configured root")
    repo, args = parsed
    if args[:2] == ["worktree", "prune"] and len(args) == 2:
        return ScopeDecision("approved", "git.worktree.prune")
    if args[:2] == ["worktree", "add"] and len(args) == 4:
        target = _canonical_path(args[2], cwd, planned=True)
        if target and _contained(target, template.temporary_roots) and _ref_allowed(args[3], template.git_ref_rules):
            return ScopeDecision("approved", "git.worktree.create")
        return ScopeDecision("denied", reason="worktree target or ref outside scope")
    if args[:2] == ["worktree", "remove"] and len(args) == 3:
        target = _canonical_path(args[2], cwd)
        if target and target != repo and _contained(target, template.temporary_roots):
            return ScopeDecision("approved", "git.worktree.remove")
        return ScopeDecision("denied", reason="worktree target outside temporary roots")
    if args[:2] == ["commit", "-S"] and len(args) == 4 and args[2] in {"-m", "--message"} and 0 < len(args[3]) <= 240 and "\n" not in args[3]:
        return ScopeDecision("approved", "git.commit.signed")
    if args[:1] == ["push"] and len(args) == 3:
        remote, refspec = args[1:]
        if remote.startswith("-") or "://" in remote or remote.startswith("git@") or refspec.count(":") != 1:
            return ScopeDecision("denied", reason="invalid remote/refspec")
        source, destination = refspec.split(":", 1)
        configured = dict(template.git_remotes).get(remote)
        destinations = _remote_destinations(repo, remote)
        if (configured and destinations
                and all(any(url.startswith(prefix) for prefix in configured) for url in destinations)
                and _ref_allowed(source, template.git_ref_rules)
                and _ref_allowed(destination, template.git_ref_rules)):
            return ScopeDecision("approved", "git.push.configured_remote")
        return ScopeDecision("denied", reason="remote URL or refspec outside scope")
    if args[:1] == ["push"]:
        return ScopeDecision("denied", reason="Git push flags or shape are not eligible")
    return ScopeDecision("not_applicable")


def _image_allowed(image: str, prefixes: tuple[str, ...]) -> bool:
    image = image.lower()
    return all(ch not in image for ch in " @") and any(image.startswith(prefix) for prefix in prefixes)


def _persisted_docker_context_is_local() -> bool:
    """Fail closed from Docker's local config only; never contact a daemon."""
    if any(os.environ.get(key) for key in ("DOCKER_HOST", "DOCKER_CONTEXT")):
        return False
    config_dir = Path(os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker")))
    try:
        config = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return True
    except (OSError, ValueError, TypeError):
        return False
    # Docker invokes docker-credential-<name> for either setting.  A scoped
    # auto-approval cannot safely prove that helper benign, so do not reach the
    # Docker CLI at all when local config asks it to execute one.
    if (isinstance(config.get("credsStore"), str) and config["credsStore"].strip()) or (
        isinstance(config.get("credHelpers"), dict)
        and any(isinstance(value, str) and value.strip() for value in config["credHelpers"].values())
    ):
        return False
    current = config.get("currentContext", "default")
    if current in (None, "", "default"):
        return True
    if not isinstance(current, str):
        return False
    try:
        meta = json.loads((config_dir / "contexts" / "meta" / current / "meta.json").read_text(encoding="utf-8"))
        host = meta["Endpoints"]["docker"]["Host"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return isinstance(host, str) and (host.startswith("unix://") or host.startswith("npipe://"))


def _evaluate_docker(argv: list[str], template: ProjectScopeTemplate, cwd: Path) -> ScopeDecision:
    if not _persisted_docker_context_is_local():
        return ScopeDecision("denied", reason="remote Docker daemon/context is not eligible")
    args = argv[1:]
    if args[:1] == ["push"] and len(args) == 2 and _image_allowed(args[1], template.docker_registry_prefixes):
        return ScopeDecision("approved", "docker.push.configured_registry")
    if args[:1] != ["build"]:
        return ScopeDecision("denied", reason="Docker operation is not eligible")
    i = 1
    tag = None
    dockerfile = None
    while i < len(args) - 1:
        if args[i] in {"-t", "--tag"} and tag is None and i + 1 < len(args) - 1:
            tag = args[i + 1]; i += 2
        elif args[i] in {"-f", "--file"} and dockerfile is None and i + 1 < len(args) - 1:
            dockerfile = args[i + 1]; i += 2
        else:
            return ScopeDecision("denied", reason="Docker build flag is not eligible")
    if i != len(args) - 1:
        return ScopeDecision("denied", reason="Docker build requires one context")
    context = _canonical_path(args[-1], cwd)
    if context is None or context != cwd or context not in template.repository_roots:
        return ScopeDecision("denied", reason="Docker context outside configured repository")
    if dockerfile:
        path = _canonical_path(dockerfile, cwd)
        if path is None or not _contained(path, (context,)):
            return ScopeDecision("denied", reason="Dockerfile outside build context")
    if tag is not None and not _image_allowed(tag, template.docker_registry_prefixes):
        return ScopeDecision("denied", reason="Docker tag outside configured registry")
    return ScopeDecision("approved", "docker.build")


def _decorate_decision(decision: ScopeDecision, activation: ActivatedProjectScope) -> ScopeDecision:
    labels = {
        "git.worktree.create": "temporary_root",
        "git.worktree.remove": "temporary_root",
        "git.worktree.prune": "repository_root",
        "git.commit.signed": "repository_root",
        "git.push.configured_remote": "repository_root",
        "docker.build": "repository_root",
        "docker.push.configured_registry": "docker_registry_prefix",
    }
    return ScopeDecision(
        decision.status, decision.operation, decision.reason, activation.template_id,
        activation.activation_id, activation.policy_digest, activation.session_key,
        time.time(), labels.get(decision.operation or ""),
    )


def _kanban_scope_activation(context: TerminalApprovalContext) -> ActivatedProjectScope | None:
    """Resolve an opaque dispatcher attempt ref; env values are only lookup keys."""
    try:
        from agent.delegation_context import is_dispatcher_owned_worker_context
        if not is_dispatcher_owned_worker_context():
            return None
        task_id = os.environ.get("HERMES_KANBAN_TASK", "")
        board = os.environ.get("HERMES_KANBAN_BOARD", "")
        claim_lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK", "")
        attempt_ref = os.environ.get("HERMES_KANBAN_SCOPE_ATTEMPT", "")
        raw_run = os.environ.get("HERMES_KANBAN_RUN_ID", "")
        board_db = os.environ.get("HERMES_KANBAN_DB", "")
        run_id = int(raw_run)
        broker = os.environ.get("HERMES_KANBAN_SCOPE_BROKER", "")
        if not all((task_id, board, claim_lock, attempt_ref, board_db, broker)) or run_id <= 0:
            return None
        from hermes_cli.kanban_scope_lineage import request_broker_scope
        scope = request_broker_scope(broker, attempt_ref)
        if scope is None:
            return None
        data = scope["template"]
        from tools.project_scope_approval import ProjectScopeTemplate, project_scope_policy_digest
        template = ProjectScopeTemplate(data["template_id"], tuple(Path(x) for x in data["repository_roots"]),
            tuple(Path(x) for x in data["temporary_roots"]), tuple((x[0], tuple(x[1])) for x in data["git_remotes"]),
            tuple(data["git_ref_rules"]), tuple(data["docker_registry_prefixes"]), frozenset(data["allowed_operations"]))
        digest = scope["digest"]
        if not isinstance(digest, str) or project_scope_policy_digest(template) != digest:
            return None
        return ActivatedProjectScope(template.template_id, context.session_key, scope["root_ref"],
                                     time.time(), template, digest)
    except Exception:
        return None


def evaluate_project_scope(context: TerminalApprovalContext) -> ScopeDecision:
    """Evaluate the immutable activation snapshot; no active scope is inert."""
    activation = get_active_project_scope(context.session_key)
    if activation is None:
        grant = get_delegated_project_scope(context.session_key)
        if grant is not None and context.execution_identity == grant.recipient_subagent_id:
            # Preserve the child session in decision/audit while binding every
            # policy field to the original immutable root snapshot.
            activation = ActivatedProjectScope(
                grant.template.template_id, context.session_key, grant.root_activation_id,
                grant.issued_at, grant.template, grant.policy_digest,
            )
    if activation is None:
        activation = _kanban_scope_activation(context)
    if activation is None:
        return ScopeDecision("not_applicable")
    argv = _parse_argv(context.raw_command)
    if argv is None:
        return _decorate_decision(ScopeDecision("not_applicable"), activation)
    cwd = _canonical_path(context.effective_cwd, Path("/"))
    if cwd is None:
        return _decorate_decision(ScopeDecision("denied", reason="effective cwd cannot be resolved"), activation)
    decision = _evaluate_git(argv, activation.template, cwd) if argv[0] == "git" else _evaluate_docker(argv, activation.template, cwd)
    if decision.status == "approved" and decision.operation not in activation.template.allowed_operations:
        decision = ScopeDecision("denied", reason="operation not declared by activation snapshot")
    return _decorate_decision(decision, activation)


def revalidate_project_scope(context: TerminalApprovalContext, decision: ScopeDecision) -> ScopeDecision:
    """Repeat local path/config checks at the execution boundary.

    This closes the guard-to-exec stale-decision window for mutable paths and
    Git metadata.  It cannot make independent user-space file reads and exec
    atomic; an attacker who races both checks remains a documented residual
    risk and requires OS-level descriptor/transaction support to eliminate.
    """
    current = evaluate_project_scope(context)
    if (current.status != "approved" or decision.status != "approved"
            or current.activation_id != decision.activation_id
            or current.policy_digest != decision.policy_digest
            or current.operation != decision.operation):
        return ScopeDecision(
            "denied", reason="project scope changed before execution",
            template_id=decision.template_id, activation_id=decision.activation_id,
            policy_digest=decision.policy_digest, session_key=context.session_key,
            timestamp=time.time(), matched_root_label=decision.matched_root_label,
        )
    return current


def scoped_execution_command(raw_command: str, decision: ScopeDecision) -> str | None:
    """Return a hook-suppressed argv for an approved Git operation only.

    Git command-line config has highest precedence, disabling both configured
    ``core.hooksPath`` and executable default hooks without changing ordinary
    terminal commands. Unexpected parsing refuses execution rather than
    weakening the scope boundary.
    """
    if decision.operation not in {"git.commit.signed", "git.push.configured_remote"}:
        return raw_command
    argv = _parse_argv(raw_command)
    if not argv or argv[0] != "git":
        return None
    return shlex.join(["git", "-c", "core.hooksPath=/dev/null", *argv[1:]])


def build_project_scope_audit_payload(decision: ScopeDecision) -> dict[str, object]:
    """Return allowlisted scope decision metadata without command operands."""
    return {
        "event": "project_scope_auto_approved" if decision.status == "approved" else "project_scope_denied",
        "template_id": decision.template_id,
        "activation_id": decision.activation_id,
        "session_id": decision.session_key,
        "policy_digest": decision.policy_digest,
        "timestamp": decision.timestamp,
        "operation": decision.operation,
        "decision": decision.status,
        "decision_reason": decision.reason or ("matched immutable policy" if decision.status == "approved" else "scope not granted"),
        "matched_root_label": decision.matched_root_label,
    }
