"""Candidate ranking, topic continuity, and per-session prompt injection."""
from __future__ import annotations

from pathlib import Path
from typing import Any


def redacted_candidate_exc_info() -> tuple[type[RuntimeError], RuntimeError, Any]:
    """Synthetic traceback for failures that may carry private request data."""
    try:
        raise RuntimeError("candidate lookup unavailable") from None
    except RuntimeError as redacted:
        redacted.__context__ = None
        return type(redacted), redacted, redacted.__traceback__


def detect_candidate_topic(
    msg: str, prev_msg: str | None, llm_tc: bool | None, *,
    client_factory, detector_path: Path, logger, redacted_exc_info,
) -> dict[str, Any] | None:
    """Detect continuity; an unavailable detector means full injection."""
    embed_fn: Any = None
    try:
        client = client_factory()
        if client:
            embed_fn = lambda text: (client.embed([text]) or [None])[0]
    except Exception:
        # Client construction is a pluggable boundary; errors may contain URLs or secrets.
        logger.warning("skill-graph: candidate embedding client unavailable [location=candidate.client]",
                       exc_info=redacted_exc_info())

    try:
        import importlib.util as importlib_util
        if detector_path.exists():
            spec = importlib_util.spec_from_file_location("topic_detection", str(detector_path))
            if spec is None or spec.loader is None:
                raise ImportError("topic_detection not importable")
            detector = importlib_util.module_from_spec(spec)
            spec.loader.exec_module(detector)
            return detector.detect_topic_shift(
                msg, prev_msg=prev_msg,
                llm_topic_continuation=llm_tc,
                embed_fn=embed_fn,
            )
    except Exception:
        # Dynamic loader and detector invoke arbitrary callbacks; never expose their errors.
        logger.warning("skill-graph: candidate topic detection unavailable [location=candidate.topic]",
                       exc_info=redacted_exc_info())
    return None


def rank_skill_candidates(
    intents: list[str], *, embedding_search, ensure_graph, search_graph,
    logger, redacted_exc_info,
) -> list[dict[str, Any]]:
    """Merge semantic results, falling back to lexical search per intent."""
    merged: dict[str, dict[str, Any]] = {}
    for intent in intents[:6]:
        hits = embedding_search(intent, topk=5)
        if not hits:
            try:
                conn = ensure_graph()
                lex = search_graph(intent, conn, limit=5)
                for result in lex:
                    hits.append({
                        "name": result.get("name", ""),
                        "description": result.get("description", ""),
                        "score": 0.5,
                        "scenes": [],
                    })
            except Exception:
                # Retrieval is best-effort; SQL and graph callbacks can carry request text.
                logger.warning("skill-graph: candidate lexical fallback unavailable [location=candidate.lexical]",
                               exc_info=redacted_exc_info())
        for hit in hits:
            name = hit.get("name", "")
            if not name:
                continue
            if name not in merged or hit.get("score", 0) > merged[name].get("score", 0):
                merged[name] = hit
    return sorted(merged.values(), key=lambda result: -result.get("score", 0))


def build_skill_candidates_context(
    user_message: str,
    session_id: str = "",
    is_first_turn: bool = False,
    prev_msg: str | None = None,
    prev_intents: list[str] | None = None,
    *, config_reader, split_intents, detect_topic, rank_candidates,
    injected_names_cache: dict[str, set[str]], logger,
) -> tuple[str | None, list[str]]:
    """Return candidate block and intents, or ``(None, [])`` when skipped.

    Continuations inject only unseen candidates; topic shifts and failures
    inject the full list. Empty intent splits use the raw message; unavailable
    embeddings use lexical lookup. Short conversational messages are skipped.
    """
    if not user_message or not isinstance(user_message, str):
        return None, []
    msg = user_message.strip()
    if not msg:
        return None, []

    # Cost guard: skip conversational filler and very short messages.
    if len(msg) < 12:
        return None, []
    lowered = msg.lower()
    _trivial_prefixes = (
        "hi", "hello", "hey", "thanks", "thank you", "ok", "okay",
        "好的", "谢谢", "你好", "嗯", "okay", "知道了", "收到",
    )
    if lowered in _trivial_prefixes or any(
        lowered.startswith(p) for p in _trivial_prefixes
    ):
        return None, []
    # Pure confirmation / single-word replies
    if msg in ("好", "可以", "行", "ok", "yes", "no", "y", "n", "done", "完成", "继续"):
        return None, []

    # Config gate: injection can be disabled entirely
    if config_reader().get("inject_candidates") is False:
        return None, []

    # Intent split (one lightweight LLM call). Pass prev_intents to get
    # topic_continuation judgment (piggy-backed, zero extra cost).
    intents, scene, llm_tc = split_intents(msg, prev_intents=prev_intents)
    if not intents:
        intents = [msg]

    # LLM judgment is usable only when the previous intents are available.
    topic_result = detect_topic(msg, prev_msg, llm_tc if prev_intents else None)

    # Determine injection mode: full or delta
    force_full = True  # missing detector or fallback always uses full injection
    if not is_first_turn and topic_result and topic_result.get("method") != "fallback":
        force_full = not topic_result.get("topic_continuation", False)

    ranked = rank_candidates(intents)
    if not ranked:
        return None, []

    # ── Delta filtering ──
    injected_key = f"_injected:{session_id}"
    if force_full:
        # Reset: inject all, rebuild tracking set
        candidates = ranked[:10]
        injected_names_cache[injected_key] = set(
            c["name"] for c in candidates
        )
        mode_label = "full"
    else:
        # Delta: only inject candidates not yet seen this session
        already = injected_names_cache.get(injected_key, set())
        delta = [c for c in ranked[:10] if c["name"] not in already]
        if not delta:
            logger.info("skill-graph: no new candidates (all %d already injected)", len(already))
            return None, []
        candidates = delta
        injected_names_cache[injected_key] = already | set(
            c["name"] for c in candidates
        )
        mode_label = "delta"

    # Cap the injection budget (~2K tokens ≈ keep candidates lean).
    lines = []
    for c in candidates:
        desc = (c.get("description") or "")[:120]
        lines.append(f"- {c['name']}: {desc}  (score={c.get('score', 0):.2f})")
    block = (
        "Relevant skills you may want to load (skill_load) for this request:\n"
        + "\n".join(lines)
    )

    tc_str = ""
    if topic_result:
        tc_str = f" [topic={topic_result.get('method')}, cont={topic_result.get('topic_continuation')}]"

    logger.info(
        "skill-graph: injected %d skill candidates (%s mode, from %d intents): %s%s",
        len(candidates), mode_label, len(intents),
        ", ".join(c["name"] for c in candidates),
        tc_str,
    )
    return block, intents
