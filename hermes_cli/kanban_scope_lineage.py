"""Durable, dispatcher-owned project-scope lineage for Kanban attempts.

Only the trusted project-scope command creates a root.  Cards and workers carry
an opaque attempt reference; policy snapshots never enter card text or prompts.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time
import uuid
from typing import Any

_MAX_DEPTH = 8
_known_registry_paths: set[Path] = set()


@dataclass(frozen=True)
class KanbanScopeRoot:
    root_ref: str
    board: str
    task_id: str
    assignee: str | None
    activation_id: str
    root_session_key: str
    policy_digest: str
    template_json: str


@dataclass(frozen=True)
class KanbanScopeAttempt:
    attempt_ref: str
    root_ref: str
    board: str
    task_id: str
    run_id: int
    claim_lock: str
    depth: int


def registry_path(board_db: str | Path) -> Path:
    """Keep the capability registry next to the board DB, not in task data."""
    path = Path(board_db).expanduser().resolve()
    return path.with_name(path.name + ".scope-registry.sqlite3")


def _connect(path: str | Path) -> sqlite3.Connection:
    target = registry_path(path)
    _known_registry_paths.add(target)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS project_scope_roots (
      root_ref TEXT PRIMARY KEY, board TEXT NOT NULL, task_id TEXT NOT NULL,
      assignee TEXT, activation_id TEXT NOT NULL, root_session_key TEXT NOT NULL,
      policy_digest TEXT NOT NULL, template_json TEXT NOT NULL, active INTEGER NOT NULL,
      issued_at INTEGER NOT NULL, UNIQUE(board, task_id, activation_id)
    );
    CREATE TABLE IF NOT EXISTS project_scope_attempts (
      attempt_ref TEXT PRIMARY KEY, root_ref TEXT NOT NULL, board TEXT NOT NULL,
      task_id TEXT NOT NULL, run_id INTEGER NOT NULL, claim_lock TEXT NOT NULL,
      depth INTEGER NOT NULL, active INTEGER NOT NULL, issued_at INTEGER NOT NULL,
      UNIQUE(board, task_id, run_id)
    );
    CREATE INDEX IF NOT EXISTS idx_project_scope_attempt_identity
      ON project_scope_attempts(board, task_id, run_id, claim_lock, active);
    """)
    return conn


def _snapshot(activation: Any) -> str:
    t = activation.template
    return json.dumps({
        "template_id": t.template_id,
        "repository_roots": [str(x) for x in t.repository_roots],
        "temporary_roots": [str(x) for x in t.temporary_roots],
        "git_remotes": [[name, list(prefixes)] for name, prefixes in t.git_remotes],
        "git_ref_rules": list(t.git_ref_rules),
        "docker_registry_prefixes": list(t.docker_registry_prefixes),
        "allowed_operations": sorted(t.allowed_operations),
    }, sort_keys=True, separators=(",", ":"))


def grant_root(board_db: str | Path, board: str, task_id: str, assignee: str | None, activation: Any) -> KanbanScopeRoot:
    """Persist an explicit parent-authorized card root from an active snapshot."""
    if not all(isinstance(x, str) and x for x in (board, task_id, activation.activation_id, activation.session_key)):
        raise ValueError("invalid root grant identity")
    root = KanbanScopeRoot(uuid.uuid4().hex, board, task_id, assignee, activation.activation_id,
                           activation.session_key, activation.policy_digest, _snapshot(activation))
    conn = _connect(board_db)
    try:
        with conn:
            conn.execute("UPDATE project_scope_roots SET active=0 WHERE board=? AND task_id=?", (board, task_id))
            conn.execute("""INSERT INTO project_scope_roots
              (root_ref,board,task_id,assignee,activation_id,root_session_key,policy_digest,template_json,active,issued_at)
              VALUES (?,?,?,?,?,?,?,?,1,?)""",
              (root.root_ref, root.board, root.task_id, root.assignee, root.activation_id,
               root.root_session_key, root.policy_digest, root.template_json, int(time.time())))
    finally:
        conn.close()
    return root


def revoke_activation(activation_id: str, *, registry_path: str | Path) -> None:
    conn = sqlite3.connect(registry_path)
    try:
        with conn:
            conn.execute("UPDATE project_scope_roots SET active=0 WHERE activation_id=?", (activation_id,))
            conn.execute("UPDATE project_scope_attempts SET active=0 WHERE root_ref IN "
                         "(SELECT root_ref FROM project_scope_roots WHERE activation_id=?)", (activation_id,))
    finally:
        conn.close()


def revoke_activation_everywhere(activation_id: str) -> None:
    for path in tuple(_known_registry_paths):
        try:
            revoke_activation(activation_id, registry_path=path)
        except (OSError, sqlite3.Error):
            pass


def _root_for(conn: sqlite3.Connection, board: str, task_id: str, parent_attempt: str | None) -> tuple[sqlite3.Row, int] | None:
    if parent_attempt:
        parent = conn.execute("""SELECT a.root_ref,a.depth,r.* FROM project_scope_attempts a
          JOIN project_scope_roots r ON r.root_ref=a.root_ref
          WHERE a.attempt_ref=? AND a.active=1 AND r.active=1""", (parent_attempt,)).fetchone()
        if parent and parent["board"] == board and int(parent["depth"]) < _MAX_DEPTH:
            return parent, int(parent["depth"]) + 1
        return None
    root = conn.execute("SELECT * FROM project_scope_roots WHERE board=? AND task_id=? AND active=1", (board, task_id)).fetchone()
    return (root, 0) if root else None


def bind_attempt(board_db: str | Path, board: str, task_id: str, run_id: int, claim_lock: str,
                 *, parent_attempt: str | None = None, max_depth: int = _MAX_DEPTH) -> KanbanScopeAttempt | None:
    """Mint a fresh one-use binding after dispatcher claim; old attempt is inert."""
    if not (isinstance(run_id, int) and run_id > 0 and isinstance(claim_lock, str) and claim_lock):
        return None
    conn = _connect(board_db)
    try:
        source = _root_for(conn, board, task_id, parent_attempt)
        if source is None or source[1] > max_depth:
            return None
        row, depth = source
        with conn:
            conn.execute("UPDATE project_scope_attempts SET active=0 WHERE board=? AND task_id=?", (board, task_id))
            attempt = KanbanScopeAttempt(uuid.uuid4().hex, row["root_ref"], board, task_id, run_id, claim_lock, depth)
            conn.execute("""INSERT INTO project_scope_attempts
              (attempt_ref,root_ref,board,task_id,run_id,claim_lock,depth,active,issued_at)
              VALUES (?,?,?,?,?,?,?,1,?)""",
              (attempt.attempt_ref, attempt.root_ref, board, task_id, run_id, claim_lock, depth, int(time.time())))
        return attempt
    finally:
        conn.close()


def _live_dispatch_claim(board_db: str | Path, task_id: str, run_id: int, claim_lock: str) -> sqlite3.Row | None:
    """Read the dispatcher-owned task/run claim; every mismatch fails closed."""
    try:
        conn = sqlite3.connect(Path(board_db).expanduser().resolve())
        conn.row_factory = sqlite3.Row
        try:
            task = conn.execute(
                "SELECT id,assignee,status,current_run_id,claim_lock FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            if task is None or task["status"] != "running":
                return None
            if int(task["current_run_id"] or 0) != run_id or task["claim_lock"] != claim_lock:
                return None
            run = conn.execute(
                "SELECT id,status,claim_lock FROM task_runs WHERE id=? AND task_id=?",
                (run_id, task_id),
            ).fetchone()
            if run is None or run["status"] != "running" or run["claim_lock"] != claim_lock:
                return None
            return task
        finally:
            conn.close()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None


def bind_for_dispatch(board_db: str | Path, board: str, task_id: str, run_id: int, claim_lock: str) -> KanbanScopeAttempt | None:
    """Bind only the live dispatcher claim; ambiguity and reassignment fail closed."""
    task = _live_dispatch_claim(board_db, task_id, run_id, claim_lock)
    if task is None:
        return None
    direct = bind_attempt(board_db, board, task_id, run_id, claim_lock)
    if direct is not None:
        # A root grant includes the concrete card's assignee.  A later reassignment
        # is a new authority decision, never an implicit transfer.
        conn = _connect(board_db)
        try:
            root = conn.execute("SELECT assignee FROM project_scope_roots WHERE root_ref=? AND active=1", (direct.root_ref,)).fetchone()
            if root is None or root["assignee"] != task["assignee"]:
                conn.execute("UPDATE project_scope_attempts SET active=0 WHERE attempt_ref=?", (direct.attempt_ref,))
                conn.commit()
                return None
        finally:
            conn.close()
        return direct
    try:
        board_conn = sqlite3.connect(board_db)
        try:
            parents = [r[0] for r in board_conn.execute("SELECT parent_id FROM task_links WHERE child_id=?", (task_id,))]
        finally:
            board_conn.close()
    except sqlite3.Error:
        return None
    registry = _connect(board_db)
    try:
        candidates = registry.execute("""SELECT attempt_ref FROM project_scope_attempts a
          JOIN project_scope_roots r ON r.root_ref=a.root_ref
          WHERE a.board=? AND a.task_id IN (%s) AND a.active=1 AND r.active=1""" % ",".join("?" for _ in parents),
          [board, *parents]).fetchall() if parents else []
        if len(candidates) != 1:
            return None
        return bind_attempt(board_db, board, task_id, run_id, claim_lock, parent_attempt=candidates[0]["attempt_ref"])
    finally:
        registry.close()


def resolve_attempt(board_db: str | Path, board: str, task_id: str, run_id: int, claim_lock: str,
                    attempt_ref: str | None = None) -> KanbanScopeAttempt | None:
    # This is the terminal/pre-execution guard: the persisted capability is
    # insufficient until the board's current task/run claim says it is live.
    task = _live_dispatch_claim(board_db, task_id, run_id, claim_lock)
    if task is None:
        return None
    conn = _connect(board_db)
    try:
        row = conn.execute("""SELECT a.*,r.assignee AS root_assignee FROM project_scope_attempts a
          JOIN project_scope_roots r ON r.root_ref=a.root_ref
          WHERE a.board=? AND a.task_id=? AND a.run_id=? AND a.claim_lock=?
            AND a.active=1 AND r.active=1""", (board, task_id, run_id, claim_lock)).fetchone()
        if row is None or (attempt_ref is not None and row["attempt_ref"] != attempt_ref):
            return None
        # Assignment is an authority boundary, not merely a dispatch hint.
        # Re-read it at every terminal guard/pre-exec lookup and revoke the
        # complete root lineage on transfer so a stale attempt cannot execute.
        if row["root_assignee"] != task["assignee"]:
            with conn:
                conn.execute("UPDATE project_scope_roots SET active=0 WHERE root_ref=?", (row["root_ref"],))
                conn.execute("UPDATE project_scope_attempts SET active=0 WHERE root_ref=?", (row["root_ref"],))
            return None
        return KanbanScopeAttempt(row["attempt_ref"], row["root_ref"], row["board"], row["task_id"],
                                  int(row["run_id"]), row["claim_lock"], int(row["depth"]))
    finally:
        conn.close()


def template_for_attempt(board_db: str | Path, attempt: KanbanScopeAttempt):
    """Return the immutable snapshot only after exact live binding verification."""
    from tools.project_scope_approval import ProjectScopeTemplate
    conn = _connect(board_db)
    try:
        row = conn.execute("SELECT template_json,policy_digest FROM project_scope_roots WHERE root_ref=? AND active=1", (attempt.root_ref,)).fetchone()
        if row is None:
            return None
        data = json.loads(row["template_json"])
        template = ProjectScopeTemplate(data["template_id"], tuple(Path(x) for x in data["repository_roots"]),
            tuple(Path(x) for x in data["temporary_roots"]), tuple((x[0], tuple(x[1])) for x in data["git_remotes"]),
            tuple(data["git_ref_rules"]), tuple(data["docker_registry_prefixes"]), frozenset(data["allowed_operations"]))
        from tools.project_scope_approval import project_scope_policy_digest
        if project_scope_policy_digest(template) != row["policy_digest"]:
            with conn:
                conn.execute("UPDATE project_scope_roots SET active=0 WHERE root_ref=?", (attempt.root_ref,))
                conn.execute("UPDATE project_scope_attempts SET active=0 WHERE root_ref=?", (attempt.root_ref,))
            return None
        return template, row["policy_digest"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    finally:
        conn.close()
