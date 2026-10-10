"""Behavior and private-error contracts for skill-graph slash discovery."""
from __future__ import annotations

import importlib.util
import logging
import sqlite3
from pathlib import Path

import pytest


PLUGIN_PATH = Path(__file__).parents[2] / "plugins" / "skill-graph" / "__init__.py"


@pytest.fixture
def graph(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    spec = importlib.util.spec_from_file_location("test_slash_discovery_plugin", PLUGIN_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    module._init_db(db)
    module._migrate_db(db)
    db.executemany(
        "INSERT INTO skill_nodes (name, description, category, scenes) VALUES (?, ?, ?, ?)",
        [
            ("orchid", "Flowering skill", "garden", '["coding", "research"]'),
            ("fern", "Shade skill", "garden", "[invalid-private-value"),
            ("moss", "Soft skill", "garden", "[]"),
        ],
    )
    db.commit()
    monkeypatch.setattr(module, "_ensure_graph", lambda: db)
    monkeypatch.setattr(module, "_find_all_skills_dirs", lambda: [])
    monkeypatch.setattr(module, "_db_path", lambda: home / "graph.db")
    yield module, db
    db.close()


def test_discovery_aliases_help_and_search_result_format(graph, monkeypatch):
    module, db = graph
    calls = []

    def search(query, conn, limit):
        calls.append((query, conn, limit))
        return [{"name": "orchid", "description": "Flowering skill", "score": 0.75,
                 "relevance": "direct", "relationship_chain": ["term orchid", "related orchid"]}]

    monkeypatch.setattr(module, "_search_graph", search)
    assert module._handle_slash_command("search") == module._slash_help()
    assert module._handle_slash_command("stats") == module._handle_slash_command("status")
    assert module._handle_slash_command("search garden") == (
        "Search results for: garden\n\n"
        "  orchid                               Flowering skill [direct]"
        "  chain: term orchid → related orchid"
    )
    score = module._handle_slash_command("score garden")
    assert score == module._handle_slash_command("explain garden")
    assert "orchid" in score and "score=0.7500" in score and "(no stats)" in score
    assert module._handle_slash_command("score") == "Usage: /skill-graph score <query>"
    assert "orchid" in module._handle_slash_command("list")
    assert calls == [("garden", db, 15), ("garden", db, 8), ("garden", db, 8)]
    monkeypatch.setattr(module, "_search_graph", lambda *_args, **_kw: [])
    assert module._handle_slash_command("search missing") == "No skills found for: missing"
    with pytest.raises(ValueError, match="Unsupported graph discovery command"):
        module._handle_slash_discovery("unknown", "")


def test_scene_list_show_missing_usage_and_malformed_json(graph, caplog):
    module, _db = graph
    with caplog.at_level(logging.ERROR, logger=module.__name__):
        scene_list = module._handle_slash_command("scene")
    assert scene_list == module._handle_slash_command("scene list")
    assert "Scene distribution (3 skills):" in scene_list
    assert "  coding          1" in scene_list
    assert "  research        1" in scene_list
    assert "  (untagged)      2" in scene_list
    assert "invalid scene data" in caplog.text
    assert "invalid-private-value" not in caplog.text
    assert module._handle_slash_command("scene show coding") == (
        "Skills with scene 'coding' (1):\n\n"
        "  orchid                                   Flowering skill"
    )
    assert module._handle_slash_command("scene show missing") == "No skills with scene: missing"
    assert module._handle_slash_command("scene show") == (
        "Usage: /sg scene list | /sg scene show <scene-name>"
    )


def test_scene_failure_redacts_query_and_sql_error(graph, monkeypatch, caplog):
    module, _db = graph

    def fail():
        raise sqlite3.OperationalError("/private/secret-source-path/private-query")

    monkeypatch.setattr(module, "_ensure_graph", fail)
    with caplog.at_level(logging.ERROR, logger=module.__name__):
        result = module._handle_slash_command("scene show private-query")
    assert result == "Scene command failed: could not read graph results"
    assert len(caplog.records) == 1
    assert "fail:" in caplog.text
    assert "secret-source-path" not in caplog.text
    assert "private-query" not in caplog.text
    assert caplog.records[0].exc_info is None
