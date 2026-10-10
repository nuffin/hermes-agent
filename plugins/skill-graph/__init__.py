"""
Skill Graph plugin — knowledge graph for skills discovery.

Builds a SQLite graph from SKILL.md relations, exposes:
- ``skill_graph_search`` — find skills by intent
- ``skill_load`` — load a skill's full content from graph-managed dirs

Maintains the graph incrementally across sessions.

SKILL.md relations format (frontmatter):
    metadata:
      hermes:
        relations:
          - type: depends_on
            target: another-skill
            properties:
              reason: "why"
              strength: strong|medium|weak

Config (in config.yaml):
    skills:
      config:
        skill-graph:
          source_dirs:
            - ~/path/to/extra/skills
"""

from __future__ import annotations
import json
import logging
import os
import re
import sqlite3
import threading
import time
import traceback
import hermes_yaml as yaml
from pathlib import Path
from typing import Any, Mapping

from hermes_constants import (
    get_bundled_skills_dir,
    get_hermes_home,
    get_skills_dir,
)

logger = logging.getLogger(__name__)

def _log_fallback_exception(message: str, exc: Exception) -> None:
    """Log stack locations without exception text or source lines containing secrets."""
    stack = " -> ".join(
        f"{frame.f_code.co_name}:{line}"
        for frame, line in traceback.walk_tb(exc.__traceback__)
    )
    logger.error("%s (stack: %s)", message, stack)

# ── Constants ───────────────────────────────────────────────────────────────

DEFAULT_RELATION_TYPES = {
    "depends_on", "supported_by", "alternative_to", "complemented_by",
    "similar_to", "belongs_to_domain", "used_in_workflow", "supersedes",
}

GRAPH_DB_FILENAME = "skill-graph.db"
_RUNTIME_SOURCE_DIRS: list[Path] = []


def _skill_graph_config() -> dict[str, Any]:
    """Return the active profile's skill-graph settings mapping."""
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
        value = (((config.get("skills") or {}).get("config") or {}).get("skill-graph") or {})
        return value if isinstance(value, dict) else {}
    except (ImportError, OSError, TypeError, ValueError, AttributeError, RuntimeError, yaml.YAMLError) as exc:
        _log_fallback_exception("skill-graph: could not read profile configuration", exc)
        return {}


def _resolve_config_path(value: str | os.PathLike[str]) -> Path:
    """Expand a configured path; relative paths are scoped to HERMES_HOME."""
    path = Path(os.path.expandvars(os.path.expanduser(str(value))))
    return (path if path.is_absolute() else get_hermes_home() / path).resolve()


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path.absolute())
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _is_under(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError):
        return False

# ── SQLite schema ──────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS skill_nodes (
    name        TEXT PRIMARY KEY,
    category    TEXT DEFAULT '',
    description TEXT DEFAULT '',
    tags        TEXT DEFAULT '[]',      -- JSON array
    scenes      TEXT DEFAULT '[]',      -- JSON array, from metadata.hermes.scenes
    enriched INTEGER DEFAULT 0,      -- 1 if tags/scenes were LLM-generated
    enriched_at TEXT DEFAULT NULL,    -- timestamp of last successful enrichment
    file_path   TEXT DEFAULT '',
    content_hash TEXT DEFAULT '',
    last_parsed REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS skill_edges (
    source      TEXT NOT NULL REFERENCES skill_nodes(name),
    target      TEXT NOT NULL REFERENCES skill_nodes(name),
    rel_type    TEXT NOT NULL,
    properties  TEXT DEFAULT '{}',       -- JSON dict
    PRIMARY KEY (source, target, rel_type)
);

CREATE INDEX IF NOT EXISTS idx_edges_source ON skill_edges(source);
CREATE INDEX IF NOT EXISTS idx_edges_target ON skill_edges(target);
CREATE INDEX IF NOT EXISTS idx_edges_type   ON skill_edges(rel_type);

CREATE VIRTUAL TABLE IF NOT EXISTS skill_fts USING fts5(
    name, category, description, tags, scenes,
    tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS skill_terms (
    term        TEXT NOT NULL,
    skill_name  TEXT NOT NULL REFERENCES skill_nodes(name),
    strength    REAL DEFAULT 1.0,
    source      TEXT DEFAULT 'tag',   -- 'name' | 'tag' | 'description'
    PRIMARY KEY (term, skill_name)
);

CREATE INDEX IF NOT EXISTS idx_terms_term ON skill_terms(term);
CREATE INDEX IF NOT EXISTS idx_terms_skill ON skill_terms(skill_name);

CREATE TABLE IF NOT EXISTS skill_term_stats (
    skill_name   TEXT NOT NULL REFERENCES skill_nodes(name),
    term         TEXT NOT NULL,
    search_count INTEGER DEFAULT 1,
    load_count   INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    last_searched TEXT,
    last_loaded   TEXT,
    PRIMARY KEY (skill_name, term)
);

CREATE TABLE IF NOT EXISTS skill_embeddings (
    skill_name  TEXT PRIMARY KEY REFERENCES skill_nodes(name),
    vector      BLOB NOT NULL,          -- 1024 × float32 = 4096 bytes
    model       TEXT NOT NULL,          -- 'bge-m3' (record model for invalidation)
    dim         INTEGER NOT NULL,       -- 1024
    updated_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_embeddings_model ON skill_embeddings(model);
"""

# ── Database helpers ────────────────────────────────────────────────────────


def _db_path() -> Path:
    """Return path to graph DB under the active Hermes home.

    Priority:
    1. skills.config.skill-graph.db_path from config.yaml (explicit override)
    2. HERMES_BUNDLED_PLUGINS → root level (profiles/<name>/skill-graph.db)
    3. Default → under personal/ (profiles/<name>/personal/skill-graph.db)
    """
    # Priority 1: config.yaml override
    try:
        raw = _skill_graph_config().get("db_path")
        if raw:
            return _resolve_config_path(raw)
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        _log_fallback_exception("skill-graph: invalid DB path override; using profile default", exc)

    hermes_home = get_hermes_home()
    if os.environ.get("HERMES_BUNDLED_PLUGINS"):
        return hermes_home / GRAPH_DB_FILENAME
    return hermes_home / "personal" / GRAPH_DB_FILENAME


def _get_conn() -> sqlite3.Connection:
    """Get a thread-safe connection."""
    db_path = _db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    """Ensure schema exists."""
    conn.executescript(SCHEMA_SQL)
    conn.commit()


# ── Skill directory discovery ──────────────────────────────────────────────


def _read_source_dirs_from_config() -> list[Path]:
    """Read ``skills.config.skill-graph.source_dirs`` from config.yaml.

    Supports ``:read-only`` suffix per entry (e.g. ``~/bundled/skills:read-only``).
    The suffix is stripped from the returned path but tracked separately via
    ``_get_read_only_source_dirs()``.
    """
    dirs: list[Path] = []
    try:
        raw = _skill_graph_config().get("source_dirs", [])
        if isinstance(raw, list):
            for entry in raw:
                entry_str = str(entry)
                clean = entry_str.rsplit(":read-only", 1)[0].strip()
                p = _resolve_config_path(clean)
                if p.is_dir():
                    dirs.append(p)
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        _log_fallback_exception("skill-graph: could not resolve configured source directories", exc)
    return dirs


def _get_read_only_source_dirs() -> set[Path]:
    """Return configured source directories marked ``:read-only``."""
    ro_dirs: set[Path] = set()
    try:
        raw = _skill_graph_config().get("source_dirs", [])
        if isinstance(raw, list):
            for entry in raw:
                entry_str = str(entry)
                if entry_str.rstrip().endswith(":read-only"):
                    clean = entry_str.rsplit(":read-only", 1)[0].strip()
                    path = _resolve_config_path(clean)
                    if path.is_dir():
                        ro_dirs.add(path)
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        _log_fallback_exception("skill-graph: could not resolve read-only source directories", exc)
    return ro_dirs


def _is_read_only_skill(skill_path: str) -> bool:
    """Check if a skill's file path is under a read-only source dir."""
    ro_dirs = _get_read_only_source_dirs()
    if not ro_dirs:
        return False
    resolved = os.path.realpath(skill_path)
    for ro_dir in ro_dirs:
        ro_str = str(ro_dir)
        if resolved.startswith(ro_str + os.sep) or resolved == ro_str:
            return True
    return False


def _find_all_skills_dirs() -> list[Path]:
    """Return list of directories to scan for SKILL.md files.

    Precedence is profile-local, bundled, agent-created, then explicitly
    configured sources. No implicit global/default-profile directory is read.
    """
    hermes_home = get_hermes_home()
    bundled_default = Path(__file__).resolve().parents[2] / "skills"
    dirs = [
        get_skills_dir(),
        get_bundled_skills_dir(bundled_default),
        hermes_home / "skill-graph" / "agent-created",
        *_read_source_dirs_from_config(),
        *_RUNTIME_SOURCE_DIRS,
    ]

    try:
        from agent.skill_utils import get_external_skills_dirs

        dirs.extend(get_external_skills_dirs())
    except (ImportError, OSError, TypeError, RuntimeError) as exc:
        _log_fallback_exception("skill-graph: could not discover external skill directories", exc)
    return [path for path in _dedupe_paths(dirs) if path.is_dir()]


def _find_skill_path(name: str) -> Path | None:
    """Find a SKILL.md by name across all configured dirs.

    Returns the first match in :func:`_find_all_skills_dirs` precedence order.
    """
    skill_dirs = _find_all_skills_dirs()
    skills = _scan_skill_mds(skill_dirs)
    for n, path in skills:
        if n == name:
            return path
    return None


# ── SKILL.md scanner & parser ──────────────────────────────────────────────


def _scan_skill_mds(skill_dirs: list[Path]) -> list[tuple[str, Path]]:
    """Scan all skill directories for SKILL.md files.

    Deduplicates by real path (resolving symlinks) so the same skill
    discovered via different routes (symlink vs original, multiple
    base_dirs) is only indexed once.

    Returns list of (skill_name, skill_md_path).
    """
    results: list[tuple[str, Path]] = []
    seen_realpaths: set[str] = set()
    seen_names: set[str] = set()

    for base_dir in skill_dirs:
        if not base_dir.exists():
            continue
        for cat_dir in base_dir.iterdir():
            if not cat_dir.is_dir() or cat_dir.name.startswith("."):
                continue
            # Flat layout: <name>/SKILL.md
            skill_md = cat_dir / "SKILL.md"
            if skill_md.exists():
                name = cat_dir.name
                real = os.path.realpath(skill_md)
                dedup_key = f"{name}\x00{real}"
                if dedup_key not in seen_realpaths:
                    seen_realpaths.add(dedup_key)
                    seen_names.add(name)
                    results.append((name, skill_md))
                continue
            # Nested layout: <cat>/<name>/SKILL.md
            for name_dir in cat_dir.iterdir():
                if not name_dir.is_dir() or name_dir.name.startswith("."):
                    continue
                skill_md = name_dir / "SKILL.md"
                if skill_md.exists():
                    name = name_dir.name
                    real = os.path.realpath(skill_md)
                    dedup_key = f"{name}\x00{real}"
                    if dedup_key not in seen_realpaths:
                        seen_realpaths.add(dedup_key)
                        seen_names.add(name)
                        results.append((name, skill_md))
    return results


def _parse_skill_md(path: Path) -> dict[str, Any]:
    """Parse SKILL.md and extract metadata for the graph.

    Returns dict with keys: name, category, description, tags, relations, content_hash
    """
    result: dict[str, Any] = {
        "name": path.parent.name,
        "category": "",
        "description": "",
        "tags": [],
        "scenes": [],
        "relations": [],
        "content_hash": "",
    }

    try:
        content = path.read_text(encoding="utf-8-sig", errors="replace")
        result["content_hash"] = str(hash(content))

        content_str = content.lstrip("\ufeff")
        if content_str.startswith("---"):
            end = content_str.find("---", 3)
            if end != -1:
                frontmatter = content_str[3:end].strip()
                try:
                    meta = yaml.safe_load(frontmatter) or {}
                except yaml.YAMLError:
                    meta = {}

                result["name"] = meta.get("name", result["name"])
                result["category"] = meta.get("category", "") or \
                    meta.get("metadata", {}).get("hermes", {}).get("category", "")
                result["description"] = meta.get("description", "")

                tags = meta.get("metadata", {}).get("hermes", {}).get("tags", [])
                if isinstance(tags, str):
                    tags = [t.strip() for t in tags.split(",") if t.strip()]
                result["tags"] = [str(tag) for tag in tags] if isinstance(tags, list) else []

                scenes = meta.get("metadata", {}).get("hermes", {}).get("scenes", [])
                if isinstance(scenes, str):
                    scenes = [s.strip() for s in scenes.split(",") if s.strip()]
                result["scenes"] = [str(scene) for scene in scenes] if isinstance(scenes, list) else []

                relations = meta.get("metadata", {}).get("hermes", {}).get("relations", [])
                if isinstance(relations, list):
                    result["relations"] = relations

                related = meta.get("metadata", {}).get("hermes", {}).get("related_skills", [])
                if isinstance(related, str):
                    related = [t.strip() for t in related.split(",") if t.strip()]
                if isinstance(related, list):
                    for rs in related:
                        if not any(r.get("target") == rs for r in result["relations"]):
                            result["relations"].append({
                                "type": "similar_to",
                                "target": rs,
                                "properties": {"source": "legacy_related_skills"},
                            })
    except (OSError, TypeError, ValueError, AttributeError, yaml.YAMLError) as exc:
        _log_fallback_exception("skill-graph: could not parse skill metadata", exc)

    return result


# ── Term extraction ─────────────────────────────────────────────────────────

# English stop words filtered from description terms
_STOP_WORDS: set[str] = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "dare", "ought",
    "of", "in", "on", "at", "to", "for", "with", "by", "from", "as",
    "into", "through", "during", "before", "after", "above", "below",
    "between", "out", "off", "over", "under", "again", "further", "then",
    "once", "here", "there", "when", "where", "why", "how", "all", "each",
    "every", "both", "few", "more", "most", "other", "some", "such", "no",
    "nor", "not", "only", "own", "same", "so", "than", "too", "very",
    "and", "but", "or", "if", "because", "until", "while", "about",
    "using", "via", "its", "their", "your", "this", "that", "these",
    "those", "which", "what", "who", "whom", "make", "made", "set", "get",
}


def _extract_skill_terms(name: str, tags: list[str], description: str) -> list[tuple[str, float, str]]:
    """Extract (term, strength, source) triples from a skill's metadata.

    Sources:
      - name: split on ``-_``, each part strength=1.0
      - tags: each tag verbatim, strength=1.0
      - description: English words (3+ chars) + Chinese phrases (2-6 chars), strength=0.7
    """
    seen: dict[str, tuple[float, str]] = {}

    def add(t: str, s: float, src: str):
        t = t.strip().lower()
        if t and (t not in seen or seen[t][0] < s):
            seen[t] = (s, src)

    # From name
    clean = name.replace("_", "-")
    for part in clean.split("-"):
        if len(part) > 1:
            add(part, 1.0, "name")

    # From tags
    for tag in tags:
        t = tag.strip().lower().replace("_", "-")
        if len(t) > 0:
            add(t, 1.0, "tag")

    # From description
    if description:
        for w in re.findall(r"[a-zA-Z][a-zA-Z]{2,}", description):
            w = w.lower()
            if w not in _STOP_WORDS and len(w) > 2:
                add(w, 0.7, "description")
        for c in re.findall(r"[\u4e00-\u9fff]{2,6}", description):
            if len(c) > 1:
                add(c, 0.7, "description")

    return [(t, s, src) for t, (s, src) in seen.items()]


# ── Graph sync ──────────────────────────────────────────────────────────────


def _dedup_skills(skills: list[tuple[str, Path]]) -> dict[str, Path]:
    """Deduplicate by name and real path, preserving source precedence."""
    deduped: dict[str, Path] = {}
    seen_realpaths: set[str] = set()
    for name, path in skills:
        real = os.path.realpath(path)
        if name in deduped or real in seen_realpaths:
            continue
        seen_realpaths.add(real)
        deduped[name] = path
    return deduped


def _upsert_skill(conn: sqlite3.Connection, name: str, path: Path, now: float) -> dict[str, Any]:
    """Parse a SKILL.md and upsert its node + edges + FTS into the graph."""
    info = _parse_skill_md(path)
    tags_json = json.dumps(info["tags"], ensure_ascii=False)

    conn.execute(
        """INSERT OR REPLACE INTO skill_nodes
           (name, category, description, tags, scenes, file_path, content_hash, last_parsed)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, info["category"], info["description"],
         tags_json, json.dumps(info["scenes"], ensure_ascii=False), str(path), info["content_hash"], now),
    )
    conn.execute("DELETE FROM skill_edges WHERE source = ?", (name,))

    for rel in info.get("relations", []):
        rel_type = rel.get("type", "similar_to")
        target = rel.get("target", "")
        props = rel.get("properties", {})
        if not target:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO skill_edges (source, target, rel_type, properties) VALUES (?, ?, ?, ?)",
            (name, target, rel_type, json.dumps(props, ensure_ascii=False)),
        )
        reverse_type = _reverse_type(rel_type)
        if reverse_type:
            reverse_props = {"inferred": True, "reason": f"reverse of {rel_type}"}
            conn.execute(
                "INSERT OR IGNORE INTO skill_edges (source, target, rel_type, properties) VALUES (?, ?, ?, ?)",
                (target, name, reverse_type, json.dumps(reverse_props)),
            )

    tags_text = " ".join(info.get("tags", []))
    scenes_text = " ".join(info.get("scenes", []))
    conn.execute("DELETE FROM skill_fts WHERE name = ?", (name,))
    conn.execute(
        "INSERT INTO skill_fts (name, category, description, tags, scenes) VALUES (?, ?, ?, ?, ?)",
        (name, info.get("category", ""), info.get("description", ""), tags_text, scenes_text),
    )

    # Upsert terms
    conn.execute("DELETE FROM skill_terms WHERE skill_name = ?", (name,))
    terms = _extract_skill_terms(name, info.get("tags", []), info.get("description", ""))
    for term_text, strength, source in terms:
        conn.execute(
            "INSERT OR IGNORE INTO skill_terms (term, skill_name, strength, source) VALUES (?, ?, ?, ?)",
            (term_text, name, strength, source),
        )


    # Enrichment detection: mark skills missing tags or scenes
    if _needs_enrichment(info):
        conn.execute(
            "UPDATE skill_nodes SET enriched = 0 WHERE name = ?",
            (name,),
        )
    else:
        conn.execute(
            "UPDATE skill_nodes SET enriched = 1 WHERE name = ?",
            (name,),
        )

    return info


# ── Embedding ──────────────────────────────────────────────────────────────

# Load adjacent siblings even when a caller uses spec_from_file_location
# without inserting the plugin package in sys.modules.
try:
    from . import skill_graph_embeddings as _embedding_impl
except ImportError:
    import importlib.util as _embedding_importlib

    _embedding_spec = _embedding_importlib.spec_from_file_location(
        f"{__name__}.skill_graph_embeddings", Path(__file__).with_name("skill_graph_embeddings.py")
    )
    assert _embedding_spec is not None and _embedding_spec.loader is not None
    _embedding_impl = _embedding_importlib.module_from_spec(_embedding_spec)
    _embedding_spec.loader.exec_module(_embedding_impl)


_bare_embedding_backend: Any = None


def _load_embedding_backend() -> Any:
    # Resolve the adjacent backend in both package and bare-spec loader modes.
    try:
        from . import embedding_client
        return embedding_client
    except ImportError:
        global _bare_embedding_backend
        if _bare_embedding_backend is None:
            import importlib.util as importlib_util

            spec = importlib_util.spec_from_file_location(
                f"{__name__}.embedding_client", Path(__file__).with_name("embedding_client.py")
            )
            if spec is None or spec.loader is None:
                raise ImportError("embedding_client.py not importable")
            module = importlib_util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _bare_embedding_backend = module
        return _bare_embedding_backend


def _embedding_client() -> Any:
    return _embedding_impl.embedding_client(
        config_reader=_skill_graph_config, load_backend=_load_embedding_backend, logger=logger,
    )


def _embedding_text(name: str, info: dict[str, Any]) -> str:
    return _embedding_impl.embedding_text(name, info)


def _compute_embedding_for_skill(
    conn: sqlite3.Connection, name: str, info: dict[str, Any]
) -> bool:
    return _embedding_impl.compute_embedding_for_skill(
        conn, name, info, client_factory=_embedding_client,
        load_backend=_load_embedding_backend, text_builder=_embedding_text,
        logger=logger, redacted_exc_info=_redacted_candidate_exc_info,
    )


def _rebuild_embeddings(conn: sqlite3.Connection) -> int:
    return _embedding_impl.rebuild_embeddings(
        conn, client_factory=_embedding_client, load_backend=_load_embedding_backend,
        text_builder=_embedding_text, logger=logger,
        redacted_exc_info=_redacted_candidate_exc_info, log_error=_log_fallback_exception,
    )


def _drop_embeddings(conn: sqlite3.Connection, name: str) -> None:
    return _embedding_impl.drop_embeddings(conn, name, log_error=_log_fallback_exception)


def _embedding_search(
    query: str, topk: int = 5, scenes: list[str] | None = None
) -> list[dict[str, Any]]:
    return _embedding_impl.embedding_search(
        query, topk, scenes, client_factory=_embedding_client,
        load_backend=_load_embedding_backend, ensure_graph=_ensure_graph,
        get_node_info=_get_node_info, logger=logger,
        redacted_exc_info=_redacted_candidate_exc_info,
    )

# ── pre_llm_call: candidate injection (Plan A) ─────────────────────────────

try:
    from . import skill_graph_candidates as _candidate_impl
except ImportError:
    import importlib.util as _candidate_importlib

    _candidate_spec = _candidate_importlib.spec_from_file_location(
        f"{__name__}.skill_graph_candidates", Path(__file__).with_name("skill_graph_candidates.py")
    )
    assert _candidate_spec is not None and _candidate_spec.loader is not None
    _candidate_impl = _candidate_importlib.module_from_spec(_candidate_spec)
    _candidate_spec.loader.exec_module(_candidate_impl)

# Preserve the plugin-level cache object for session tracking and external hooks.
_injected_names_cache: dict[str, set[str]] = {}

_redacted_candidate_exc_info = _candidate_impl.redacted_candidate_exc_info

def _detect_candidate_topic(
    msg: str, prev_msg: str | None, llm_tc: bool | None,
) -> dict[str, Any] | None:
    return _candidate_impl.detect_candidate_topic(
        msg, prev_msg, llm_tc, client_factory=_embedding_client,
        detector_path=Path(__file__).resolve().parent.parent.parent / "agent" / "topic_detection.py",
        logger=logger, redacted_exc_info=_redacted_candidate_exc_info,
    )

def _rank_skill_candidates(intents: list[str]) -> list[dict[str, Any]]:
    return _candidate_impl.rank_skill_candidates(
        intents, embedding_search=_embedding_search, ensure_graph=_ensure_graph,
        search_graph=_search_graph, logger=logger,
        redacted_exc_info=_redacted_candidate_exc_info,
    )

def _build_skill_candidates_context(
    user_message: str,
    session_id: str = "",
    is_first_turn: bool = False,
    prev_msg: str | None = None,
    prev_intents: list[str] | None = None,
) -> tuple[str | None, list[str]]:
    return _candidate_impl.build_skill_candidates_context(
        user_message, session_id, is_first_turn, prev_msg, prev_intents,
        config_reader=_skill_graph_config, split_intents=_split_intents,
        detect_topic=_detect_candidate_topic, rank_candidates=_rank_skill_candidates,
        injected_names_cache=_injected_names_cache, logger=logger,
    )


# ── LLM enrichment and intent split ─────────────────────────────────────────
# The bare-spec loader does not register the plugin as a package in sys.modules.
try:
    from . import skill_graph_enrichment as _enrichment_impl
except ImportError:
    import importlib.util as _enrichment_importlib

    _enrichment_spec = _enrichment_importlib.spec_from_file_location(
        f"{__name__}.skill_graph_enrichment", Path(__file__).with_name("skill_graph_enrichment.py")
    )
    assert _enrichment_spec is not None and _enrichment_spec.loader is not None
    _enrichment_impl = _enrichment_importlib.module_from_spec(_enrichment_spec)
    _enrichment_spec.loader.exec_module(_enrichment_impl)

SCENE_VOCABULARY = _enrichment_impl.SCENE_VOCABULARY
SCENE_VOCABULARY_DESC = _enrichment_impl.SCENE_VOCABULARY_DESC


def _needs_enrichment(info: dict[str, Any]) -> bool:
    return _enrichment_impl.needs_enrichment(info)


def _build_enrichment_prompt(skill_name: str, content: str) -> str:
    return _enrichment_impl.build_enrichment_prompt(skill_name, content)


def _parse_enrichment_response(data: Any) -> tuple[bool, dict[str, Any] | None]:
    return _enrichment_impl.parse_enrichment_response(data)


def _request_enrichment_with_retries(url: str, headers: dict, payload: dict) -> dict[str, Any] | None:
    return _enrichment_impl.request_enrichment_with_retries(
        url, headers, payload, parse_response=_parse_enrichment_response, logger=logger,
    )


def _call_llm_for_enrichment(prompt: str) -> dict[str, Any] | None:
    return _enrichment_impl.call_llm_for_enrichment(
        prompt, request_enrichment=_request_enrichment_with_retries, logger=logger,
    )


def _resolve_llm_provider(config: dict | None = None) -> tuple[str, str, str] | None:
    return _enrichment_impl.resolve_llm_provider(
        config, logger=logger, log_fallback_exception=_log_fallback_exception,
    )


def _split_intents(
    user_message: str, prev_intents: list[str] | None = None,
) -> tuple[list[str], str | None, bool | None]:
    return _enrichment_impl.split_intents(
        user_message, prev_intents, resolve_provider=_resolve_llm_provider,
        logger=logger, log_fallback_exception=_log_fallback_exception,
    )

try:
    from . import skill_graph_enrichment_store as _enrichment_store
except ImportError:
    import importlib.util as _store_importlib

    _store_spec = _store_importlib.spec_from_file_location(
        f"{__name__}.skill_graph_enrichment_store",
        Path(__file__).with_name("skill_graph_enrichment_store.py"),
    )
    assert _store_spec is not None and _store_spec.loader is not None
    _enrichment_store = _store_importlib.module_from_spec(_store_spec)
    _store_spec.loader.exec_module(_enrichment_store)


def _patch_skill_frontmatter(skill_path: str, tags: list[str], scenes: list[str]) -> bool:
    return _enrichment_store.patch_skill_frontmatter(
        skill_path, tags, scenes, log_fallback_exception=_log_fallback_exception,
    )


def _enrich_skill(conn: sqlite3.Connection, skill_name: str) -> bool:
    return _enrichment_store.enrich_skill(
        conn, skill_name,
        build_prompt=_build_enrichment_prompt,
        call_llm=_call_llm_for_enrichment,
        extract_terms=_extract_skill_terms,
        is_under=_is_under,
        is_read_only=_is_read_only_skill,
        bundled_skills_dir=get_bundled_skills_dir,
        patch_frontmatter=_patch_skill_frontmatter,
        scene_vocabulary=SCENE_VOCABULARY,
        plugin_file=__file__,
        logger=logger,
        log_fallback_exception=_log_fallback_exception,
    )



def _enrich_pending_skills(conn: sqlite3.Connection, limit: int = 10,
                             force: bool = False) -> int:
    return _enrichment_store.enrich_pending_skills(
        conn, limit, force, enrich_one=_enrich_skill, logger=logger,
    )



def _full_rebuild(conn: sqlite3.Connection) -> int:
    """Full rebuild: scan all skills dirs, rebuild graph from scratch."""
    skill_dirs = _find_all_skills_dirs()
    logger.info("skill-graph: starting full rebuild — scanning %d skill directories", len(skill_dirs))
    skills = _scan_skill_mds(skill_dirs)
    deduped = _dedup_skills(skills)

    now = time.time()
    conn.execute("DELETE FROM skill_edges")
    conn.execute("DELETE FROM skill_nodes")
    conn.execute("DELETE FROM skill_fts")
    conn.execute("DELETE FROM skill_terms")

    # Drop and recreate FTS table to ensure correct schema
    # (old DBs may have content='' which breaks JOIN queries)
    conn.execute("DROP TABLE IF EXISTS skill_fts")
    conn.execute("DROP TABLE IF EXISTS skill_fts_data")
    conn.execute("DROP TABLE IF EXISTS skill_fts_idx")
    conn.execute("DROP TABLE IF EXISTS skill_fts_docsize")
    conn.execute("DROP TABLE IF EXISTS skill_fts_config")
    conn.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS skill_fts USING fts5(
            name, category, description, tags, scenes,
            tokenize='porter unicode61'
        );
    """)

    for name, path in deduped.items():
        _upsert_skill(conn, name, path, now)
    conn.commit()

    # Compute embeddings for all skills (best-effort, TEI/CPU)
    _rebuild_embeddings(conn)

    logger.info("Skill graph rebuilt: %d skills", len(deduped))
    return len(deduped)


def _incremental_sync(conn: sqlite3.Connection) -> int:
    """Incremental sync: only re-parse skills whose mtime changed."""
    skill_dirs = _find_all_skills_dirs()
    skills = _scan_skill_mds(skill_dirs)
    deduped = _dedup_skills(skills)

    db_nodes: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT name, content_hash, last_parsed, file_path FROM skill_nodes"
    ):
        db_nodes[row["name"]] = {
            "content_hash": row["content_hash"],
            "last_parsed": row["last_parsed"],
            "file_path": row["file_path"],
        }

    now = time.time()
    parsed_count = 0
    skipped_count = 0

    for name, path in deduped.items():
        existing = db_nodes.get(name)

        # Condition 1: not in DB → must upsert
        if existing is None:
            _upsert_skill(conn, name, path, now)
            parsed_count += 1
            continue

        # Condition 2: in DB but file changed (mtime newer or path relocated) → upsert
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0
        if mtime > existing["last_parsed"] or existing["file_path"] != str(path):
            _upsert_skill(conn, name, path, now)
            parsed_count += 1
            continue

        # Condition 3: in DB and unchanged → skip
        skipped_count += 1

    current_names = set(deduped.keys())
    db_names = set(db_nodes.keys())
    stale = db_names - current_names
    for name in stale:
        conn.execute("DELETE FROM skill_edges WHERE source = ? OR target = ?", (name, name))
        conn.execute("DELETE FROM skill_nodes WHERE name = ?", (name,))
        conn.execute("DELETE FROM skill_fts WHERE name = ?", (name,))
        conn.execute("DELETE FROM skill_terms WHERE skill_name = ?", (name,))
        _drop_embeddings(conn, name)

    conn.commit()

    # Compute embeddings for any new/changed skills lacking one
    _rebuild_embeddings(conn)

    logger.info(
        "Skill graph synced: %d parsed, %d unchanged, %d removed, %d total",
        parsed_count, skipped_count, len(stale), len(deduped),
    )
    return len(deduped)


def _sync_graph(conn: sqlite3.Connection) -> int:
    """Sync the graph with the filesystem. Full if empty, incremental otherwise."""
    count = conn.execute("SELECT COUNT(*) FROM skill_nodes").fetchone()[0]
    if count == 0:
        return _full_rebuild(conn)
    return _incremental_sync(conn)


def _update_single_skill(conn: sqlite3.Connection, skill_name: str) -> bool:
    """Re-parse a single skill and update its node + edges + FTS."""
    skill_path = _find_skill_path(skill_name)
    if skill_path is None:
        logger.debug("skill-graph: skill '%s' not found on disk, skipping", skill_name)
        return False
    now = time.time()
    _upsert_skill(conn, skill_name, skill_path, now)
    # Mark active-profile skills as needing external organization.
    if _is_under(skill_path, get_skills_dir()):
        conn.execute(
            "UPDATE skill_nodes SET needs_organizing = 1 WHERE name = ? AND (needs_organizing IS NULL OR needs_organizing = 0)",
            (skill_name,),
        )
    conn.commit()

    # Recompute embedding for the changed skill (incremental maintenance)
    info = _parse_skill_md(skill_path)
    _compute_embedding_for_skill(conn, skill_name, info)
    conn.commit()

    logger.debug("skill-graph: updated single skill '%s' (%s)", skill_name, skill_path)
    return True


def _reverse_type(rel_type: str) -> str | None:
    """Return the reverse relation type, or None if symmetric."""
    mapping = {
        "depends_on": "supported_by",
        "supported_by": "depends_on",
        "supersedes": "superseded_by",
        "superseded_by": "supersedes",
    }
    return mapping.get(rel_type)


# ── Graph search ────────────────────────────────────────────────────────────


# The plugin may be loaded by spec_from_file_location without registration in
# sys.modules. In that case relative imports are unavailable, so load the
# sibling by its adjacent path rather than a global sys.path import.
try:
    from . import skill_graph_search as _search_impl
except ImportError:
    import importlib.util as _importlib_util

    _search_spec = _importlib_util.spec_from_file_location(
        f"{__name__}.skill_graph_search", Path(__file__).with_name("skill_graph_search.py")
    )
    assert _search_spec is not None and _search_spec.loader is not None
    _search_impl = _importlib_util.module_from_spec(_search_spec)
    _search_spec.loader.exec_module(_search_impl)


def _search_graph(query: str, conn: sqlite3.Connection, limit: int = 10,
                  scenes: list[str] | None = None) -> list[dict[str, Any]]:
    """Search via the sibling; retain plugin-level dependency patch seams."""
    return _search_impl.search_graph(
        query, conn, limit, scenes, fts_query=_fts_query,
        extract_terms=_extract_terms, get_node_info=_get_node_info,
        boost_search_results=_boost_search_results,
        boost_scene_results=_boost_scene_results, fallback_search=_fallback_search,
    )


def _boost_search_results(
    conn: sqlite3.Connection, results: dict[str, dict[str, Any]], terms: list[str],
) -> None:
    return _search_impl.boost_search_results(
        conn, results, terms, log_error=_log_fallback_exception,
    )


def _boost_scene_results(
    conn: sqlite3.Connection, results: dict[str, dict[str, Any]], scenes: list[str],
) -> None:
    return _search_impl.boost_scene_results(
        conn, results, scenes, log_error=_log_fallback_exception,
    )


def _fallback_search(query: str, conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    return _search_impl.fallback_search(query, conn, limit, extract_terms=_extract_terms)


def _fts_query(query: str) -> str:
    return _search_impl.fts_query(query)


def _extract_terms(query: str) -> list[str]:
    return _search_impl.extract_terms(query)


def _get_node_info(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    return _search_impl.get_node_info(conn, name)

# ── Plugin state ────────────────────────────────────────────────────────────

_graph_lock = threading.Lock()
_global_conn: sqlite3.Connection | None = None
_global_synced = False


def _ensure_graph() -> sqlite3.Connection:
    """Lazy-init the graph DB connection. Syncs on first access."""
    global _global_conn, _global_synced
    if _global_conn is None:
        conn = _get_conn()
        _init_db(conn)
        _migrate_db(conn)
        _global_conn = conn
    if not _global_synced:
        with _graph_lock:
            if not _global_synced:
                _sync_graph(_global_conn)
                _global_synced = True
    return _global_conn


# ── Schema migration helper ──────────────────────────────────────────────────

def _migrate_db(conn: sqlite3.Connection) -> None:
    """Apply schema changes that can't be done via CREATE TABLE IF NOT EXISTS."""
    # v2: add success_count column
    try:
        conn.execute("ALTER TABLE skill_term_stats ADD COLUMN success_count INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        conn.execute("ALTER TABLE skill_term_stats ADD COLUMN last_searched TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE skill_term_stats ADD COLUMN last_loaded TEXT")
    except sqlite3.OperationalError:
        pass
    # v6: enriched_at for cooldown tracking
    try:
        conn.execute(
            "ALTER TABLE skill_nodes ADD COLUMN enriched_at TEXT DEFAULT NULL")
    except sqlite3.OperationalError:
        pass

    # v4: scenes + enriched for LLM-powered scene/tag generation
    for col, col_type in (
        ("scenes", "TEXT DEFAULT '[]'"),
        ("enriched", "INTEGER DEFAULT 0"),
    ):
        try:
            conn.execute(f"ALTER TABLE skill_nodes ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass

    # v7: migrate auto_tagged → enriched (rename)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(skill_nodes)")}
        if "auto_tagged" in cols and "enriched" in cols:
            conn.execute(
                "UPDATE skill_nodes SET enriched = auto_tagged "
                "WHERE enriched = 0 AND auto_tagged = 1"
            )
        if "auto_tagged_at" in cols and "enriched_at" in cols:
            conn.execute(
                "UPDATE skill_nodes SET enriched_at = auto_tagged_at "
                "WHERE enriched_at IS NULL AND auto_tagged_at IS NOT NULL"
            )
        conn.commit()
    except sqlite3.OperationalError:
        pass

    # v3: soft delete + needs_organizing for lifecycle management
    for col, col_type in (
        ("is_deleted", "INTEGER DEFAULT 0"),
        ("deleted_at", "TEXT"),
        ("needs_organizing", "INTEGER DEFAULT 0"),
    ):
        try:
            conn.execute(f"ALTER TABLE skill_nodes ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass

    # v5: FTS5 scenes column — virtual tables can't be ALTERed, must rebuild
    _fts_cols = conn.execute("PRAGMA table_info(skill_fts)").fetchall()
    _fts_names = {row[1] for row in _fts_cols}
    if "scenes" not in _fts_names:
        logger.info("skill-graph: migrating FTS table — adding scenes column")
        # Save existing data (fetch all + commit to release read lock before DROP)
        _rows = conn.execute(
            "SELECT name, category, description, tags FROM skill_fts"
        ).fetchall()
        conn.commit()
        # Drop old FTS + shadow tables
        for _t in ("skill_fts", "skill_fts_data", "skill_fts_idx",
                    "skill_fts_docsize", "skill_fts_config"):
            conn.execute(f"DROP TABLE IF EXISTS {_t}")
        # Recreate with scenes column
        conn.executescript("""
            CREATE VIRTUAL TABLE skill_fts USING fts5(
                name, category, description, tags, scenes,
                tokenize='porter unicode61'
            );
        """)
        # Repopulate (scenes data may not exist yet, so use empty string)
        for _r in _rows:
            _skill_scenes = conn.execute(
                "SELECT scenes FROM skill_nodes WHERE name = ?", (_r["name"],)
            ).fetchone()
            _scenes_text = " ".join(
                json.loads(_skill_scenes[0])
            ) if _skill_scenes and _skill_scenes[0] else ""
            conn.execute(
                "INSERT INTO skill_fts (name, category, description, tags, scenes) "
                "VALUES (?, ?, ?, ?, ?)",
                (_r["name"], _r["category"], _r["description"], _r["tags"],
                 _scenes_text),
            )
        conn.commit()
        logger.info("skill-graph: FTS migration complete — %d rows repopulated", len(_rows))



def _persist_source_dirs(source_dirs: list[Any]) -> None:
    """Atomically update only the active profile's graph source list."""
    from hermes_cli.config import save_config

    path = ("skills", "config", "skill-graph", "source_dirs")
    partial = {"skills": {"config": {"skill-graph": {"source_dirs": source_dirs}}}}
    save_config(partial, preserve_keys={path}, merge_existing=True)


def _change_source_dir(action: str, path_str: str, *, persist: bool) -> tuple[Path, int, str]:
    """Apply one source-dir change and rebuild; returns target, count, note."""
    target = _resolve_config_path(path_str)
    if action == "add_dir" and not target.is_dir():
        raise ValueError(f"Not a directory: {target}")

    note = ""
    if persist:
        raw = _skill_graph_config().get("source_dirs", [])
        source_dirs = list(raw) if isinstance(raw, list) else []
        resolved = [_resolve_config_path(entry) for entry in source_dirs]
        if action == "add_dir":
            if target in resolved:
                note = "already present"
            else:
                source_dirs.append(path_str)
                _persist_source_dirs(source_dirs)
        elif target in resolved:
            source_dirs = [
                entry for entry in source_dirs
                if _resolve_config_path(entry) != target
            ]
            _persist_source_dirs(source_dirs)
        else:
            note = "not present"
    elif action == "add_dir":
        if target not in _RUNTIME_SOURCE_DIRS:
            _RUNTIME_SOURCE_DIRS.append(target)
        else:
            note = "already present"
    elif target in _RUNTIME_SOURCE_DIRS:
        _RUNTIME_SOURCE_DIRS.remove(target)
    else:
        note = "not present"

    conn = _ensure_graph()
    with _graph_lock:
        count = _full_rebuild(conn)
    return target, count, note

def _show_graph_config() -> str:
    """Return current graph config (slash command 'config' default action)."""
    try:
        conn = _ensure_graph()
        db_path = _db_path()
        scanned = _find_all_skills_dirs()
        cfg_dirs = _read_source_dirs_from_config()
        skill_count = conn.execute("SELECT COUNT(*) FROM skill_nodes").fetchone()[0]
        db_size = db_path.stat().st_size if db_path.exists() else 0
        lines = [
            "Skill Graph configuration",
            f"  DB path:     {db_path}",
            f"  DB size:     {db_size / 1024:.1f} KB",
            f"  Skills:      {skill_count}",
            f"  Source dirs (config): {cfg_dirs}" if cfg_dirs else "  Source dirs (config): (none)",
            "  Scanned dirs:",
        ]
        for d in scanned:
            cnt = len(list(d.rglob("SKILL.md"))) if d.exists() else 0
            lines.append(f"    {d}  ({cnt} SKILL.md)")
        return "\n".join(lines)
    except (OSError, sqlite3.Error, RuntimeError, TypeError, ValueError, AttributeError) as exc:
        _log_fallback_exception("skill-graph: could not display configuration", exc)
        return "Config failed: could not display graph configuration"

def _handle_source_dir_config(action: str, path_str: str) -> str:
    """Add or remove a source_dir at runtime and persist to config.yaml."""
    if not path_str:
        return f"Usage: /sg config {action} <path>"
    try:
        mapped = {"add": "add_dir", "remove": "remove_dir"}.get(action)
        if mapped is None:
            return f"Unknown action: {action}"
        target, count, note = _change_source_dir(mapped, path_str, persist=True)
        suffix = f" ({note})" if note else ""
        return f"✅ {action}ed {target}{suffix}\n   Graph rebuilt: {count} skills indexed."
    except (ImportError, OSError, sqlite3.Error, RuntimeError, TypeError, ValueError,
            AttributeError, yaml.YAMLError) as exc:
        _log_fallback_exception("skill-graph: could not change source directory", exc)
        return f"Config {action} failed: could not change source directory"


def _handle_skill_graph_config(args: dict | None = None, **kw) -> str:
    """Handle skill_graph_config tool — add/remove/list source_dirs."""
    if not isinstance(args, dict):
        return json.dumps({"success": False, "error": "args must be dict"})
    action = args.get("action", "")
    path_str = args.get("path", "")
    persist = args.get("persist", True)
    try:
        if action == "list_dirs":
            cfg_dirs = _read_source_dirs_from_config()
            return json.dumps({"success": True, "source_dirs": [str(d) for d in cfg_dirs],
                               "runtime_source_dirs": [str(d) for d in _RUNTIME_SOURCE_DIRS],
                               "scanned_dirs": [str(d) for d in _find_all_skills_dirs() if d.exists()],
                               "persisted": persist}, default=str)
        if action in ("add_dir", "remove_dir"):
            if not path_str:
                return json.dumps({"success": False, "error": "path required"})
            target, count, note = _change_source_dir(action, path_str, persist=bool(persist))
            result = {"success": True, "action": action, "path": str(target),
                      "skills_indexed": count, "persisted": bool(persist)}
            if note:
                result["note"] = note
            return json.dumps(result)
        return json.dumps({"success": False, "error": f"Unknown action: {action}. Use add_dir, remove_dir, or list_dirs."})
    except (ImportError, OSError, sqlite3.Error, RuntimeError, TypeError, ValueError,
            AttributeError, yaml.YAMLError) as exc:
        _log_fallback_exception("skill-graph: configuration tool failed", exc)
        return json.dumps({"success": False, "error": "Could not update graph configuration"})

# Keep plugin-level entry points as patch seams for callers and tests. The sibling
# is file-loaded once, independently of the plugin's synthetic package name.
_slash_module = None


def _slash_delegate(name: str, *args):
    global _slash_module
    if _slash_module is None:
        import importlib.util

        path = Path(__file__).with_name("skill_graph_slash.py")
        spec = importlib.util.spec_from_file_location(f"{__name__}.skill_graph_slash", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load skill-graph slash handler: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _slash_module = module
    return getattr(_slash_module, name)(globals(), *args)

def _format_edges(skill_name: str) -> str:
    return _slash_delegate("_format_edges", skill_name)


def _format_terms(skill_name: str) -> str:
    return _slash_delegate("_format_terms", skill_name)


def _slash_graph_status() -> str:
    return _slash_delegate("_slash_graph_status")


def _slash_graph_rebuild(rest: str) -> str:
    return _slash_delegate("_slash_graph_rebuild", rest)


def _slash_help() -> str:
    return _slash_delegate("_slash_help")


def _slash_graph_search(rest: str) -> str:
    return _slash_delegate("_slash_graph_search", rest)


def _slash_graph_scene(rest: str) -> str:
    return _slash_delegate("_slash_graph_scene", rest)


def _slash_graph_list(_rest: str) -> str:
    return _slash_delegate("_slash_graph_list", _rest)


def _slash_graph_score(rest: str) -> str:
    return _slash_delegate("_slash_graph_score", rest)


def _handle_slash_discovery(subcmd: str, rest: str) -> str:
    return _slash_delegate("_handle_slash_discovery", subcmd, rest)


def _slash_graph_config(rest: str) -> str:
    return _slash_delegate("_slash_graph_config", rest)


def _handle_slash_command(args: str) -> str | None:
    return _slash_delegate("_handle_slash_command", args)

# ── Tool handlers ───────────────────────────────────────────────────────────


def _handle_skill_graph_search(args: dict | None = None, **kw) -> str:
    """Handle skill_graph_search tool call."""
    if not isinstance(args, dict):
        args = kw.get("args", kw)
    query = args.get("query", "") if isinstance(args, dict) else ""
    limit = int(args.get("limit", 10)) if isinstance(args, dict) else 10
    list_all = args.get("list_all", False) if isinstance(args, dict) else False
    scenes = args.get("scenes") if isinstance(args, dict) else None
    if isinstance(scenes, str):
        scenes = [s.strip() for s in scenes.split(",") if s.strip()]
    elif not isinstance(scenes, list):
        scenes = None

    if not query and not list_all:
        return json.dumps({
            "success": False,
            "error": "query is required",
            "hint": "Pass a query describing what you want to do, "
                    "or set list_all=True to browse all skills.",
        })

    try:
        conn = _ensure_graph()
        with _graph_lock:
            if list_all:
                cursor = conn.execute(
                    """SELECT name, category, description, tags, file_path, needs_organizing
                       FROM skill_nodes
                       WHERE (is_deleted IS NULL OR is_deleted = 0)
                       ORDER BY name"""
                )
                results = []
                for row in cursor:
                    results.append({
                        "name": row["name"],
                        "category": row["category"] or "",
                        "description": row["description"] or "",
                        "tags": json.loads(row["tags"]) if row["tags"] else [],
                        "file_path": row["file_path"],
                        "relevance": "listed",
                        "score": 0.0,
                        "needs_organizing": bool(dict(row).get("needs_organizing")) or False,
                    })
                total = len(results)
                hint = "All skills listed by name. Call skill_load(name) to load full content."
                logger.info("skill-graph: search list_all=True → %d results (total: %d)", total, total)
                logger.debug("skill-graph: search list_all=True → skills: %s", [r["name"] for r in results])
                return json.dumps({
                    "success": True,
                    "query": "",
                    "results": results,
                    "edges_between_results": [],
                    "total_skills_in_graph": total,
                    "result_count": len(results),
                    "hint": hint,
                    "note": "list_all=True — results sorted by name, not by relevance score.",
                }, ensure_ascii=False)

            search_kwargs: dict[str, Any] = {"limit": limit}
            if scenes:
                search_kwargs["scenes"] = scenes
            results = _search_graph(query, conn, **search_kwargs)
            total = conn.execute("SELECT COUNT(*) FROM skill_nodes").fetchone()[0]
            result_names = [r["name"] for r in results]
            edges_between = []
            if len(result_names) > 1:
                placeholders = ",".join("?" for _ in result_names)
                cursor = conn.execute(
                    f"""SELECT source, target, rel_type, properties
                       FROM skill_edges
                       WHERE source IN ({placeholders})
                         AND target IN ({placeholders})
                       ORDER BY rel_type""",
                    result_names + result_names,
                )
                for row in cursor:
                    edges_between.append({
                        "source": row["source"],
                        "target": row["target"],
                        "type": row["rel_type"],
                        "properties": json.loads(row["properties"]) if row["properties"] else {},
                    })

        if results and results[0].get("score", 0) < 0.3:
            hint = (
                "Top results have low confidence. "
                "Retry skill_graph_search() with different keywords, "
                "or use skill_graph_search(list_all=True) to browse all skills."
            )
        else:
            hint = "Call skill_load(name) to load full content of a discovered skill."
        logger.info(
            "skill-graph: search query=%r limit=%d → %d results (total: %d)",
            query, limit, len(results), total,
        )
        logger.debug("skill-graph: search query=%r → skills: %s", query, result_names)
        return json.dumps({
            "success": True,
            "query": query,
            "results": results,
            "edges_between_results": edges_between,
            "total_skills_in_graph": total,
            "result_count": len(results),
            "hint": hint,
        }, ensure_ascii=False)

    except Exception as e:
        logger.exception("skill_graph_search failed")
        return json.dumps({"success": False, "error": str(e)})


def _handle_skill_load(args: dict | None = None, **kw) -> str:
    """Handle skill_load tool call. Loads full SKILL.md content by name."""
    if not isinstance(args, dict):
        args = kw.get("args", kw)
    name = args.get("name", "") if isinstance(args, dict) else ""

    if not name:
        return json.dumps({
            "success": False,
            "error": "name is required",
            "hint": "Pass the name of the skill to load (from skill_graph_search results)",
        })

    try:
        path = _find_skill_path(name)
        if path is None:
            return json.dumps({
                "success": False,
                "error": f"Skill '{name}' not found in any configured directory",
                "hint": "Use skill_graph_search() to discover available skills",
            })

        content = path.read_text(encoding="utf-8-sig", errors="replace")
        info = _parse_skill_md(path)
        skill_dir = str(path.parent)

        # Track load event: increment load_count for skill's own terms
        try:
            _conn = _ensure_graph()
            _sg_terms = _extract_terms(info.get("description", "") or "")
            _sg_terms.append(info["name"].lower())
            for _t in set(_sg_terms):
                _conn.execute(
                    """INSERT INTO skill_term_stats (skill_name, term, search_count, load_count)
                       VALUES (?, ?, 0, 1)
                       ON CONFLICT(skill_name, term) DO UPDATE SET load_count = load_count + 1""",
                    (info["name"], _t),
                )
            _conn.commit()
        except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError,
                KeyError, AttributeError, IndexError) as exc:
            _log_fallback_exception("skill-graph: load stats update failed", exc)

        return json.dumps({
            "success": True,
            "name": info["name"],
            "content": content,
            "category": info["category"],
            "description": info["description"],
            "tags": info["tags"],
            "relations": info["relations"],
            "file_path": str(path),
            "skill_dir": skill_dir,
        }, ensure_ascii=False)

    except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError,
            KeyError, AttributeError, IndexError, yaml.YAMLError) as exc:
        _log_fallback_exception("skill-graph: skill load failed", exc)
        return json.dumps({"success": False, "error": "Could not load skill"})


# ── Plugin entry point ──────────────────────────────────────────────────────


def _skill_graph_mode_enabled() -> bool:
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
        return bool((config.get("agent") or {}).get("skill_graph_mode", False))
    except (ImportError, OSError, RuntimeError, TypeError, ValueError,
            KeyError, AttributeError, yaml.YAMLError) as exc:
        _log_fallback_exception("skill-graph: could not read graph mode", exc)
        return False


def _gateway_extension_skills() -> list[tuple[str, str]]:
    """Read the declarative gateway list from the configured extensions file."""
    raw_path = _skill_graph_config().get("extensions_file")
    if not raw_path:
        return []
    try:
        path = _resolve_config_path(raw_path)
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, TypeError, ValueError):
        return []

    extras: list[tuple[str, str]] = []
    in_gateways = False
    for line in lines:
        line = line.rstrip()
        if line.startswith("## Pre-installed Gateways"):
            in_gateways = True
            continue
        if in_gateways and line.startswith("## "):
            break
        if not in_gateways or not line.startswith("| `"):
            continue
        cells = line.split("|")
        if len(cells) < 3:
            continue
        name = cells[1].strip().strip("`")
        description = cells[2].strip()
        if name and not name.startswith("-"):
            extras.append((name, description))
    return extras


def _render_skill_graph_prompt(session_info: Mapping[str, Any]) -> str:
    """Render graph discovery guidance only when mode and both tools are live."""
    tools = set(session_info.get("valid_tool_names") or ())
    if not session_info.get("skill_graph_mode") or not {
        "skill_graph_search", "skill_load"
    }.issubset(tools):
        return ""

    from agent.prompt_builder import SKILL_GRAPH_GUIDANCE, SKILL_GRAPH_IDENTITY

    description = "Skill knowledge graph — discover and load skills by intent"
    companion = _find_skill_path("skill-graph")
    if companion is not None:
        parsed = _parse_skill_md(companion)
        description = parsed.get("description") or description

    available = [f"- skill-graph — {str(description)[:100]}"]
    available.extend(f"- {name} — {desc[:100]}" for name, desc in _gateway_extension_skills())
    available_block = "Available Skills\n" + "\n".join(available)
    return "\n\n".join((SKILL_GRAPH_IDENTITY, available_block, SKILL_GRAPH_GUIDANCE))


def register(ctx):
    """Register the skill-graph plugin."""

    # ── Tool: skill_graph_search ──
    ctx.register_tool(
        name="skill_graph_search",
        toolset="skills",
        schema={
            "name": "skill_graph_search",
            "description": (
                "PREFERRED skill discovery method. Search the skill knowledge "
                "graph by intent instead of skills_list(). Uses typed "
                "relationships (depends_on, complemented_by, alternative_to) "
                "and full-text + graph traversal to find relevant skills. "
                "After finding skills, call skill_load(name) to get full content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language description of what you want to do "
                                       "(e.g. 'Python code review', 'deploy kubernetes', "
                                       "'database performance tuning')",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 10)",
                        "default": 10,
                    },
                    "list_all": {
                        "type": "boolean",
                        "description": "List all available skills by name (bypasses scoring). Use when search results have low confidence.",
                        "default": False,
                    },
                    "scenes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional scene filter — skills matching these scenes get ×1.3 score boost. Use scene vocabulary: coding, writing, research, design, devops, hermes, media, common. Soft weighting, not hard filtering.",
                    },
                },
            },
        },
        handler=_handle_skill_graph_search,
        description="Skill graph search — PREFERRED over skills_list()",
        check_fn=None,
    )

    # ── Tool: skill_load ──
    ctx.register_tool(
        name="skill_load",
        toolset="skills",
        schema={
            "name": "skill_load",
            "description": (
                "Load a skill's full SKILL.md content by name. "
                "Use after skill_graph_search() to retrieve the complete "
                "instructions. Returns the raw SKILL.md plus parsed metadata "
                "(category, description, tags, relations, file paths). "
                "Alternative to skill_view() — works for skills in graph-managed dirs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name (from skill_graph_search results)",
                    },
                },
                "required": ["name"],
            },
        },
        handler=_handle_skill_load,
        description="Load skill content by name",
        check_fn=None,
    )


    # ── Tool: skill_graph_config ──
    ctx.register_tool(
        name="skill_graph_config",
        toolset="skills",
        schema={
            "name": "skill_graph_config",
            "description": (
                "Manage skill-graph source directories at runtime without restarting Hermes. "
                "Add or remove directories for skill discovery, or list current configuration. "
                "Changes persist to config.yaml when persist=true (default). "
                "The graph is automatically rebuilt after add/remove."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "add_dir, remove_dir, or list_dirs",
                        "enum": ["add_dir", "remove_dir", "list_dirs"],
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory path (required for add_dir/remove_dir)",
                    },
                    "persist": {
                        "type": "boolean",
                        "description": "Save to config.yaml (default: true). Set false for ephemeral changes.",
                        "default": True,
                    },
                },
                "required": ["action"],
            },
        },
        handler=_handle_skill_graph_config,
        description="Manage skill-graph source directories at runtime",
        check_fn=None,
    )

    # Cache-safe, profile-scoped prompt guidance. Core persists the rendered
    # bytes for resume and re-renders only at the normal invalidation boundary.
    ctx.register_system_prompt_section(
        "skill-graph.discovery",
        _render_skill_graph_prompt,
        position="after_memory",
        max_chars=4000,
    )

    # ── Slash command: /skill-graph ──
    ctx.register_command(
        name="skill-graph",
        handler=_handle_slash_command,
        description="Skill knowledge graph: rebuild, status, help",
        args_hint="rebuild|status|config [add|remove] <path>",
    )

    # ── Alias: /sg → same handler as /skill-graph ──
    ctx.register_command(
        name="sg",
        handler=_handle_slash_command,
        description="Alias for /skill-graph",
        args_hint="rebuild|status|config [add|remove] <path>",
    )

    # ── Hook: pre_tool_call — gate search_files behind skill_graph_search ──
    # search_files: prevents the model from bypassing skill discovery by
    #   going straight to filesystem search (e.g. "find project files").
    # read_file is intentionally excluded — it's the execution step after a
    #   skill has been loaded (or reading a known config/source file) and
    #   should not be gated.
    # find is not a real Hermes tool; search_files replaced it.
    _gated_tools = frozenset({"search_files"})
    _graph_searched: bool = False  # per-turn flag
    _last_turn_id: str = ""  # for per-turn reset detection

    # Resolve mode once for the session's tool-gating policy.
    _graph_mode = _skill_graph_mode_enabled()

    def _on_pre_tool_call(tool_name: str, args: dict | None = None, **kw: Any) -> dict | str | None:
        nonlocal _graph_searched, _graph_mode, _last_turn_id
        turn_id = kw.get("turn_id", "")

        # Per-turn reset: when turn_id changes, clear the flag
        if turn_id and turn_id != _last_turn_id:
            _graph_searched = False
            _last_turn_id = turn_id

        # A graph search or a pre-injected-candidate load satisfies discovery.
        if tool_name in {"skill_graph_search", "skill_load"}:
            _graph_searched = True
            return None

        # Check gating: skill-graph mode + restricted tool + not yet searched
        if (
            _graph_mode
            and tool_name in _gated_tools
            and not _graph_searched
        ):
            return {"action": "block", "message":
                f"Tool '{tool_name}' is blocked until you call "
                f"skill_graph_search() first. This profile requires graph "
                f"discovery before filesystem searches."
            }
        return None

    ctx.register_hook("pre_tool_call", _on_pre_tool_call)

    # ── Hook: on_session_start — ensure DB ──
    def _on_session_start(**kw):
        try:
            _ensure_graph()
            logger.info("Skill graph ready")
        except Exception:
            logger.exception("skill-graph: on_session_start failed")

    ctx.register_hook("on_session_start", _on_session_start)

    _last_loaded_skill: str | None = None

    def _on_post_tool_call(**kw):
        nonlocal _last_loaded_skill
        tool_name = kw.get("tool_name", "")

        # Track skill_load → when quality-gate loads, mark the previous skill as successful
        if tool_name == "skill_load":
            skill_name = (kw.get("args", {}) or {}).get("name", "") or ""
            if not skill_name:
                return
            if skill_name == "quality-gate" and _last_loaded_skill:
                try:
                    conn = _get_conn()
                    conn.execute(
                        "UPDATE skill_term_stats SET success_count = success_count + 1 WHERE skill_name = ?",
                        (_last_loaded_skill,),
                    )
                    conn.commit()
                except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError,
                        KeyError, AttributeError, IndexError) as exc:
                    _log_fallback_exception("skill-graph: success stat update failed", exc)
                _last_loaded_skill = None
            else:
                _last_loaded_skill = skill_name
            return

        # Handle successful skill_manage batches → update affected graph nodes.
        if tool_name != "skill_manage":
            return
        args = kw.get("args", {})
        if not isinstance(args, dict):
            return
        result = kw.get("result")
        if isinstance(result, str):
            try:
                parsed_result = json.loads(result)
            except (TypeError, ValueError):
                parsed_result = None
            if isinstance(parsed_result, dict) and parsed_result.get("success") is False:
                return

        operations = args.get("operations")
        if not isinstance(operations, list):
            # Compatibility with the legacy single-operation shape.
            operations = [args]
        changes = [
            (str(op.get("action") or ""), str(op.get("name") or ""))
            for op in operations
            if isinstance(op, dict) and op.get("name")
        ]
        if not changes:
            return
        try:
            conn = _ensure_graph()
            with _graph_lock:
                for action, skill_name in changes:
                    if action == "delete":
                        conn.execute(
                            "UPDATE skill_nodes SET is_deleted = 1, "
                            "deleted_at = datetime('now') WHERE name = ?",
                            (skill_name,),
                        )
                        logger.info("skill-graph: soft-deleted skill '%s'", skill_name)
                    elif action in {"create", "patch", "write_file", "remove_file", "edit"}:
                        updated = _update_single_skill(conn, skill_name)
                        if updated:
                            logger.info(
                                "skill-graph: updated skill '%s' after %s",
                                skill_name, action,
                            )
                conn.commit()
        except Exception:
            logger.exception("skill-graph: post_tool_call failed for %s", changes)

    ctx.register_hook("post_tool_call", _on_post_tool_call)

    # ── Hook: pre_llm_call — inject skill candidates (Plan A) ──

    # Per-session state for delta injection (prev message + intents)
    _pre_llm_session_data: dict[str, dict] = {}  # session_id → {msg, intents}

    def _on_pre_llm_call(**kw):
        try:
            user_message = kw.get("user_message") or ""
            if not user_message or not isinstance(user_message, str):
                return None
            session_id = kw.get("session_id") or ""
            is_first_turn = kw.get("is_first_turn", False)

            # Retrieve previous message/intents for topic detection
            session_data = _pre_llm_session_data.get(session_id, {})
            prev_msg = session_data.get("msg")
            prev_intents = session_data.get("intents")

            block, intents = _build_skill_candidates_context(
                user_message,
                session_id=session_id,
                is_first_turn=is_first_turn,
                prev_msg=prev_msg,
                prev_intents=prev_intents,
            )

            # Update session state with current message + intents for next turn
            _pre_llm_session_data[session_id] = {
                "msg": user_message.strip(),
                "intents": intents,
            }

            # Cap session_data cache (avoid unbounded growth in long-lived processes)
            if len(_pre_llm_session_data) > 100:
                # Drop oldest entries (rough heuristic)
                oldest = sorted(_pre_llm_session_data.keys())[:20]
                for k in oldest:
                    if k != session_id:
                        del _pre_llm_session_data[k]

            if not block:
                return None
            # pre_llm_call context dict: {"context": str} — the core appends
            # this to the API copy of the user message (api_content sidecar),
            # never to the stored transcript content.
            return {"context": block}
        except Exception:
            logger.exception("skill-graph: pre_llm_call failed")
            return None

    ctx.register_hook("pre_llm_call", _on_pre_llm_call)

    logger.info(
        "skill-graph plugin registered: tools=skill_graph_search+skill_load+skill_graph_config, "
        "cmd=/skill-graph, hooks=on_session_start+post_tool_call+pre_tool_call+pre_llm_call"
    )
