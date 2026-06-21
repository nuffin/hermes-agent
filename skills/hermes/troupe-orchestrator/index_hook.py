"""Graph index hook for troupe-orchestrator.

Injects worker names from troupe roster.yaml into the skill graph as
terms, so searching for a worker name directly returns this skill.
"""

from pathlib import Path
import sqlite3
import yaml
import json


def on_graph_index(
    conn: sqlite3.Connection,
    skill_dir: Path,
    skill_name: str,
    info: dict,
) -> None:
    """Called by skill-graph after indexing this skill.

    Reads `~/.hermes/troupe/roster.yaml` and injects each worker name
    as a `name_literal` term (strength 1.0) so the graph can match on
    character/worker names.
    """
    # Try profile-level troupe first, then global
    hermes_home = Path.home() / ".hermes"
    candidates = [
        hermes_home / "troupe" / "roster.yaml",                     # global
    ]
    # Also check profile-level troupe for skill-graph-managed profiles
    _env_home = Path.home() / ".hermes" / "profiles"
    if _env_home.exists():
        for pdir in _env_home.iterdir():
            candidate = pdir / "troupe" / "roster.yaml"
            if candidate.exists():
                candidates.append(candidate)

    seen = set()
    for roster_path in candidates:
        if not roster_path.exists() or roster_path in seen:
            continue
        seen.add(roster_path)
        try:
            data = yaml.safe_load(roster_path.read_text(encoding="utf-8"))
            if not data or "workers" not in data:
                continue
            for w in data["workers"]:
                name = w.get("name", "").strip().lower()
                if not name or len(name) < 2:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO skill_terms (term, skill_name, strength, source) VALUES (?, ?, ?, ?)",
                    (name, skill_name, 1.0, "name_literal"),
                )
        except Exception:
            pass  # skip malformed roster files

    conn.commit()
