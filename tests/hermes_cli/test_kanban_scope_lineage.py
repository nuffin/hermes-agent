from __future__ import annotations

from pathlib import Path


def _template(tmp_path: Path):
    from tools.project_scope_approval import ProjectScopeTemplate
    repo = tmp_path / "repo"
    temp = tmp_path / "tmp"
    repo.mkdir(); temp.mkdir()
    return ProjectScopeTemplate(
        "release-scope", (repo,), (temp,), (("origin", ("https://example.invalid/",)),),
        ("refs/heads/main",), ("registry.example/",), frozenset({"git.commit.signed"}),
    )


def _activation(tmp_path: Path):
    template = _template(tmp_path)
    from tools import project_scope_approval as scope
    activation = scope.ActivatedProjectScope(
        "release-scope", "parent", "root-a", 0, template,
        scope.project_scope_policy_digest(template), "test-confirmation",
    )
    with scope._lock:
        scope._active["parent"] = activation
    return activation


def _grant(db: Path, board: str, task_id: str, assignee: str | None, activation):
    from hermes_cli import kanban_scope_lineage as lineage
    from tools.project_scope_approval import _issue_trusted_kanban_grant
    receipt = _issue_trusted_kanban_grant("parent", activation, board, task_id, assignee)
    assert receipt is not None
    return lineage.grant_root(db, board, task_id, assignee, receipt=receipt)


def _claimed_task(db: Path, *, assignee: str = "worker"):
    from hermes_cli import kanban_db as kb
    conn = kb.connect(db)
    try:
        task_id = kb.create_task(conn, title="opaque", assignee=assignee, initial_status="running")
        task = kb.claim_task(conn, task_id, claimer="test:dispatcher")
        assert task and task.current_run_id and task.claim_lock
        return task
    finally:
        conn.close()


def test_ordinary_card_has_no_scope_grant(tmp_path):
    from hermes_cli import kanban_scope_lineage as lineage
    assert lineage.resolve_attempt(tmp_path / "board.db", "board-a", "t-card", 1, "lock") is None


def test_root_grant_binds_exact_board_card_run_and_claim(tmp_path):
    from hermes_cli import kanban_scope_lineage as lineage
    activation = _activation(tmp_path)
    db = tmp_path / "board.db"
    task = _claimed_task(db)
    root = _grant(db, "board-a", task.id, "worker", activation)
    attempt = lineage.bind_for_dispatch(db, "board-a", task.id, task.current_run_id, task.claim_lock)
    assert attempt and attempt.root_ref == root.root_ref
    assert lineage.resolve_attempt(db, "board-a", task.id, task.current_run_id, task.claim_lock) == attempt
    assert lineage.resolve_attempt(db, "board-b", task.id, task.current_run_id, task.claim_lock) is None
    assert lineage.resolve_attempt(db, "board-a", task.id, task.current_run_id + 1, task.claim_lock) is None
    assert lineage.resolve_attempt(db, "board-a", task.id, task.current_run_id, "other") is None


def test_retry_is_fresh_binding_and_revoke_cascades(tmp_path):
    from hermes_cli import kanban_scope_lineage as lineage
    activation = _activation(tmp_path)
    db = tmp_path / "board.db"
    task = _claimed_task(db)
    _grant(db, "board-a", task.id, "worker", activation)
    one = lineage.bind_attempt(db, "board-a", task.id, 7, "lock-7")
    two = lineage.bind_attempt(db, "board-a", task.id, 8, "lock-8")
    assert one and two and one.attempt_ref != two.attempt_ref
    assert lineage.resolve_attempt(db, "board-a", task.id, 7, "lock-7") is None
    lineage.revoke_activation("root-a", registry_path=lineage.registry_path(db))
    assert lineage.resolve_attempt(db, "board-a", task.id, 8, "lock-8") is None


def test_descendant_inherits_without_card_text_and_bounds_depth_cycle(tmp_path):
    from hermes_cli import kanban_scope_lineage as lineage
    activation = _activation(tmp_path)
    db = tmp_path / "board.db"
    _grant(db, "board-a", "root", "worker", activation)
    parent = lineage.bind_attempt(db, "board-a", "root", 1, "one")
    child = lineage.bind_attempt(db, "board-a", "child", 2, "two", parent_attempt=parent.attempt_ref)
    assert child and child.root_ref == parent.root_ref and child.depth == 1
    assert lineage.bind_attempt(db, "board-a", "loop", 3, "three", parent_attempt=child.attempt_ref, max_depth=1) is None


def test_dispatcher_rejects_unknown_or_reassigned_root_card(tmp_path):
    from hermes_cli import kanban_scope_lineage as lineage
    db = tmp_path / "board.db"
    task = _claimed_task(db, assignee="worker")
    _grant(db, "board-a", task.id, "worker", _activation(tmp_path))
    assert lineage.bind_for_dispatch(db, "board-a", "unknown", task.current_run_id, task.claim_lock) is None
    conn = __import__("sqlite3").connect(db)
    try:
        conn.execute("UPDATE tasks SET assignee = ? WHERE id = ?", ("other", task.id))
        conn.commit()
    finally:
        conn.close()
    assert lineage.bind_for_dispatch(db, "board-a", task.id, task.current_run_id, task.claim_lock) is None


def test_reassignment_after_bind_revokes_before_execution(tmp_path):
    from hermes_cli import kanban_scope_lineage as lineage
    db = tmp_path / "board.db"
    task = _claimed_task(db, assignee="worker")
    _grant(db, "board-a", task.id, "worker", _activation(tmp_path))
    attempt = lineage.bind_for_dispatch(db, "board-a", task.id, task.current_run_id, task.claim_lock)
    assert attempt is not None
    conn = __import__("sqlite3").connect(db)
    try:
        conn.execute("UPDATE tasks SET assignee=? WHERE id=?", ("replacement", task.id))
        conn.commit()
    finally:
        conn.close()
    assert lineage.resolve_attempt(db, "board-a", task.id, task.current_run_id, task.claim_lock, attempt.attempt_ref) is None
    assert lineage.template_for_attempt(db, attempt) is None


def test_tampered_snapshot_digest_fails_closed(tmp_path):
    from hermes_cli import kanban_scope_lineage as lineage
    db = tmp_path / "board.db"
    task = _claimed_task(db)
    _grant(db, "board-a", task.id, "worker", _activation(tmp_path))
    attempt = lineage.bind_for_dispatch(db, "board-a", task.id, task.current_run_id, task.claim_lock)
    assert attempt is not None
    conn = __import__("sqlite3").connect(lineage.registry_path(db))
    try:
        conn.execute("UPDATE project_scope_roots SET template_json=? WHERE root_ref=?", ('{"template_id":"tampered"}', attempt.root_ref))
        conn.commit()
    finally:
        conn.close()
    assert lineage.template_for_attempt(db, attempt) is None


def test_terminal_lookup_denies_archived_or_replayed_dispatch_attempt(tmp_path):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_scope_lineage as lineage
    activation = _activation(tmp_path)
    db = tmp_path / "board.db"
    task = _claimed_task(db)
    _grant(db, "board-a", task.id, "worker", activation)
    attempt = lineage.bind_for_dispatch(db, "board-a", task.id, task.current_run_id, task.claim_lock)
    assert attempt
    conn = kb.connect(db)
    try:
        assert kb.archive_task(conn, task.id)
    finally:
        conn.close()
    assert lineage.resolve_attempt(db, "board-a", task.id, task.current_run_id, task.claim_lock, attempt.attempt_ref) is None


def test_temp_home_worker_binding_blocks_after_parent_revoke(tmp_path, monkeypatch):
    from hermes_cli import kanban_scope_lineage as lineage
    from tools.project_scope_approval import TerminalApprovalContext, evaluate_project_scope
    activation = _activation(tmp_path)
    db = tmp_path / "board.db"
    task = _claimed_task(db)
    _grant(db, "board-a", task.id, "worker", activation)
    attempt = lineage.bind_for_dispatch(db, "board-a", task.id, task.current_run_id, task.claim_lock)
    assert attempt
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "board-a")
    monkeypatch.setenv("HERMES_KANBAN_TASK", task.id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", task.claim_lock)
    monkeypatch.setenv("HERMES_KANBAN_SCOPE_ATTEMPT", attempt.attempt_ref)
    repo = activation.template.repository_roots[0]
    context = TerminalApprovalContext(f"git -C {repo} commit -S -m ok", "local", "worker", str(repo), str(repo), False, True)
    assert evaluate_project_scope(context).status == "approved"
    lineage.revoke_activation("root-a", registry_path=lineage.registry_path(db))
    assert evaluate_project_scope(context).status == "not_applicable"
