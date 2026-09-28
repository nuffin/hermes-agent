"""Apply compression configuration to a running agent without rebuilding it."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Mapping, Sequence
from typing import Any, cast

from utils import is_truthy_value

logger = logging.getLogger(__name__)


def _freeze_config_value(value: Any) -> Any:
    """Make config values comparable without depending on a surface-specific loader."""
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _freeze_config_value(item)) for key, item in value.items()))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze_config_value(item) for item in value)
    try:
        hash(value)
    except TypeError:
        return repr(value)
    return value


def compression_config_signature(cfg: dict | None) -> tuple:
    """Stable snapshot of only the compression and context-window settings applied live."""
    if not isinstance(cfg, dict):
        return ()
    raw_compression = cfg.get("compression")
    raw_model = cfg.get("model")
    compression = cast(dict[str, Any], raw_compression) if isinstance(raw_compression, dict) else {}
    model = cast(dict[str, Any], raw_model) if isinstance(raw_model, dict) else {}
    return (
        ("compression", _freeze_config_value(compression)),
        ("model.context_length", _freeze_config_value(model.get("context_length"))),
    )


def _compressor_ctor_default(name: str, fallback: Any) -> Any:
    """Read a ContextCompressor ctor default so unset-key restoration cannot drift."""
    try:
        import inspect

        from agent.context_compressor import ContextCompressor

        default = inspect.signature(ContextCompressor.__init__).parameters[name].default
        return fallback if default is inspect.Parameter.empty else default
    except Exception:
        return fallback


def _default_threshold_tokens_cap() -> Any:
    """Return the construction default for an absent threshold_tokens config key."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    return (DEFAULT_CONFIG.get("compression") or {}).get("threshold_tokens")


def _derived_default_threshold_percent(agent: Any, compression: dict) -> float:
    """Mirror construction's threshold default and Codex autoraise resolution."""
    try:
        pct = float(_compressor_ctor_default("threshold_percent", 0.50))
    except (TypeError, ValueError):
        pct = 0.50
    try:
        from agent.agent_init import _resolve_compression_threshold
        from agent.auxiliary_client import (
            _compression_threshold_for_model,
            _is_codex_gpt54_or_gpt55,
            _is_codex_spark,
        )

        model, provider = getattr(agent, "model", "") or "", getattr(agent, "provider", "") or ""
        autoraise_enabled = str(compression.get("codex_gpt55_autoraise", True)).lower() in {"true", "1", "yes"}
        pct, _notice = _resolve_compression_threshold(
            pct,
            _compression_threshold_for_model(model, provider, allow_codex_gpt55_autoraise=autoraise_enabled),
            model=model,
            is_codex_autoraise=_is_codex_gpt54_or_gpt55(model, provider) or _is_codex_spark(model, provider),
        )
    except Exception:
        pass
    return pct


# (config key == compressor attr, ctor-default fallback, min_value)
_COMPRESSION_INT_KEYS = (
    ("proactive_prune_tokens", 0, 0),
    ("proactive_prune_min_result_chars", 8000, 0),
    ("proactive_prune_min_reclaim_tokens", 4096, 0),
    ("protect_last_n", 20, 0),
    ("min_tail_user_messages", 1, 1),
)


def apply_live_compression_config(agent: Any, cfg: dict | None) -> None:
    """Update an agent's compressor in place, restoring construction defaults for removed keys."""
    cfg = cfg if isinstance(cfg, dict) else {}
    raw_compression = cfg.get("compression")
    compression = cast(dict[str, Any], raw_compression) if isinstance(raw_compression, dict) else {}
    from agent.agent_init import config_context_length_for_runtime, set_config_context_length

    enabled_raw = compression.get("enabled", True)
    agent.compression_enabled = enabled_raw if isinstance(enabled_raw, bool) else str(enabled_raw).lower() in {"true", "1", "yes"}
    agent.codex_responses_native_compaction = is_truthy_value(compression.get("codex_responses_native", False))
    native_threshold_raw = compression.get("codex_responses_compact_threshold", 200_000)
    try:
        if isinstance(native_threshold_raw, bool) or (native_threshold := int(native_threshold_raw)) <= 0:
            raise ValueError
    except (TypeError, ValueError):
        logger.warning("Invalid compression.codex_responses_compact_threshold=%r; using 200000.", native_threshold_raw)
        native_threshold = 200_000
    agent.codex_responses_compact_threshold = native_threshold
    with contextlib.suppress(TypeError, ValueError):
        agent.compression_idle_compact_after_seconds = max(0, int(compression.get("idle_compact_after_seconds", 0) or 0))
    cc = getattr(agent, "context_compressor", None)
    if cc is None:
        return

    default_tail = str(_compressor_ctor_default("tail_mode", "lean"))
    mode = str(compression.get("tail_mode", default_tail) or default_tail).strip().lower()
    cc.tail_mode = mode if mode in ("legacy", "lean") else default_tail
    for key, fallback, min_value in _COMPRESSION_INT_KEYS:
        default = int(_compressor_ctor_default(key, fallback))
        raw = compression.get(key, default)
        with contextlib.suppress(TypeError, ValueError):
            setattr(cc, key, max(min_value, default if raw is None else int(raw)))
    with contextlib.suppress(TypeError, ValueError):
        ratio_raw = compression.get("target_ratio", _compressor_ctor_default("summary_target_ratio", 0.20))
        cc.summary_target_ratio = max(0.10, min(float(ratio_raw), 0.80))
    raw_thresholds = compression.get("model_thresholds")
    cc.model_thresholds = {
        str(key): float(value) for key, value in raw_thresholds.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    } if isinstance(raw_thresholds, dict) else {}

    from agent.context_compressor import resolve_model_threshold

    pct: float | None = None
    if "threshold" in compression:
        with contextlib.suppress(TypeError, ValueError):
            pct = float(compression["threshold"])
    if pct is None:
        pct = _derived_default_threshold_percent(agent, compression)
    cc._config_threshold_percent = cc._configured_threshold_percent = pct
    base = cc._base_threshold_percent = resolve_model_threshold(
        getattr(agent, "model", "") or "", cc.model_thresholds, pct, getattr(agent, "provider", "") or "",
    )
    try:
        cc.threshold_percent = cc._effective_threshold_percent(cc.context_length, base)
    except Exception:
        cc.threshold_percent = pct

    new_ctx = config_context_length_for_runtime(agent, cfg)
    if new_ctx is not None:
        set_config_context_length(agent, new_ctx)
        with contextlib.suppress(Exception):
            cc.context_length = new_ctx
    elif getattr(cc, "_config_context_length", None) is not None:
        set_config_context_length(agent, None)
        cc._resolved_context_length = None
    cc.threshold_tokens_cap = cc._coerce_threshold_tokens_cap(
        compression.get("threshold_tokens", _default_threshold_tokens_cap())
    )
    cc._threshold_tokens = cc._tail_token_budget = None
