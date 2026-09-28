"""Live compression: config hot-reload onto a running agent, pending model switch apply, /compress
(CompressionLockHeld when a turn holds the lock), session-key sync after compress. Bodies are rebound
onto server.py's globals (method_ctx.bind_module) and reference them bare."""

from __future__ import annotations

import contextlib

from agent.compression_live_config import (
    apply_live_compression_config as _apply_live_compression_config,
    compression_config_signature as _tui_compression_config_signature,
)

from .method_ctx import bind_module


def _sync_agent_compression_with_config(sid: str, session: dict) -> None:
    """Adopt compression.* / model.context_length edits at turn start (messaging gateways rebuild the
    agent on these keys; Desktop/TUI keeps the live compressor, so it must be updated in place).

    Desktop/TUI only synced the model; the live compressor kept the threshold captured at agent creation
    (#95151).
    """
    agent = session.get("agent")
    if agent is None:
        return
    cfg = _load_cfg() or {}
    signature = _tui_compression_config_signature(cfg)
    seen = session.get("config_compression_seen")
    session["config_compression_seen"] = signature
    if signature == seen:
        return
    try:
        _apply_live_compression_config(agent, cfg)
    except Exception as e:
        logger.warning("Could not apply live compression config for %s: %s", sid, e)


def _apply_pending_model_switch(sid: str, session: dict) -> None:
    """Apply a model switch queued (``session["pending_model_switch"]``) while a turn was running. Runs on
    the TURN thread at turn start — nothing in flight — so the in-place swap (client rebuild) is safe. A
    failed switch keeps the current model and never blocks the turn."""
    pending = session.pop("pending_model_switch", None)
    if not pending or session.get("agent") is None:
        return
    try:
        result = _apply_model_switch(sid, session, pending["raw"], confirm_expensive_model=bool(pending.get("confirm_expensive_model")))
        # Honour the expensive-model confirm: surface the warning and drop the switch rather than spend
        # on a model the user never confirmed.
        if result.get("confirm_required"):
            _emit("error", sid, {"message": result.get("confirm_message") or result.get("warning") or ""})
    except Exception as e:
        _emit("error", sid, {"message": f"Could not switch model: {e}"})


class CompressionLockHeld(Exception):
    """Raised by _compress_session_history when a concurrent compression_locks row skipped compression."""

    def __init__(self, holder: str | None = None):
        self.holder = holder
        super().__init__(f"Compression lock held: {holder or 'unknown'}")


def _compress_session_history(
    session: dict, focus_topic: str | None = None, approx_tokens: int | None = None,
    before_messages: list | None = None, history_version: int | None = None,
) -> tuple[int, dict]:
    """Single choke point for all manual-compress routes. ``focus_topic`` is the RAW argument string after
    ``/compress``; the shared core (``agent.conversation_compression_manual``) parses boundary forms
    (``here [N]``, ``up to here``, ``--keep N``) so a partial compress triggers on EVERY route instead of a
    FULL compress focused on the literal text. ``--preview`` returns without touching history."""
    from agent.conversation_compression import finalize_context_engine_compression_notification
    from agent.conversation_compression_manual import (
        AGGRESSIVE_UNSUPPORTED, MIN_MESSAGES, compress_now, parse_compress_args)
    agent = session["agent"]
    # Snapshot under the lock so the LLM-bound compression call does NOT hold history_lock for the
    # request — otherwise prompt.submit etc. block on the dispatcher loop while compaction runs.
    if before_messages is None or history_version is None:
        with session["history_lock"]:
            before_messages, history_version = list(session.get("history", [])), int(session.get("history_version", 0))
    if len(before_messages) < MIN_MESSAGES:
        return 0, _get_usage(agent)
    request = parse_compress_args(focus_topic or "")
    if request.aggressive:
        raise ValueError(AGGRESSIVE_UNSUPPORTED)
    # RPC thread: bind the session cwd, or the boundary prompt rebuild resolves the backend's cwd and
    # persists a prompt every other process then rejects as stale runtime (fresh build, no tools pin).
    tokens = _set_session_context(session.get("session_key") or "", cwd=_session_cwd(session))
    try:
        result = compress_now(agent, before_messages, request, task_id=session.get("session_key") or "default")
    finally:
        _clear_session_context(tokens)
    if result.status == "preview":
        return 0, _get_usage(agent)
    # Lock-skipped: raise so callers surface a clear message instead of "No changes from compression".
    if result.status == "lock_skipped":
        raise CompressionLockHeld(result.lock_holder)
    if result.status != "compressed":
        return 0, _get_usage(agent)
    with session["history_lock"]:
        if int(session.get("history_version", 0)) != history_version:
            # External mutation during compaction — drop the result so we don't clobber concurrent edits.
            finalize_context_engine_compression_notification(agent, committed=False)
            return 0, _get_usage(agent)
        session["history"] = result.after_messages
        session["history_version"] = history_version + 1
    return result.removed, _get_usage(agent)


def _sync_session_key_after_compress(
    sid: str, session: dict, *, clear_pending_title: bool = True, restart_slash_worker: bool = True
) -> None:
    """Re-anchor the gateway-side ``session_key`` when _compress_context rotates ``agent.session_id``;
    otherwise approval routing, slash worker, DB lookups and yolo state keep targeting the ended parent.
    ``clear_pending_title``: True for manual /compress (title belongs to the old session), False for
    post-turn auto-compression. ``restart_slash_worker``: False only when the caller manages the worker."""
    agent = session.get("agent")
    new_session_id = getattr(agent, "session_id", None) or ""
    old_key = session.get("session_key", "") or ""
    if not new_session_id or new_session_id == old_key:
        return
    if not _transfer_active_session_slot(sid, session, new_session_id=new_session_id):
        logger.warning(
            "Compression session lease did not re-anchor: sid=%s old_session_id=%s new_session_id=%s",
            sid, old_key, new_session_id,
        )
    # Even if the approval module fails to import, anchor session_key on the continuation id.
    session["session_key"] = new_session_id
    with contextlib.suppress(Exception):
        from tools import approval
        with contextlib.suppress(Exception):
            approval.unregister_gateway_notify(old_key)
        with contextlib.suppress(Exception):
            if approval.is_session_yolo_enabled(old_key):
                approval.enable_session_yolo(new_session_id)
                approval.disable_session_yolo(old_key)
        with contextlib.suppress(Exception):
            approval.register_gateway_notify(new_session_id, lambda data: _emit_approval_request(sid, data))
    # Invalidate any in-flight ``_drain_queued_prompt`` claim taken under the pre-rotation key: a raced
    # drain must not dispatch on the continuation (its envelope is restored to the queue).
    session["_queued_prompt_generation"] = int(session.get("_queued_prompt_generation", 0)) + 1
    if clear_pending_title:
        session["pending_title"] = None
    if restart_slash_worker:
        with contextlib.suppress(Exception):
            _restart_slash_worker(sid, session)


def register(server) -> None:
    """Publish this module's helpers + handlers onto ``server``, rebound to its globals."""
    bind_module(globals(), server, skip=("_",))
