"""Slash command presentation and dispatch for the skill-graph plugin.

The facade is passed per invocation so monkeypatches on its entry points and
profile-scoped DB/lock operations remain late-bound under both plugin loaders.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Mapping

import hermes_yaml as yaml
from hermes_constants import get_hermes_home

# ── Slash command handler ───────────────────────────────────────────────────


def _format_edges(graph: Mapping[str, Any], skill_name: str) -> str:
    """Query and format graph edges only."""
    try:
        conn = graph['_ensure_graph']()
        rows = conn.execute(
            """SELECT source, target, rel_type, properties FROM skill_edges
               WHERE source = ? OR target = ?
               ORDER BY rel_type, source""",
            (skill_name, skill_name),
        ).fetchall()
        if not rows:
            return f"No relations defined for: {skill_name}"
        seen: set[tuple[str, str, str]] = set()
        parts = []
        for src, tgt, rel, props in rows:
            key = (src, tgt, rel)
            if key in seen:
                continue
            seen.add(key)
            arrow = f"  {src} ──({rel})──> {tgt}"
            reason = ""
            if isinstance(props, str) and props:
                import json as _j
                try:
                    reason = _j.loads(props).get("reason", "")
                except (ValueError, TypeError, AttributeError) as exc:
                    graph['_log_fallback_exception']("skill-graph: invalid edge properties", exc)
                    reason = props[:40]
            elif isinstance(props, dict):
                reason = props.get("reason", "")
            if reason:
                parts.append(f"{arrow:55s} {reason[:50]}")
            else:
                parts.append(arrow)
        return "Edges:\n" + "\n".join(parts) + "\n"
    except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError,
            AttributeError, IndexError) as exc:
        graph['_log_fallback_exception']("skill-graph: could not format edges", exc)
        return ""


def _format_terms(graph: Mapping[str, Any], skill_name: str) -> str:
    """Query and format term associations with inline stats."""
    try:
        conn = graph['_ensure_graph']()
        parts = []

        # Skill's own terms
        terms = conn.execute(
            "SELECT t.term, t.strength, t.source, "
            "COALESCE(s.search_count,0) AS sc, COALESCE(s.load_count,0) AS lc, "
            "COALESCE(s.success_count,0) AS suc "
            "FROM skill_terms t "
            "LEFT JOIN skill_term_stats s ON t.skill_name = s.skill_name AND t.term = s.term "
            "WHERE t.skill_name = ? ORDER BY t.strength DESC, t.source",
            (skill_name,),
        ).fetchall()
        if terms:
            term_lines = ["", "  Terms:"]
            for t in terms:
                _sc, _lc, _suc = t['sc'], t['lc'], t['suc']
                _eff = (_suc * 2 + _lc) / max(_sc * 3, 1)
                _conf = 1 - __import__("math").pow(0.5, _sc / 5)
                _adj = (_eff - 0.5) * 2
                _th = _adj / (1 + abs(_adj) * 0.5)
                _boost = 0.1 * _th * _conf
                _sign = "+" if _boost > 0 else ""
                _stats = f"s={_sc}/l={_lc}/ok={_suc}/b={_sign}{_boost:.3f}".replace("+-", "")
                term_lines.append(
                    f"    {skill_name} ──({t['source']})──> {t['term']}  [{_stats}]"
                )
            parts.append("\n".join(term_lines))

        # Reverse lookup
        rev = conn.execute(
            "SELECT t.skill_name, t.strength, t.source, "
            "COALESCE(s.search_count,0) AS sc, COALESCE(s.load_count,0) AS lc, "
            "COALESCE(s.success_count,0) AS suc "
            "FROM skill_terms t "
            "LEFT JOIN skill_term_stats s ON t.skill_name = s.skill_name AND t.term = s.term "
            "WHERE t.term = ? ORDER BY t.strength DESC",
            (skill_name,),
        ).fetchall()
        if rev:
            rev_lines = ["", "  Skills with this term:"]
            for sn, s, src, sc, lc, suc in rev:
                _eff2 = (suc * 2 + lc) / max(sc * 3, 1)
                _conf2 = 1 - __import__("math").pow(0.5, sc / 5)
                _adj2 = (_eff2 - 0.5) * 2
                _th2 = _adj2 / (1 + abs(_adj2) * 0.5)
                _boost2 = 0.1 * _th2 * _conf2
                _sign2 = "+" if _boost2 > 0 else ""
                rev_lines.append(f"    {sn:40s} ──({src})──> {skill_name}  [s={sc}/l={lc}/ok={suc}/b={_sign2}{_boost2:.3f}]".replace("+-", ""))
            parts.append("\n".join(rev_lines))

        return "\n".join(parts) if parts else ""
    except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError,
            AttributeError, IndexError) as exc:
        graph['_log_fallback_exception']("skill-graph: could not format terms", exc)
        return ""


def _slash_graph_status(graph: Mapping[str, Any]) -> str:
    """Report graph counts and indexed source directories."""
    try:
        conn = graph['_ensure_graph']()
        node_count = conn.execute("SELECT COUNT(*) FROM skill_nodes").fetchone()[0]
        edge_count = conn.execute("SELECT COUNT(*) FROM skill_edges").fetchone()[0]
        term_count = conn.execute("SELECT COUNT(DISTINCT term) FROM skill_terms").fetchone()[0]
        db_path = graph['_db_path']()

        if node_count == 0:
            with graph['_graph_lock']:
                count = graph['_sync_graph'](conn)
            node_count = count
            edge_count = conn.execute("SELECT COUNT(*) FROM skill_edges").fetchone()[0]

        scanned = graph['_find_all_skills_dirs']()
        dirs_info = []
        for d in scanned:
            if d.exists():
                cnt = sum(
                    1 for root, dirs, files in os.walk(str(d), followlinks=True)
                    if "SKILL.md" in files
                )
            else:
                cnt = 0
            dirs_info.append(f"    {d}  ({cnt} SKILL.md)")
        dirs_text = "\n".join(dirs_info) if dirs_info else "    (none)"

        db_size = db_path.stat().st_size if db_path.exists() else 0
        return (
            f"Skill Graph status\n"
            f"  Skills:  {node_count}\n"
            f"  Edges:   {edge_count}\n"
            f"  Terms:   {term_count}\n"
            f"  DB size: {db_size / 1024:.1f} KB\n"
            f"  DB path: {db_path}\n"
            f"  Scanned dirs:\n{dirs_text}"
        )
    except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError,
            KeyError, AttributeError, IndexError) as exc:
        graph['_log_fallback_exception']("skill-graph: status check failed", exc)
        return "Status check failed: could not read graph status"


def _slash_graph_rebuild(graph: Mapping[str, Any], rest: str) -> str:
    """Rebuild and optionally launch the legacy enrichment worker."""
    try:
        force = rest.strip() == "--force"
        conn = graph['_ensure_graph']()
        with graph['_graph_lock']:
            count = graph['_full_rebuild'](conn)
        where = "enriched = 0 AND (is_deleted IS NULL OR is_deleted = 0)"
        if not force:
            where += (" AND (enriched_at IS NULL "
                      "OR enriched_at < datetime('now', '-5 minutes'))")
        pending = conn.execute(
            f"SELECT COUNT(*) FROM skill_nodes WHERE {where}"
        ).fetchone()[0]
        if pending > 0:
            import subprocess as _sp
            _log_dir = get_hermes_home() / "personal" / "skill-graph"
            _log_dir.mkdir(parents=True, exist_ok=True)
            _log_file = str(_log_dir / "enrichment.log")
            _lock_file = _log_dir / ".enrichment.lock"

            # Prevent concurrent enrichment runs
            if _lock_file.exists():
                import psutil
                try:
                    _stale_pid = int(_lock_file.read_text(encoding="utf-8-sig").strip())
                    if psutil.pid_exists(_stale_pid):
                        _cmd = " ".join(psutil.Process(_stale_pid).cmdline()).lower()
                        if "enrich" in _cmd or "skill_graph" in _cmd:
                            return (
                                f"Skill graph rebuilt: {count} skills indexed. "
                                f"{pending} skills pending enrichment — "
                                f"background enrichment already running (pid {_stale_pid})."
                            )
                        graph["logger"].warning("skill-graph: stale enrichment lock (pid %d alive but not enrich worker)", _stale_pid)
                except (ValueError, OSError, psutil.Error) as exc:
                    graph['_log_fallback_exception']("skill-graph: stale enrichment lock", exc)
            _script = _log_dir / "_enrich_worker.py"
            _script.write_text(f"""\
import importlib.util, sys, os, atexit, logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout)

_lock = "{_lock_file}"
_lock_dir = "{_log_dir}"
os.makedirs(_lock_dir, exist_ok=True)
with open(_lock, 'w') as f:
    f.write(str(os.getpid()))
atexit.register(lambda: os.remove(_lock) if os.path.exists(_lock) else None)

spec = importlib.util.spec_from_file_location(
    'skill_graph', "{Path(graph['__file__']).resolve()}",
    submodule_search_locations=["{Path(graph['__file__']).parent.resolve()}"],
)
mod = importlib.util.module_from_spec(spec)
sys.modules['skill_graph'] = mod
spec.loader.exec_module(mod)
conn = mod._get_conn()
mod._enrich_pending_skills(conn, limit={pending}, force={force})
""")
            with open(_log_file, "a", encoding="utf-8") as log:
                _sp.Popen(
                    [__import__("sys").executable, str(_script)],
                    stdout=log, stderr=log,
                    start_new_session=True,
                )
            msg = f"Skill graph rebuilt: {count} skills indexed. {pending} skills pending enrichment — running in background."
            if force:
                msg += " (forced — cooldown bypassed)"
            return msg
        return f"Skill graph rebuilt: {count} skills indexed."
    except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError,
            KeyError, AttributeError, IndexError, yaml.YAMLError) as exc:
        graph['_log_fallback_exception']("skill-graph: rebuild failed", exc)
        return "Rebuild failed: could not rebuild skill graph"


def _slash_help(graph: Mapping[str, Any]) -> str:
    """Default slash help, also used when search has no query."""
    return (
        "/skill-graph — Skill knowledge graph\n\n"
        "Subcommands:\n"
        "  /skill-graph search <query>   Search skills by intent\n"
        "  /skill-graph show <name>      Show full skill content (preview)\n"
        "  /skill-graph info <name>      Show skill metadata\n"
        "  /skill-graph terms <name>     Show term associations with stats\n"
        "  /skill-graph score <query>    Show scoring breakdown with term stats\n"
        "  /skill-graph list             List all skills in graph\n"
        "  /skill-graph config           Show configuration (paths, DB)\n"
        "  /skill-graph status           Show graph stats\n"
        "  /skill-graph rebuild          Force full graph rebuild\n"
        "  /skill-graph scene [list|show]  List scene distribution or show skills per scene\n"
    )


def _slash_graph_search(graph: Mapping[str, Any], rest: str) -> str:
    if not rest:
        return graph['_slash_help']()
    try:
        conn = graph['_ensure_graph']()
        with graph['_graph_lock']:
            results = graph['_search_graph'](rest, conn, limit=15)
        if not results:
            return f"No skills found for: {rest}"
        lines = [f"Search results for: {rest}", ""]
        for r in results:
            rel = r.get("relevance", "")
            chain = r.get("relationship_chain", [])
            extra = f" [{rel}]" if rel else ""
            if chain:
                extra += f"  chain: {' → '.join(chain[:2])}"
            lines.append(f"  {r['name']:35s}  {r.get('description', '')[:55]}{extra}")
        return "\n".join(lines)
    except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError, KeyError, AttributeError, IndexError) as exc:
        graph['_log_fallback_exception']("skill-graph: search failed", exc)
        return "Search failed: could not search skills"


def _slash_scene_distribution(graph: Mapping[str, Any], conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT scenes FROM skill_nodes WHERE (is_deleted IS NULL OR is_deleted = 0)").fetchall()
    counts, untagged = {}, 0
    for row in rows:
        try:
            slist = json.loads(row["scenes"]) if row["scenes"] else []
        except (ValueError, TypeError) as exc:
            graph['_log_fallback_exception']("skill-graph: invalid scene data", exc)
            slist = []
        if not slist:
            untagged += 1
        for s in slist:
            counts[s] = counts.get(s, 0) + 1
    lines = [f"Scene distribution ({len(rows)} skills):", ""]
    for s in graph['SCENE_VOCABULARY']:
        lines.append(f"  {s:12s} {counts.get(s, 0):4d}")
    if untagged:
        lines.append(f"  {'(untagged)':12s} {untagged:4d}")
    other = sum(v for k, v in counts.items() if k not in graph['SCENE_VOCABULARY'])
    if other:
        lines.append(f"  {'(other)':12s} {other:4d}")
    return "\n".join(lines)


def _slash_scene_show(graph: Mapping[str, Any], conn: sqlite3.Connection, scene_arg: str) -> str:
    rows = conn.execute(
        "SELECT name, description FROM skill_nodes WHERE (is_deleted IS NULL OR is_deleted = 0) "
        "AND instr(scenes, ?) > 0 ORDER BY name", (json.dumps(scene_arg),)).fetchall()
    if not rows:
        return f"No skills with scene: {scene_arg}"
    lines = [f"Skills with scene '{scene_arg}' ({len(rows)}):", ""]
    for row in rows:
        lines.append(f"  {row['name']:40s} {(row['description'] or '')[:80]}")
    return "\n".join(lines)


def _slash_graph_scene(graph: Mapping[str, Any], rest: str) -> str:
    parts = rest.strip().split(None, 1)
    scene_sub = parts[0].lower() if parts else "list"
    scene_arg = parts[1] if len(parts) > 1 else ""
    try:
        conn = graph['_ensure_graph']()
        if scene_sub == "list":
            return _slash_scene_distribution(graph, conn)
        if scene_sub == "show" and scene_arg:
            return _slash_scene_show(graph, conn, scene_arg)
        return "Usage: /sg scene list | /sg scene show <scene-name>"
    except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError, KeyError, AttributeError, IndexError) as exc:
        graph['_log_fallback_exception']("skill-graph: scene command failed", exc)
        return "Scene command failed: could not read graph results"


def _slash_graph_list(graph: Mapping[str, Any], _rest: str) -> str:
    try:
        conn = graph['_ensure_graph']()
        rows = conn.execute("SELECT name, description, category FROM skill_nodes ORDER BY name").fetchall()
        if not rows:
            return "No skills in graph."
        lines = [f"Skills in graph ({len(rows)}):", ""]
        for r in rows:
            lines.append(f"  {r['name']:35s}  [{r['category']}]  {(r['description'] or '')[:60]}")
        return "\n".join(lines)
    except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError, KeyError, AttributeError, IndexError) as exc:
        graph['_log_fallback_exception']("skill-graph: list failed", exc)
        return "List failed: could not read graph results"


def _slash_graph_score(graph: Mapping[str, Any], rest: str) -> str:
    """Show detailed scoring breakdown for a search query."""
    if not rest:
        return "Usage: /skill-graph score <query>"
    try:
        conn = graph['_ensure_graph']()
        with graph['_graph_lock']:
            results = graph['_search_graph'](rest, conn, limit=8)
            lines = [f"Score breakdown for: {rest}", ""]
            for r in results:
                name = r["name"]
                stats_rows = conn.execute(
                    "SELECT term, load_count, search_count FROM skill_term_stats WHERE skill_name = ?", (name,)
                ).fetchall()
                stats_line = (
                    "; ".join(f"{s['term']}: load={s['load_count']}/{s['search_count']}"
                              for s in stats_rows[:5]) if stats_rows else "(no stats)"
                )
                lines.append(f"  {name:40s} score={r['score']:.4f}  [{r.get('relevance', '?')}]")
                lines.append(f"  {'':40s}  stats: {stats_line}")
            lines.append(f"\n{len(results)} results shown")
            return "\n".join(lines)
    except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError, KeyError, AttributeError, IndexError) as exc:
        graph['_log_fallback_exception']("skill-graph: score breakdown failed", exc)
        return "Score breakdown failed: could not read graph results"


def _handle_slash_discovery(graph: Mapping[str, Any], subcmd: str, rest: str) -> str:
    """Dispatch graph-wide discovery commands and aliases."""
    handlers = {
        "status": graph['_slash_graph_status'], "stats": graph['_slash_graph_status'],
        "search": graph['_slash_graph_search'],
        "scene": graph['_slash_graph_scene'],
        "list": graph['_slash_graph_list'],
        "score": graph['_slash_graph_score'], "explain": graph['_slash_graph_score'],
    }
    if subcmd not in handlers:
        raise ValueError("Unsupported graph discovery command")
    if subcmd in ("status", "stats"):
        return handlers[subcmd]()
    return handlers[subcmd](rest)


def _slash_graph_config(graph: Mapping[str, Any], rest: str) -> str:
    """Dispatch configuration display or source-directory changes."""
    rest_parts = rest.strip().split(None, 1) if rest.strip() else []
    config_action = rest_parts[0].lower() if rest_parts else "show"
    config_arg = rest_parts[1] if len(rest_parts) > 1 else ""
    if config_action in ("add", "remove"):
        return graph['_handle_source_dir_config'](config_action, config_arg)
    return graph['_show_graph_config']()


def _handle_slash_command(graph: Mapping[str, Any], args: str) -> str | None:
    parts = args.strip().split(None, 1) if args.strip() else []
    subcmd = parts[0].lower() if parts else "help"
    rest = parts[1] if len(parts) > 1 else ""

    if subcmd in ("status", "stats", "search", "list", "score", "explain", "scene"):
        return graph['_handle_slash_discovery'](subcmd, rest)
    if subcmd == "config":
        return graph['_slash_graph_config'](rest)
    if subcmd == "rebuild":
        return graph['_slash_graph_rebuild'](rest)

    if subcmd == "show":
        """Show full skill content (preview)."""
        if not rest:
            return "Usage: /skill-graph show <skill-name>"
        try:
            result = graph['_handle_skill_load']({"name": rest})
            data = json.loads(result)
            if not data.get("success"):
                return f"Not found: {rest}"
            content = data.get("content", "")
            return (
                f"Skill: {data['name']} ({len(content)} chars)\n"
                f"  Description: {data.get('description', '')}\n"
                f"  Category:    {data.get('category', '')}\n"
                f"\n{content[:2000]}"
            )
        except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError,
                KeyError, AttributeError, IndexError) as exc:
            graph['_log_fallback_exception']("skill-graph: show failed", exc)
            return "Show failed: could not load skill"

    elif subcmd == "info":
        """Show skill metadata only."""
        if not rest:
            return "Usage: /skill-graph info <skill-name>"
        try:
            conn = graph['_ensure_graph']()
            node = conn.execute(
                "SELECT name, category, description, tags, file_path FROM skill_nodes WHERE name = ?",
                (rest,),
            ).fetchone()
            if not node:
                return f"Not found: {rest}  (try /sg list)"
            return "\n".join([
                f"Node: {node['name']}",
                f"  Category:    {node['category'] or ''}",
                f"  Description: {node['description'] or ''}",
                f"  Tags:        {node['tags'] or ''}",
                f"  Path:        {node['file_path'] or ''}",
            ])
        except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError,
                KeyError, AttributeError, IndexError) as exc:
            graph['_log_fallback_exception']("skill-graph: info failed", exc)
            return "Info failed: could not read skill metadata"

    elif subcmd == "terms":
        """Show term associations with stats."""
        if not rest:
            return "Usage: /skill-graph terms <skill-name>"
        try:
            return graph['_format_terms'](rest)
        except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError,
                KeyError, AttributeError, IndexError) as exc:
            graph['_log_fallback_exception']("skill-graph: terms failed", exc)
            return "Terms failed: could not read skill terms"

    else:
        # Unknown command — try proxying to a skill in the graph
        if subcmd:
            try:
                conn = graph['_ensure_graph']()
                _node = conn.execute(
                    "SELECT file_path FROM skill_nodes WHERE name = ?", (subcmd,)
                ).fetchone()
                if _node:
                    _result = graph['_handle_skill_load']({"name": subcmd})
                    _data = json.loads(_result)
                    if _data.get("success"):
                        _content = _data.get("content", "")
                        return (
                            f"Loaded skill: {subcmd}\n"
                            f"  Description: {_data.get('description', '')}\n"
                            f"  Category:    {_data.get('category', '')}\n"
                            f"  Content ({len(_content)} chars):\n"
                            f"{_content[:500]}\n"
                            f"...\n"
                            f"(Use /sg info {subcmd} for metadata, "
                            f"/sg terms {subcmd} for term details)"
                        )
            except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError,
                    KeyError, AttributeError, IndexError) as exc:
                graph['_log_fallback_exception']("skill-graph: command proxy failed", exc)
        return graph['_slash_help']()
