"""LLM enrichment transport, prompt construction, and intent splitting."""
from __future__ import annotations

import json
import os
import time
from typing import Any

import hermes_yaml as yaml

# ── LLM Enrichment ────────────────────────────────────────────────────────────

# Scene vocabulary — fixed set of valid scene values for LLM enrichment
SCENE_VOCABULARY = [
    "coding",       # Writing/fixing/reviewing code, PRs, git operations
    "writing",      # Composing text — papers, articles, docs, fiction
    "research",     # Investigating, exploring, searching, auditing
    "design",       # Architecture, system design, planning, prototyping
    "devops",       # Deploying, configuring, docker, infrastructure
    "hermes",       # Hermes agent config, plugins, skills, meta-work
    "media",        # Audio, video, image production
    "common",       # Cross-scene, applicable in any context
]


def needs_enrichment(info: dict[str, Any]) -> bool:
    """Check if a skill needs LLM enrichment (missing tags or scenes)."""
    return not info.get("tags") or not info.get("scenes")


def build_enrichment_prompt(skill_name: str, content: str) -> str:
    """Build the LLM prompt for enriching a skill."""
    scene_desc = "\n".join(
        f"  - {s}: {SCENE_VOCABULARY_DESC.get(s, '')}"
        for s in SCENE_VOCABULARY
    )
    return f"""You are a skill metadata tagger. Analyze this SKILL.md and produce:
1. tags: 3-8 lowercase descriptive tags (domain, tech stack, activity type)
2. scenes: 1-3 scene values from the fixed scene vocabulary below
3. suggestions: any scene candidates NOT in the vocabulary that might apply

Scene vocabulary (ONLY use these for the "scenes" field):
{scene_desc}

Rules for scenes:
- Pick 1-3 most applicable scenes from the vocabulary above
- Include "common" only if the skill truly applies across many scenes
- Be conservative — if unsure, prefer fewer scenes

Rules for tags:
- Lowercase, descriptive, 3-8 tags
- Include: domain, primary tech/tool, activity type
- Example: ["python", "fastapi", "backend", "code-review"]

Respond with ONLY a JSON object (no markdown, no explanation):
{{"tags": ["tag1", "tag2", ...], "scenes": ["scene1", ...], "suggestions": []}}

Skill: {skill_name}
Content:
{content[:4000]}"""


SCENE_VOCABULARY_DESC = {
    "coding": "Writing/fixing/reviewing code, PRs, git operations",
    "writing": "Composing text — papers, articles, docs, fiction",
    "research": "Investigating, exploring, searching, auditing",
    "design": "Architecture, system design, planning, prototyping",
    "devops": "Deploying, configuring, docker, infrastructure",
    "hermes": "Hermes agent config, plugins, skills, meta-work",
    "media": "Audio, video, image production",
    "common": "Cross-scene, applicable in any context",
}


def parse_enrichment_response(data: Any) -> tuple[bool, dict[str, Any] | None]:
    """Distinguish empty content (retry) from non-object JSON (stop)."""
    text = (data.get("choices", [{}])[0]
            .get("message", {}).get("content", "")).strip()
    if not text:
        return True, None
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:]) if len(lines) > 1 else text
        if text.endswith("```"):
            text = text[:-3]
    if text.startswith("json"):
        text = text[4:].strip()
    result = json.loads(text)
    return False, result if isinstance(result, dict) else None


def request_enrichment_with_retries(url: str, headers: dict, payload: dict, *, parse_response, logger) -> dict[str, Any] | None:
    """Bound transport/parse retries and keep all failure logs credential-free."""
    import requests

    for attempt in range(3):
        resp = None
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            empty, result = parse_response(resp.json())
            if not empty:
                return result
            location = "enrichment.empty"
        except (requests.RequestException, json.JSONDecodeError, IndexError,
                AttributeError, TypeError, KeyError):
            location = "enrichment.request"
        # A response's status_code is untrusted; log only a real HTTP integer.
        code = getattr(resp, "status_code", None)
        status = code if type(code) is int and 100 <= code <= 599 else None
        logger.warning("skill-graph: %s [attempt=%d/3 status=%s]",
                       location, attempt + 1, status)
        if attempt < 2:
            time.sleep(2 * (attempt + 1))
    return None


def call_llm_for_enrichment(prompt: str, *, request_enrichment, logger) -> dict[str, Any] | None:
    """Call the configured LLM provider for enrichment; return JSON or None."""
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY, has_usable_secret
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
        sg_config = config.get("skills", {}).get("config", {}).get("skill-graph", {})
        enrichment_cfg = sg_config.get("enrichment", {}) if isinstance(sg_config, dict) else {}
        preferred_provider = enrichment_cfg.get("provider", "")

        def _resolve_provider(pid: str) -> tuple[str, str] | None:
            """Return (api_key, base_url) for a provider, or None."""
            pconfig = PROVIDER_REGISTRY.get(pid)
            if not pconfig:
                return None
            for env_var in pconfig.api_key_env_vars:
                key_val = os.environ.get(env_var, "")
                if has_usable_secret(key_val):
                    url = os.environ.get(pconfig.base_url_env_var, "") if pconfig.base_url_env_var else ""
                    if not url:
                        url = pconfig.inference_base_url or ""
                    return key_val, url
            return None

        provider_name = ""
        api_key = ""
        base_url = ""
        if preferred_provider:
            resolved = _resolve_provider(preferred_provider)
            if resolved:
                provider_name = preferred_provider
                api_key, base_url = resolved
            else:
                logger.warning("skill-graph: enrichment unavailable [location=enrichment.provider]")
                return None
        else:
            # Default: use the main agent's configured provider
            main_model_cfg = config.get("model", {})
            main_provider = main_model_cfg.get("provider", "")
            if main_provider:
                resolved = _resolve_provider(main_provider)
                if resolved:
                    provider_name = main_provider
                    api_key, base_url = resolved
            if not provider_name:
                # Last resort: first provider with a usable API key
                for pid in PROVIDER_REGISTRY:
                    if pid in ("copilot", "lmstudio"):
                        continue
                    resolved = _resolve_provider(pid)
                    if resolved:
                        provider_name = pid
                        api_key, base_url = resolved
                        break

        if not provider_name:
            logger.warning("skill-graph: enrichment unavailable [location=enrichment.provider]")
            return None

        # Resolve model: enrichment config → main agent model → deepseek-v4-flash
        try:
            model_name = enrichment_cfg.get("model", "") or config.get("model", {}).get("default", "")
        except (AttributeError, TypeError):
            logger.warning("skill-graph: enrichment model fallback [location=enrichment.model]")
            model_name = ""
        model_name = model_name or "deepseek-v4-flash"

        # Normalise base_url: strip /v1 suffix if present (we add it below)
        base_url = base_url.rstrip("/")
        if base_url.endswith("/v1"):
            base_url = base_url[:-3]

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 2048,
        }
        return request_enrichment(f"{base_url}/v1/chat/completions", headers, payload)
    except (ImportError, OSError, TypeError, ValueError, AttributeError,
            RuntimeError, KeyError, yaml.YAMLError):
        logger.warning("skill-graph: enrichment unavailable [location=enrichment.setup]")
        return None


# ── Intent Split ───────────────────────────────────────────────────────────


def resolve_llm_provider(
    config: dict | None = None, *, logger, log_fallback_exception,
) -> tuple[str, str, str] | None:
    """Resolve (provider_name, api_key, base_url) for LLM calls.

    Mirrors enrichment's provider resolution: preferred provider from
    config → main agent provider → first provider with a usable API key.
    """
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY, has_usable_secret
        from hermes_cli.config import load_config_readonly
    except (ImportError, OSError, TypeError, ValueError, AttributeError,
            RuntimeError, yaml.YAMLError) as exc:
        log_fallback_exception("skill-graph: could not import provider configuration", exc)
        return None
    if config is None:
        try:
            config = load_config_readonly() or {}
        except (ImportError, OSError, TypeError, ValueError, AttributeError,
                RuntimeError, yaml.YAMLError) as exc:
            log_fallback_exception("skill-graph: could not read provider configuration", exc)
            return None
    if not isinstance(config, dict):
        return None

    sg_config = config.get("skills", {}).get("config", {}).get("skill-graph", {})
    if not isinstance(sg_config, dict):
        sg_config = {}

    def _resolve_provider(pid: str) -> tuple[str, str] | None:
        pconfig = PROVIDER_REGISTRY.get(pid)
        if not pconfig:
            return None
        for env_var in pconfig.api_key_env_vars:
            key_val = os.environ.get(env_var, "")
            if has_usable_secret(key_val):
                url = os.environ.get(pconfig.base_url_env_var, "") if pconfig.base_url_env_var else ""
                if not url:
                    url = pconfig.inference_base_url or ""
                return key_val, url
        return None

    provider_name = ""
    api_key = ""
    base_url = ""
    preferred = sg_config.get("enrichment", {}).get("provider", "") if isinstance(sg_config.get("enrichment"), dict) else ""
    if preferred:
        resolved = _resolve_provider(preferred)
        if resolved:
            provider_name = preferred
            api_key, base_url = resolved
    if not provider_name:
        main_provider = config.get("model", {}).get("provider", "")
        if main_provider:
            resolved = _resolve_provider(main_provider)
            if resolved:
                provider_name = main_provider
                api_key, base_url = resolved
    if not provider_name:
        for pid in PROVIDER_REGISTRY:
            if pid in ("copilot", "lmstudio"):
                continue
            resolved = _resolve_provider(pid)
            if resolved:
                provider_name = pid
                api_key, base_url = resolved
                break
    if not provider_name:
        return None
    return provider_name, api_key, base_url


def split_intents(
    user_message: str,
    prev_intents: list[str] | None = None,
    *, resolve_provider, logger, log_fallback_exception,
) -> tuple[list[str], str | None, bool | None]:
    """Split a user message into intent sentences via one lightweight LLM call.

    Returns ``(intents, scene, topic_continuation)``:

    - ``intents``: list of intent sentences (``[]`` on failure — caller falls
      back to treating the whole message as one intent).
    - ``scene``: one of coding|writing|research|design|devops|hermes|media|common,
      or ``None`` if the LLM didn't return it.
    - ``topic_continuation``: ``True``/``False`` from the LLM when
      ``prev_intents`` is provided, ``None`` if unavailable. Piggy-backed onto
      the intent-split call (zero extra API cost).

    Multi-intent: each intent is a *sentence* (embedding-friendly), not a
    keyword list. When ``prev_intents`` is given, the prompt includes the
    previous message's intents so the model can judge topic continuity.
    """
    if not user_message or not user_message.strip():
        return [], None, None

    try:
        import requests
    except ImportError:
        logger.warning("skill-graph: intent split skipped — requests not installed")
        return [], None, None

    resolved = resolve_provider()
    if resolved is None:
        logger.warning("skill-graph: intent split skipped — no provider with API key")
        return [], None, None
    provider_name, api_key, base_url = resolved

    # Model: intent_split_model config → enrichment model → main model → default
    model_name = "deepseek-v4-flash"
    try:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly() or {}
        sg = config.get("skills", {}).get("config", {}).get("skill-graph", {})
        if isinstance(sg, dict):
            model_name = (
                sg.get("intent_split_model")
                or (sg.get("enrichment", {}) or {}).get("model")
                or config.get("model", {}).get("default")
                or "deepseek-v4-flash"
            )
    except (ImportError, OSError, TypeError, ValueError, AttributeError,
            RuntimeError, yaml.YAMLError) as exc:
        log_fallback_exception("skill-graph: intent split model fallback", exc)

    base_url = base_url.rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]

    # Build topic-continuation prompt section (only when prev_intents given)
    topic_section = ""
    if prev_intents:
        prev_text = " | ".join(prev_intents[:3])
        topic_section = (
            f"\n--- PREVIOUS MESSAGE INTENTS ---\n{prev_text}\n--- END ---\n\n"
            'Also include this field:\n'
            '"topic_continuation": true/false — is this new message continuing '
            "the same topic/task as the previous message (whose intents are "
            "shown above), or is it a new topic?\n"
            "Judge by the core intent, not surface words. Follow-ups, "
            'clarifications, and confirmations are "continuation". A brand-new '
            'task or unrelated question is "new topic".\n\n'
        )

    prompt = (
        "Split the user's message into separate intents. Each intent should be "
        "ONE complete sentence describing a single topic the user wants done. "
        "Output ONLY JSON:\n"
        '{"intents": ["<intent 1 as a sentence>", "<intent 2 as a sentence>", ...], '
        '"scene": "<one of: coding|writing|research|design|devops|hermes|media|common>"'
        + (', "topic_continuation": true/false}' if prev_intents else "}")
        + "\nRules:\n"
        "- Keep the user's original meaning; do not add requirements.\n"
        "- 1-6 intents. If the message is a single topic, return exactly 1 intent "
        "with the full message rephrased as a sentence.\n"
        "- Each intent must be self-contained (no 'it'/'that' references across intents).\n"
        "- Scene: pick the single most applicable value.\n"
        + topic_section
        + "User message:\n"
        f"{user_message}"
    )

    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 1024,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    import time as _time
    for _attempt in range(3):
        try:
            resp = requests.post(
                f"{base_url}/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            text = (data.get("choices", [{}])[0]
                    .get("message", {}).get("content", "")).strip()
            if not text:
                if _attempt < 2:
                    _time.sleep(1.5 * (_attempt + 1))
                    continue
                logger.warning("skill-graph: intent split empty response [attempt=3/3]")
                return [], None, None

            # Strip markdown code fences if present
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:]) if len(lines) > 1 else text
                if text.endswith("```"):
                    text = text[:-3]
            if text.startswith("json"):
                text = text[4:].strip()

            result = json.loads(text)
            intents = result.get("intents", []) if isinstance(result, dict) else []
            intents = [str(i).strip() for i in intents if str(i).strip()]
            scene = result.get("scene") if isinstance(result, dict) else None
            tc = result.get("topic_continuation") if isinstance(result, dict) else None
            # Coerce tc to bool/None
            if isinstance(tc, str):
                tc = tc.lower().strip() in ("true", "yes", "1")
            elif not isinstance(tc, bool):
                tc = None
            if intents:
                return intents, scene, tc
            logger.warning("skill-graph: intent split returned no intents")
            return [], None, None

        except (requests.RequestException, json.JSONDecodeError):
            logger.warning(
                "skill-graph: intent split request failed [attempt=%d/3]",
                _attempt + 1,
            )
            if _attempt < 2:
                _time.sleep(1.5 * (_attempt + 1))

    return [], None, None
