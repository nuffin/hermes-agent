"""Write Guard — configurable file/directory write protection.

Hooks: pre_skill_create, pre_skill_write_file, pre_tool_call.

Config (in $HERMES_HOME/config.yaml):

  write_guard:
    rules:
      - glob: "**/*.log"
        policy: approve
      - glob: "/tmp/**"
        policy: approve
      - glob: "**/node_modules/**"
        policy: deny
    default: block

Policies:
  approve — silently allow
  block   — block with explanation, ask LLM to confirm (default)
  deny    — reject without asking
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
_SKILLS_DIR = _HERMES_HOME / "skills"


def register(ctx):
    ctx.register_hook("pre_skill_create", _check_skill_dir_exists)
    ctx.register_hook("pre_skill_write_file", _check_skill_file_exists)
    ctx.register_hook("pre_tool_call", _check_tool_file_write)


# ══════════════════════════════════════════════════════════════════════════
#  Config
# ══════════════════════════════════════════════════════════════════════════

def _load_rules() -> tuple[list[dict], str]:
    """Load write_guard rules from $HERMES_HOME/config.yaml."""
    try:
        import yaml
        cfg_path = _HERMES_HOME / "config.yaml"
        if not cfg_path.exists():
            return [], "block"
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
        wg = cfg.get("write_guard", {})
        rules = wg.get("rules", [])
        default = wg.get("default", "block")
        if default not in ("approve", "block", "deny"):
            default = "block"
        return rules, default
    except Exception:
        return [], "block"


def _match(target: str, rules: list[dict]) -> str | None:
    """Return the policy for the first matching glob, or None."""
    for rule in rules:
        glob = rule.get("glob", "")
        if not glob:
            continue
        expanded = os.path.expanduser(glob)
        # Use Path.match() which supports ** recursive matching
        if Path(target).match(expanded):
            return rule.get("policy", "block")
    return None


# ══════════════════════════════════════════════════════════════════════════
#  Git helpers
# ══════════════════════════════════════════════════════════════════════════

def _is_gitignored(path: Path) -> bool:
    try:
        parent = path.parent if not path.is_dir() else path
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(path)],
            cwd=parent, timeout=3,
        )
        return result.returncode == 0
    except Exception:
        return False


def _in_git_repo(path: Path) -> bool:
    try:
        parent = path.parent if not path.is_dir() else path
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=parent, capture_output=True, text=True, timeout=3,
        )
        return result.returncode == 0
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════
#  Skill lifecycle hooks
# ══════════════════════════════════════════════════════════════════════════

def _check_skill_dir_exists(**kw) -> dict | None:
    name = kw.get("name", "")
    if not name:
        return None
    target_dir = _SKILLS_DIR / name
    if target_dir.exists():
        return _apply_policy(
            "block",
            f"Skill '{name}' already exists at {target_dir}.\n"
            f"Inspect the existing SKILL.md first.",
        )
    return None


def _check_skill_file_exists(**kw) -> dict | None:
    name = kw.get("name", "")
    file_path = kw.get("file_path", "")
    if not name or not file_path:
        return None
    skill_dir = _SKILLS_DIR / name
    if not skill_dir.is_dir():
        return None
    target = skill_dir / file_path
    if target.exists():
        kind = "directory" if target.is_dir() else "file"
        return _apply_policy(
            "block",
            f"Target {kind} already exists: {target}\n"
            f"Inspect the existing content first.",
        )
    return None


# ══════════════════════════════════════════════════════════════════════════
#  pre_tool_call: generic write_file / patch
# ══════════════════════════════════════════════════════════════════════════

_WRITE_TOOLS = {"write_file", "patch"}


def _check_tool_file_write(**kw) -> dict | None:
    tool_name = kw.get("tool_name", "")
    if tool_name not in _WRITE_TOOLS:
        return None

    args = kw.get("arguments", {})
    if not isinstance(args, dict):
        return None

    target = args.get("path") or args.get("file_path") or ""
    if not target:
        return None

    p = _resolve(target)
    rules, default_policy = _load_rules()

    # Config rules override
    policy = _match(str(p), rules) or default_policy
    if policy == "approve":
        return None
    if policy == "deny":
        return {
            "action": "block",
            "reason": f"Write denied by write_guard config: {p}",
        }

    # File already exists
    if p.exists():
        kind = "directory" if p.is_dir() else "file"
        return _apply_policy(
            "block",
            f"Target {kind} already exists: {p}\n"
            f"Inspect the existing content first.\n"
            f"Only proceed if the write is intentional.",
        )

    # New file inside git repo, not gitignored
    if _in_git_repo(p) and not _is_gitignored(p):
        return _apply_policy(
            "block",
            f"Target file inside a git repo and NOT gitignored: {p}\n"
            f"This will create untracked content in `git status`.\n"
            f"Decide: (a) add to git after writing, or\n"
            f"       (b) add to .gitignore first.",
        )

    return None


# ══════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════

def _apply_policy(policy: str, reason: str) -> dict | None:
    if policy == "approve":
        return None
    if policy == "deny":
        return {"action": "block", "reason": f"Denied by policy: {reason}"}
    return {"action": "block", "reason": reason}


def _resolve(target: str) -> Path:
    p = Path(target)
    if not p.is_absolute():
        p = Path(os.environ.get("PWD", os.getcwd())) / p
    return p.resolve()
