"""
Global Todo plugin — persistent, cross-profile todo system.

Stores todos in a shared SQLite DB at a configurable path
(default: ~/.hermes/personal/todo/todo.db).

Configuration (in config.yaml):
    todo:
      base: ~/.hermes/personal/todo     # Default
      auto_clean_days: 0                 # 0=disabled, N=auto-delete done items older than N days

Tools:
  - global_todo_add     — add a global todo
  - global_todo_list    — list todos (across all scopes by default)
  - global_todo_done    — mark a todo done
  - global_todo_cancel  — cancel a todo
  - profile_todo_add    — add a todo scoped to current profile
  - profile_todo_list   — list current profile's todos
  - profile_todo_done   — mark a todo done, attributing to current profile

Slash commands:
  /todo add <content>   — quick add
  /todo list             — list pending
  /todo done <id>        — mark done
"""

from __future__ import annotations
import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TODO_DB_FILENAME = "todo.db"
TODO_LOCK = threading.Lock()

# ── Schema ──────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS todo_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    content       TEXT NOT NULL,
    status        TEXT DEFAULT 'pending',
    priority      TEXT DEFAULT 'P2',
    category      TEXT DEFAULT '',
    notes         TEXT DEFAULT '',
    project       TEXT DEFAULT '',
    deadline      TEXT,
    scope         TEXT DEFAULT 'global',
    profile       TEXT DEFAULT '',
    worker        TEXT DEFAULT '',
    created_at    TEXT DEFAULT (datetime('now')),
    done_at       TEXT,
    done_profile  TEXT DEFAULT '',
    done_worker   TEXT DEFAULT '',
    updated_at    TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_todo_status   ON todo_items(status);
CREATE INDEX IF NOT EXISTS idx_todo_scope    ON todo_items(scope);
CREATE INDEX IF NOT EXISTS idx_todo_category ON todo_items(category);
CREATE INDEX IF NOT EXISTS idx_todo_profile  ON todo_items(profile);
"""

# ── Config helpers ──────────────────────────────────────────────────────


def _read_config() -> dict:
    """Read the ``todo`` section from config.yaml."""
    try:
        from hermes_cli.config import load_config
        config = load_config()
        return config.get("todo", {})
    except Exception:
        return {}


def _base_path() -> Path:
    """Return the resolved todo base directory from config."""
    cfg = _read_config()
    raw = cfg.get("base", "~/.hermes/personal/todo")
    return Path(os.path.expandvars(os.path.expanduser(str(raw)))).resolve()


def _db_path() -> Path:
    return _base_path() / TODO_DB_FILENAME


# ── DB helpers ──────────────────────────────────────────────────────────


def _get_conn() -> sqlite3.Connection:
    db_path = _db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def _ensure_db() -> sqlite3.Connection:
    conn = _get_conn()
    _init_db(conn)
    return conn


# ── Auto-clean ──────────────────────────────────────────────────────────


def _auto_clean(conn: sqlite3.Connection) -> None:
    """Remove ``done``/``cancelled`` items older than ``auto_clean_days``."""
    cfg = _read_config()
    days = cfg.get("auto_clean_days", 0)
    if not days or int(days) <= 0:
        return
    days = int(days)
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    cursor = conn.execute(
        "DELETE FROM todo_items WHERE status IN ('done','cancelled')"
        " AND done_at IS NOT NULL AND done_at < ?",
        (cutoff,),
    )
    deleted = cursor.rowcount
    if deleted:
        logger.info(
            "global-todo: auto-cleaned %d items (cutoff=%s, days=%d)",
            deleted, cutoff, days,
        )
    conn.commit()


# ── Tool handlers ───────────────────────────────────────────────────────


def _get_current_profile() -> str:
    """Return the active profile name, or ``'default'``."""
    return os.environ.get("HERMES_PROFILE", os.environ.get("HERMES_PROFILE_NAME", "default"))


def _handle_global_todo_add(args: dict | None = None, **kw: Any) -> str:
    if not isinstance(args, dict):
        args = kw.get("args", kw)
    content = (args.get("content", "") if isinstance(args, dict) else "").strip()
    if not content:
        return json.dumps({"success": False, "error": "content is required"})

    priority = args.get("priority", "P2")
    category = args.get("category", "")
    notes = args.get("notes", "")
    project = args.get("project", "")
    deadline = args.get("deadline", "")
    scope = args.get("scope", "global")
    profile = args.get("profile", "")
    worker = args.get("worker", "")

    try:
        conn = _ensure_db()
        with TODO_LOCK:
            _auto_clean(conn)
            cursor = conn.execute(
                """INSERT INTO todo_items
                   (content, priority, category, notes, project, deadline,
                    scope, profile, worker)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (content, priority, category, notes, project,
                 deadline or None, scope, profile, worker),
            )
            conn.commit()
            todo_id = cursor.lastrowid
        return json.dumps({
            "success": True,
            "id": todo_id,
            "scope": scope,
            "message": f"Todo #{todo_id} added [{scope}]",
        }, ensure_ascii=False)
    except Exception as e:
        logger.exception("global_todo_add failed")
        return json.dumps({"success": False, "error": str(e)})


def _handle_global_todo_list(args: dict | None = None, **kw: Any) -> str:
    if not isinstance(args, dict):
        args = kw.get("args", kw)

    status = args.get("status", "pending")
    category = args.get("category", "")
    scope_filter = args.get("scope", "")
    profile_filter = args.get("profile", "")
    worker_filter = args.get("worker", "")

    try:
        conn = _ensure_db()
        conditions: list[str] = []
        params: list[str] = []

        if status:
            conditions.append("status = ?")
            params.append(status)
        if category:
            conditions.append("category = ?")
            params.append(category)
        if scope_filter:
            conditions.append("scope = ?")
            params.append(scope_filter)
        if profile_filter:
            conditions.append("profile = ?")
            params.append(profile_filter)
        if worker_filter:
            conditions.append("worker = ?")
            params.append(worker_filter)

        where = " AND ".join(conditions) if conditions else "1=1"
        rows = conn.execute(
            f"SELECT * FROM todo_items WHERE {where}"
            " ORDER BY CASE priority"
            "   WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 WHEN 'P3' THEN 3"
            "   ELSE 4 END, created_at DESC LIMIT 200",
            params,
        ).fetchall()

        results = [{
            "id": r["id"],
            "content": r["content"],
            "status": r["status"],
            "priority": r["priority"],
            "category": r["category"] or "",
            "notes": r["notes"] or "",
            "project": r["project"] or "",
            "deadline": r["deadline"] or "",
            "scope": r["scope"],
            "profile": r["profile"] or "",
            "worker": r["worker"] or "",
            "created_at": r["created_at"],
            "done_at": r["done_at"] or "",
        } for r in rows]

        # Build summary by scope (pending counts per scope)
        if status == "pending":
            scope_rows = conn.execute(
                "SELECT scope, COUNT(*) as cnt FROM todo_items WHERE status='pending' GROUP BY scope ORDER BY scope"
            ).fetchall()
            summary_by_scope = [{"scope": r["scope"], "count": r["cnt"]} for r in scope_rows]
        else:
            summary_by_scope = []

        return json.dumps({
            "success": True,
            "count": len(results),
            "results": results,
            "summary_by_scope": summary_by_scope,
        }, ensure_ascii=False)
    except Exception as e:
        logger.exception("global_todo_list failed")
        return json.dumps({"success": False, "error": str(e)})


def _handle_global_todo_done(args: dict | None = None, **kw: Any) -> str:
    if not isinstance(args, dict):
        args = kw.get("args", kw)
    todo_id = args.get("id", 0)
    if not todo_id:
        return json.dumps({"success": False, "error": "id is required"})

    profile = args.get("profile", "")
    worker = args.get("worker", "")
    notes = args.get("notes", "")

    try:
        conn = _ensure_db()
        with TODO_LOCK:
            _auto_clean(conn)
            extras: list[str] = []
            eparams: list[str] = []
            if profile:
                extras.append("done_profile = ?")
                eparams.append(profile)
            if worker:
                extras.append("done_worker = ?")
                eparams.append(worker)
            if notes:
                extras.append(
                    "notes = CASE WHEN notes = '' THEN ? ELSE notes || '\\n' || ? END"
                )
                eparams.extend([notes, notes])

            set_clause = "status = 'done', done_at = datetime('now'), updated_at = datetime('now')"
            if extras:
                set_clause += ", " + ", ".join(extras)

            eparams.append(str(todo_id))
            conn.execute(
                f"UPDATE todo_items SET {set_clause} WHERE id = ?",
                eparams,
            )
            conn.commit()
        return json.dumps({
            "success": True,
            "message": f"Todo #{todo_id} marked as done",
        }, ensure_ascii=False)
    except Exception as e:
        logger.exception("global_todo_done failed")
        return json.dumps({"success": False, "error": str(e)})


def _handle_global_todo_cancel(args: dict | None = None, **kw: Any) -> str:
    if not isinstance(args, dict):
        args = kw.get("args", kw)
    todo_id = args.get("id", 0)
    if not todo_id:
        return json.dumps({"success": False, "error": "id is required"})

    reason = args.get("reason", "")

    try:
        conn = _ensure_db()
        with TODO_LOCK:
            _auto_clean(conn)
            if reason:
                conn.execute(
                    "UPDATE todo_items SET status = 'cancelled',"
                    " notes = CASE WHEN notes = '' THEN ? ELSE notes || '\\n' || ? END,"
                    " updated_at = datetime('now') WHERE id = ?",
                    (reason, reason, todo_id),
                )
            else:
                conn.execute(
                    "UPDATE todo_items SET status = 'cancelled',"
                    " updated_at = datetime('now') WHERE id = ?",
                    (todo_id,),
                )
            conn.commit()
        return json.dumps({
            "success": True,
            "message": f"Todo #{todo_id} cancelled",
        }, ensure_ascii=False)
    except Exception as e:
        logger.exception("global_todo_cancel failed")
        return json.dumps({"success": False, "error": str(e)})


# ── Profile-scoped wrappers ────────────────────────────────────────────


def _handle_profile_todo_add(args: dict | None = None, **kw: Any) -> str:
    """Add a todo scoped to the current Hermes profile."""
    if not isinstance(args, dict):
        args = kw.get("args", kw)
    profile = _get_current_profile()
    args["scope"] = profile
    args["profile"] = args.get("profile") or profile
    return _handle_global_todo_add(args)


def _handle_profile_todo_list(args: dict | None = None, **kw: Any) -> str:
    """List todos from the current profile."""
    if not isinstance(args, dict):
        args = kw.get("args", kw)
    args["scope"] = _get_current_profile()
    return _handle_global_todo_list(args)


def _handle_profile_todo_done(args: dict | None = None, **kw: Any) -> str:
    """Mark a todo done, attributing to the current profile."""
    if not isinstance(args, dict):
        args = kw.get("args", kw)
    args["profile"] = _get_current_profile()
    return _handle_global_todo_done(args)


# ── Slash command ───────────────────────────────────────────────────────


def _handle_todo_slash(args_str: str) -> str:
    """Handle ``/todo <subcommand> [args]``."""
    args_str = (args_str or "").strip()
    if not args_str:
        return (
            "Todo — persistent todo system\\n\\n"
            "Subcommands:\\n"
            "  /todo add <content>       Add a global todo\\n"
            "  /todo list [status]       List todos (default: pending)\\n"
            "  /todo done <id>           Mark done\\n"
            "  /todo cancel <id>         Cancel a todo\\n"
            "  /todo config              Show configuration"
        )

    parts = args_str.split(None, 1)
    subcmd = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if subcmd == "add":
        if not rest:
            return "Usage: /todo add <content>"
        result = _handle_global_todo_add({
            "content": rest,
            "scope": "global",
        })
        data = json.loads(result)
        if data.get("success"):
            return f"✅ Todo #{data['id']} added: {rest}"
        return f"❌ {data.get('error', 'unknown error')}"

    elif subcmd == "list":
        status = rest if rest in ("pending", "done", "cancelled") else "pending"
        result = _handle_global_todo_list({"status": status})
        data = json.loads(result)
        if not data.get("success"):
            return f"❌ {data.get('error', 'unknown error')}"
        items = data.get("results", [])
        if not items:
            return f"No {status} todos."
        lines = [f"Todos ({status}, {len(items)}):", ""]

        # Summary by scope
        summary = data.get("summary_by_scope", [])
        if summary and len(summary) > 1:
            scope_counts = " | ".join(
                f"{s['scope']}={s['count']}" for s in summary
            )
            lines.append(f"  📊 Per scope: {scope_counts}")
            lines.append("")

        for t in items:
            flag = "[P0]" if t["priority"] == "P0" else ""
            scope_tag = f"[{t['scope']}]" if t["scope"] != "global" else ""
            cat_tag = f"({t['category']})" if t["category"] else ""
            worker_tag = f"~{t['worker']}" if t["worker"] else ""
            deadline_tag = f" ⏰{t['deadline']}" if t["deadline"] else ""
            lines.append(
                f"  #{t['id']:>4} {flag:<4} {scope_tag}{cat_tag}{worker_tag}"
                f"  {t['content'][:60]} {deadline_tag}"
            )
        return "\n".join(lines)

    elif subcmd == "done":
        if not rest or not rest.isdigit():
            return "Usage: /todo done <id>"
        result = _handle_global_todo_done({"id": int(rest)})
        data = json.loads(result)
        if data.get("success"):
            return f"✅ Todo #{rest} marked as done"
        return f"❌ {data.get('error', 'unknown error')}"

    elif subcmd == "cancel":
        if not rest or not rest.isdigit():
            return "Usage: /todo cancel <id>"
        result = _handle_global_todo_cancel({"id": int(rest)})
        data = json.loads(result)
        if data.get("success"):
            return f"✅ Todo #{rest} cancelled"
        return f"❌ {data.get('error', 'unknown error')}"

    elif subcmd == "config":
        cfg = _read_config()
        base = _base_path()
        db_path = _db_path()
        days = cfg.get("auto_clean_days", 0)
        db_size = db_path.stat().st_size if db_path.exists() else 0
        conn = _ensure_db()
        total = conn.execute("SELECT COUNT(*) FROM todo_items").fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM todo_items WHERE status='pending'"
        ).fetchone()[0]
        return (
            f"Todo configuration\\n"
            f"  Base path:  {base}\\n"
            f"  DB:         {db_path} ({db_size / 1024:.1f} KB)\\n"
            f"  Auto-clean: {'disabled' if days == 0 else f'{days} days'}\\n"
            f"  Items:      {total} total, {pending} pending"
        )

    else:
        return f"Unknown subcommand: {subcmd}. Try: add, list, done, cancel, config"


# ── Plugin registration ────────────────────────────────────────────────


def register(ctx):
    """Register global-todo plugin."""

    # ── Tools ──────────────────────────────────────────────────────────

    tools = [
        ("global_todo_add", "Add a persistent todo to the global todo system. "
            "Use for cross-profile items, unknowns, and tracked tasks.",
         {
             "type": "object",
             "properties": {
                 "content":  {"type": "string", "description": "Todo text"},
                 "priority": {"type": "string", "description": "P0/P1/P2/P3", "default": "P2"},
                 "category": {"type": "string", "description": "Category tag (unknown, bug, design-decision, etc.)"},
                 "notes":    {"type": "string", "description": "Additional context"},
                 "project":  {"type": "string", "description": "Associated project name"},
                 "deadline": {"type": "string", "description": "Deadline (ISO date)"},
                 "scope":    {"type": "string", "description": "'global' or profile name"},
                 "profile":  {"type": "string", "description": "Source profile name"},
                 "worker":   {"type": "string", "description": "Source worker name"},
             },
             "required": ["content"],
         }),

        ("global_todo_list", "List todos from the global system. "
            "Defaults to pending items across all scopes. "
            "Filter by status, category, scope, profile, or worker.",
         {
             "type": "object",
             "properties": {
                 "status":   {"type": "string", "description": "pending/done/cancelled"},
                 "category": {"type": "string", "description": "Filter by category"},
                 "scope":    {"type": "string", "description": "Filter by scope (global or profile name)"},
                 "profile":  {"type": "string", "description": "Filter by source profile"},
                 "worker":   {"type": "string", "description": "Filter by source worker"},
             },
         }),

        ("global_todo_done", "Mark a todo as done. "
            "Optionally record who completed it.",
         {
             "type": "object",
             "properties": {
                 "id":      {"type": "integer", "description": "Todo ID"},
                 "profile": {"type": "string", "description": "Completing profile"},
                 "worker":  {"type": "string", "description": "Completing worker"},
                 "notes":   {"type": "string", "description": "Completion notes"},
             },
             "required": ["id"],
         }),

        ("global_todo_cancel", "Cancel/abandon a todo without completing it. "
            "Optionally record a reason.",
         {
             "type": "object",
             "properties": {
                 "id":     {"type": "integer", "description": "Todo ID"},
                 "reason": {"type": "string", "description": "Why it was cancelled"},
             },
             "required": ["id"],
         }),

        ("profile_todo_add", "Add a todo scoped to the current Hermes profile. "
            "Same as global_todo_add but scope auto-set to current profile.",
         {
             "type": "object",
             "properties": {
                 "content":  {"type": "string", "description": "Todo text"},
                 "priority": {"type": "string", "description": "P0/P1/P2/P3", "default": "P2"},
                 "category": {"type": "string", "description": "Category tag"},
                 "notes":    {"type": "string", "description": "Context"},
                 "project":  {"type": "string", "description": "Associated project"},
                 "deadline": {"type": "string", "description": "Deadline (ISO date)"},
                 "worker":   {"type": "string", "description": "Source worker name"},
             },
             "required": ["content"],
         }),

        ("profile_todo_list", "List todos scoped to the current Hermes profile.",
         {
             "type": "object",
             "properties": {
                 "status":   {"type": "string", "description": "pending/done/cancelled"},
                 "category": {"type": "string", "description": "Filter by category"},
                 "worker":   {"type": "string", "description": "Filter by worker"},
             },
         }),

        ("profile_todo_done", "Mark a todo as done, attributing to current profile.",
         {
             "type": "object",
             "properties": {
                 "id":    {"type": "integer", "description": "Todo ID"},
                 "notes": {"type": "string", "description": "Completion notes"},
             },
             "required": ["id"],
         }),
    ]

    for name, desc, schema in tools:
        ctx.register_tool(
            name=name,
            toolset="todo",  # separate toolset for clean grouping
            schema={
                "name": name,
                "description": desc,
                "parameters": schema,
            },
            handler=globals()[f"_handle_{name}"],
            description=desc,
            check_fn=None,
        )

    # ── Slash commands ─────────────────────────────────────────────────

    ctx.register_command(
        name="todo",
        handler=_handle_todo_slash,
        description="Persistent todo system: add, list, done, cancel, config",
        args_hint="add|list|done|cancel|config",
    )

    # ── Start-up ───────────────────────────────────────────────────────

    def _on_session_start(**kw):
        try:
            conn = _ensure_db()
            _auto_clean(conn)
            conn.close()
        except Exception:
            logger.exception("global-todo: on_session_start failed")

    ctx.register_hook("on_session_start", _on_session_start)

    logger.info(
        "global-todo plugin registered: 7 tools (global_todo_* + profile_todo_*),"
        " /todo command, on_session_start hook"
    )
