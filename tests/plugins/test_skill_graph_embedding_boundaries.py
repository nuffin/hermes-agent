"""Embedding persistence and retrieval contracts under isolated profile state."""
from __future__ import annotations

import importlib.util
import importlib
import logging
import sqlite3
import struct
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest


PLUGIN_PATH = Path(__file__).parents[2] / "plugins" / "skill-graph" / "__init__.py"
PRIVATE = "secret-skill https://private.invalid token-private private embedding text"


@pytest.fixture
def graph(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    spec = importlib.util.spec_from_file_location(
        "skill_graph_embedding_boundaries", PLUGIN_PATH,
        submodule_search_locations=[str(PLUGIN_PATH.parent)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    backend = importlib.import_module(f"{module.__name__}.embedding_client")
    # numpy is optional in this test env; exercise the real orchestration with
    # deterministic float32 serialization and ranking, not a downloaded model.
    monkeypatch.setattr(backend, "to_blob", lambda vec: struct.pack(f"{len(vec)}f", *vec))
    monkeypatch.setattr(backend, "cosine_batch", lambda query, blobs: [
        sum(a * b for a, b in zip(
            struct.unpack(f"{len(query) // 4}f", query),
            struct.unpack(f"{len(blob) // 4}f", blob),
        )) for blob in blobs
    ])
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    module._init_db(conn)
    module._migrate_db(conn)
    yield module, conn
    conn.close()


def _add_skills(conn, count):
    conn.executemany(
        "INSERT INTO skill_nodes (name, description) VALUES (?, ?)",
        [(f"skill-{i:02d}", "private embedding text") for i in range(count)],
    )
    conn.commit()


def _assert_redacted(caplog, location):
    records = [r for r in caplog.records if f"location=embedding.{location}" in r.message]
    assert records
    for record in records:
        assert record.exc_info is not None
        assert record.exc_info[1].__context__ is None
    for secret in ("secret-skill", "private.invalid", "token-private", "private embedding text"):
        assert secret not in caplog.text


def test_single_embedding_success_and_failure_do_not_forge_rows(graph, monkeypatch, caplog):
    sg, conn = graph
    _add_skills(conn, 1)
    client = Mock()
    client.is_available.return_value = True
    client.model_name.return_value = "bge-m3"
    client.embed.return_value = [[0.25, 0.75]]
    monkeypatch.setattr(sg, "_embedding_client", lambda: client)
    assert sg._compute_embedding_for_skill(conn, "skill-00", {"description": "data"}) is True
    assert tuple(conn.execute("SELECT dim, model FROM skill_embeddings").fetchone()) == (2, "bge-m3")

    client.embed.side_effect = RuntimeError(PRIVATE)
    with caplog.at_level(logging.WARNING):
        assert sg._compute_embedding_for_skill(conn, "secret-skill", {"description": PRIVATE}) is False
    assert conn.execute("SELECT COUNT(*) FROM skill_embeddings").fetchone()[0] == 1
    _assert_redacted(caplog, "single")


def test_batch_failure_preserves_only_complete_earlier_chunks(graph, monkeypatch, caplog):
    sg, conn = graph
    _add_skills(conn, 34)
    client = Mock()
    client.is_available.return_value = True
    client.model_name.return_value = "bge-m3"
    client.embed.side_effect = [
        [[0.5, 0.5] for _ in range(32)],
        RuntimeError(PRIVATE),
    ]
    monkeypatch.setattr(sg, "_embedding_client", lambda: client)
    with caplog.at_level(logging.WARNING):
        assert sg._rebuild_embeddings(conn) == 32
    assert client.embed.call_count == 2
    assert conn.execute("SELECT COUNT(*) FROM skill_embeddings").fetchone()[0] == 32
    assert [r[0] for r in conn.execute("SELECT skill_name FROM skill_embeddings ORDER BY skill_name")][-1] == "skill-31"
    _assert_redacted(caplog, "batch")


def test_incomplete_batch_cannot_report_success(graph, monkeypatch, caplog):
    sg, conn = graph
    _add_skills(conn, 2)
    client = Mock()
    client.is_available.return_value = True
    client.model_name.return_value = "bge-m3"
    client.embed.return_value = [[1.0]]
    monkeypatch.setattr(sg, "_embedding_client", lambda: client)
    with caplog.at_level(logging.WARNING):
        assert sg._rebuild_embeddings(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM skill_embeddings").fetchone()[0] == 0
    _assert_redacted(caplog, "batch")


def test_unserializable_row_is_skipped_without_losing_other_rows(graph, monkeypatch, caplog):
    sg, conn = graph
    _add_skills(conn, 2)
    client = Mock()
    client.is_available.return_value = True
    client.model_name.return_value = "bge-m3"
    client.embed.return_value = [[1.0], [0.5]]
    monkeypatch.setattr(sg, "_embedding_client", lambda: client)
    backend = importlib.import_module(f"{sg.__name__}.embedding_client")
    serialize = backend.to_blob

    def fail_one(vec):
        if vec == [0.5]:
            raise ValueError(PRIVATE)
        return serialize(vec)

    monkeypatch.setattr(backend, "to_blob", fail_one)
    with caplog.at_level(logging.WARNING):
        assert sg._rebuild_embeddings(conn) == 1
    assert [r[0] for r in conn.execute("SELECT skill_name FROM skill_embeddings")] == ["skill-00"]
    _assert_redacted(caplog, "row")


def test_batch_availability_failure_is_redacted(graph, monkeypatch, caplog):
    sg, conn = graph
    _add_skills(conn, 1)
    client = Mock()
    client.is_available.side_effect = RuntimeError(PRIVATE)
    monkeypatch.setattr(sg, "_embedding_client", lambda: client)
    with caplog.at_level(logging.WARNING):
        assert sg._rebuild_embeddings(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM skill_embeddings").fetchone()[0] == 0
    _assert_redacted(caplog, "availability")


def test_drop_failure_logs_without_skill_name(graph, caplog):
    sg, conn = graph
    _add_skills(conn, 1)
    conn.execute("DROP TABLE skill_embeddings")
    with caplog.at_level(logging.WARNING):
        sg._drop_embeddings(conn, "secret-skill")
    assert "location=embedding.drop" in caplog.text
    for secret in ("secret-skill", "no such table"):
        assert secret not in caplog.text


def test_drop_success_deletes_only_requested_row(graph):
    sg, conn = graph
    _add_skills(conn, 2)
    conn.executemany(
        "INSERT INTO skill_embeddings VALUES (?, ?, ?, ?, ?)",
        [(f"skill-{i:02d}", b"xxxx", "bge-m3", 1, 1.0) for i in range(2)],
    )
    sg._drop_embeddings(conn, "skill-00")
    assert [r[0] for r in conn.execute("SELECT skill_name FROM skill_embeddings")] == ["skill-01"]


def test_search_success_and_private_failures(graph, monkeypatch, caplog):
    sg, conn = graph
    _add_skills(conn, 1)
    to_blob = importlib.import_module(f"{sg.__name__}.embedding_client").to_blob

    conn.execute(
        "INSERT INTO skill_embeddings VALUES (?, ?, ?, ?, ?)",
        ("skill-00", to_blob([1.0, 0.0]), "bge-m3", 2, 1.0),
    )
    monkeypatch.setattr(sg, "_ensure_graph", lambda: conn)
    client = Mock()
    client.is_available.return_value = True
    client.embed.return_value = [[1.0, 0.0]]
    monkeypatch.setattr(sg, "_embedding_client", lambda: client)
    assert sg._embedding_search("query")[0]["name"] == "skill-00"

    client.is_available.side_effect = RuntimeError(PRIVATE)
    with caplog.at_level(logging.WARNING):
        assert sg._embedding_search(PRIVATE) == []
    _assert_redacted(caplog, "search.availability")
    caplog.clear()
    client.is_available.side_effect = None
    client.embed.side_effect = RuntimeError(PRIVATE)
    with caplog.at_level(logging.WARNING):
        assert sg._embedding_search(PRIVATE) == []
    _assert_redacted(caplog, "search")


def test_real_client_cpu_fallback_stores_embedding(graph, monkeypatch, caplog):
    sg, conn = graph
    _add_skills(conn, 1)
    backend = importlib.import_module(f"{sg.__name__}.embedding_client")

    class FakeCpuModel:
        def __init__(self, model, device):
            assert (model, device) == ("BAAI/bge-m3", "cpu")

        def encode(self, texts, normalize_embeddings):
            assert normalize_embeddings is True
            class Vector(list):
                def tolist(self):
                    return list(self)

            return [Vector([1.0, 0.0]) for _ in texts]

    fake_package = ModuleType("sentence_transformers")
    setattr(fake_package, "SentenceTransformer", FakeCpuModel)
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_package)
    monkeypatch.setattr(backend, "_CPU_MODEL", None)
    monkeypatch.setattr(backend, "gpu_health_check", lambda *_a, **_kw: True)
    monkeypatch.setattr(backend, "_detect_protocol", lambda _endpoint: "tei")
    monkeypatch.setattr(backend, "_embed_tei", Mock(side_effect=RuntimeError(PRIVATE)))
    monkeypatch.setattr(sg, "_embedding_client", lambda: backend.EmbeddingClient(
        lambda: {"embedding_backend": "auto", "embedding_model": "bge-m3"},
    ))
    with caplog.at_level(logging.WARNING):
        assert sg._rebuild_embeddings(conn) == 1
    assert "falling back to CPU" in caplog.text
    assert PRIVATE not in caplog.text
    row = conn.execute("SELECT model, dim, vector FROM skill_embeddings").fetchone()
    assert (row["model"], row["dim"]) == ("bge-m3", 2)
    assert row["vector"] == backend.to_blob([1.0, 0.0])


@pytest.mark.parametrize("register_package", [True, False], ids=["package-spec", "bare-spec"])
def test_loader_modes_resolve_siblings_and_keep_late_patch_seams(
    tmp_path, monkeypatch, register_package,
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    name = f"skill_graph_embedding_mode_{'package' if register_package else 'bare'}"
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_PATH,
        submodule_search_locations=[str(PLUGIN_PATH.parent)] if register_package else None,
    )
    assert spec and spec.loader
    sg = importlib.util.module_from_spec(spec)
    if register_package:
        monkeypatch.setitem(sys.modules, name, sg)
    spec.loader.exec_module(sg)
    assert sg._embedding_text("first", {"description": "one"}) == "first\none"
    real_backend = sg._load_embedding_backend()
    assert real_backend is sg._load_embedding_backend()
    assert real_backend.EmbeddingClient(lambda: {})

    backend = ModuleType(f"{name}.embedding_client")
    setattr(backend, "EmbeddingClient", lambda reader: (reader(), client)[1])
    setattr(backend, "to_blob", lambda vec: struct.pack(f"{len(vec)}f", *vec))
    setattr(backend, "cosine_batch", lambda _query, blobs: [0.9 for _ in blobs])
    monkeypatch.setattr(sg, "_load_embedding_backend", lambda: backend)
    monkeypatch.setattr(sg, "_skill_graph_config", lambda: {"embedding_backend": "test"})
    client = Mock()
    client.is_available.return_value = True
    client.model_name.return_value = "test-model"
    client.embed.return_value = [[1.0, 0.0]]
    assert sg._embedding_client() is client

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        sg._init_db(conn)
        sg._migrate_db(conn)
        conn.execute("INSERT INTO skill_nodes (name, description) VALUES (?, ?)", ("first", "original"))
        # Both seams are read when invoked, not captured by the sibling loader.
        monkeypatch.setattr(sg, "_embedding_text", lambda _name, _info: "patched text")
        assert sg._compute_embedding_for_skill(conn, "first", {})
        client.embed.assert_called_with(["patched text"])
        monkeypatch.setattr(sg, "_ensure_graph", lambda: conn)
        monkeypatch.setattr(sg, "_get_node_info", lambda _conn, _name: {
            "description": "patched node", "scenes": ["coding"],
        })
        assert sg._embedding_search("search", scenes=["coding"]) == [{
            "name": "first", "description": "patched node", "scenes": ["coding"], "score": 1.0,
        }]
        # Replacing the client after one search must affect the next call.
        monkeypatch.setattr(sg, "_embedding_client", lambda: None)
        assert sg._embedding_search("search") == []
    finally:
        conn.close()
