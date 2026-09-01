# RFC: Project-Scoped Approvals

**Status:** Implemented with session-bound activation transport.
**Decision:** Introduce opt-in, session-only, typed project-scope approvals as inert configuration templates. A matching template may approve only a narrowly parsed operation after explicit activation and all existing unconditional safeguards.

> **Verified implementation note (2026-09-01):** The branch contains the internal evaluator, immutable session snapshot/digest, session registry, terminal context forwarding, execution-boundary revalidation, and focused contract coverage. `/project-scope activate` renders and stores a reviewed immutable policy snapshot; `confirm` binds that exact snapshot rather than reloading mutable configuration. Gateway control uses the shared approval/session trust predicate and denies delegated or unattended sources. It emits allowlisted audit metadata including root-match label, policy digest, timestamp, session/activation identifiers, and decision reason.
>
> **Mint authority boundary:** Public raw template, activation, and delegated-grant APIs cannot mint. A successful interactive `confirm` creates an immutable, one-use receipt bound to the exact session and reviewed template; `confirm-kanban` similarly creates a one-use receipt bound to live activation, board, card, and assignee. Every root mint validates the live activation/session and receipt binding. The dispatcher alone performs child lifecycle minting from its persisted parent edge. Python name privacy is not the boundary: arbitrary native in-process code is within Hermes' trusted-core TCB. Plugin, model, prompt, and tool-caller paths do not receive a public mint API or caller-provided boolean/token/duck-typed object path.

## Motivation

Repeated confirmation for bounded release-adjacent work is costly, while broad command or shell allowlists are unsafe: they cannot reliably bind filesystem, repository, remote, reference, registry, execution context, and command shape. This RFC defines a deliberately narrow capability model that reduces repeated prompts without authorizing arbitrary shell text or weakening existing protections.

## Authority and configuration

A project scope is a reusable, **inert** template under `approvals.project_scope_templates`. Configuration declares intent only: it does not activate on startup, matching CWD, command text, task, prompt, skill, delegation, or child-worker activity.

## Delegation and Kanban lineage

An activated parent session does **not** automatically authorize delegates or
Kanban workers. A trusted interactive parent must make a separate, explicit
root grant for the exact runtime recipient. The approval registry binds a
direct delegate grant to its child session and subagent identity; descendants
may only derive the same immutable policy digest through that verified edge.
They cannot select a template, renew, replace, transfer, widen, or create an
unrelated root. Lineages are depth-bounded and live-checked at guard and
execution time, so root revocation, session clear, expiry, or a broken parent
edge makes all descendants inert before execution.

Kanban cards are noninteractive and inert without an explicit parent root
grant. The trusted `/project-scope grant-kanban <board> <card>` surface first
renders the redacted immutable policy summary plus exact board/card/assignee
identity, then requires `confirm-kanban <token>`. Confirmation writes durable
capability-registry metadata adjacent to the board DB; it stores a policy
snapshot/digest and opaque IDs only, never card text or a model-facing policy
token. The durable row is not authority and can never restore an activation:
every root/attempt lookup requires the current in-memory activation for the
persisted root session key to have the exact immutable activation ID,
snapshot, and digest. Consequently daemon/process restart invalidates all
outstanding roots and attempts unless a supported explicit activation lifecycle
reestablishes them; there is no implicit restore from a Kanban row. The dispatcher
mints a fresh opaque attempt reference after each claim and binds it to exact
board, card, run ID, and claim lock. Same-card retries mint a new reference;
replay, moved-board, lock/run mismatch, reassignment, ambiguous parent lineage,
cycle, and depth overflow fail closed. A direct, uniquely verified parent card
may pass the identical snapshot/digest to a descendant automatically, but this
does not grant child ownership or card mutation. Parent activation replacement,
revoke/session clear, root cancellation, claim loss, or worker exit makes the
root and every derived attempt inert; lookup happens at every terminal guard and
again immediately before execution.

The following are not authority sources and cannot select, activate, extend, or renew a policy: task directories or titles, prompt text, skills, agent/delegation instructions, model output, turn environment variables, child agents, `task_id`, or a command-embedded `cd`.

Templates have a unique lowercase-kebab-case ASCII `id`; absolute, accessible directory roots; canonical normalized remote URL prefixes; restricted full-ref rules; normalized registry/namespace prefixes ending in `/`; and a closed `allowed_operations` enum. Empty, relative, inaccessible, non-directory, duplicate-after-canonicalization, malformed, or unknown values invalidate that template. Unknown operation IDs fail closed.

The v1 shape is intentionally a commented, inert template, not a default or migration:

```yaml
approvals:
  project_scope_templates:
    - id: example-release-scope
      repository_roots:
        - /absolute/path/to/repository
      temporary_roots:
        - /absolute/path/to/temporary-work
      git_remotes:
        - name: origin
          url_prefixes:
            - https://git.example.invalid/organization/
      git_ref_rules:
        - refs/heads/release/*
      docker_registry_prefixes:
        - registry.example.invalid/team/
      allowed_operations:
        - git.worktree.create
        - git.worktree.prune
        - git.worktree.remove
        - git.commit.signed
        - git.push.configured_remote
        - docker.build
        - docker.push.configured_registry
      activation: explicit-session-approval
      expires: session
```

In v1, `activation` must be `explicit-session-approval` and `expires` must be `session`; no configuration value can enable automatic activation or a longer lifetime. A remote name alone is insufficient: each permitted name must be paired with one or more normalized URL prefixes. Existing command allowlist entries are never templates, and templates never create permanent allowlist entries.

## Session activation and revocation

Before use, the user requests a configured template by exact ID. Hermes presents its redacted, normalized summary—canonical roots, operations, remote/ref restrictions, registry prefixes, session expiry, and unchanged hardline, user-deny, and file protections—and stores that validated immutable snapshot plus its canonical digest with the short-lived confirmation token. Confirming consumes the token and binds the stored snapshot, never a template reloaded from configuration; replacement of an ID between display and confirm cannot broaden authorization.

An affirmative response creates an in-memory activation containing `template_id`, `session_key`, `activation_id`, `issued_at`, and a validated immutable template snapshot with its SHA-256 policy digest. At most one template is active per session. Activating another template requires a new confirmation and atomically replaces the prior activation. Editing configuration under the same template ID cannot expand an existing session; a user must explicitly activate the edited template to obtain its new digest. The user may revoke the active template at any time; revocation applies before the next command and is audited. `clear_session(session_key)` revokes the entry automatically. Activations are neither persisted nor reusable in another session. The gateway invokes this control surface only when the shared approval/session context identifies a trusted interactive, non-delegated source; webhook, API, and delegated paths are explicitly denied for list, activation, confirmation, and revocation.

Scopes cannot be enabled by force modes, smart approval, permanent allowlists, tool parameters, or child agents. Each delegated call is independently evaluated. A child sharing its parent's session key may consume the parent-session activation, but cannot activate, revoke, extend, or transfer it; a child with another key is inactive.

## Immutable authorization context and ordering

The terminal boundary constructs an immutable approval context before authorization and execution:

```python
@dataclass(frozen=True)
class TerminalApprovalContext:
    raw_command: str
    backend_type: str
    session_key: str
    supplied_workdir: str | None
    effective_cwd: str
    background: bool
    has_host_access: bool
```

`raw_command` remains the exact tool input for existing hardline, deny, and dangerous-pattern checks. `effective_cwd` is calculated once through the existing CWD resolver—preserving its explicit-workdir, session-CWD, then configured-default precedence—and is passed unchanged to both evaluation and execution. Evaluators must not recalculate CWD from command text, inspect a `cd` operand, or use task paths.

Terminal-local unconditional guards—hardline detection, `sudo -S` protection, and `approvals.deny`—run before every backend-specific shortcut, including isolated Docker, Daytona, and Vercel container paths. Only ordinary approval prompting and scoped-policy evaluation may be skipped for an isolated container. Scoped evaluation occurs only after those floors and only for a user-active template. A `not_applicable` or `denied` scope result preserves ordinary dangerous-command and prompt behavior; it is never a bypass. A scoped approval is positive only for an eligible typed operation and does not alter yolo behavior, `approvals.mode`, permanent allowlists, or file-tool behavior.

## V1 admission, operations, and canonicalization

V1 admits exactly one shell-free command name and argv vector. Before `shlex.split(posix=True)`, a conservative lexical gate rejects newlines, command separators, pipelines, backgrounding, redirects, substitutions, expansion syntax, globbing, and unbalanced quotes. It also rejects shell/interpreter carriers and evaluation forms, assignment-prefix execution, `sudo`, `env` indirection, `xargs`, `find -exec`, and `command`, `eval`, `source`, or `.`. The executable basename and every flag and positional argument must match the operation's exact grammar; unknown elements are rejected.

This gate controls only scope eligibility. Rejected input remains subject to the ordinary safety path and is not thereby safe.

For every path-bearing operand and `effective_cwd`, paths are resolved relative to `effective_cwd`, never process CWD. Existing paths use strict realpath resolution. For a planned non-existing target, the resolver finds and resolves the nearest existing ancestor, rejects `.` and `..` traversal in the remaining suffix, then appends and normalizes it. Containment is equality or component-wise descent, never string-prefix matching. Relevant operands and local Git effective push metadata are re-resolved immediately before foreground execution or background spawn; a changed/revoked activation, scope-relevant path, or remote configuration blocks execution. This is a fail-closed user-space recheck, not a claim of atomicity: a hostile actor able to race the final metadata/path read and process launch remains a residual OS-level TOCTOU risk until descriptor- or transaction-based execution is available.

The closed v1 operation enum is:

- `git.worktree.create`, `git.worktree.prune`, and `git.worktree.remove`: exact `git -C <repo> worktree ...` forms; effective CWD and `-C` must be the configured repository root; destinations/removal targets must be under a temporary root, with symlink escape and sensitive/root/home/system targets refused.
- `git.commit.signed`: a narrow `git -C <repo> commit -S ...` grammar with reviewed message syntax; no hooks/config overrides, pathspec-from-file, amend, no-verify, or arbitrary Git `-c`.
- `git.push.configured_remote`: exact configured remote name and full refspec; Git's effective push destination set is read from local metadata (`pushurl` entries when present, otherwise `url`) and every destination plus both source/destination refs must match template rules. Force behavior, deletion, mirror/all, tags, URL remotes, and config overrides are excluded.
- `docker.build`: a closed benign flag set with explicit repository-contained context (and allowed Dockerfile path); no host networking, daemon/context controls, secrets/SSH injection, arbitrary build arguments, or lifecycle behavior. A supported tag must match an allowed registry prefix. Environment overrides and the persisted Docker `currentContext` are checked from local configuration only; default or explicitly local Unix/npipe metadata is eligible, while unknown or remote metadata fails closed without contacting a daemon.
- `docker.push.configured_registry`: exact image-reference grammar with normalized registry/namespace prefix matching; no daemon/context controls or tag/digest rewrites outside the prefix.

There is no generic filesystem or cleanup capability in v1. Any future cleanup operation requires its own explicit single-argv grammar, temporary-root containment, sensitive target exclusions, and revalidation.

## Hard exclusions

A project scope never auto-approves shell compound syntax; arbitrary scripts; privilege escalation; deployments; SSH or remote execution; database mutation or destruction; system-service actions; Git force push, ref deletion or rewrite, tags, mirror/all pushes, unconfigured remotes, URL remotes, or nonconforming refspecs; or Docker/Podman lifecycle, login/logout, removal/prune, context selection, remote daemon controls, or environment-based daemon redirection.

Matching a scope never suppresses hardline blocks, user deny rules, Docker lifecycle/remote-daemon controls, force-push restrictions, separate file-tool denials, or cross-profile safeguards.

## Audit and privacy

Activation, revocation, and scoped auto-approval use the existing observer pair with a structured `project_scope` payload. Events include the event kind, template and activation/session IDs, backend, operation, decision/reason, canonical root identifiers and operand-to-root match labels, policy version/hash, timestamp, and standard correlation fields.

Audit and diagnostic output must not retain raw command text or arguments, commit message bodies, credentials, tokens, signed payloads, private remote userinfo or query data, build arguments, or secret-bearing operands. Structured fields are allowlisted; any human-readable summary uses forced sensitive-text redaction.

## Compatibility

This feature is additive. Existing `approvals.mode`, yolo, `approvals.deny`, hardline patterns, smart approvals, session pattern approvals, command allowlists, approval transports, callbacks, and file-tool gates remain unchanged. Missing `project_scope_templates` is a no-op. An invalid template fails closed with a user-visible configuration diagnostic without changing unrelated approval behavior. There is no activation persistence, migration, allowlist import, or project-specific live default.

## Test strategy

Implementation must add focused tests using a temporary Hermes home and preserve existing approval, terminal-workdir, and write-gate coverage. New coverage includes:

1. template parsing and validation, including malformed values, duplicate IDs, invalid roots, unknown operations, and template inertness;
2. explicit activation, exact-one-template binding, revocation, session cleanup, nonpersistence, cross-session rejection, and delegated evaluation;
3. safety ordering, proving hardline, sudo-stdin, and user-deny checks still block first and force/yolo/mode-off cannot create a scope bypass;
4. foreground and background terminal integration with the raw command, backend, session, supplied workdir, and resolved effective CWD;
5. strict canonicalization, including symlink and nearest-existing-ancestor escapes, traversal, wrong CWD, and protected targets;
6. rejection of compound syntax, expansion, redirects, globbing, interpreter carriers, and multi-stage commands while ordinary approval behavior remains available;
7. Git and Docker repository, remote, ref, registry, force, lifecycle, daemon/context, and redacted-observer cases;
8. activation-display/confirm configuration replacement, proving the confirmation activates the reviewed snapshot and digest rather than a broadened same-ID template;
9. gateway interactive versus webhook/API/delegated control authorization for activation, confirmation, and revocation; and
10. command-registration parity, documenting that `/project-scope` stays available as `/hermes project-scope` on Slack's 50-command cap while remaining native on eligible surfaces.

## Non-goals

This RFC does not implement a general terminal allowlist, shell-language analysis, arbitrary command authorization, persistent project activation, automatic discovery or activation of a scope, broader cleanup behavior, a change to file-tool authorization, or a weakening of current safety controls. Broader workflows remain individually approved until a later RFC defines and reviews a new typed operation and its exact grammar.
