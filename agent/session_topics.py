"""Session topic segmentation runtime.

Topic state is durable in ``state.db`` while the cached system prompt remains
byte-stable.  The only model instruction is appended to the current user
message's ephemeral/``api_content`` tail, and the normal assistant response is
the only classifier call: no auxiliary or hidden LLM request is made.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Optional

logger = logging.getLogger("run_agent")

TOPIC_MESSAGE_FIELD = "_topic_id"
TOPIC_SESSION_CONFIG_KEY = "topic_segmentation_enabled"
_TOPIC_LINE_RE = re.compile(
    r"(?im)^[ \t]*TOPIC:[ \t]*([^\r\n]+?)[ \t]*\Z"
)
_TOPIC_WORD_RE = re.compile(r"[a-z0-9]+")
_MAX_TOPIC_TITLE_CHARS = 64
TOPIC_SEGMENTATION_RUNTIME_FAILURE_CODE = "topic_segmentation_runtime_failed"
TOPIC_SEGMENTATION_RUNTIME_FAILURE_MESSAGE = (
    "Topic segmentation failed safely before the turn could be finalized."
)


class TopicSegmentationRuntimeError(RuntimeError):
    """An enabled durable topic operation failed and the turn must not downgrade."""

    code = TOPIC_SEGMENTATION_RUNTIME_FAILURE_CODE

    def __init__(self) -> None:
        # This exception is returned by public CLI/API turn paths. Keep it
        # invariant across read, index, and transition failures so session IDs
        # and backend exception text cannot escape through error formatting.
        super().__init__(TOPIC_SEGMENTATION_RUNTIME_FAILURE_MESSAGE)


class TopicPrepublicationCapabilityError(RuntimeError):
    """Dynamic selected-topic capability refusal before any durable turn setup."""

    def __init__(self, failure: tuple[str, str]) -> None:
        self.failure = failure
        super().__init__(failure[1])


TOPIC_PREPUBLICATION_CAPABILITY_FAILURE_CODE = "topic_prepublication_capability_unsupported"
TOPIC_PREPUBLICATION_CAPABILITY_FAILURE_MESSAGE = (
    "This runtime cannot safely publish a selected-topic turn. Use a supported runtime or disable topic segmentation."
)


def selected_topic_prepublication_capability_failure(agent: Any) -> tuple[str, str] | None:
    """Return a stable pre-admission refusal for paths that cannot defer publication."""
    if not getattr(agent, "_topic_segmentation_enabled", False):
        return None
    if getattr(agent, "api_mode", None) in {"codex_app_server", "codex_responses"}:
        return TOPIC_PREPUBLICATION_CAPABILITY_FAILURE_CODE, TOPIC_PREPUBLICATION_CAPABILITY_FAILURE_MESSAGE
    # Tool calls (including provider-native calls) persist before execution and
    # can publish commentary/results before topic activation. Selected-topic
    # turns therefore support text-only runtimes until deferred execution exists.
    if getattr(agent, "tools", None):
        return TOPIC_PREPUBLICATION_CAPABILITY_FAILURE_CODE, TOPIC_PREPUBLICATION_CAPABILITY_FAILURE_MESSAGE
    # Provider memory and the built-in writable memory tool can persist before
    # final topic activation. There is no deferred execution contract for them.
    manager = getattr(agent, "_memory_manager", None)
    if manager is not None:
        try:
            from agent.memory_manager import memory_provider_tools_exposed
            if memory_provider_tools_exposed(agent):
                return TOPIC_PREPUBLICATION_CAPABILITY_FAILURE_CODE, TOPIC_PREPUBLICATION_CAPABILITY_FAILURE_MESSAGE
        except Exception:
            # An uninspectable external memory surface is not safe to admit.
            return TOPIC_PREPUBLICATION_CAPABILITY_FAILURE_CODE, TOPIC_PREPUBLICATION_CAPABILITY_FAILURE_MESSAGE
    builtin_memory_writable = bool(
        getattr(agent, "_memory_store", None) is not None
        and (getattr(agent, "_memory_enabled", False) or getattr(agent, "_user_profile_enabled", False))
    )
    for tool in getattr(agent, "tools", None) or ():
        if (
            builtin_memory_writable
            and isinstance(tool, dict)
            and tool.get("function", {}).get("name") == "memory"
        ):
            return TOPIC_PREPUBLICATION_CAPABILITY_FAILURE_CODE, TOPIC_PREPUBLICATION_CAPABILITY_FAILURE_MESSAGE
    return None


def selected_topic_store_preflight_failure(agent: Any) -> tuple[str, str] | None:
    """Probe the selected topic store before any turn-publication side effect.

    Building a topic prompt only reads the already-selected store and its topic rows.  It is
    therefore the strongest pure preflight available: an unavailable/malformed store is rejected
    before runtime publication, identity binding, user-content logging, hooks, or persistence.
    Topic creation/transition remains deliberately later because it is a durable mutation.
    """
    if not getattr(agent, "_topic_segmentation_enabled", False):
        return None
    try:
        topic_prompt_context(agent)
    except TopicSegmentationRuntimeError:
        return TOPIC_SEGMENTATION_RUNTIME_FAILURE_CODE, TOPIC_SEGMENTATION_RUNTIME_FAILURE_MESSAGE
    except Exception as exc:
        _topic_runtime_failure("preflight", exc)
        return TOPIC_SEGMENTATION_RUNTIME_FAILURE_CODE, TOPIC_SEGMENTATION_RUNTIME_FAILURE_MESSAGE
    return None


def retry_pending_topic_retraction(agent: Any) -> tuple[str, str] | None:
    """Retry only an exact prior failed-turn retraction before admitting another turn."""
    pending = getattr(agent, "_pending_topic_retraction", None)
    if not isinstance(pending, dict):
        return None
    ids = pending.get("message_ids")
    db = getattr(agent, "_session_db", None)
    retract = getattr(db, "retract_topic_turn_messages", None)
    if not (isinstance(ids, list) and ids and callable(retract)):
        return "topic_segmentation_retraction_indeterminate", "Topic segmentation cleanup must be retried before continuing."
    try:
        retracted = retract(
            pending.get("session_id"), ids, turn_lease_holder=pending.get("turn_lease_holder"),
        )
    except Exception:
        logger.warning("selected-topic retraction retry failed", exc_info=True)
        return "topic_segmentation_retraction_indeterminate", "Topic segmentation cleanup must be retried before continuing."
    if retracted != len(list(dict.fromkeys(ids))):
        return "topic_segmentation_retraction_indeterminate", "Topic segmentation cleanup must be retried before continuing."
    delattr(agent, "_pending_topic_retraction")
    return None


def _topic_runtime_failure(operation: str, exc: Exception | None = None) -> TopicSegmentationRuntimeError:
    """Classify a topic failure without retaining identifier-bearing exception text."""
    if exc is None:
        logger.warning("topic segmentation runtime failure: operation=%s", operation)
    else:
        logger.warning(
            "topic segmentation runtime failure: operation=%s error_type=%s",
            operation, type(exc).__name__,
        )
    return TopicSegmentationRuntimeError()


def _configured_default(config: Any) -> bool:
    session = config.get("session", {}) if isinstance(config, dict) else {}
    topics = session.get("topic_segmentation", {}) if isinstance(session, dict) else {}
    return bool(topics.get("enabled", False)) if isinstance(topics, dict) else False


def initialize_topic_segmentation(agent: Any, config: Any) -> None:
    """Resolve global + per-session enablement and restore the active topic.

    A persisted session override wins over the global default.  Every failure
    disables the feature for this agent rather than changing normal session
    loading or making a hidden model call.
    """
    enabled = _configured_default(config)
    agent._topic_segmentation_default = enabled
    db = getattr(agent, "_session_db", None)
    session_id = getattr(agent, "session_id", None)
    session_override: Optional[bool] = None
    failure: TopicSegmentationRuntimeError | None = None
    if db is not None and session_id:
        try:
            override = db.get_session_model_config_value(
                session_id, TOPIC_SESSION_CONFIG_KEY, None
            )
            if isinstance(override, bool):
                session_override = override
                enabled = override
        except Exception as exc:
            failure = _topic_runtime_failure("restore_override", exc) if enabled else None
            logger.warning("Could not restore disabled topic-segmentation state for session=%s", session_id, exc_info=True)
        if failure is not None:
            raise failure
    if session_override is not None and isinstance(
        getattr(agent, "_session_init_model_config", None), dict
    ):
        # Compression rotation copies this seed into the continuation row.
        agent._session_init_model_config[TOPIC_SESSION_CONFIG_KEY] = session_override
    agent._topic_segmentation_enabled = enabled
    agent._active_topic_id = None
    if enabled and db is not None and session_id:
        try:
            active = db.get_active_topic(session_id)
            agent._active_topic_id = active["id"] if active else None
        except Exception as exc:
            failure = _topic_runtime_failure("restore_active_topic", exc)
        else:
            failure = None
        if failure is not None:
            raise failure
    _sync_context_engine_topic(agent)


def refresh_topic_segmentation(agent: Any) -> None:
    """Restore topic state after a reused agent changes physical sessions."""
    initialize_topic_segmentation(
        agent,
        {
            "session": {
                "topic_segmentation": {
                    "enabled": bool(getattr(agent, "_topic_segmentation_default", False))
                }
            }
        },
    )


def _sync_context_engine_topic(agent: Any) -> None:
    """Keep all in-place compaction paths scoped to the selected topic."""
    engine = getattr(agent, "context_compressor", None)
    if engine is not None:
        engine._active_topic_id = (
            getattr(agent, "_active_topic_id", None)
            if getattr(agent, "_topic_segmentation_enabled", False)
            else None
        )


def normalize_topic_title(value: Any) -> str:
    """Return a short stable display title accepted from model/CLI output."""
    text = " ".join(str(value or "").strip().split())
    text = re.sub(r"^[`'\"*_\-]+|[`'\"*_\-]+$", "", text).strip()
    return text[:_MAX_TOPIC_TITLE_CHARS].rstrip()


def _topic_key(value: Any) -> str:
    return "-".join(_TOPIC_WORD_RE.findall(normalize_topic_title(value).lower()))


def parse_topic_signal(content: Any) -> Optional[str]:
    """Parse the final valid ``TOPIC:`` line; malformed/absent output is a no-op."""
    if not isinstance(content, str):
        return None
    match = _TOPIC_LINE_RE.search(content)
    if match is None:
        return None
    title = normalize_topic_title(match.group(1))
    return title or None


def match_existing_topic(title: str, topics: Iterable[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Conservatively fuzzy-match general and hyphen-qualified topic names."""
    wanted = _topic_key(title)
    if not wanted:
        return None
    exact = []
    prefix = []
    wanted_parts = wanted.split("-")
    for topic in topics:
        key = _topic_key(topic.get("title"))
        if key == wanted:
            exact.append(topic)
        elif key and (
            key.split("-")[0] == wanted_parts[0]
            and (key.startswith(wanted + "-") or wanted.startswith(key + "-"))
        ):
            prefix.append(topic)
    candidates = exact or prefix
    return candidates[0] if len(candidates) == 1 else None


def _initial_topic_title(message: Any) -> str:
    if isinstance(message, list):
        message = " ".join(
            str(part.get("text") or "")
            for part in message
            if isinstance(part, dict) and part.get("type") == "text"
        )
    text = normalize_topic_title(message)
    if not text:
        return "session"
    first_line = text.splitlines()[0]
    words = first_line.split()
    return normalize_topic_title(" ".join(words[:6])) or "session"


def prepare_topic_turn(
    agent: Any,
    messages: list[dict[str, Any]],
    current_turn_user_idx: int,
    original_user_message: Any,
) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
    """Bind this turn to the active topic and load only that topic's model history.

    The durable database is the owner.  If it cannot be read, the supplied
    history is retained (normal chat remains usable) and segmentation is
    disabled for this agent so a partial topic view is never silently claimed.
    """
    prior = list(messages[:current_turn_user_idx])
    if not getattr(agent, "_topic_segmentation_enabled", False):
        return messages, current_turn_user_idx, prior
    db = getattr(agent, "_session_db", None)
    session_id = getattr(agent, "session_id", None)
    if db is None or not session_id:
        raise _topic_runtime_failure("prepare_missing_store")
    if not (0 <= current_turn_user_idx < len(messages)):
        return messages, current_turn_user_idx, prior

    user_message = messages[current_turn_user_idx]
    history: list[dict[str, Any]] = []
    topic_id = 0
    try:
        holder = getattr(agent, "_active_session_turn_lease_holder", None)
        if holder is None:
            # Preserve the SQLite SessionDB contract, which has no holder kwarg.
            active = db.ensure_session_topic(session_id, _initial_topic_title(original_user_message))
        else:
            active = db.ensure_session_topic(
                session_id, _initial_topic_title(original_user_message), turn_lease_holder=holder,
            )
        topic_id = int(active["id"])
        history = db.get_messages_as_conversation(
            session_id,
            include_ancestors=True,
            repair_alternation=True,
            include_row_ids=True,
            topic_id=topic_id,
        )
    except Exception as exc:
        failure = _topic_runtime_failure("load_history", exc)
    else:
        failure = None
    if failure is not None:
        raise failure

    current_row_id = user_message.get("_row_id") if isinstance(user_message, dict) else None
    if isinstance(current_row_id, int):
        history = [row for row in history if row.get("_row_id") != current_row_id]
    user_message[TOPIC_MESSAGE_FIELD] = topic_id
    rebuilt = [*history, user_message]
    agent._active_topic_id = topic_id
    _sync_context_engine_topic(agent)
    agent._persist_user_message_idx = len(rebuilt) - 1
    return rebuilt, len(rebuilt) - 1, history


def topic_prompt_context(agent: Any) -> str:
    """Build the per-turn topic index/instruction without touching the system prompt."""
    if not getattr(agent, "_topic_segmentation_enabled", False):
        return ""
    db = getattr(agent, "_session_db", None)
    session_id = getattr(agent, "session_id", None)
    if db is None or not session_id:
        raise _topic_runtime_failure("build_index_missing_store")
    topics: list[dict[str, Any]] = []
    try:
        topics = db.get_topics(session_id)
    except Exception as exc:
        failure = _topic_runtime_failure("build_index", exc)
    else:
        failure = None
    if failure is not None:
        raise failure
    lines = ["[SESSION TOPICS — classify this turn; no extra model call]"]
    for topic in topics[:8]:
        marker = " active" if topic.get("state") == "active" else ""
        lines.append(
            f"- {topic['id']}: {topic['title']} ({topic.get('message_count', 0)} messages{marker})"
        )
    lines.extend(
        (
            "Append exactly one line to the response: TOPIC: <short-name>",
            "Reuse an existing short name for a follow-up; choose a new short name only for a clear subject change.",
        )
    )
    return "\n".join(lines)


def merge_topic_prompt_context(existing: str, agent: Any) -> str:
    block = topic_prompt_context(agent)
    if not block:
        return existing
    return f"{existing.rstrip()}\n\n{block}" if existing and existing.strip() else block


def process_turn_topic(agent: Any, messages: list[dict[str, Any]], final_response: Any) -> None:
    """Apply the response topic to the entire current turn, atomically when rows exist.

    The user row is crash-persisted before the provider call.  A later topic
    transition therefore retags every already-durable row in this turn inside
    the same transaction that activates/creates the topic; unflushed rows carry
    ``_topic_id`` into the normal append-only persistence funnel.
    """
    if not getattr(agent, "_topic_segmentation_enabled", False):
        return
    db = getattr(agent, "_session_db", None)
    session_id = getattr(agent, "session_id", None)
    if db is None or not session_id:
        raise _topic_runtime_failure("transition_missing_store")
    start = getattr(agent, "_persist_user_message_idx", None)
    if not isinstance(start, int) or not (0 <= start < len(messages)):
        return

    title = parse_topic_signal(final_response)
    topic_id = 0
    try:
        topics = db.get_topics(session_id)
        active = next((topic for topic in topics if topic.get("state") == "active"), None)
        target = match_existing_topic(title, topics) if title else active
        if target is None and not title:
            return
        turn_rows = [row for row in messages[start:] if isinstance(row, dict)]
        row_ids = [
            row["_row_id"]
            for row in turn_rows
            if isinstance(row.get("_row_id"), int) and row["_row_id"] > 0
        ]
        selected = db.activate_topic_for_messages(
            session_id,
            topic_id=int(target["id"]) if target else None,
            title=title if target is None else None,
            message_ids=row_ids,
            turn_lease_holder=getattr(agent, "_active_session_turn_lease_holder", None),
        )
        topic_id = int(selected["id"])
    except Exception as exc:
        failure = _topic_runtime_failure("transition", exc)
    else:
        failure = None
    if failure is not None:
        raise failure

    agent._active_topic_id = topic_id
    _sync_context_engine_topic(agent)
    for row in messages[start:]:
        if isinstance(row, dict):
            row[TOPIC_MESSAGE_FIELD] = topic_id
