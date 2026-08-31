"""Contract tests for session-only project-scoped terminal approvals.

These tests deliberately exercise the proposed typed scope API rather than the
legacy command allowlist.  A configured template is inert until the user
explicitly binds it to a session; a matching command is never a capability by
itself.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import approval as approval_module
import tools.terminal_tool as terminal_tool


_ALLOWED_OPERATIONS = {
    "git.worktree.create",
    "git.worktree.prune",
    "git.worktree.remove",
    "git.commit.signed",
    "git.push.configured_remote",
    "docker.build",
    "docker.push.configured_registry",
}


def _api():
    """Keep missing implementation failures precise during parallel work."""
    required = (
        "TerminalApprovalContext",
        "load_project_scope_templates",
        "activate_project_scope",
        "revoke_project_scope",
        "get_active_project_scope",
        "evaluate_project_scope",
        "build_project_scope_audit_payload",
    )
    missing = [name for name in required if not hasattr(approval_module, name)]
    assert not missing, "project-scope API is missing: " + ", ".join(missing)
    return SimpleNamespace(**{name: getattr(approval_module, name) for name in required})


def _template(repo: Path, temporary: Path, **overrides):
    value = {
        "id": "release-scope",
        "repository_roots": [str(repo)],
        "temporary_roots": [str(temporary)],
        "git_remotes": [{"name": "origin", "url_prefixes": ["https://git.example.test/team/"]}],
        "git_ref_rules": ["refs/heads/release/*"],
        "docker_registry_prefixes": ["registry.example.test/team/"],
        "allowed_operations": sorted(_ALLOWED_OPERATIONS),
        "activation": "explicit-session-approval",
        "expires": "session",
    }
    value.update(overrides)
    return value


@pytest.fixture
def scope_config(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    temporary = tmp_path / "temporary"
    repo.mkdir()
    temporary.mkdir()
    state = {"project_scope_templates": [_template(repo, temporary)]}
    monkeypatch.setattr(approval_module, "_get_approval_config", lambda: state)
    return repo, temporary, state


def _context(api, command: str, session: str, cwd: Path, *, workdir=None, backend="local"):
    return api.TerminalApprovalContext(
        raw_command=command,
        backend_type=backend,
        session_key=session,
        supplied_workdir=str(workdir) if workdir is not None else None,
        effective_cwd=str(cwd),
        background=False,
        has_host_access=False,
    )


def _decision_status(decision):
    if isinstance(decision, dict):
        return decision["status"]
    return decision.status


def _field(value, name):
    if isinstance(value, dict):
        return value[name]
    return getattr(value, name)


class TestProjectScopeTemplateValidation:
    @pytest.mark.parametrize(
        "mutator",
        [
            lambda t: t.update(id="Release Scope"),
            lambda t: t.update(repository_roots=["relative/repo"]),
            lambda t: t.update(temporary_roots=[]),
            lambda t: t.update(allowed_operations=["git.push.everywhere"]),
            lambda t: t.update(git_remotes=[{"name": "origin", "url_prefixes": []}]),
            lambda t: t.update(git_ref_rules=["HEAD"]),
            lambda t: t.update(docker_registry_prefixes=["registry.example.test/team"]),
            lambda t: t.update(activation="automatic"),
            lambda t: t.update(expires="forever"),
        ],
    )
    def test_invalid_templates_fail_closed_individually(self, scope_config, mutator):
        api = _api()
        repo, temporary, state = scope_config
        invalid = _template(repo, temporary)
        mutator(invalid)
        state["project_scope_templates"] = [invalid, _template(repo, temporary, id="valid-scope")]

        templates = api.load_project_scope_templates()

        assert "valid-scope" in templates
        assert "release-scope" not in templates

    def test_duplicate_and_missing_roots_are_not_accepted(self, scope_config):
        api = _api()
        repo, temporary, state = scope_config
        missing = repo.parent / "missing"
        state["project_scope_templates"] = [
            _template(repo, temporary),
            _template(repo, temporary, id="release-scope"),
            _template(missing, temporary, id="missing-root"),
        ]

        templates = api.load_project_scope_templates()

        assert "release-scope" not in templates
        assert "missing-root" not in templates

    def test_configured_template_is_inert_until_explicit_session_activation(self, scope_config):
        api = _api()
        repo, _, _ = scope_config
        context = _context(api, "git -C . worktree prune", "session-a", repo)

        assert _decision_status(api.evaluate_project_scope(context)) == "not_applicable"
        api.activate_project_scope("session-a", "release-scope")
        assert _decision_status(api.evaluate_project_scope(context)) == "approved"


class TestProjectScopeLifecycle:
    def test_activation_is_exactly_one_template_per_session_and_is_not_persistent(self, scope_config):
        api = _api()
        repo, temporary, state = scope_config
        state["project_scope_templates"].append(_template(repo, temporary, id="other-scope"))

        first = api.activate_project_scope("session-a", "release-scope")
        second = api.activate_project_scope("session-a", "other-scope")

        active = api.get_active_project_scope("session-a")
        assert _field(active, "template_id") == "other-scope"
        assert _field(first, "activation_id") != _field(second, "activation_id")
        assert "other-scope" not in approval_module._permanent_approved

    def test_revoke_and_clear_session_remove_scope_before_next_command(self, scope_config):
        api = _api()
        repo, _, _ = scope_config
        api.activate_project_scope("session-a", "release-scope")
        assert api.revoke_project_scope("session-a") is True
        assert api.get_active_project_scope("session-a") is None

        api.activate_project_scope("session-a", "release-scope")
        approval_module.clear_session("session-a")
        context = _context(api, "git -C . worktree prune", "session-a", repo)
        assert api.get_active_project_scope("session-a") is None
        assert _decision_status(api.evaluate_project_scope(context)) == "not_applicable"

    def test_activation_cannot_be_selected_by_command_task_or_child_metadata(self, scope_config):
        api = _api()
        repo, _, _ = scope_config
        context = _context(api, "git -C . worktree prune", "child-session", repo)

        # Matching CWD/command and arbitrary metadata are not authority sources.
        assert _decision_status(api.evaluate_project_scope(context)) == "not_applicable"
        api.activate_project_scope("parent-session", "release-scope")
        assert _decision_status(api.evaluate_project_scope(context)) == "not_applicable"


class TestScopeOrderingAndTerminalContext:
    @pytest.mark.parametrize("command", ["rm -rf /", "sudo -S id", "git push origin refs/heads/release/x"])
    def test_hardline_sudo_and_user_deny_run_before_matching_scope(self, scope_config, monkeypatch, command):
        api = _api()
        repo, _, state = scope_config
        state["deny"] = ["git push origin *"]
        api.activate_project_scope("session-a", "release-scope")
        monkeypatch.setattr(approval_module, "_get_approval_config", lambda: state)
        result = approval_module.check_all_command_guards(
            command, "local", terminal_context=_context(api, command, "session-a", repo)
        )
        assert result["approved"] is False
        assert not result.get("project_scope_approved", False)

    def test_terminal_forwards_one_immutable_effective_cwd_to_guard_and_execution(self, monkeypatch):
        captured = []
        executed = []

        class FakeEnv:
            env = {}
            def execute(self, command, **kwargs):
                executed.append((command, kwargs))
                return {"output": "ok", "returncode": 0}

        monkeypatch.setattr(terminal_tool, "_active_environments", {"task": FakeEnv()})
        monkeypatch.setattr(terminal_tool, "_last_activity", {})
        monkeypatch.setattr(terminal_tool, "_task_env_overrides", {"task": {"cwd": "/session/cwd"}})
        monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: {"env_type": "local", "cwd": "/default", "timeout": 60, "lifetime_seconds": 3600})
        monkeypatch.setattr(terminal_tool, "_check_all_guards", lambda command, env_type, **kw: captured.append((command, env_type, kw)) or {"approved": True})

        result = json.loads(terminal_tool.terminal_tool(command="git -C /untrusted worktree prune", task_id="task", workdir="/tool/cwd"))

        assert result["exit_code"] == 0
        context = captured[0][2]["terminal_context"]
        assert context.raw_command == "git -C /untrusted worktree prune"
        assert context.session_key == "task"
        assert context.supplied_workdir == "/tool/cwd"
        assert context.effective_cwd == "/tool/cwd"
        assert executed[0][1]["cwd"] == context.effective_cwd


class TestScopedOperationEligibility:
    @pytest.mark.parametrize("command", [
        "git -C . worktree prune; id", "git -C . worktree prune && id",
        "git -C . worktree prune | cat", "git -C . worktree prune > out",
        "git -C . worktree prune $(id)", "git -C . worktree prune *",
        "bash -c 'git -C . worktree prune'", "python -c 'print(1)'",
        "env X=1 git -C . worktree prune", "git -C . worktree prune\n id",
    ])
    def test_shell_compounds_interpreters_and_globs_are_never_scoped(self, scope_config, command):
        api = _api()
        repo, _, _ = scope_config
        api.activate_project_scope("session-a", "release-scope")
        assert _decision_status(api.evaluate_project_scope(_context(api, command, "session-a", repo))) == "not_applicable"

    def test_existing_nonexisting_symlink_and_dotdot_escapes_are_denied(self, scope_config):
        api = _api()
        repo, temporary, _ = scope_config
        outside = repo.parent / "outside"
        outside.mkdir()
        (temporary / "escape").symlink_to(outside, target_is_directory=True)
        api.activate_project_scope("session-a", "release-scope")

        for destination in (temporary / "escape" / "existing", temporary / "escape" / "planned", temporary / ".." / "outside" / "planned"):
            command = f"git -C {repo} worktree add {destination} refs/heads/release/x"
            assert _decision_status(api.evaluate_project_scope(_context(api, command, "session-a", repo))) == "denied"

    def test_remote_ref_registry_and_force_constraints_fail_closed(self, scope_config):
        api = _api()
        repo, _, _ = scope_config
        api.activate_project_scope("session-a", "release-scope")
        denied = (
            f"git -C {repo} push origin refs/heads/release/x:refs/heads/main",
            f"git -C {repo} push --force origin refs/heads/release/x:refs/heads/release/x",
            f"git -C {repo} push https://evil.example/x refs/heads/release/x:refs/heads/release/x",
            "docker push registry.example.test/other/image:latest",
            "docker --host tcp://remote.example build .",
            "docker run registry.example.test/team/image:latest",
        )
        for command in denied:
            assert _decision_status(api.evaluate_project_scope(_context(api, command, "session-a", repo))) == "denied", command


class TestAuditAndDelegation:
    def test_audit_payload_is_allowlisted_and_redacts_sensitive_command_content(self, scope_config):
        api = _api()
        repo, _, _ = scope_config
        api.activate_project_scope("session-a", "release-scope")
        command = f"git -C {repo} commit -S -m 'token=super-secret-message'"
        decision = api.evaluate_project_scope(_context(api, command, "session-a", repo))

        payload = api.build_project_scope_audit_payload(decision)
        serialized = json.dumps(payload)
        assert payload["event"] == "project_scope_auto_approved"
        assert payload["template_id"] == "release-scope"
        assert "super-secret-message" not in serialized
        assert "raw_command" not in payload
        assert "token=" not in serialized

    def test_delegated_child_may_consume_only_same_session_existing_activation(self, scope_config):
        api = _api()
        repo, _, _ = scope_config
        api.activate_project_scope("parent", "release-scope")
        command = "git -C . worktree prune"

        assert _decision_status(api.evaluate_project_scope(_context(api, command, "parent", repo))) == "approved"
        assert _decision_status(api.evaluate_project_scope(_context(api, command, "child", repo))) == "not_applicable"
        with pytest.raises((PermissionError, ValueError, RuntimeError)):
            api.activate_project_scope("child", "release-scope", delegated=True)


class TestHighSecurityRevalidation:
    def _write_remote(self, repo: Path, *, url: str, pushurls=()):
        git = repo / ".git"
        if not git.exists():
            import subprocess
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
        git.mkdir(exist_ok=True)
        lines = ['[core]', '\trepositoryformatversion = 0', '\tbare = false', '[remote "origin"]', f"\turl = {url}"]
        lines.extend(f"\tpushurl = {pushurl}" for pushurl in pushurls)
        (git / "config").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_git_push_uses_all_effective_pushurls_and_url_fallback(self, scope_config):
        api = _api()
        repo, _, _ = scope_config
        api.activate_project_scope("session-a", "release-scope")
        command = "git -C . push origin refs/heads/release/x:refs/heads/release/x"

        self._write_remote(repo, url="https://git.example.test/team/repo")
        assert _decision_status(api.evaluate_project_scope(_context(api, command, "session-a", repo))) == "approved"
        self._write_remote(repo, url="https://evil.example/fetch", pushurls=("https://git.example.test/team/one", "https://git.example.test/team/two"))
        assert _decision_status(api.evaluate_project_scope(_context(api, command, "session-a", repo))) == "approved"
        self._write_remote(repo, url="https://git.example.test/team/repo", pushurls=("https://git.example.test/team/one", "https://evil.example/push"))
        assert _decision_status(api.evaluate_project_scope(_context(api, command, "session-a", repo))) == "denied"

    def test_persisted_docker_context_is_checked_without_daemon_contact(self, scope_config, monkeypatch, tmp_path):
        api = _api()
        repo, _, _ = scope_config
        docker_config = tmp_path / "docker"
        docker_config.mkdir()
        monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
        monkeypatch.delenv("DOCKER_HOST", raising=False)
        monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
        api.activate_project_scope("session-a", "release-scope")
        context = _context(api, "docker build .", "session-a", repo)

        (docker_config / "config.json").write_text('{"currentContext": "default"}')
        assert _decision_status(api.evaluate_project_scope(context)) == "approved"
        (docker_config / "config.json").write_text('{"currentContext": "remote"}')
        assert _decision_status(api.evaluate_project_scope(context)) == "denied"
        meta = docker_config / "contexts" / "meta" / "remote"
        meta.mkdir(parents=True)
        (meta / "meta.json").write_text('{"Endpoints":{"docker":{"Host":"unix:///var/run/docker.sock"}}}')
        assert _decision_status(api.evaluate_project_scope(context)) == "approved"
        monkeypatch.setenv("DOCKER_CONTEXT", "remote")
        assert _decision_status(api.evaluate_project_scope(context)) == "denied"

    def test_activation_snapshot_cannot_expand_after_same_id_config_edit(self, scope_config):
        api = _api()
        repo, temporary, state = scope_config
        api.activate_project_scope("session-a", "release-scope")
        state["project_scope_templates"] = [_template(
            repo, temporary, docker_registry_prefixes=["registry.example.test/"],
        )]
        decision = api.evaluate_project_scope(_context(
            api, "docker push registry.example.test/other/image:latest", "session-a", repo,
        ))
        assert _decision_status(decision) == "denied"
        payload = api.build_project_scope_audit_payload(decision)
        assert {"policy_digest", "session_id", "timestamp", "decision_reason", "matched_root_label"} <= set(payload)
        assert "raw_command" not in payload

    def test_terminal_revalidates_after_guard_before_execution(self, monkeypatch):
        api = _api()
        executed = []
        context = _context(api, "git -C . worktree prune", "task", Path("/tmp"))
        decision = SimpleNamespace(status="approved", activation_id="activation", policy_digest="digest", operation="git.worktree.prune")

        class FakeEnv:
            env = {}
            def execute(self, command, **kwargs):
                executed.append(command)
                return {"output": "unexpected", "returncode": 0}

        monkeypatch.setattr(terminal_tool, "_active_environments", {"task": FakeEnv()})
        monkeypatch.setattr(terminal_tool, "_last_activity", {})
        monkeypatch.setattr(terminal_tool, "_task_env_overrides", {"task": {"cwd": "/tmp"}})
        monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: {"env_type": "local", "cwd": "/tmp", "timeout": 60, "lifetime_seconds": 3600})
        monkeypatch.setattr(terminal_tool, "_check_all_guards", lambda *a, **k: {"approved": True, "project_scope_context": context, "project_scope_decision": decision})
        import tools.project_scope_approval as scope_module
        monkeypatch.setattr(scope_module, "revalidate_project_scope", lambda *a: SimpleNamespace(status="denied", reason="project scope changed before execution"))

        result = json.loads(terminal_tool.terminal_tool(command="git -C . worktree prune", task_id="task"))
        assert result["status"] == "blocked"
        assert not executed
