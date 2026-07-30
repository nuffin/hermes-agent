"""Delete Guard — block skill deletions without confirmation.

Hooks: pre_skill_delete, pre_skill_remove_file.
Uses $HERMES_HOME for profile-aware skill paths.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
_SKILLS_DIR = _HERMES_HOME / "skills"


def register(ctx):
    ctx.register_hook("pre_skill_delete", _check_skill_delete)
    ctx.register_hook("pre_skill_remove_file", _check_remove_file)


def _check_skill_delete(**kw) -> dict | None:
    name = kw.get("name", "")
    if not name:
        return None
    target_dir = _SKILLS_DIR / name
    if target_dir.is_dir():
        return {
            "action": "block",
            "reason": (
                f"Skill '{name}' exists at {target_dir} and will be "
                f"permanently deleted.\n"
                f"Inspect the directory contents first.\n"
                f"Only proceed if you are certain this skill is no longer needed."
            ),
        }
    return None


def _check_remove_file(**kw) -> dict | None:
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
        return {
            "action": "block",
            "reason": (
                f"Target {kind} will be deleted: {target}\n"
                f"Inspect the contents first.\n"
                f"Only proceed if you are certain this file is no longer needed."
            ),
        }
    return None
