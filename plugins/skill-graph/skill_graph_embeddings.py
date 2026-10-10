"""Embedding persistence and semantic retrieval for skill-graph."""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any


def embedding_client(*, config_reader, load_backend, logger) -> Any:
    """Build a client using the active profile's configuration."""
    try:
        backend = load_backend()
    except ImportError:
        logger.warning("skill-graph: embedding_client.py not importable")
        return None
    return backend.EmbeddingClient(config_reader)


def embedding_text(name: str, info: dict[str, Any]) -> str:
    """Build the text to embed for a skill (name + category + description + tags)."""
    parts = [name]
    if info.get("category"):
        parts.append(info["category"])
    if info.get("description"):
        parts.append(str(info["description"]))
    tags = info.get("tags") or []
    if tags:
        parts.append(" ".join(str(t) for t in tags))
    return "\n".join(parts)


def compute_embedding_for_skill(
    conn: sqlite3.Connection, name: str, info: dict[str, Any],
    *, client_factory, load_backend, text_builder, logger, redacted_exc_info,
) -> bool:
    """Compute and store one embedding; leave failures for the next rebuild."""
    client = client_factory()
    if client is None:
        return False
    try:
        to_blob = load_backend().to_blob
    except ImportError:
        logger.warning("skill-graph: embedding_client.py not importable")
        return False
    try:
        if not client.is_available():
            logger.info("skill-graph: embedding backend unavailable; embedding deferred")
            return False
        text = text_builder(name, info)
        vec = client.embed([text])[0]
        if not vec:
            logger.warning("skill-graph: empty embedding [location=embedding.single]")
            return False
        model = client.model_name()
        blob = to_blob(vec)
        dim = len(vec)
        conn.execute(
            """INSERT OR REPLACE INTO skill_embeddings
               (skill_name, vector, model, dim, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (name, blob, model, dim, time.time()),
        )
        return True
    except Exception:
        logger.warning("skill-graph: embedding failed [location=embedding.single]",
                       exc_info=redacted_exc_info())
        return False


def rebuild_embeddings(
    conn: sqlite3.Connection, *, client_factory, load_backend, text_builder,
    logger, redacted_exc_info, log_error,
) -> int:
    """Batch missing embeddings, committing complete earlier chunks on failure."""
    try:
        to_blob = load_backend().to_blob
    except ImportError:
        logger.warning("skill-graph: embedding_client.py not importable")
        return 0
    client = client_factory()
    if client is None:
        return 0
    try:
        if not client.is_available():
            logger.info("skill-graph: embedding backend unavailable — embeddings deferred")
            return 0
    except Exception:
        logger.warning("skill-graph: embedding availability failed [location=embedding.availability]",
                       exc_info=redacted_exc_info())
        return 0

    try:
        model = client.model_name()
    except Exception:
        logger.warning("skill-graph: embedding model unavailable [location=embedding.model]",
                       exc_info=redacted_exc_info())
        return 0
    rows = conn.execute(
        """SELECT n.name, n.category, n.description, n.tags
           FROM skill_nodes n
           LEFT JOIN skill_embeddings e ON e.skill_name = n.name
           WHERE e.skill_name IS NULL OR e.model != ?
             AND (n.is_deleted IS NULL OR n.is_deleted = 0)""",
        (model,),
    ).fetchall()
    if not rows:
        return 0

    texts = []
    for row in rows:
        name = row["name"]
        info = {
            "category": row["category"],
            "description": row["description"],
            "tags": json.loads(row["tags"]) if row["tags"] else [],
        }
        texts.append(text_builder(name, info))

    logger.info("skill-graph: computing embeddings for %d skills", len(texts))
    # TEI max_client_batch_size is 32.
    BATCH = 32
    vecs: list[list[float]] = []
    for i in range(0, len(texts), BATCH):
        chunk = texts[i : i + BATCH]
        try:
            chunk_vecs = client.embed(chunk)
            if len(chunk_vecs) != len(chunk) or any(not vec for vec in chunk_vecs):
                raise ValueError("incomplete embedding batch")
            vecs.extend(chunk_vecs)
        except Exception:
            logger.warning("skill-graph: batch embedding failed [location=embedding.batch]",
                           exc_info=redacted_exc_info())
            break

    now = time.time()
    count = 0
    expected_dim = len(vecs[0]) if vecs else 0
    for row, vec in zip(rows, vecs):
        if len(vec) != expected_dim:
            logger.warning("skill-graph: inconsistent embedding dimension [location=embedding.row]")
            continue
        try:
            blob = to_blob(vec)
            conn.execute(
                """INSERT OR REPLACE INTO skill_embeddings
                   (skill_name, vector, model, dim, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (row["name"], blob, model, len(vec), now),
            )
            count += 1
        except Exception:
            logger.warning("skill-graph: embedding row failed [location=embedding.row]",
                           exc_info=redacted_exc_info())
    try:
        conn.commit()
    except sqlite3.Error as exc:
        log_error("skill-graph: embedding commit failed [location=embedding.commit]", exc)
        conn.rollback()
        return 0
    logger.info("skill-graph: stored %d embeddings", count)
    return count


def drop_embeddings(conn: sqlite3.Connection, name: str, *, log_error) -> None:
    """Remove embedding rows for a skill (on delete)."""
    try:
        conn.execute("DELETE FROM skill_embeddings WHERE skill_name = ?", (name,))
    except sqlite3.Error as exc:
        log_error("skill-graph: embedding removal failed [location=embedding.drop]", exc)


def embedding_search(
    query: str, topk: int = 5, scenes: list[str] | None = None, *,
    client_factory, load_backend, ensure_graph, get_node_info, logger,
    redacted_exc_info,
) -> list[dict[str, Any]]:
    """Return top-k cosine results with a soft scene boost, or [] if unavailable."""
    try:
        backend = load_backend()
        # Keep the same dependency check as the original implementation.
        backend.EmbeddingClient
        to_blob, cosine_batch = backend.to_blob, backend.cosine_batch
    except ImportError:
        return []
    client = client_factory()
    if client is None:
        return []
    try:
        if not client.is_available():
            return []
    except Exception:
        logger.warning("skill-graph: search backend unavailable [location=embedding.search.availability]",
                       exc_info=redacted_exc_info())
        return []

    try:
        conn = ensure_graph()
        conn.row_factory = sqlite3.Row
        qvec = client.embed([query])[0]
        if not qvec:
            return []
        qblob = to_blob(qvec)
        rows = conn.execute("SELECT skill_name, vector FROM skill_embeddings").fetchall()
        if not rows:
            return []
        names = [r["skill_name"] for r in rows]
        blobs = [r["vector"] for r in rows]
        scores = cosine_batch(qblob, blobs)
        ranked = sorted(zip(names, scores), key=lambda x: -x[1])

        scenes_set = set(scenes or [])
        out: list[dict[str, Any]] = []
        for name, score in ranked[: topk * 3]:
            info = get_node_info(conn, name) or {}
            skill_scenes = info.get("scenes") or []
            effective = score
            if scenes_set and any(s in scenes_set for s in skill_scenes):
                effective = min(1.0, score * 1.3)
            out.append({
                "name": name,
                "description": info.get("description", ""),
                "score": round(effective, 4),
                "scenes": skill_scenes,
            })
        out.sort(key=lambda x: -x["score"])
        return out[:topk]
    except Exception:
        logger.warning("skill-graph: embedding search failed [location=embedding.search]",
                       exc_info=redacted_exc_info())
        return []
