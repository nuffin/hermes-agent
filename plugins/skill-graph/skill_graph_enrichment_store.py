"""Skill enrichment frontmatter persistence and per-skill graph updates."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Callable

import hermes_yaml as yaml


def patch_skill_frontmatter(
    skill_path: str, tags: list[str], scenes: list[str], *,
    log_fallback_exception: Callable[[str, Exception], None],
) -> bool:
    """Write tags and scenes back to editable SKILL.md frontmatter."""
    try:
        path = Path(skill_path)
        content = path.read_text(encoding="utf-8-sig", errors="replace")
        content_str = content.lstrip("\ufeff")
        if not content_str.startswith("---"):
            return False
        end = content_str.find("---", 3)
        if end == -1:
            return False

        frontmatter = content_str[3:end].strip()
        try:
            meta = yaml.safe_load(frontmatter) or {}
        except yaml.YAMLError as exc:
            log_fallback_exception("skill-graph: invalid frontmatter [location=frontmatter.parse]", exc)
            return False

        hermes_meta = meta.setdefault("metadata", {}).setdefault("hermes", {})
        hermes_meta["tags"] = tags
        hermes_meta["scenes"] = scenes
        new_fm = yaml.safe_dump(meta, default_flow_style=False, allow_unicode=True).strip()
        new_content = f"---\n{new_fm}\n---{content_str[end + 3:]}"
        path.write_text(new_content, encoding="utf-8")
        return True
    except (OSError, UnicodeError, TypeError, ValueError, AttributeError,
            RuntimeError, yaml.YAMLError) as exc:
        log_fallback_exception("skill-graph: frontmatter patch failed [location=frontmatter.patch]", exc)
        return False


def enrich_skill(
    conn: sqlite3.Connection, skill_name: str, *,
    build_prompt: Callable[[str, str], str],
    call_llm: Callable[[str], dict[str, Any] | None],
    extract_terms: Callable[[str, list[str], str], list[tuple[str, float, str]]],
    is_under: Callable[[Path, Path], bool],
    is_read_only: Callable[[str], bool],
    bundled_skills_dir: Callable[[Path], Path],
    patch_frontmatter: Callable[[str, list[str], list[str]], bool],
    scene_vocabulary: Any,
    plugin_file: str,
    logger: logging.Logger,
    log_fallback_exception: Callable[[str, Exception], None],
) -> bool:
    """Enrich one skill, committing DB and best-effort writable frontmatter."""
    node = conn.execute(
        "SELECT file_path, content_hash FROM skill_nodes WHERE name = ?",
        (skill_name,),
    ).fetchone()
    if not node or not node["file_path"]:
        return False

    skill_path = node["file_path"]
    try:
        content = Path(skill_path).read_text(encoding="utf-8-sig", errors="replace")
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        log_fallback_exception("skill-graph: enrichment read failed [location=enrichment.read]", exc)
        return False

    prompt = build_prompt(skill_name, content)
    result = call_llm(prompt)
    if result is None:
        return False

    tags = result.get("tags", [])
    scenes = result.get("scenes", [])
    suggestions = result.get("suggestions", [])
    if not isinstance(tags, list) or not isinstance(scenes, list):
        return False

    valid_scenes = [s for s in scenes if s in scene_vocabulary]
    if suggestions:
        logger.info("skill-graph: enrichment suggestions received [location=enrichment.suggestions]")

    conn.execute(
        "UPDATE skill_nodes SET tags = ?, scenes = ?, enriched = 1, enriched_at = datetime('now') WHERE name = ?",
        (json.dumps(tags, ensure_ascii=False),
         json.dumps(valid_scenes, ensure_ascii=False),
         skill_name),
    )
    tags_text = " ".join(tags)
    scenes_text = " ".join(valid_scenes)
    conn.execute("DELETE FROM skill_fts WHERE name = ?", (skill_name,))
    conn.execute(
        "INSERT INTO skill_fts (name, category, description, tags, scenes) VALUES (?, ?, ?, ?, ?)",
        (skill_name, "", "", tags_text, scenes_text),
    )
    conn.execute("DELETE FROM skill_terms WHERE skill_name = ?", (skill_name,))
    terms = extract_terms(skill_name, tags, "")
    for term_text, strength, source in terms:
        conn.execute(
            "INSERT OR IGNORE INTO skill_terms (term, skill_name, strength, source) VALUES (?, ?, ?, ?)",
            (term_text, skill_name, strength, source),
        )

    bundled_skills = bundled_skills_dir(Path(plugin_file).resolve().parents[2] / "skills")
    if is_under(Path(skill_path), bundled_skills):
        logger.info("skill-graph: enriched tags=%d scenes=%d (bundled dir, DB only)",
                    len(tags), len(valid_scenes))
    elif not is_read_only(skill_path):
        wrote = patch_frontmatter(skill_path, tags, valid_scenes)
        if wrote:
            logger.info("skill-graph: enriched tags=%d scenes=%d (wrote SKILL.md)",
                        len(tags), len(valid_scenes))
        else:
            logger.info("skill-graph: enriched tags=%d scenes=%d (SKILL.md write failed)",
                        len(tags), len(valid_scenes))
    else:
        logger.info("skill-graph: enriched tags=%d scenes=%d (read-only, DB only)",
                    len(tags), len(valid_scenes))

    conn.commit()
    return True


def enrich_pending_skills(
    conn: sqlite3.Connection, limit: int = 10, force: bool = False, *,
    enrich_one: Callable[[sqlite3.Connection, str], bool],
    logger: logging.Logger,
) -> int:
    """Process pending skills, respecting the five-minute retry cooldown."""
    if force:
        where = "enriched = 0 AND (is_deleted IS NULL OR is_deleted = 0)"
    else:
        where = ("enriched = 0 AND (is_deleted IS NULL OR is_deleted = 0) "
                 "AND (enriched_at IS NULL "
                 "OR enriched_at < datetime('now', '-5 minutes'))")
    rows = conn.execute(
        f"SELECT name FROM skill_nodes WHERE {where} LIMIT ?",
        (limit,),
    ).fetchall()

    total = len(rows)
    if total > 10:
        logger.info("skill-graph: %d skills need enrichment — this may take a while, please wait...", total)
    logger.info("skill-graph: enriching %d pending skills", total)
    count = 0
    for i, row in enumerate(rows, 1):
        logger.info("skill-graph: enrich [%d/%d]", i, total)
        if enrich_one(conn, row["name"]):
            count += 1
        else:
            logger.warning("skill-graph: enrich FAILED [%d/%d]", i, total)
    logger.info("skill-graph: enrich done — %d/%d succeeded", count, total)
    return count
