"""Lexical search phases, ranking, and query helpers for the skill graph."""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

def search_graph(query: str, conn: sqlite3.Connection, limit: int = 10,
                 scenes: list[str] | None = None, *, fts_query, extract_terms,
                 get_node_info, boost_search_results, boost_scene_results,
                 fallback_search) -> list[dict[str, Any]]:
    """Search the skill graph by intent query.

    If ``scenes`` is provided, skill results matching those scenes get a
    ×1.3 score boost in Phase 5 (soft weighting, not hard filtering).
    """
    results: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()

    # Phase 1: FTS5 direct search — include BM25 rank for normalized scoring
    fts_query = fts_query(query)
    if fts_query:
        cursor = conn.execute(
            """SELECT n.name, n.category, n.description, n.tags, n.file_path, f.rank
               FROM skill_fts f
               JOIN skill_nodes n ON f.name = n.name
               WHERE skill_fts MATCH ?
                 AND (n.is_deleted IS NULL OR n.is_deleted = 0)
               ORDER BY rank
               LIMIT ?""",
            (fts_query, limit * 2),
        )
        for row in cursor:
            name = row["name"]
            seen.add(name)
            tags = json.loads(row["tags"]) if row["tags"] else []
            # Normalize BM25: rank is negative (closer to 0 = better)
            bm25_score = 1.0 / (1.0 + abs(row["rank"]))
            results[name] = {
                "name": name,
                "category": row["category"],
                "description": row["description"],
                "tags": tags,
                "file_path": row["file_path"],
                "relevance": "direct",
                "relationship_chain": [],
                "score": bm25_score,
            }

    # Phase 2: Graph expansion — only fills gaps not found by FTS5
    expansion_queue = list(seen)
    while expansion_queue and len(results) < limit * 3:
        current = expansion_queue.pop(0)
        cursor = conn.execute(
            """SELECT e.target, e.rel_type, e.properties, n.category, n.description
               FROM skill_edges e
               JOIN skill_nodes n ON e.target = n.name
               WHERE e.source = ?
               ORDER BY e.rel_type
               LIMIT 5""",
            (current,),
        )
        for row in cursor:
            target = row["target"]
            if target in seen:
                continue
            seen.add(target)
            props = json.loads(row["properties"]) if row["properties"] else {}
            rel_type = row["rel_type"]
            reason = props.get("reason", f"via {rel_type}")
            _score = 0.8 if rel_type == "supersedes" else 0.5
            results[target] = {
                "name": target,
                "category": row["category"] or "",
                "description": row["description"] or "",
                "tags": [],
                "file_path": "",
                "relevance": "expansion",
                "relationship_chain": [f"{current} --({rel_type})--> {target}: {reason}"],
                "score": _score,
            }
            expansion_queue.append(target)

    # Phase 3: Tag match (existing) — uses its own dedup set so it doesn't
    # block Phase 4 from finding higher-scoring term matches.
    _tag_seen: set[str] = set()
    terms = extract_terms(query)
    for term in terms:
        cursor = conn.execute(
            """SELECT name FROM skill_nodes WHERE instr(tags, ?) > 0 AND (is_deleted IS NULL OR is_deleted = 0)""",
            (json.dumps(term),),
        )
        for row in cursor:
            if row["name"] not in _tag_seen:
                _tag_seen.add(row["name"])
                info = get_node_info(conn, row["name"])
                if info:
                    info["relevance"] = "tag_match"
                    info["score"] = 0.7
                    results[info["name"]] = info

    # Phase 4: Term table match — direct term→skill lookup from the
    # skill_terms table (auto-extracted from name, tags, description).
    # This catches Chinese terms and split-name parts that FTS5 misses.
    for term in terms:
        if seen:
            cursor = conn.execute(
                """SELECT t.skill_name, t.strength, t.source, n.category, n.description
                   FROM skill_terms t
                   JOIN skill_nodes n ON t.skill_name = n.name AND (n.is_deleted IS NULL OR n.is_deleted = 0)
                   WHERE t.term = ? AND t.skill_name NOT IN ({})
                   ORDER BY t.strength DESC
                   LIMIT 5""".format(",".join("?" for _ in seen)),
                (term.lower(),) + tuple(seen),
            )
        else:
            cursor = conn.execute(
                """SELECT t.skill_name, t.strength, t.source, n.category, n.description
                   FROM skill_terms t
                   JOIN skill_nodes n ON t.skill_name = n.name AND (n.is_deleted IS NULL OR n.is_deleted = 0)
                   WHERE t.term = ?
                   ORDER BY t.strength DESC
                   LIMIT 5""",
                (term.lower(),),
            )
        for row in cursor:
            sname = row["skill_name"]
            _term_score = 0.8 * row["strength"]
            if sname in results:
                # Don't overwrite — take the higher score
                if results[sname]["score"] < _term_score:
                    results[sname]["score"] = _term_score
                    results[sname]["relevance"] = "term_match"
                    results[sname]["relationship_chain"] = [
                        f"term[{term}] → {sname} (strength={row['strength']}, source={row['source']})"
                    ]
                continue
            seen.add(sname)
            results[sname] = {
                "name": sname,
                "category": row["category"] or "",
                "description": row["description"] or "",
                "tags": [],
                "file_path": "",
                "relevance": "term_match",
                "relationship_chain": [f"term[{term}] → {sname} (strength={row['strength']}, source={row['source']})"],
                "score": 0.8 * row["strength"],
            }

    # Phase 5: Term-based scoring boost + search stats
    boost_search_results(conn, results, terms)
    if scenes:
        boost_scene_results(conn, results, scenes)

    sorted_results = sorted(results.values(), key=lambda r: -r["score"])
    if not sorted_results:
        return fallback_search(query, conn, limit)
    return sorted_results[:limit]


def boost_search_results(
    conn: sqlite3.Connection, results: dict[str, dict[str, Any]], terms: list[str],
    *, log_error,
) -> None:
    """Apply confidence-weighted term stats without losing search results on stats errors."""
    _norm_terms = [t.lower() for t in terms]
    for sname, r in results.items():
        _placeholders = ",".join("?" for _ in _norm_terms)
        _term_rows = conn.execute(
            f"SELECT term FROM skill_terms WHERE skill_name = ? AND term IN ({_placeholders})",
            (sname,) + tuple(_norm_terms),
        ).fetchall()
        matched_terms = [row["term"] for row in _term_rows]
        if matched_terms:
            for mt in matched_terms:
                try:
                    conn.execute(
                        """INSERT INTO skill_term_stats (skill_name, term, search_count, load_count)
                           VALUES (?, ?, 1, 0)
                           ON CONFLICT(skill_name, term) DO UPDATE SET search_count = search_count + 1""",
                        (sname, mt),
                    )
                except sqlite3.Error as exc:
                    log_error("skill-graph: search stat update failed", exc)
            try:
                rows = conn.execute(
                    """SELECT term, load_count, search_count, success_count FROM skill_term_stats
                       WHERE skill_name = ? AND term IN ({})"""
                    .format(",".join("?" for _ in matched_terms)),
                    (sname,) + tuple(mt.lower() for mt in matched_terms),
                ).fetchall()
                if rows:
                    import math as _m
                    _avg_eff = sum(
                        (r["success_count"] * 2 + r["load_count"]) / max(r["search_count"] * 3, 1)
                        for r in rows
                    ) / len(rows)
                    _confidence = 1 - _m.pow(0.5, sum(r["search_count"] for r in rows) / max(len(rows), 1) / 5)
                    _adj = (_avg_eff - 0.5) * 2
                    _tanh = _adj / (1 + abs(_adj) * 0.5)  # tanh approximation
                    r["score"] *= (1.0 + 0.1 * _tanh * _confidence)
            except (sqlite3.Error, ArithmeticError, TypeError, ValueError) as exc:
                log_error("skill-graph: search stat boost failed", exc)
    conn.commit()


def boost_scene_results(
    conn: sqlite3.Connection, results: dict[str, dict[str, Any]], scenes: list[str],
    *, log_error,
) -> None:
    """Soft-boost matching scenes while preserving the parent's isolated search stats."""
    scene_set = {scene.lower() for scene in scenes}
    for sname, result in results.items():
        row = conn.execute("SELECT scenes FROM skill_nodes WHERE name = ?", (sname,)).fetchone()
        if not row:
            continue
        try:
            skill_scenes = json.loads(row[0])
            if isinstance(skill_scenes, list) and skill_scenes:
                skill_set = {scene.lower() for scene in skill_scenes}
                if skill_set != {"common"} and scene_set & skill_set:
                    result["score"] *= 1.3
                    result["_scene_boost"] = True
        except (json.JSONDecodeError, TypeError, AttributeError) as exc:
            log_error("skill-graph: invalid scene metadata", exc)


def fallback_search(query: str, conn: sqlite3.Connection, limit: int = 10, *, extract_terms) -> list[dict[str, Any]]:
    """Broad fallback when the primary search returns nothing.

    Returns skills whose name, tags, or description contain any of the
    query terms, ordered by term strength. This catches skills that FTS5
    and exact-term matching miss (e.g. stemming mismatches, partial words).
    """
    terms = extract_terms(query)
    if not terms:
        # No parseable terms — return top skills by name
        cursor = conn.execute(
            "SELECT name, category, description, tags, file_path FROM skill_nodes WHERE (is_deleted IS NULL OR is_deleted = 0) ORDER BY name LIMIT ?",
            (limit,),
        )
        fallback = []
        for row in cursor:
            fallback.append({
                "name": row["name"],
                "category": row["category"] or "",
                "description": row["description"] or "",
                "tags": json.loads(row["tags"]) if row["tags"] else [],
                "file_path": row["file_path"],
                "relevance": "fallback",
                "score": 0.1,
            })
        return fallback

    results: dict[str, dict[str, Any]] = {}
    for term in terms:
        cursor = conn.execute(
            """SELECT n.name, n.category, n.description, n.tags, n.file_path
               FROM skill_nodes n
               WHERE (n.is_deleted IS NULL OR n.is_deleted = 0)
                 AND (instr(n.name, ?) > 0
                  OR instr(n.description, ?) > 0
                  OR instr(n.tags, ?) > 0)
               LIMIT ?""",
            (term, term, json.dumps(term), limit),
        )
        for row in cursor:
            if row["name"] not in results:
                results[row["name"]] = {
                    "name": row["name"],
                    "category": row["category"] or "",
                    "description": row["description"] or "",
                    "tags": json.loads(row["tags"]) if row["tags"] else [],
                    "file_path": row["file_path"],
                    "relevance": "fallback",
                    "score": 0.2,
                }
    sorted_results = sorted(results.values(), key=lambda r: -r["score"])
    return sorted_results[:limit]


def fts_query(query: str) -> str:
    """Convert a natural language query to an FTS5 query string.

    For ASCII-heavy queries, builds an AND query from multi-char terms.
    For Chinese-heavy queries (single-char tokens from unicode61), returns
    empty so _search_graph falls through to term-table matching (Phase 4).
    """
    terms = re.findall(r"[a-zA-Z0-9_\u4e00-\u9fff_-]+", query.lower())
    # Split Chinese multi-char terms into individual characters for FTS5
    # compatibility, since unicode61 tokenizes each CJK char separately.
    flat: list[str] = []
    for t in terms:
        if re.match(r"^[\u4e00-\u9fff]+$", t) and len(t) > 1:
            flat.extend(list(t))  # each CJK char is its own token
        else:
            flat.append(t)
    has_ascii = any(t.isascii() for t in flat)
    if has_ascii:
        # Quote each term so FTS5 treats hyphens and other special chars
        # as literal text, not column-filter operators.
        return " AND ".join(f'"{t}"' for t in flat if len(t) > 1)
    return " OR ".join(t for t in flat if len(t) > 1) if flat else ""


def extract_terms(query: str) -> list[str]:
    """Extract meaningful search terms from a query string."""
    terms = re.findall(r"[a-zA-Z0-9_\u4e00-\u9fff_-]+", query.lower())
    return [t for t in terms if len(t) > 1]


def get_node_info(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    """Fetch full node info from the database."""
    cursor = conn.execute(
        """SELECT name, category, description, tags, file_path, needs_organizing
           FROM skill_nodes WHERE name = ? AND (is_deleted IS NULL OR is_deleted = 0)""",
        (name,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {
        "name": row["name"],
        "category": row["category"],
        "description": row["description"],
        "tags": json.loads(row["tags"]) if row["tags"] else [],
        "file_path": row["file_path"],
        "needs_organizing": bool(dict(row).get("needs_organizing")) or False,
    }
