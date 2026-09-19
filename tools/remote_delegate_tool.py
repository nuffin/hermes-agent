"""Bounded remote delegation over a configured Hermes ``/v1/runs`` API."""

import json
import os
import re
import uuid
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from tools.registry import registry


_ERROR_MESSAGE = "Remote delegation request failed."
_PUBLIC_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,255}$")


class _NoRedirect(HTTPRedirectHandler):
    """Fail closed rather than forwarding bearer credentials to a redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _load_remote_delegation_config() -> dict:
    """Read the active profile's remote-delegation configuration."""
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
        section = config.get("remote_delegation") if isinstance(config, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception:
        try:
            from cli import CLI_CONFIG

            section = CLI_CONFIG.get("remote_delegation")
            return section if isinstance(section, dict) else {}
        except Exception:
            return {}


def _target_config(target: Any) -> Optional[Dict[str, str]]:
    """Resolve one configured target without exposing its private fields."""
    if not isinstance(target, str) or not target:
        return None
    targets = _load_remote_delegation_config().get("targets")
    candidate = targets.get(target) if isinstance(targets, dict) else None
    if not isinstance(candidate, dict):
        return None
    base_url = candidate.get("base_url")
    api_key_env = candidate.get("api_key_env")
    profile = candidate.get("profile")
    if not isinstance(base_url, str) or not isinstance(api_key_env, str):
        return None
    parsed = urlsplit(base_url.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    if parsed.query or parsed.fragment:
        return None
    result = {
        "base_url": urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")),
        "api_key_env": api_key_env,
    }
    if isinstance(profile, str) and profile:
        result["profile"] = profile
    return result


def check_remote_delegate_requirements() -> bool:
    """Expose the tool only when at least one usable target is configured."""
    targets = _load_remote_delegation_config().get("targets")
    if not isinstance(targets, dict):
        return False
    for name in targets:
        config = _target_config(name)
        if config and os.environ.get(config["api_key_env"]):
            return True
    return False


def _safe_error(target: Any = None) -> str:
    result: Dict[str, str] = {"error": _ERROR_MESSAGE}
    if isinstance(target, str) and target:
        result["target"] = target
    return json.dumps(result)


def _instructions(task: Dict[str, Any], profile: Optional[str]) -> str:
    """Put task contract metadata in the remote run instructions, not input."""
    contract: Dict[str, Any] = {
        "context": task.get("context", ""),
        "output_schema": task.get("output_schema"),
    }
    if profile:
        contract["profile"] = profile
    return (
        "You are a remote delegated worker. Follow this task contract exactly. "
        "Return only the final answer required by the contract.\n"
        + json.dumps(contract, ensure_ascii=False, separators=(",", ":"))
    )


def _idempotency_key(request_id: Any) -> Optional[str]:
    if request_id is not None:
        if not isinstance(request_id, str) or not request_id:
            return None
        if len(request_id) > 220 or any(ord(char) < 33 or ord(char) > 126 for char in request_id):
            return None
        return "remote-delegation-" + request_id
    return "remote-delegation-" + uuid.uuid4().hex


def _request(config: Dict[str, str], path: str, method: str, body: Optional[dict] = None,
             idempotency_key: Optional[str] = None) -> Optional[dict]:
    api_key = os.environ.get(config["api_key_env"])
    if not api_key:
        return None
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    try:
        request = Request(config["base_url"] + path, data=data, headers=headers, method=method)
        with build_opener(_NoRedirect()).open(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except (HTTPError, URLError, OSError, ValueError, UnicodeDecodeError):
        return None


def _public_run(target: str, payload: Optional[dict], default_status: Optional[str] = None) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not _PUBLIC_TOKEN_RE.fullmatch(run_id):
        return None
    status = payload.get("status", default_status)
    if not isinstance(status, str) or not _PUBLIC_TOKEN_RE.fullmatch(status):
        return None
    result: Dict[str, str] = {"target": target, "remote_id": run_id}
    result["status"] = status
    return result


def remote_delegate_task(*, action: str = "spawn", target: Any = None, tasks: Any = None,
                         remote_id: Any = None, message: Any = None) -> str:
    """Dispatch or control remote runs using a configured target name only."""
    config = _target_config(target)
    if config is None:
        return _safe_error()
    if action == "spawn":
        if not isinstance(tasks, list) or not tasks:
            return _safe_error(target)
        validated_tasks = []
        for task in tasks:
            if not isinstance(task, dict) or not isinstance(task.get("goal"), str) or not task["goal"]:
                return _safe_error(target)
            if "context" in task and not isinstance(task["context"], str):
                return _safe_error(target)
            if "output_schema" in task and not isinstance(task["output_schema"], dict):
                return _safe_error(target)
            key = _idempotency_key(task.get("request_id"))
            if key is None:
                return _safe_error(target)
            validated_tasks.append((task, key))
        results = []
        for task, key in validated_tasks:
            payload = _request(
                config,
                "/v1/runs",
                "POST",
                {"input": task["goal"], "instructions": _instructions(task, config.get("profile"))},
                key,
            )
            public = _public_run(target, payload, "queued")
            if public is None:
                if not results:
                    return _safe_error(target)
                # Earlier calls may have created durable runs. Preserve only
                # their public handles so callers can recover without replay.
                result: Dict[str, Any] = {"target": target, "runs": results, "error": _ERROR_MESSAGE}
                return json.dumps(result)
            results.append(public)
        return json.dumps({"target": target, "runs": results})

    if (
        action not in {"status", "steer", "stop"}
        or not isinstance(remote_id, str)
        or not _PUBLIC_TOKEN_RE.fullmatch(remote_id)
    ):
        return _safe_error(target)
    if action == "status":
        public = _public_run(target, _request(config, f"/v1/runs/{remote_id}", "GET"))
    elif action == "steer":
        if not isinstance(message, str) or not message:
            return _safe_error(target)
        public = _public_run(
            target,
            _request(config, f"/v1/runs/{remote_id}/steer", "POST", {"input": message}),
            "steered",
        )
    else:
        public = _public_run(
            target,
            _request(config, f"/v1/runs/{remote_id}/stop", "POST"),
            "stopping",
        )
    return json.dumps(public) if public is not None else _safe_error(target)


REMOTE_DELEGATE_TASK_SCHEMA = {
    "name": "remote_delegate_task",
    "description": "Run bounded tasks on a configured remote Hermes target and check or control their public run status.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["spawn", "status", "steer", "stop"],
                "description": "The remote delegation operation. Defaults to spawn.",
            },
            "target": {"type": "string", "description": "Configured remote delegation target name."},
            "tasks": {
                "type": "array",
                "minItems": 1,
                "description": "Tasks to spawn; one remote run is created per task.",
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string"},
                        "context": {"type": "string"},
                        "output_schema": {"type": "object"},
                        "request_id": {"type": "string"},
                    },
                    "required": ["goal"],
                },
            },
            "remote_id": {"type": "string", "description": "Remote run ID returned by this tool."},
            "message": {"type": "string", "description": "Required steer message."},
        },
        "required": ["target"],
    },
}


registry.register(
    name="remote_delegate_task",
    toolset="remote_delegation",
    schema=REMOTE_DELEGATE_TASK_SCHEMA,
    handler=lambda args, **_kw: remote_delegate_task(
        action=args.get("action", "spawn"),
        target=args.get("target"),
        tasks=args.get("tasks"),
        remote_id=args.get("remote_id"),
        message=args.get("message"),
    ),
    check_fn=check_remote_delegate_requirements,
    emoji="🌐",
)
