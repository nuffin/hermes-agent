"""Skill enrichment frontmatter, persistence, and private log contracts."""
from __future__ import annotations

import importlib.util
import json
import logging
import sqlite3
from pathlib import Path

import pytest


PLUGIN_PATH = Path(__file__).parents[2] / "plugins" / "skill-graph" / "__init__.py"
SECRET = "SECRET-skill-path-tags-scenes-suggestions"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    spec = importlib.util.spec_from_file_location("test_skill_graph_frontmatter_plugin", PLUGIN_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    module._init_db(conn)
    yield module, conn, tmp_path
    conn.close()


def _skill(conn, path, name="test-skill"):
    conn.execute("INSERT INTO skill_nodes (name, file_path) VALUES (?, ?)", (name, str(path)))
    conn.commit()


def _private(caplog):
    assert SECRET not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_frontmatter_patch_success_and_invalid_metadata(env, caplog):
    module, _, root = env
    path = root / "writable" / "SKILL.md"
    path.parent.mkdir()
    path.write_text("---\nname: test-skill\nmetadata:\n  hermes:\n    other: kept\n---\nBody\n")
    assert module._patch_skill_frontmatter(str(path), ["python"], ["coding"])
    text = path.read_text()
    assert "other: kept" in text and "tags:" in text and "scenes:" in text
    assert text.endswith("---\nBody\n")

    for invalid in ("metadata: null", "metadata: []", "metadata:\n  hermes: false"):
        path.write_text(f"---\n{invalid}\n---\nBody\n")
        with caplog.at_level(logging.WARNING, logger=module.__name__):
            assert module._patch_skill_frontmatter(str(path), [SECRET], [SECRET]) is False
        assert path.read_text() == f"---\n{invalid}\n---\nBody\n"
        _private(caplog)
        caplog.clear()


@pytest.mark.parametrize("failure", ["missing", "malformed", "write"])
def test_frontmatter_io_parse_failures_are_private(env, monkeypatch, caplog, failure):
    module, _, root = env
    path = root / SECRET / "SKILL.md"
    if failure != "missing":
        path.parent.mkdir()
        path.write_text("---\nmetadata: [invalid\n---\nBody\n" if failure == "malformed" else
                        "---\nname: test-skill\n---\nBody\n")
    if failure == "write":
        original = Path.write_text

        def fail_write(self, *args, **kwargs):
            if self == path:
                raise OSError(SECRET)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", fail_write)
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        assert module._patch_skill_frontmatter(str(path), [SECRET], [SECRET]) is False
    assert "frontmatter" in caplog.text
    _private(caplog)


def test_enrich_read_failure_private_and_no_db_update(env, caplog):
    module, conn, root = env
    path = root / SECRET / "SKILL.md"
    _skill(conn, path)
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        assert module._enrich_skill(conn, "test-skill") is False
    assert conn.execute("SELECT enriched FROM skill_nodes WHERE name = 'test-skill'").fetchone()[0] == 0
    assert "enrichment.read" in caplog.text
    _private(caplog)


@pytest.mark.parametrize("policy", ["writable", "read-only", "bundled", "write-failed"])
def test_enrich_persists_db_and_observes_write_policy(env, monkeypatch, caplog, policy):
    module, conn, root = env
    directory = root / ("bundled" if policy == "bundled" else "skills")
    directory.mkdir()
    path = directory / "SKILL.md"
    original = "---\nname: test-skill\n---\nOriginal content\n"
    path.write_text(original)
    _skill(conn, path, SECRET)
    monkeypatch.setattr(module, "_call_llm_for_enrichment", lambda prompt: {
        "tags": [SECRET], "scenes": ["coding", SECRET], "suggestions": [SECRET],
    })
    monkeypatch.setattr(module, "get_bundled_skills_dir", lambda fallback: root / "bundled")
    monkeypatch.setattr(module, "_is_read_only_skill", lambda skill_path: policy == "read-only")
    if policy == "write-failed":
        original_write = Path.write_text

        def fail_write(self, *args, **kwargs):
            if self == path:
                raise OSError(SECRET)
            return original_write(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", fail_write)
    with caplog.at_level(logging.INFO, logger=module.__name__):
        assert module._enrich_skill(conn, SECRET) is True
    row = conn.execute("SELECT enriched, tags, scenes FROM skill_nodes WHERE name = ?", (SECRET,)).fetchone()
    assert row["enriched"] == 1
    assert json.loads(row["tags"]) == [SECRET]
    assert json.loads(row["scenes"]) == ["coding"]
    assert ("tags:" in path.read_text()) == (policy == "writable")
    assert ("DB only" in caplog.text) == (policy in ("bundled", "read-only"))
    assert "enrichment suggestions" in caplog.text
    _private(caplog)
