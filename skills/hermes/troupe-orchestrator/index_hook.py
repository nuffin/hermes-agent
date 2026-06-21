"""Graph index hook for troupe-orchestrator.

Injects worker/character names from troupe roster.yaml into the skill graph
as terms, so searching for a character name directly returns this skill.
"""

from pathlib import Path
import sqlite3
import yaml


def on_graph_index(
    conn: sqlite3.Connection,
    skill_dir: Path,
    skill_name: str,
    info: dict,
) -> None:
    """Called by skill-graph after indexing this skill.

    Reads troupe roster files (~/.hermes/<profiles>/*/troupe/roster.yaml)
    and injects each character/worker name as a term so the graph can
    match on character/worker names like '科万', '莫维斯' etc.
    """
    hermes_home = Path.home() / ".hermes"
    candidates = [hermes_home / "troupe" / "roster.yaml"]

    _env_home = hermes_home / "profiles"
    if _env_home.exists():
        for pdir in sorted(_env_home.iterdir()):
            candidate = pdir / "troupe" / "roster.yaml"
            if candidate.exists():
                candidates.append(candidate)

    seen_paths = set()
    for roster_path in candidates:
        if not roster_path.exists() or roster_path in seen_paths:
            continue
        seen_paths.add(roster_path)
        try:
            data = yaml.safe_load(roster_path.read_text(encoding="utf-8"))
            if not data:
                continue

            # Merge all dict-format sources (characters, orchestrators, workers, factions)
            items = {}
            for key in ("characters", "orchestrators", "workers", "factions"):
                src = data.get(key, {})
                if isinstance(src, dict):
                    items.update(src)
            if not items:
                continue

            if isinstance(items, dict):
                for slug, char_info in items.items():
                    name = (char_info.get("display_name", slug) or slug).strip().lower()
                    if len(name) >= 2:
                        conn.execute(
                            "INSERT OR IGNORE INTO skill_terms (term, skill_name, strength, source) VALUES (?, ?, ?, ?)",
                            (name, skill_name, 1.0, "name_literal"),
                        )
                    if slug != name and len(slug) >= 2:
                        conn.execute(
                            "INSERT OR IGNORE INTO skill_terms (term, skill_name, strength, source) VALUES (?, ?, ?, ?)",
                            (slug, skill_name, 0.9, "name_slug"),
                        )
            else:
                # List format: [{name, ...}]
                for entry in items:
                    name = (entry.get("name", "") if isinstance(entry, dict) else str(entry)).strip().lower()
                    if len(name) >= 2:
                        conn.execute(
                            "INSERT OR IGNORE INTO skill_terms (term, skill_name, strength, source) VALUES (?, ?, ?, ?)",
                            (name, skill_name, 1.0, "name_literal"),
                        )
        except Exception:
            pass

    conn.commit()
