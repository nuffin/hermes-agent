"""Tests for write-guard and delete-guard plugins.

Does NOT import from hermes — these are standalone unit tests
for the hook handler functions.  No plugin runtime required.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

# ── Load plugin modules from their directory (names use hyphens) ─────────

_HERE = Path(__file__).resolve().parent
_WRITE_GUARD_DIR = _HERE.parent
_DELETE_GUARD_DIR = _HERE.parents[1] / "delete-guard"


def _load_module(dirpath: Path, modname: str):
    init = dirpath / "__init__.py"
    spec = importlib.util.spec_from_file_location(modname, init)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


os.environ.setdefault("HERMES_HOME", str(Path.home() / ".hermes"))

write_guard = _load_module(_WRITE_GUARD_DIR, "write_guard")
delete_guard = _load_module(_DELETE_GUARD_DIR, "delete_guard")


# ══════════════════════════════════════════════════════════════════════════
#  Fixtures
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def tmp_skill(tmp_path):
    """Create a fake HERMES_HOME with a skill directory."""
    home = tmp_path / "hermes"
    skills = home / "skills"
    skills.mkdir(parents=True)
    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(home)
    write_guard._HERMES_HOME = home
    write_guard._SKILLS_DIR = skills
    delete_guard._HERMES_HOME = home
    delete_guard._SKILLS_DIR = skills
    yield home
    if old:
        os.environ["HERMES_HOME"] = old


# ══════════════════════════════════════════════════════════════════════════
#  write-guard — pre_skill_create
# ══════════════════════════════════════════════════════════════════════════

class TestPreSkillCreate:
    def test_blocks_existing_skill(self, tmp_skill):
        (tmp_skill / "skills" / "existing").mkdir()
        result = write_guard._check_skill_dir_exists(name="existing")
        assert result is not None
        assert result["action"] == "block"
        assert "already exists" in result["reason"]

    def test_allows_new_skill(self, tmp_skill):
        result = write_guard._check_skill_dir_exists(name="new-skill")
        assert result is None

    def test_empty_name_passes(self):
        assert write_guard._check_skill_dir_exists(name="") is None
        assert write_guard._check_skill_dir_exists() is None


# ══════════════════════════════════════════════════════════════════════════
#  write-guard — pre_skill_write_file
# ══════════════════════════════════════════════════════════════════════════

class TestPreSkillWriteFile:
    def test_blocks_existing_file(self, tmp_skill):
        skill = tmp_skill / "skills" / "myskill"
        refs = skill / "references"
        refs.mkdir(parents=True)
        (refs / "exists.md").write_text("old content")

        result = write_guard._check_skill_file_exists(
            name="myskill", file_path="references/exists.md"
        )
        assert result is not None
        assert result["action"] == "block"
        assert "already exists" in result["reason"]

    def test_allows_new_file(self, tmp_skill):
        skill = tmp_skill / "skills" / "myskill"
        skill.mkdir(parents=True)

        result = write_guard._check_skill_file_exists(
            name="myskill", file_path="references/new.md"
        )
        assert result is None

    def test_unknown_skill_passes(self):
        assert write_guard._check_skill_file_exists(
            name="nonexistent", file_path="any.md"
        ) is None

    def test_empty_params_passes(self):
        assert write_guard._check_skill_file_exists(name="") is None
        assert write_guard._check_skill_file_exists(name="x", file_path="") is None


# ══════════════════════════════════════════════════════════════════════════
#  write-guard — pre_tool_call (generic write_file / patch)
# ══════════════════════════════════════════════════════════════════════════

class TestPreToolCall:
    def test_blocks_existing_file(self, tmp_path):
        f = tmp_path / "exists.txt"
        f.write_text("hello")
        result = write_guard._check_tool_file_write(
            tool_name="write_file", arguments={"path": str(f)}
        )
        assert result is not None
        assert result["action"] == "block"

    def test_allows_new_file(self, tmp_path):
        f = tmp_path / "new.txt"
        result = write_guard._check_tool_file_write(
            tool_name="write_file", arguments={"path": str(f)}
        )
        assert result is None

    def test_ignores_non_write_tools(self):
        result = write_guard._check_tool_file_write(
            tool_name="read_file", arguments={"path": "/etc/hosts"}
        )
        assert result is None

    def test_ignores_missing_path(self):
        result = write_guard._check_tool_file_write(
            tool_name="write_file", arguments={"content": "x"}
        )
        assert result is None

    def test_blocks_patch_tool_too(self, tmp_path):
        f = tmp_path / "patchme.py"
        f.write_text("# old")
        result = write_guard._check_tool_file_write(
            tool_name="patch", arguments={"path": str(f)}
        )
        assert result is not None
        assert result["action"] == "block"

    def test_blocks_directory_writes(self, tmp_path):
        d = tmp_path / "adir"
        d.mkdir()
        result = write_guard._check_tool_file_write(
            tool_name="write_file", arguments={"path": str(d)}
        )
        assert result is not None
        assert "directory" in result["reason"]


# ══════════════════════════════════════════════════════════════════════════
#  write-guard — config rules
# ══════════════════════════════════════════════════════════════════════════

class TestConfig:
    def test_default_policy_is_block(self):
        _, default = write_guard._load_rules()
        assert default == "block"

    def test_match_approve(self):
        rules = [{"glob": "**/*.log", "policy": "approve"}]
        assert write_guard._match("/var/log/app.log", rules) == "approve"
        # Bare filenames: use *.log for that
        rules2 = [{"glob": "*.log", "policy": "approve"}]
        assert write_guard._match("app.log", rules2) == "approve"

    def test_match_deny(self):
        rules = [{"glob": "/tmp/*", "policy": "deny"}]
        assert write_guard._match("/tmp/secret", rules) == "deny"

    def test_no_match(self):
        rules = [{"glob": "*.log", "policy": "approve"}]
        assert write_guard._match("main.py", rules) is None

    def test_first_match_wins(self):
        rules = [
            {"glob": "*.log", "policy": "deny"},
            {"glob": "**/*.log", "policy": "approve"},
        ]
        assert write_guard._match("error.log", rules) == "deny"


# ══════════════════════════════════════════════════════════════════════════
#  delete-guard — pre_skill_delete
# ══════════════════════════════════════════════════════════════════════════

class TestPreSkillDelete:
    def test_blocks_existing_skill(self, tmp_skill):
        (tmp_skill / "skills" / "old-skill").mkdir()
        result = delete_guard._check_skill_delete(name="old-skill")
        assert result is not None
        assert result["action"] == "block"
        assert "permanently deleted" in result["reason"]

    def test_allows_nonexistent_skill(self, tmp_skill):
        result = delete_guard._check_skill_delete(name="ghost")
        assert result is None

    def test_empty_name_passes(self):
        assert delete_guard._check_skill_delete(name="") is None


# ══════════════════════════════════════════════════════════════════════════
#  delete-guard — pre_skill_remove_file
# ══════════════════════════════════════════════════════════════════════════

class TestPreSkillRemoveFile:
    def test_blocks_existing_file(self, tmp_skill):
        skill = tmp_skill / "skills" / "myskill"
        (skill / "references").mkdir(parents=True)
        (skill / "references" / "old.md").write_text("data")
        result = delete_guard._check_remove_file(
            name="myskill", file_path="references/old.md"
        )
        assert result is not None
        assert result["action"] == "block"
        assert "will be deleted" in result["reason"]

    def test_allows_nonexistent_file(self, tmp_skill):
        skill = tmp_skill / "skills" / "myskill"
        skill.mkdir(parents=True)
        result = delete_guard._check_remove_file(
            name="myskill", file_path="ghost.md"
        )
        assert result is None

    def test_unknown_skill_passes(self):
        assert delete_guard._check_remove_file(
            name="nope", file_path="x.md"
        ) is None

    def test_empty_params_passes(self):
        assert delete_guard._check_remove_file(name="") is None
        assert delete_guard._check_remove_file(name="x", file_path="") is None
