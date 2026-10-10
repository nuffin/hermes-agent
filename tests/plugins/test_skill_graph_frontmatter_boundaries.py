"""Skill enrichment frontmatter, persistence, and private log contracts."""
from __future__ import annotations

import importlib.util
import json
import logging
import sqlite3
import sys
from pathlib import Path

import pytest


PLUGIN_PATH = Path(__file__).parents[2] / "plugins" / "skill-graph" / "__init__.py"
SECRET = "SECRET-skill-path-tags-scenes-suggestions"


@pytest.fixture(params=["bare", "package"])
def env(tmp_path, monkeypatch, request):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    name = "test_skill_graph_frontmatter_plugin"
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_PATH,
        submodule_search_locations=[str(PLUGIN_PATH.parent)] if request.param == "package" else None,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    if request.param == "package":
        monkeypatch.setitem(sys.modules, name, module)
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


def test_single_skill_uses_late_bound_seams_and_commits_fts_terms(env, monkeypatch, caplog):
    module, conn, root = env
    path = root / "bundled" / "SKILL.md"
    path.parent.mkdir()
    path.write_text("---\nname: test-skill\n---\nBody\n")
    _skill(conn, path, SECRET)
    conn.execute("INSERT INTO skill_fts (name, tags) VALUES (?, ?)", (SECRET, "old"))
    conn.execute("INSERT INTO skill_terms (term, skill_name) VALUES (?, ?)", ("old", SECRET))
    conn.commit()

    prompts = []
    monkeypatch.setattr(module, "_build_enrichment_prompt", lambda n, c: prompts.append((n, c)) or "prompt")
    monkeypatch.setattr(module, "_call_llm_for_enrichment", lambda p: {
        "tags": [SECRET], "scenes": ["coding", "not-a-scene"], "suggestions": [SECRET],
    } if p == "prompt" else None)
    monkeypatch.setattr(module, "_extract_skill_terms", lambda n, t, d: [("new", 0.7, "tag")])
    monkeypatch.setattr(module, "get_bundled_skills_dir", lambda fallback: path.parent)
    monkeypatch.setattr(module, "_patch_skill_frontmatter", lambda *args: pytest.fail("bundled write"))
    with caplog.at_level(logging.INFO, logger=module.__name__):
        assert module._enrich_skill(conn, SECRET)
    assert prompts == [(SECRET, path.read_text())]
    assert conn.in_transaction is False
    row = conn.execute("SELECT enriched, enriched_at, tags, scenes FROM skill_nodes WHERE name = ?", (SECRET,)).fetchone()
    assert row["enriched"] == 1 and row["enriched_at"]
    assert json.loads(row["tags"]) == [SECRET]
    assert json.loads(row["scenes"]) == ["coding"]
    assert [tuple(r) for r in conn.execute("SELECT tags, scenes FROM skill_fts WHERE name = ?", (SECRET,))] == [(SECRET, "coding")]
    assert [tuple(r) for r in conn.execute("SELECT term, strength, source FROM skill_terms WHERE skill_name = ?", (SECRET,))] == [("new", 0.7, "tag")]
    assert "tags:" not in path.read_text()
    _private(caplog)


def test_db_error_before_frontmatter_write_can_be_rolled_back(env, monkeypatch):
    module, conn, root = env
    path = root / "skills" / "SKILL.md"
    path.parent.mkdir()
    original = "---\nname: test-skill\n---\nBody\n"
    path.write_text(original)
    _skill(conn, path)
    conn.execute("CREATE TRIGGER abort_term_update BEFORE DELETE ON skill_terms BEGIN SELECT RAISE(ABORT, 'blocked'); END")
    conn.execute("INSERT INTO skill_terms (term, skill_name) VALUES ('old', 'test-skill')")
    conn.commit()
    monkeypatch.setattr(module, "_call_llm_for_enrichment", lambda prompt: {"tags": ["new"], "scenes": ["coding"]})
    monkeypatch.setattr(module, "_patch_skill_frontmatter", lambda *args: pytest.fail("write before DB update"))
    with pytest.raises(sqlite3.IntegrityError, match="blocked"):
        module._enrich_skill(conn, "test-skill")
    conn.rollback()
    assert tuple(conn.execute("SELECT enriched, tags FROM skill_nodes WHERE name = 'test-skill'").fetchone()) == (0, "[]")
    assert [r[0] for r in conn.execute("SELECT term FROM skill_terms")] == ["old"]
    assert path.read_text() == original


def test_pending_cooldown_force_and_private_names(env, monkeypatch, caplog):
    module, conn, root = env
    _skill(conn, root / "unused", SECRET)
    assert conn.execute(
        "SELECT is_deleted FROM skill_nodes WHERE name = ?", (SECRET,)
    ).fetchone()[0] == 0
    conn.execute("UPDATE skill_nodes SET enriched_at = datetime('now') WHERE name = ?", (SECRET,))
    calls = []
    monkeypatch.setattr(module, "_enrich_skill", lambda db, name: calls.append((db, name)) or False)
    assert module._enrich_pending_skills(conn) == 0
    with caplog.at_level(logging.INFO, logger=module.__name__):
        assert module._enrich_pending_skills(conn, force=True) == 0
    assert calls == [(conn, SECRET)]
    assert "FAILED" in caplog.text
    _private(caplog)
