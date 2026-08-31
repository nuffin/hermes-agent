"""In-memory, explicitly activated project-scoped terminal approvals.

This module deliberately has no command execution or prompt/UI surface.  A
configured template is inert until a trusted approval surface explicitly calls
:func:`activate_project_scope` for the current session.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import configparser
import fnmatch
import os
import re
import shlex
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


@dataclass(frozen=True)
class ScopeDecision:
    status: str  # not_applicable | denied | approved
    operation: str | None = None
    reason: str | None = None
    template_id: str | None = None
    activation_id: str | None = None


_lock = threading.RLock()
_active: dict[str, ActivatedProjectScope] = {}


def _observe(event: str, activation: ActivatedProjectScope, *, operation: str | None = None) -> None:
    """Emit allowlisted scope metadata only; never raw command/config values."""
    try:
        from tools.approval import _fire_approval_hook
        _fire_approval_hook("post_approval_response", project_scope={
            "event": event,
            "template_id": activation.template_id,
            "activation_id": activation.activation_id,
            "operation": operation,
            "decision": "approved" if event != "project_scope_revoked" else "revoked",
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
    return {"id": template.template_id, "repository_roots": [str(p) for p in template.repository_roots], "temporary_roots": [str(p) for p in template.temporary_roots], "allowed_operations": sorted(template.allowed_operations), "git_remotes": [name for name, _ in template.git_remotes], "git_ref_rules": list(template.git_ref_rules), "docker_registry_prefixes": list(template.docker_registry_prefixes), "expires": "session"}


def activate_project_scope(session_key: str, template_id: str, *, delegated: bool = False) -> ActivatedProjectScope | None:
    """Bind one prevalidated template to a session after explicit user consent.

    This API does not infer consent from command text, task metadata, CWD, or
    environment.  Callers must supply the current session and an exact ID from
    a user-facing confirmation flow.
    """
    if delegated:
        raise PermissionError("delegated callers cannot activate a project scope")
    if not isinstance(session_key, str) or not session_key:
        return None
    if template_id not in load_project_scope_templates():
        return None
    activation = ActivatedProjectScope(template_id, session_key, uuid.uuid4().hex, time.time())
    with _lock:
        _active[session_key] = activation
    _observe("project_scope_activated", activation)
    return activation


def revoke_project_scope(session_key: str) -> bool:
    with _lock:
        activation = _active.pop(session_key, None)
    if activation is not None:
        _observe("project_scope_revoked", activation)
        return True
    return False


def clear_project_scope_session(session_key: str) -> None:
    revoke_project_scope(session_key)


def get_active_project_scope(session_key: str) -> ActivatedProjectScope | None:
    with _lock:
        return _active.get(session_key)


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


def _remote_url(repo: Path, remote_name: str) -> str | None:
    git = repo / ".git"
    try:
        if git.is_file():
            marker = git.read_text(encoding="utf-8", errors="replace").strip()
            if not marker.startswith("gitdir: "):
                return None
            git = (repo / marker[8:].strip()).resolve(strict=True)
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(git / "config", encoding="utf-8")
        section = f'remote "{remote_name}"'
        return parser.get(section, "url", fallback=None)
    except (OSError, configparser.Error):
        return None


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
        url = _remote_url(repo, remote)
        if configured and url and any(url.startswith(prefix) for prefix in configured) and _ref_allowed(source, template.git_ref_rules) and _ref_allowed(destination, template.git_ref_rules):
            return ScopeDecision("approved", "git.push.configured_remote")
        return ScopeDecision("denied", reason="remote URL or refspec outside scope")
    if args[:1] == ["push"]:
        return ScopeDecision("denied", reason="Git push flags or shape are not eligible")
    return ScopeDecision("not_applicable")


def _image_allowed(image: str, prefixes: tuple[str, ...]) -> bool:
    image = image.lower()
    return all(ch not in image for ch in " @") and any(image.startswith(prefix) for prefix in prefixes)


def _evaluate_docker(argv: list[str], template: ProjectScopeTemplate, cwd: Path) -> ScopeDecision:
    if any(os.environ.get(key) for key in ("DOCKER_HOST", "DOCKER_CONTEXT")):
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


def evaluate_project_scope(context: TerminalApprovalContext) -> ScopeDecision:
    """Evaluate an active template only; no active template is always inert."""
    activation = get_active_project_scope(context.session_key)
    if activation is None:
        return ScopeDecision("not_applicable")
    template = load_project_scope_templates().get(activation.template_id)
    if template is None:
        return ScopeDecision("denied", reason="active template is no longer valid", template_id=activation.template_id, activation_id=activation.activation_id)
    argv = _parse_argv(context.raw_command)
    if argv is None:
        return ScopeDecision("not_applicable", template_id=template.template_id, activation_id=activation.activation_id)
    cwd = _canonical_path(context.effective_cwd, Path("/"))
    if cwd is None:
        return ScopeDecision("denied", reason="effective cwd cannot be resolved", template_id=template.template_id, activation_id=activation.activation_id)
    decision = _evaluate_git(argv, template, cwd) if argv[0] == "git" else _evaluate_docker(argv, template, cwd)
    if decision.status == "approved" and decision.operation not in template.allowed_operations:
        decision = ScopeDecision("denied", reason="operation not declared by template")
    return ScopeDecision(decision.status, decision.operation, decision.reason, template.template_id, activation.activation_id)


def build_project_scope_audit_payload(decision: ScopeDecision) -> dict[str, object]:
    """Return allowlisted scope decision metadata without command operands."""
    return {
        "event": "project_scope_auto_approved" if decision.status == "approved" else "project_scope_denied",
        "template_id": decision.template_id,
        "activation_id": decision.activation_id,
        "operation": decision.operation,
        "decision": decision.status,
    }
