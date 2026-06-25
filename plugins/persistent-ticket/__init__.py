"""
Persistent Ticket plugin — cross-profile ticket/issue tracking system.

Stores tickets in a shared SQLite DB at a configurable path
(default: ~/.hermes/personal/ticket/ticket.db).

Configuration (in config.yaml):
    ticket:
      base: ~/.hermes/personal/ticket     # Default
      auto_clean_days: 0                 # 0=disabled, N=auto-delete done items older than N days

Tools:
  - ticket_add     — add a global todo
  - ticket_list    — list todos (across all scopes by default)
  - ticket_done    — mark a todo done
  - ticket_cancel  — cancel a todo
  - profile_ticket_add    — add a todo scoped to current profile
  - profile_ticket_list   — list current profile's todos
  - profile_ticket_done   — mark a todo done, attributing to current profile

Slash commands:
  /ticket add <content>   — quick add
  /ticket list             — list pending
  /ticket done <id>        — mark done
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

TICKET_DB_FILENAME = "todo.db"
TICKET_LOCK = threading.Lock()

# ── Schema ──────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ticket_items (
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

CREATE INDEX IF NOT EXISTS idx_ticket_status   ON ticket_items(status);
CREATE INDEX IF NOT EXISTS idx_ticket_scope    ON ticket_items(scope);
CREATE INDEX IF NOT EXISTS idx_ticket_category ON ticket_items(category);
CREATE INDEX IF NOT EXISTS idx_ticket_profile  ON ticket_items(profile);
"""

# ── Config helpers ──────────────────────────────────────────────────────


def _read_config() -> dict:
    """Read the ``ticket`` section from config.yaml."""
    try:
        from hermes_cli.config import load_config
        config = load_config()
        return config.get("ticket", {})
    except Exception:
        return {}


def _base_path() -> Path:
    """Return the resolved ticket base directory from config."""
    cfg = _read_config()
    raw = cfg.get("base", "~/.hermes/personal/ticket")
    return Path(os.path.expandvars(os.path.expanduser(str(raw)))).resolve()


def _db_path() -> Path:
    return _base_path() / TICKET_DB_FILENAME


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
        "DELETE FROM ticket_items WHERE status IN ('done','cancelled')"
        " AND done_at IS NOT NULL AND done_at < ?",
        (cutoff,),
    )
    deleted = cursor.rowcount
    if deleted:
        logger.info(
            "persistent-ticket: auto-cleaned %d items (cutoff=%s, days=%d)",
            deleted, cutoff, days,
        )
    conn.commit()


# ── Tool handlers ───────────────────────────────────────────────────────


def _get_current_profile() -> str:
    """Return the active profile name, or ``'default'``."""
    return os.environ.get("HERMES_PROFILE", os.environ.get("HERMES_PROFILE_NAME", "default"))


def _handle_ticket_add(args: dict | None = None, **kw: Any) -> str:
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
        with TICKET_LOCK:
            _auto_clean(conn)
            cursor = conn.execute(
                """INSERT INTO ticket_items
                   (content, priority, category, notes, project, deadline,
                    scope, profile, worker)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (content, priority, category, notes, project,
                 deadline or None, scope, profile, worker),
            )
            conn.commit()
            ticket_id = cursor.lastrowid
        return json.dumps({
            "success": True,
            "id": ticket_id,
            "scope": scope,
            "message": f"Ticket #{todo_id} added [{scope}]",
        }, ensure_ascii=False)
    except Exception as e:
        logger.exception("ticket_add failed")
        return json.dumps({"success": False, "error": str(e)})


def _handle_ticket_list(args: dict | None = None, **kw: Any) -> str:
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
            f"SELECT * FROM ticket_items WHERE {where}"
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
                "SELECT scope, COUNT(*) as cnt FROM ticket_items WHERE status='pending' GROUP BY scope ORDER BY scope"
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
        logger.exception("ticket_list failed")
        return json.dumps({"success": False, "error": str(e)})


def _handle_ticket_done(args: dict | None = None, **kw: Any) -> str:
    if not isinstance(args, dict):
        args = kw.get("args", kw)
    ticket_id = args.get("id", 0)
    if not ticket_id:
        return json.dumps({"success": False, "error": "id is required"})

    profile = args.get("profile", "")
    worker = args.get("worker", "")
    notes = args.get("notes", "")

    try:
        conn = _ensure_db()
        with TICKET_LOCK:
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

            eparams.append(str(ticket_id))
            conn.execute(
                f"UPDATE ticket_items SET {set_clause} WHERE id = ?",
                eparams,
            )
            conn.commit()
        return json.dumps({
            "success": True,
            "message": f"Ticket #{todo_id} marked as done",
        }, ensure_ascii=False)
    except Exception as e:
        logger.exception("ticket_done failed")
        return json.dumps({"success": False, "error": str(e)})


def _handle_ticket_cancel(args: dict | None = None, **kw: Any) -> str:
    if not isinstance(args, dict):
        args = kw.get("args", kw)
    ticket_id = args.get("id", 0)
    if not ticket_id:
        return json.dumps({"success": False, "error": "id is required"})

    reason = args.get("reason", "")

    try:
        conn = _ensure_db()
        with TICKET_LOCK:
            _auto_clean(conn)
            if reason:
                conn.execute(
                    "UPDATE ticket_items SET status = 'cancelled',"
                    " notes = CASE WHEN notes = '' THEN ? ELSE notes || '\\n' || ? END,"
                    " updated_at = datetime('now') WHERE id = ?",
                    (reason, reason, ticket_id),
                )
            else:
                conn.execute(
                    "UPDATE ticket_items SET status = 'cancelled',"
                    " updated_at = datetime('now') WHERE id = ?",
                    (ticket_id,),
                )
            conn.commit()
        return json.dumps({
            "success": True,
            "message": f"Ticket #{todo_id} cancelled",
        }, ensure_ascii=False)
    except Exception as e:
        logger.exception("ticket_cancel failed")
        return json.dumps({"success": False, "error": str(e)})


# ── Profile-scoped wrappers ────────────────────────────────────────────


def _handle_profile_ticket_add(args: dict | None = None, **kw: Any) -> str:
    """Add a ticket scoped to the current Hermes profile."""
    if not isinstance(args, dict):
        args = kw.get("args", kw)
    profile = _get_current_profile()
    args["scope"] = profile
    args["profile"] = args.get("profile") or profile
    return _handle_ticket_add(args)


def _handle_profile_ticket_list(args: dict | None = None, **kw: Any) -> str:
    """List tickets from the current profile."""
    if not isinstance(args, dict):
        args = kw.get("args", kw)
    args["scope"] = _get_current_profile()
    return _handle_ticket_list(args)


def _handle_profile_ticket_done(args: dict | None = None, **kw: Any) -> str:
    """Mark a ticket done, attributing to the current profile."""
    if not isinstance(args, dict):
        args = kw.get("args", kw)
    args["profile"] = _get_current_profile()
    return _handle_ticket_done(args)


# ── Slash command ───────────────────────────────────────────────────────


def _handle_ticket_slash(args_str: str) -> str:
    """Handle ``/ticket [scope] <subcommand> [args]``.

    If first token is a subcommand (add/list/done/cancel/config), scope defaults to global.
    If first token is a scope name (global or profile name), second token is the subcommand.
    """
    args_str = (args_str or "").strip()
    if not args_str:
        return (
            "Ticket — persistent ticket system\n\n"
            "Usage:\n"
            "  /ticket add <content>           Add a global ticket\n"
            "  /ticket global add <content>    Explicit global ticket\n"
            "  /ticket eir add <content>       Ticket scoped to a profile\n"
            "  /ticket list [status]           List all pending tickets\n"
            "  /ticket eir list                List tickets for a profile\n"
            "  /ticket done <id>               Mark done\n"
            "  /ticket cancel <id>             Cancel\n"
            "  /ticket config                  Show config\n\n"
            "Status values: pending (default), in_progress, partial, done, cancelled"
        )

    tokens = args_str.split()
    subcmds = {"add", "list", "done", "cancel", "config"}

    # Determine scope and subcommand
    if tokens[0].lower() in subcmds:
        scope = "global"
        subcmd = tokens[0].lower()
        rest = " ".join(tokens[1:])
    else:
        scope = tokens[0].lower()
        if len(tokens) < 2 or tokens[1].lower() not in subcmds:
            return f"Unknown command or missing subcommand after '{scope}'.\n" \
                   f"Try: /ticket {scope} add <content>, /ticket {scope} list, etc."
        subcmd = tokens[1].lower()
        rest = " ".join(tokens[2:])

    if subcmd == "add":
        if not rest:
            return "Usage: /ticket add <content>"
        result = _handle_ticket_add({
            "content": rest,
            "scope": scope,
        })
        data = json.loads(result)
        if data.get("success"):
            scope_tag = f" [{scope}]" if scope != "global" else ""
            return f"✅ Ticket #{data['id']} added{scope_tag}: {rest}"
        return f"❌ {data.get('error', 'unknown error')}"

    elif subcmd == "list":
        status = rest if rest in ("pending", "in_progress", "partial", "done", "cancelled") else "pending"
        list_args = {"status": status}
        if scope != "global":
            list_args["scope"] = scope
        result = _handle_ticket_list(list_args)
        data = json.loads(result)
        if not data.get("success"):
            return f"❌ {data.get('error', 'unknown error')}"
        items = data.get("results", [])
        if not items:
            scope_tag = f" for '{scope}'" if scope != "global" else ""
            return f"No {status} tickets{scope_tag}."
        scope_tag = f" ({scope})" if scope != "global" else ""
        lines = [f"Tickets{scope_tag} ({status}, {len(items)}):", ""]

        # Summary by scope (only for global/all-scope view)
        summary = data.get("summary_by_scope", [])
        if summary and len(summary) > 1 and scope == "global":
            scope_counts = " | ".join(
                f"{s['scope']}={s['count']}" for s in summary
            )
            lines.append(f"  Per scope: {scope_counts}")
            lines.append("")

        for t in items:
            flag = "[P0]" if t["priority"] == "P0" else ""
            scope_tag2 = f"[{t['scope']}]" if t["scope"] != "global" else ""
            cat_tag = f"({t['category']})" if t["category"] else ""
            worker_tag = f"~{t['worker']}" if t["worker"] else ""
            deadline_tag = f" {t['deadline']}" if t["deadline"] else ""
            lines.append(
                f"  #{t['id']:>4} {flag:<4} {scope_tag2}{cat_tag}{worker_tag}"
                f"  {t['content'][:60]}{deadline_tag}"
            )
            if t["status"] != "pending":
                lines[-1] += f" [{t['status']}]"
        return "\n".join(lines)

    elif subcmd == "done":
        if not rest or not rest.isdigit():
            return "Usage: /ticket done <id>"
        result = _handle_ticket_done({"id": int(rest)})
        data = json.loads(result)
        if data.get("success"):
            return f"Ticket #{rest} done"
        return f"Error: {data.get('error', 'unknown error')}"

    elif subcmd == "cancel":
        if not rest or not rest.isdigit():
            return "Usage: /ticket cancel <id>"
        result = _handle_ticket_cancel({"id": int(rest)})
        data = json.loads(result)
        if data.get("success"):
            return f"Ticket #{rest} cancelled"
        return f"Error: {data.get('error', 'unknown error')}"

    elif subcmd == "config":
        cfg = _read_config()
        base = _base_path()
        db_path = _db_path()
        days = cfg.get("auto_clean_days", 0)
        db_size = db_path.stat().st_size if db_path.exists() else 0
        conn = _ensure_db()
        total = conn.execute("SELECT COUNT(*) FROM ticket_items").fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM ticket_items WHERE status='pending'"
        ).fetchone()[0]
        return (
            f"Ticket configuration\\n"
            f"  Base path:  {base}\\n"
            f"  DB:         {db_path} ({db_size / 1024:.1f} KB)\\n"
            f"  Auto-clean: {'disabled' if days == 0 else f'{days} days'}\\n"
            f"  Items:      {total} total, {pending} pending"
        )

    else:
        return f"Unknown subcommand: {subcmd}. Try: add, list, done, cancel, config"


# ── Plugin registration ────────────────────────────────────────────────


def register(ctx):
    """Register persistent-ticket plugin."""

    # ── Tools ──────────────────────────────────────────────────────────

    tools = [
        ("ticket_add", "Add a persistent todo to the global todo system. "
            "Use for cross-profile items, unknowns, and tracked tasks.",
         {
             "type": "object",
             "properties": {
                 "content":  {"type": "string", "description": "Ticket text"},
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

        ("ticket_list", "List todos from the global system. "
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

        ("ticket_done", "Mark a todo as done. "
            "Optionally record who completed it.",
         {
             "type": "object",
             "properties": {
                 "id":      {"type": "integer", "description": "Ticket ID"},
                 "profile": {"type": "string", "description": "Completing profile"},
                 "worker":  {"type": "string", "description": "Completing worker"},
                 "notes":   {"type": "string", "description": "Completion notes"},
             },
             "required": ["id"],
         }),

        ("ticket_cancel", "Cancel/abandon a todo without completing it. "
            "Optionally record a reason.",
         {
             "type": "object",
             "properties": {
                 "id":     {"type": "integer", "description": "Ticket ID"},
                 "reason": {"type": "string", "description": "Why it was cancelled"},
             },
             "required": ["id"],
         }),

        ("profile_ticket_add", "Add a todo scoped to the current Hermes profile. "
            "Same as ticket_add but scope auto-set to current profile.",
         {
             "type": "object",
             "properties": {
                 "content":  {"type": "string", "description": "Ticket text"},
                 "priority": {"type": "string", "description": "P0/P1/P2/P3", "default": "P2"},
                 "category": {"type": "string", "description": "Category tag"},
                 "notes":    {"type": "string", "description": "Context"},
                 "project":  {"type": "string", "description": "Associated project"},
                 "deadline": {"type": "string", "description": "Deadline (ISO date)"},
                 "worker":   {"type": "string", "description": "Source worker name"},
             },
             "required": ["content"],
         }),

        ("profile_ticket_list", "List todos scoped to the current Hermes profile.",
         {
             "type": "object",
             "properties": {
                 "status":   {"type": "string", "description": "pending/done/cancelled"},
                 "category": {"type": "string", "description": "Filter by category"},
                 "worker":   {"type": "string", "description": "Filter by worker"},
             },
         }),

        ("profile_ticket_done", "Mark a todo as done, attributing to current profile.",
         {
             "type": "object",
             "properties": {
                 "id":    {"type": "integer", "description": "Ticket ID"},
                 "notes": {"type": "string", "description": "Completion notes"},
             },
             "required": ["id"],
         }),
    ]

    for name, desc, schema in tools:
        ctx.register_tool(
            name=name,
            toolset="ticket",  # separate toolset for clean grouping
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
        name="ticket",
        handler=_handle_ticket_slash,
        description="Ticket system: add, list, done, cancel, config",
        args_hint="add|list|done|cancel|config",
    )

    # ── Start-up ───────────────────────────────────────────────────────

    def _on_session_start(**kw):
        try:
            conn = _ensure_db()
            _auto_clean(conn)
            conn.close()
        except Exception:
            logger.exception("persistent-ticket: on_session_start failed")

    ctx.register_hook("on_session_start", _on_session_start)

    logger.info(
        "persistent-ticket plugin registered: 7 tools (ticket_* + profile_ticket_*),"
        " /ticket command, on_session_start hook"
    )
