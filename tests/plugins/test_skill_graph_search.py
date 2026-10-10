"""Lexical and semantic search keep their behavior under both plugin load paths."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest


PLUGIN_PATH = Path(__file__).parents[2] / "plugins" / "skill-graph" / "__init__.py"


@pytest.fixture(params=[False, True], ids=["bare-spec", "package-spec"])
def graph(tmp_path, monkeypatch, request):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    name = "skill_graph_search_contract"
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_PATH,
        submodule_search_locations=[str(PLUGIN_PATH.parent)] if request.param else None,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    if request.param:
        monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    module._init_db(conn)
    module._migrate_db(conn)
    yield module, conn, home
    conn.close()
    assert list(home.iterdir()) == []


def add_skill(conn, name, *, description="", tags=(), scenes=(), terms=()):
    conn.execute(
        "INSERT INTO skill_nodes (name, description, tags, scenes) VALUES (?, ?, ?, ?)",
        (name, description, json.dumps(tags), json.dumps(scenes)),
    )
    conn.execute(
        "INSERT INTO skill_fts (name, description, tags, scenes) VALUES (?, ?, ?, ?)",
        (name, description, " ".join(tags), " ".join(scenes)),
    )
    for term, strength in terms:
        conn.execute(
            "INSERT INTO skill_terms (term, skill_name, strength, source) VALUES (?, ?, ?, ?)",
            (term, name, strength, "name"),
        )


def test_fts_graph_expansion_soft_scenes_and_unknown_query(graph):
    sg, conn, _ = graph
    add_skill(conn, "python", description="Python coding", scenes=["coding"], terms=[("python", 1.0)])
    add_skill(conn, "guide", description="Documentation", scenes=["common"])
    add_skill(conn, "zebra", description="Unrelated")
    conn.execute(
        "INSERT INTO skill_edges (source, target, rel_type, properties) VALUES (?, ?, ?, ?)",
        ("python", "guide", "depends_on", '{"reason":"learn"}'),
    )
    hits = sg._search_graph("python", conn, scenes=["CODING"])
    assert [hit["name"] for hit in hits] == ["python", "guide"]
    assert hits[0]["relevance"] == "direct"  # FTS hits are excluded from term lookup
    assert hits[0]["_scene_boost"] is True
    assert hits[1]["relationship_chain"] == ["python --(depends_on)--> guide: learn"]
    assert "_scene_boost" not in hits[1]  # common is not boosted
    assert conn.execute(
        "SELECT search_count FROM skill_term_stats WHERE skill_name = 'python'"
    ).fetchone()[0] == 1
    assert sg._search_graph("nonexistentword", conn) == []
    assert [hit["name"] for hit in sg._search_graph("?", conn, limit=2)] == ["guide", "python"]


def test_term_tag_fallback_deleted_and_helper_contracts(graph):
    sg, conn, _ = graph
    add_skill(conn, "tagged", tags=["amber"], terms=[("amber", 0.9)])
    add_skill(conn, "chinese", terms=[("搜索", 1.0)])
    add_skill(conn, "partial-note", description="sunflower")
    add_skill(conn, "deleted", description="sunflower")
    conn.execute("UPDATE skill_nodes SET is_deleted = 1 WHERE name = 'deleted'")
    assert sg._fts_query("python-json") == '"python-json"'
    assert sg._fts_query("搜索") == ""
    assert sg._extract_terms("A 搜索 Python-json") == ["搜索", "python-json"]
    assert sg._search_graph("amber", conn)[0]["relevance"] == "tag_match"
    assert sg._search_graph("搜索", conn)[0]["name"] == "chinese"
    assert sg._search_graph("sunflow", conn)[0]["name"] == "partial-note"
    assert sg._get_node_info(conn, "deleted") is None
    assert sg._get_node_info(conn, "tagged")["needs_organizing"] is False


def test_plugin_level_patch_seams_are_used_by_sibling(graph, monkeypatch):
    sg, conn, _ = graph
    add_skill(conn, "patched")
    fts = Mock(return_value="")
    terms = Mock(return_value=["patched"])
    info = Mock(return_value={"name": "patched", "score": 0, "tags": []})
    stats = Mock()
    scene = Mock()
    fallback = Mock(return_value=[{"name": "fallback"}])
    monkeypatch.setattr(sg, "_fts_query", fts)
    monkeypatch.setattr(sg, "_extract_terms", terms)
    monkeypatch.setattr(sg, "_get_node_info", info)
    monkeypatch.setattr(sg, "_boost_search_results", stats)
    monkeypatch.setattr(sg, "_boost_scene_results", scene)
    monkeypatch.setattr(sg, "_fallback_search", fallback)
    assert sg._search_graph("patched", conn, scenes=["coding"])[0]["name"] == "fallback"
    fts.assert_called_once_with("patched")
    terms.assert_called_once_with("patched")
    stats.assert_called_once()
    scene.assert_called_once()
    fallback.assert_called_once_with("patched", conn, 10)
    semantic = Mock(return_value=[{"name": "semantic", "description": "match", "score": 0.9}])
    monkeypatch.setattr(sg, "_embedding_search", semantic)
    monkeypatch.setattr(sg, "_ensure_graph", Mock(side_effect=AssertionError("unexpected lexical lookup")))
    assert sg._rank_skill_candidates(["meaning"]) == [
        {"name": "semantic", "description": "match", "score": 0.9}
    ]
    semantic.assert_called_once_with("meaning", topk=5)
