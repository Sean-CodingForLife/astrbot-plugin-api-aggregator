from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from pydantic import Field as PydanticField
from pydantic.dataclasses import dataclass as pydantic_dataclass
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path


PLUGIN_NAME = "astrbot_plugin_api_aggregator"
VERSION = "0.2.0"
DATA_VERSION = 1
AGGREGATION_STRATEGIES = {"first-ok", "round-robin", "random", "priority"}
TRIGGER_MATCH_MODES = {"contains", "exact", "command"}
RESPONSE_TYPES = {"summary", "text", "image", "audio", "video"}
LANGUAGE_MODES = {"auto", "zh-CN", "en-US"}
PROXY_MODES = {"direct", "custom", "environment"}
AUTH_TYPES = {"none", "bearer", "basic", "api-key"}
API_KEY_IN_VALUES = {"header", "query"}
RESPONSE_TRANSFORMS = {
    "raw",
    "string",
    "json",
    "join-comma",
    "join-lines",
    "int",
    "float",
    "bool",
}
DEFAULT_LANGUAGE_MODE = "auto"
DEFAULT_TIMEOUT_SECONDS = 12
MAX_TIMEOUT_SECONDS = 120
MAX_RETRY_COUNT = 5
MAX_COOLDOWN_SECONDS = 3600
DEFAULT_RESPONSE_READ_LIMIT_BYTES = 4096
MAX_RESPONSE_READ_LIMIT_BYTES = 1048576
DEFAULT_TEST_LOG_LIMIT = 100
MAX_TEST_LOG_LIMIT = 1000
DEFAULT_PREVIEW_MAX_CHARS = 1200
MAX_PREVIEW_MAX_CHARS = 10000
SUPPORTED_PROXY_SCHEMES = {"http", "https"}
TEMPLATE_PATTERN = re.compile(r"{{\s*([^{}]+?)\s*}}")


@dataclass
class ApiAggregatorStore:
    root: Path
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def data_dir(self) -> Path:
        return self.root

    @property
    def data_path(self) -> Path:
        return self.root / "api_aggregator.json"

    def default_data(self) -> dict[str, Any]:
        return {
            "version": DATA_VERSION,
            "groups": [
                {
                    "id": "default",
                    "name": "Default",
                    "description": "Default API group",
                    "created_at": now_ms(),
                    "updated_at": now_ms(),
                }
            ],
            "apis": [],
            "test_logs": [],
            "settings": {
                "strategy": "first-ok",
                "cursors": {},
                "triggers": [],
            },
        }

    async def read(self) -> dict[str, Any]:
        async with self.lock:
            return self._read_unlocked()

    async def write(self, data: dict[str, Any]) -> dict[str, Any]:
        async with self.lock:
            normalized = normalize_data(data)
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.data_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(normalized, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.data_path)
            return normalized

    async def mutate(self, fn):
        async with self.lock:
            data = self._read_unlocked()
            result = fn(data)
            normalized = normalize_data(data)
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.data_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(normalized, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.data_path)
            return result, normalized

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.data_path.exists():
            return self.default_data()
        try:
            raw = json.loads(self.data_path.read_text(encoding="utf-8"))
        except Exception:
            return self.default_data()
        return normalize_data(raw)


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def normalize_language_mode(value: Any) -> str:
    mode = str(value or DEFAULT_LANGUAGE_MODE).strip()
    return mode if mode in LANGUAGE_MODES else DEFAULT_LANGUAGE_MODE


def normalize_proxy_mode(value: Any, *, proxy_url_value: Any = "") -> str:
    mode = str(value or "").strip()
    if mode in PROXY_MODES:
        return mode
    return "direct"


def runtime_language(value: Any) -> str:
    mode = normalize_language_mode(value)
    return "en-US" if mode == "auto" else mode


def text_for(language: str, zh: str, en: str) -> str:
    return zh if runtime_language(language) == "zh-CN" else en


def normalize_data(raw: dict[str, Any]) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    groups = data.get("groups") if isinstance(data.get("groups"), list) else []
    apis = data.get("apis") if isinstance(data.get("apis"), list) else []
    logs = data.get("test_logs") if isinstance(data.get("test_logs"), list) else []
    settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}

    cleaned_groups: list[dict[str, Any]] = []
    seen_groups: set[str] = set()
    for item in groups:
        if not isinstance(item, dict):
            continue
        group_id = str(item.get("id") or "").strip() or new_id("group")
        if group_id in seen_groups:
            continue
        seen_groups.add(group_id)
        name = str(item.get("name") or "").strip() or "Untitled Group"
        cleaned_groups.append(
            {
                "id": group_id,
                "name": name,
                "description": str(item.get("description") or ""),
                "created_at": int(item.get("created_at") or now_ms()),
                "updated_at": int(item.get("updated_at") or now_ms()),
            }
        )
    if not cleaned_groups:
        cleaned_groups.append(
            {
                "id": "default",
                "name": "Default",
                "description": "Default API group",
                "created_at": now_ms(),
                "updated_at": now_ms(),
            }
        )

    group_ids = {item["id"] for item in cleaned_groups}
    default_group = cleaned_groups[0]["id"]
    cleaned_apis: list[dict[str, Any]] = []
    seen_apis: set[str] = set()
    for item in apis:
        if not isinstance(item, dict):
            continue
        api_id = str(item.get("id") or "").strip() or new_id("api")
        if api_id in seen_apis:
            continue
        seen_apis.add(api_id)
        name = str(item.get("name") or "").strip() or "Untitled API"
        method = str(item.get("method") or "GET").strip().upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            method = "GET"
        group_id = str(item.get("group_id") or default_group).strip()
        if group_id not in group_ids:
            group_id = default_group
        cleaned_apis.append(
            {
                "id": api_id,
                "name": name,
                "group_id": group_id,
                "url": str(item.get("url") or "").strip(),
                "method": method,
                "query": dict(item.get("query") or {}) if isinstance(item.get("query"), dict) else {},
                "headers": dict(item.get("headers") or {}) if isinstance(item.get("headers"), dict) else {},
                "body": str(item.get("body") or ""),
                "auth_type": normalize_auth_type(item.get("auth_type")),
                "auth_config": normalize_auth_config(item.get("auth_config")),
                "priority": clamp_int(item.get("priority"), 100, 1, 1000),
                "circuit_failures": max(0, int(item.get("circuit_failures") or 0)),
                "circuit_open_until": max(0, int(item.get("circuit_open_until") or 0)),
                "enabled": bool(item.get("enabled", True)),
                "description": str(item.get("description") or ""),
                "timeout_seconds": clamp_int(
                    item.get("timeout_seconds"),
                    DEFAULT_TIMEOUT_SECONDS,
                    1,
                    MAX_TIMEOUT_SECONDS,
                ),
                "retry_count": clamp_int(
                    item.get("retry_count"),
                    0,
                    0,
                    MAX_RETRY_COUNT,
                ),
                "cooldown_seconds": clamp_int(
                    item.get("cooldown_seconds"),
                    0,
                    0,
                    MAX_COOLDOWN_SECONDS,
                ),
                "cooldown_until": max(0, int(item.get("cooldown_until") or 0)),
                "last_error": str(item.get("last_error") or ""),
                "last_status": item.get("last_status"),
                "last_ok": bool(item.get("last_ok", False)),
                "last_tested_at": item.get("last_tested_at"),
                "last_preview": str(item.get("last_preview") or ""),
                "created_at": int(item.get("created_at") or now_ms()),
                "updated_at": int(item.get("updated_at") or now_ms()),
            }
        )

    cleaned_logs = [item for item in logs if isinstance(item, dict)][-MAX_TEST_LOG_LIMIT:]
    strategy = str(settings.get("strategy") or "first-ok")
    if strategy not in AGGREGATION_STRATEGIES:
        strategy = "first-ok"
    cursors = settings.get("cursors") if isinstance(settings.get("cursors"), dict) else {}
    cleaned_cursors: dict[str, int] = {}
    for key, value in cursors.items():
        try:
            cleaned_cursors[str(key)] = max(0, int(value))
        except (TypeError, ValueError):
            continue
    triggers = settings.get("triggers") if isinstance(settings.get("triggers"), list) else []
    cleaned_triggers: list[dict[str, Any]] = []
    seen_triggers: set[str] = set()
    for item in triggers:
        if not isinstance(item, dict):
            continue
        trigger_id = str(item.get("id") or "").strip() or new_id("trigger")
        if trigger_id in seen_triggers:
            continue
        seen_triggers.add(trigger_id)
        phrase = str(item.get("trigger") or "").strip()
        if not phrase:
            continue
        mode = str(item.get("match_mode") or "contains").strip()
        if mode not in TRIGGER_MATCH_MODES:
            mode = "contains"
        strategy = str(item.get("strategy") or "first-ok").strip()
        if strategy not in AGGREGATION_STRATEGIES:
            strategy = "first-ok"
        cleaned_triggers.append(
            {
                "id": trigger_id,
                "enabled": bool(item.get("enabled", True)),
                "trigger": phrase,
                "match_mode": mode,
                "group_id": str(item.get("group_id") or "all").strip() or "all",
                "strategy": strategy,
                "preview_api_id": str(item.get("preview_api_id") or "").strip(),
                "stop_event": bool(item.get("stop_event", True)),
                "response_type": str(item.get("response_type") or "summary")
                if str(item.get("response_type") or "summary") in RESPONSE_TYPES
                else "summary",
                "response_path": str(item.get("response_path") or "").strip(),
                "response_default": str(item.get("response_default") or ""),
                "response_transform": normalize_response_transform(item.get("response_transform")),
                "created_at": int(item.get("created_at") or now_ms()),
                "updated_at": int(item.get("updated_at") or now_ms()),
            }
        )
    return {
        "version": DATA_VERSION,
        "groups": cleaned_groups,
        "apis": cleaned_apis,
        "test_logs": cleaned_logs,
        "settings": {
            "strategy": strategy,
            "cursors": cleaned_cursors,
            "triggers": cleaned_triggers,
        },
    }


async def read_json_body() -> dict[str, Any]:
    data = await request.json(default={})
    return data if isinstance(data, dict) else {}


def ok(data: Any = None):
    return json_response(data if data is not None else {})


def fail(message: str, status: int = 400):
    return error_response(message, status_code=status)


def pick_string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        k = str(key).strip()
        if not k:
            continue
        result[k] = str(item)
    return result


def pick_template_vars(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def normalize_auth_type(value: Any) -> str:
    auth_type = str(value or "none").strip().lower()
    return auth_type if auth_type in AUTH_TYPES else "none"


def normalize_api_key_in(value: Any) -> str:
    location = str(value or "header").strip().lower()
    return location if location in API_KEY_IN_VALUES else "header"


def normalize_response_transform(value: Any) -> str:
    transform = str(value or "raw").strip().lower()
    return transform if transform in RESPONSE_TRANSFORMS else "raw"


def normalize_auth_config(value: Any) -> dict[str, str]:
    raw = value if isinstance(value, dict) else {}
    return {
        "token": str(raw.get("token") or ""),
        "username": str(raw.get("username") or ""),
        "password": str(raw.get("password") or ""),
        "key_name": str(raw.get("key_name") or ""),
        "key_value": str(raw.get("key_value") or ""),
        "api_key_in": normalize_api_key_in(raw.get("api_key_in")),
    }


def resolve_data_path(source: Any, path: str) -> Any:
    current = source
    for part in [item for item in path.replace("[", ".").replace("]", "").split(".") if item]:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def build_template_context(
    api: dict[str, Any],
    extra_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    context = {
        "api": {
            "id": str(api.get("id") or ""),
            "name": str(api.get("name") or ""),
            "group_id": str(api.get("group_id") or ""),
            "method": str(api.get("method") or ""),
            "url": str(api.get("url") or ""),
        },
        "time": {
            "unix": int(now.timestamp()),
            "unix_ms": int(now.timestamp() * 1000),
            "iso": now.isoformat().replace("+00:00", "Z"),
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
        },
        "random": {
            "int": random.randint(0, 999999),
            "hex": uuid.uuid4().hex[:12],
            "uuid": uuid.uuid4().hex,
        },
        "env": dict(os.environ),
        "vars": {},
        "message": {
            "text": "",
            "command": "",
            "args_text": "",
            "args": [],
        },
        "session": {},
        "trigger": {},
    }
    if isinstance(extra_context, dict):
        for key, value in extra_context.items():
            if isinstance(value, dict) and isinstance(context.get(key), dict):
                context[key] = {**context[key], **value}
            else:
                context[key] = value
    return context


def render_template_string(
    template: Any,
    context: dict[str, Any],
    *,
    allow_json_value: bool = False,
) -> str:
    text = str(template or "")
    exact_match = TEMPLATE_PATTERN.fullmatch(text)
    if exact_match:
        value = resolve_data_path(context, exact_match.group(1).strip())
        if value is None:
            raise ValueError(f"missing template variable: {exact_match.group(1).strip()}")
        if allow_json_value and isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    def replace(match: re.Match[str]) -> str:
        path = match.group(1).strip()
        value = resolve_data_path(context, path)
        if value is None:
            raise ValueError(f"missing template variable: {path}")
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    return TEMPLATE_PATTERN.sub(replace, text)


def render_string_map_templates(value: Any, context: dict[str, Any]) -> dict[str, str]:
    raw_map = pick_string_map(value)
    return {
        key: render_template_string(item, context)
        for key, item in raw_map.items()
    }


def render_auth_config(value: Any, context: dict[str, Any]) -> dict[str, str]:
    raw = normalize_auth_config(value)
    return {
        "token": render_template_string(raw.get("token"), context),
        "username": render_template_string(raw.get("username"), context),
        "password": render_template_string(raw.get("password"), context),
        "key_name": render_template_string(raw.get("key_name"), context),
        "key_value": render_template_string(raw.get("key_value"), context),
        "api_key_in": normalize_api_key_in(raw.get("api_key_in")),
    }


def _event_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return ""


def build_message_template_context(
    message: str,
    event: AstrMessageEvent | None = None,
    rule: dict[str, Any] | None = None,
    template_vars: dict[str, Any] | None = None,
    trigger_args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text = str(message or "")
    parts = text.split(maxsplit=1)
    command = parts[0] if parts else ""
    args_text = parts[1] if len(parts) > 1 else ""
    args = args_text.split() if args_text else []
    if isinstance(trigger_args, dict):
        args_text = str(trigger_args.get("args_text") or args_text)
        captured_args = trigger_args.get("args")
        args = list(captured_args) if isinstance(captured_args, list) else (args_text.split() if args_text else [])
    session = {}
    if event is not None:
        for source_name, target_name in {
            "session_id": "id",
            "conversation_id": "conversation_id",
            "user_id": "user_id",
            "sender_id": "sender_id",
            "room_id": "room_id",
            "message_id": "message_id",
            "platform": "platform",
            "self_id": "self_id",
        }.items():
            scalar = _event_scalar(getattr(event, source_name, None))
            if scalar:
                session[target_name] = scalar
    trigger = {}
    if isinstance(rule, dict):
        trigger = {
            "id": str(rule.get("id") or ""),
            "phrase": str(rule.get("trigger") or ""),
            "match_mode": str(rule.get("match_mode") or ""),
            "group_id": str(rule.get("group_id") or ""),
            "strategy": str(rule.get("strategy") or ""),
            "args_text": args_text,
            "args": args,
        }
    vars_payload = pick_template_vars(template_vars)
    if isinstance(trigger_args, dict):
        vars_payload.setdefault("trigger_args", {"args_text": args_text, "args": args})
    return {
        "message": {
            "text": text,
            "command": command,
            "args_text": args_text,
            "args": args,
        },
        "command": {
            "name": command,
            "args_text": args_text,
            "args": args,
        },
        "session": session,
        "trigger": trigger,
        "vars": vars_payload,
    }


def parse_api_payload(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    base = dict(existing or {})
    name = str(payload.get("name", base.get("name", "")) or "").strip()
    url = str(payload.get("url", base.get("url", "")) or "").strip()
    if not name:
        raise ValueError("API name is required")
    if not url:
        raise ValueError("API URL is required")
    method = str(payload.get("method", base.get("method", "GET")) or "GET").upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise ValueError("method must be GET, POST, PUT, PATCH, or DELETE")
    ts = now_ms()
    return {
        **base,
        "id": str(base.get("id") or payload.get("id") or new_id("api")),
        "name": name,
        "group_id": str(payload.get("group_id", base.get("group_id", "default")) or "default"),
        "url": url,
        "method": method,
        "query": pick_string_map(payload.get("query", base.get("query", {}))),
        "headers": pick_string_map(payload.get("headers", base.get("headers", {}))),
        "body": str(payload.get("body", base.get("body", "")) or ""),
        "auth_type": normalize_auth_type(payload.get("auth_type", base.get("auth_type", "none"))),
        "auth_config": normalize_auth_config(payload.get("auth_config", base.get("auth_config", {}))),
        "priority": clamp_int(payload.get("priority", base.get("priority", 100)), 100, 1, 1000),
        "circuit_failures": max(0, int(base.get("circuit_failures") or 0)),
        "circuit_open_until": max(0, int(base.get("circuit_open_until") or 0)),
        "enabled": bool(payload.get("enabled", base.get("enabled", True))),
        "description": str(payload.get("description", base.get("description", "")) or ""),
        "timeout_seconds": clamp_int(
            payload.get("timeout_seconds", base.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
            DEFAULT_TIMEOUT_SECONDS,
            1,
            MAX_TIMEOUT_SECONDS,
        ),
        "retry_count": clamp_int(
            payload.get("retry_count", base.get("retry_count", 0)),
            0,
            0,
            MAX_RETRY_COUNT,
        ),
        "cooldown_seconds": clamp_int(
            payload.get("cooldown_seconds", base.get("cooldown_seconds", 0)),
            0,
            0,
            MAX_COOLDOWN_SECONDS,
        ),
        "cooldown_until": max(0, int(base.get("cooldown_until") or 0)),
        "last_error": str(base.get("last_error") or ""),
        "created_at": int(base.get("created_at") or ts),
        "updated_at": ts,
    }


def build_request(api: dict[str, Any], template_context: dict[str, Any] | None = None) -> Request:
    context = build_template_context(api, template_context)
    url = render_template_string(api.get("url"), context).strip()
    query = render_string_map_templates(api.get("query"), context)
    headers = render_string_map_templates(api.get("headers"), context)
    auth_type = normalize_auth_type(api.get("auth_type"))
    auth_config = render_auth_config(api.get("auth_config"), context)
    if auth_type == "bearer" and auth_config.get("token"):
        headers.setdefault("Authorization", f"Bearer {auth_config['token']}")
    elif auth_type == "basic" and auth_config.get("username"):
        user_pass = f"{auth_config['username']}:{auth_config.get('password', '')}".encode("utf-8")
        headers.setdefault("Authorization", f"Basic {base64.b64encode(user_pass).decode('ascii')}")
    elif auth_type == "api-key" and auth_config.get("key_name"):
        if auth_config.get("api_key_in") == "query":
            query.setdefault(auth_config["key_name"], auth_config.get("key_value", ""))
        else:
            headers.setdefault(auth_config["key_name"], auth_config.get("key_value", ""))
    if query:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{urlencode(query)}"
    method = str(api.get("method") or "GET").upper()
    body_text = render_template_string(api.get("body"), context, allow_json_value=True)
    data = None
    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        data = body_text.encode("utf-8") if body_text else b""
        headers.setdefault("Content-Type", "application/json; charset=utf-8")
    return Request(url, data=data, headers=headers, method=method)


def normalize_proxy_url(value: Any) -> str:
    proxy_url = str(value or "").strip()
    if not proxy_url:
        return ""
    lowered = proxy_url.lower()
    if "://" not in lowered:
        proxy_url = f"http://{proxy_url}"
        lowered = proxy_url.lower()
    scheme = lowered.split("://", 1)[0]
    if scheme not in SUPPORTED_PROXY_SCHEMES:
        return ""
    return proxy_url


def resolve_proxy_config(mode_value: Any, url_value: Any) -> tuple[str, str, str, str]:
    proxy_mode = normalize_proxy_mode(mode_value, proxy_url_value=url_value)
    if proxy_mode == "direct":
        return proxy_mode, "", "direct", ""
    if proxy_mode == "environment":
        return proxy_mode, "", "environment", ""
    normalized_proxy_url = normalize_proxy_url(url_value)
    if not normalized_proxy_url:
        return proxy_mode, "", "invalid", "proxy_url must use http:// or https://"
    return proxy_mode, normalized_proxy_url, "custom", ""


def open_request(req: Request, *, timeout: int, proxy_mode: str = "direct", proxy_url: str = ""):
    proxy_mode = normalize_proxy_mode(proxy_mode, proxy_url_value=proxy_url)
    if proxy_mode == "environment":
        return build_opener(ProxyHandler()).open(req, timeout=timeout)
    if proxy_mode == "direct":
        return build_opener(ProxyHandler({})).open(req, timeout=timeout)
    proxy_url = normalize_proxy_url(proxy_url)
    if not proxy_url:
        return build_opener(ProxyHandler({})).open(req, timeout=timeout)
    proxies = {"http": proxy_url, "https": proxy_url}
    return build_opener(ProxyHandler(proxies)).open(req, timeout=timeout)


def _cooldown_remaining_ms(api: dict[str, Any]) -> int:
    cooldown_until = max(0, int(api.get("cooldown_until") or 0))
    return max(0, cooldown_until - now_ms())


def _circuit_remaining_ms(api: dict[str, Any]) -> int:
    circuit_open_until = max(0, int(api.get("circuit_open_until") or 0))
    return max(0, circuit_open_until - now_ms())


def _build_attempt_result(
    api: dict[str, Any],
    *,
    ok_flag: bool,
    elapsed_ms: int,
    status: int | None,
    content_type: str,
    body_kind: str,
    body_data: Any,
    preview: str,
    error: str,
    error_type: str,
    attempt: int,
) -> dict[str, Any]:
    return {
        "api_id": api["id"],
        "api_name": api["name"],
        "ok": ok_flag,
        "elapsed_ms": elapsed_ms,
        "status": status,
        "content_type": content_type,
        "body_kind": body_kind,
        "body_data": body_data,
        "preview": preview,
        "error": error,
        "error_type": error_type,
        "attempt": attempt,
        "tested_at": now_ms(),
    }


def classify_request_error(exc: Exception) -> str:
    if isinstance(exc, ValueError) and "template variable" in str(exc).lower():
        return "template_error"
    if isinstance(exc, HTTPError):
        return "http_error"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, URLError):
        reason = str(getattr(exc, "reason", "") or exc)
        lowered = reason.lower()
        if "timed out" in lowered or "timeout" in lowered:
            return "timeout"
        if "ssl" in lowered or "handshake" in lowered:
            return "ssl_error"
        if "name or service not known" in lowered or "could not resolve" in lowered:
            return "dns_error"
        return "connection_error"
    lowered = str(exc).lower()
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if "ssl" in lowered or "handshake" in lowered:
        return "ssl_error"
    if "could not resolve" in lowered or "remote name could not be resolved" in lowered:
        return "dns_error"
    return "request_error"


def normalize_content_type(value: Any) -> str:
    return str(value or "").split(";", 1)[0].strip().lower()


def classify_response_body(content_type: str) -> str:
    normalized = normalize_content_type(content_type)
    if normalized == "application/json" or normalized.endswith("+json"):
        return "json"
    if normalized.startswith("text/") or normalized in {
        "application/xml",
        "text/xml",
        "application/javascript",
        "application/x-www-form-urlencoded",
    }:
        return "text"
    return "binary"


def parse_json_like_text(text: str) -> Any:
    stripped = text.lstrip()
    if not stripped or stripped[0] not in "[{":
        raise json.JSONDecodeError("Text does not look like JSON", text, 0)
    return json.loads(text)


def build_response_payload(content_type: str, raw: bytes) -> dict[str, Any]:
    body_kind = classify_response_body(content_type)
    if body_kind == "json":
        text = raw.decode("utf-8", errors="replace")
        try:
            data = json.loads(text)
            preview = text[:1000]
        except json.JSONDecodeError:
            body_kind = "text"
            data = text
            preview = text[:1000]
        else:
            return {
                "body_kind": body_kind,
                "body_data": data,
                "preview": preview,
            }
    if body_kind == "text":
        text = raw.decode("utf-8", errors="replace")
        try:
            data = parse_json_like_text(text)
        except json.JSONDecodeError:
            pass
        else:
            return {
                "body_kind": "json",
                "body_data": data,
                "preview": text[:1000],
            }
        return {
            "body_kind": body_kind,
            "body_data": text,
            "preview": text[:1000],
        }
    return {
        "body_kind": "binary",
        "body_data": {
            "size": len(raw),
            "content_type": normalize_content_type(content_type) or "application/octet-stream",
        },
        "preview": f"<binary:{normalize_content_type(content_type) or 'application/octet-stream'} size={len(raw)}>",
    }


async def execute_api_request(
    api: dict[str, Any],
    *,
    ignore_cooldown: bool = False,
    response_read_limit_bytes: int = DEFAULT_RESPONSE_READ_LIMIT_BYTES,
    proxy_mode: str = "direct",
    proxy_url: str = "",
    template_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    circuit_remaining_ms = _circuit_remaining_ms(api)
    if circuit_remaining_ms > 0 and not ignore_cooldown:
        return {
            "api_id": api["id"],
            "api_name": api["name"],
            "ok": False,
            "elapsed_ms": 0,
            "status": None,
            "content_type": "",
            "body_kind": "",
            "body_data": None,
            "preview": "",
            "error": f"API circuit is open for {circuit_remaining_ms} ms",
            "error_type": "circuit_open",
            "tested_at": now_ms(),
            "attempts": [],
            "attempt_count": 0,
            "cooldown_skipped": True,
            "cooldown_remaining_ms": 0,
            "circuit_open": True,
            "circuit_remaining_ms": circuit_remaining_ms,
        }
    cooldown_remaining_ms = _cooldown_remaining_ms(api)
    if cooldown_remaining_ms > 0 and not ignore_cooldown:
        return {
            "api_id": api["id"],
            "api_name": api["name"],
            "ok": False,
            "elapsed_ms": 0,
            "status": None,
            "content_type": "",
            "body_kind": "",
            "body_data": None,
            "preview": "",
            "error": f"API is in cooldown for {cooldown_remaining_ms} ms",
            "error_type": "cooldown",
            "tested_at": now_ms(),
            "attempts": [],
            "attempt_count": 0,
            "cooldown_skipped": True,
            "cooldown_remaining_ms": cooldown_remaining_ms,
            "circuit_open": False,
            "circuit_remaining_ms": 0,
        }

    timeout_seconds = clamp_int(
        api.get("timeout_seconds"),
        DEFAULT_TIMEOUT_SECONDS,
        1,
        MAX_TIMEOUT_SECONDS,
    )
    retry_count = clamp_int(api.get("retry_count"), 0, 0, MAX_RETRY_COUNT)
    read_limit = clamp_int(
        response_read_limit_bytes,
        DEFAULT_RESPONSE_READ_LIMIT_BYTES,
        1,
        MAX_RESPONSE_READ_LIMIT_BYTES,
    )
    attempts: list[dict[str, Any]] = []

    def run_once() -> dict[str, Any]:
        req = build_request(api, template_context)
        with open_request(
            req,
            timeout=timeout_seconds,
            proxy_mode=proxy_mode,
            proxy_url=proxy_url,
        ) as response:
            raw = response.read(read_limit)
            content_type = str(response.headers.get("Content-Type", "") or "")
            payload = build_response_payload(content_type, raw)
            return {
                "status": getattr(response, "status", None),
                "content_type": content_type,
                "body_kind": payload["body_kind"],
                "body_data": payload["body_data"],
                "preview": payload["preview"],
            }

    for attempt in range(1, retry_count + 2):
        started = time.perf_counter()
        try:
            result = await asyncio.to_thread(run_once)
            elapsed = int((time.perf_counter() - started) * 1000)
            attempts.append(
                _build_attempt_result(
                    api,
                    ok_flag=True,
                    elapsed_ms=elapsed,
                    status=result.get("status"),
                    content_type=str(result.get("content_type") or ""),
                    body_kind=str(result.get("body_kind") or ""),
                    body_data=result.get("body_data"),
                    preview=str(result.get("preview") or ""),
                    error="",
                    error_type="",
                    attempt=attempt,
                )
            )
            break
        except Exception as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            attempts.append(
                _build_attempt_result(
                    api,
                    ok_flag=False,
                    elapsed_ms=elapsed,
                    status=None,
                    content_type="",
                    body_kind="",
                    body_data=None,
                    preview="",
                    error=str(exc),
                    error_type=classify_request_error(exc),
                    attempt=attempt,
                )
            )

    final_attempt = attempts[-1]
    return {
        **final_attempt,
        "attempts": attempts,
        "attempt_count": len(attempts),
        "cooldown_skipped": False,
        "cooldown_remaining_ms": 0,
        "circuit_open": False,
        "circuit_remaining_ms": 0,
    }


def enabled_apis_for_group(data: dict[str, Any], group_id: str) -> list[dict[str, Any]]:
    apis = [api for api in data["apis"] if api.get("enabled")]
    if group_id != "all":
        apis = [api for api in apis if api.get("group_id") == group_id]
    return apis


def resolve_group_name(data: dict[str, Any], group_name: str) -> tuple[str | None, str | None]:
    group_name = group_name.strip()
    if not group_name or group_name == "all":
        return "all", None
    matches = [
        group
        for group in data.get("groups", [])
        if str(group.get("name") or "").strip() == group_name
    ]
    if not matches:
        return None, f"group not found: {group_name}"
    if len(matches) > 1:
        return None, f"group name is duplicated: {group_name}"
    return str(matches[0].get("id") or ""), None


def group_name_exists(data: dict[str, Any], name: str, *, exclude_id: str = "") -> bool:
    normalized = name.strip()
    return any(
        str(group.get("id") or "") != exclude_id
        and str(group.get("name") or "").strip() == normalized
        for group in data.get("groups", [])
    )


def ordered_candidates(data: dict[str, Any], group_id: str, strategy: str) -> tuple[list[dict[str, Any]], int | None]:
    candidates = enabled_apis_for_group(data, group_id)
    if not candidates:
        return [], None
    if strategy == "random":
        shuffled = list(candidates)
        random.shuffle(shuffled)
        return shuffled, None
    if strategy == "priority":
        return sorted(
            candidates,
            key=lambda item: (
                clamp_int(item.get("priority"), 100, 1, 1000),
                int(item.get("created_at") or 0),
            ),
        ), None
    if strategy == "round-robin":
        cursors = data.get("settings", {}).get("cursors", {})
        cursor = int(cursors.get(group_id, 0)) if isinstance(cursors, dict) else 0
        start = cursor % len(candidates)
        return candidates[start:] + candidates[:start], start
    return candidates, None


def record_test_result(
    data: dict[str, Any],
    result: dict[str, Any],
    *,
    test_log_limit: int = DEFAULT_TEST_LOG_LIMIT,
) -> None:
    api_id = result.get("api_id")
    for item in data["apis"]:
        if item["id"] == api_id:
            item["last_status"] = result.get("status")
            item["last_ok"] = bool(result.get("ok"))
            item["last_tested_at"] = result["tested_at"]
            item["last_preview"] = result.get("preview") or result.get("error") or ""
            item["last_error"] = str(result.get("error") or "")
            if result.get("ok"):
                item["circuit_failures"] = 0
                item["circuit_open_until"] = 0
            elif not result.get("circuit_open") and not result.get("cooldown_skipped"):
                failures = max(0, int(item.get("circuit_failures") or 0)) + 1
                item["circuit_failures"] = failures
                if failures >= 3:
                    item["circuit_open_until"] = result["tested_at"] + 60 * 1000
            cooldown_seconds = clamp_int(
                item.get("cooldown_seconds"),
                0,
                0,
                MAX_COOLDOWN_SECONDS,
            )
            item["cooldown_until"] = (
                result["tested_at"] + cooldown_seconds * 1000
                if cooldown_seconds > 0 and not result.get("ok")
                else 0
            )
            item["updated_at"] = now_ms()
            break
    data["test_logs"].append(result)
    log_limit = clamp_int(test_log_limit, DEFAULT_TEST_LOG_LIMIT, 1, MAX_TEST_LOG_LIMIT)
    data["test_logs"] = data["test_logs"][-log_limit:]


def trigger_matches(rule: dict[str, Any], message: str) -> bool:
    trigger = str(rule.get("trigger") or "").strip()
    mode = str(rule.get("match_mode") or "contains")
    message = message.strip()
    if not trigger:
        return False
    if mode == "exact":
        return message == trigger
    if mode == "command":
        return message.split(maxsplit=1)[0] == trigger
    return trigger in message


def capture_trigger_arguments(rule: dict[str, Any], message: str) -> dict[str, Any]:
    trigger = str(rule.get("trigger") or "").strip()
    mode = str(rule.get("match_mode") or "contains")
    message = message.strip()
    args_text = ""
    if mode == "command":
        if message.startswith(trigger):
            args_text = message[len(trigger):].strip()
    elif mode == "exact":
        args_text = ""
    else:
        index = message.find(trigger)
        if index >= 0:
            args_text = message[index + len(trigger):].strip()
    return {
        "args_text": args_text,
        "args": args_text.split() if args_text else [],
    }


def format_aggregate_reply(
    result: dict[str, Any],
    *,
    preview_max_chars: int = DEFAULT_PREVIEW_MAX_CHARS,
    language_mode: str = DEFAULT_LANGUAGE_MODE,
) -> str:
    selected = result.get("selected") if isinstance(result.get("selected"), dict) else None
    attempts = result.get("attempts") if isinstance(result.get("attempts"), list) else []
    if not selected:
        errors = [
            f"{item.get('api_name')}: {item.get('error_type') or 'request_error'} ({item.get('error')})"
            for item in attempts[-3:]
            if isinstance(item, dict)
        ]
        detail = "\n".join(errors)
        return "\n".join(
            [
                text_for(
                    language_mode,
                    f"API Aggregator：全部 {len(attempts)} 次尝试失败。",
                    f"API Aggregator: all {len(attempts)} attempt(s) failed.",
                ),
                detail,
            ]
        ).strip()
    preview = str(selected.get("preview") or "").strip()
    preview_limit = clamp_int(preview_max_chars, DEFAULT_PREVIEW_MAX_CHARS, 1, MAX_PREVIEW_MAX_CHARS)
    if len(preview) > preview_limit:
        preview = preview[:preview_limit] + "..."
    return "\n".join(
        [
            text_for(
                language_mode,
                f"API Aggregator：{selected.get('api_name')} 调用成功",
                f"API Aggregator: {selected.get('api_name')} OK",
            ),
            text_for(language_mode, f"状态码：{selected.get('status')}", f"Status: {selected.get('status')}"),
            text_for(language_mode, f"耗时：{selected.get('elapsed_ms')} ms", f"Elapsed: {selected.get('elapsed_ms')} ms"),
            preview,
        ]
    ).strip()


def extract_path(source: Any, path: str) -> Any:
    return resolve_data_path(source, path)


def selected_body(result: dict[str, Any]) -> Any:
    selected = result.get("selected") if isinstance(result.get("selected"), dict) else {}
    body_kind = str(selected.get("body_kind") or "")
    body_data = selected.get("body_data")
    if body_kind == "json":
        return body_data
    if body_kind == "text":
        return str(body_data or "")
    return body_data


def response_value(result: dict[str, Any], path: str) -> Any:
    source = selected_body(result)
    if not path:
        return source
    selected = result.get("selected") if isinstance(result.get("selected"), dict) else {}
    if str(selected.get("body_kind") or "") != "json":
        return None
    return extract_path(source, path)


def transform_response_value(value: Any, transform: str) -> Any:
    transform = normalize_response_transform(transform)
    if transform == "raw":
        return value
    if transform == "string":
        return "" if value is None else str(value)
    if transform == "json":
        if isinstance(value, str):
            return json.loads(value)
        return value
    if transform == "join-comma":
        if isinstance(value, list):
            return ", ".join(str(item) for item in value)
        return "" if value is None else str(value)
    if transform == "join-lines":
        if isinstance(value, list):
            return "\n".join(str(item) for item in value)
        return "" if value is None else str(value)
    if transform == "int":
        return int(value)
    if transform == "float":
        return float(value)
    if transform == "bool":
        if isinstance(value, bool):
            return value
        lowered = str(value or "").strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off", ""}:
            return False
        raise ValueError(f"cannot convert to bool: {value}")
    return value


def resolve_trigger_response_value(rule: dict[str, Any], result: dict[str, Any]) -> Any:
    value = response_value(result, str(rule.get("response_path") or ""))
    if value is None and str(rule.get("response_default") or ""):
        value = str(rule.get("response_default") or "")
    return transform_response_value(value, str(rule.get("response_transform") or "raw"))


def resolve_trigger_media_url(rule: dict[str, Any], result: dict[str, Any]) -> str:
    value = resolve_trigger_response_value(rule, result)
    return "" if value is None else str(value).strip()


def preview_response_path(
    result: dict[str, Any],
    path: str,
    *,
    language_mode: str = DEFAULT_LANGUAGE_MODE,
) -> dict[str, Any]:
    value = response_value(result, path)
    matched = path == "" or value is not None
    selected = result.get("selected") if isinstance(result.get("selected"), dict) else {}
    source = selected_body(result)
    source_type = type(source).__name__
    body_kind = str(selected.get("body_kind") or "")
    if path and body_kind != "json":
        message = text_for(
            language_mode,
            f"路径提取只支持 JSON 响应。当前响应类型：{body_kind or 'unknown'}。",
            f"Path extraction only supports JSON responses. Current body kind: {body_kind or 'unknown'}.",
        )
    else:
        message = text_for(
            language_mode,
            "路径已匹配响应数据。" if matched else f"路径未匹配最新 {source_type} 响应。",
            "Path matched response data." if matched else f"Path did not match the latest {source_type} payload.",
        )
    return {
        "matched": matched,
        "path": path,
        "value": value,
        "value_type": type(value).__name__ if value is not None else "null",
        "source_type": source_type,
        "body_kind": body_kind,
        "content_type": selected.get("content_type") or "",
        "message": message,
    }


def format_missing_url_reply(
    media_type: str,
    path: str,
    result: dict[str, Any],
    *,
    language_mode: str = DEFAULT_LANGUAGE_MODE,
) -> str:
    selected = result.get("selected") if isinstance(result.get("selected"), dict) else {}
    value = response_value(result, path)
    value_type = type(value).__name__ if value is not None else "null"
    content_type = str(selected.get("content_type") or "")
    preview = str(selected.get("preview") or "").strip()
    if len(preview) > 300:
        preview = preview[:300] + "..."
    media_label = text_for(
        language_mode,
        {"image": "图片", "audio": "音频", "video": "视频"}.get(media_type, media_type),
        media_type,
    )
    return "\n".join(
        [
            text_for(
                language_mode,
                f"API Aggregator：未在 {path or '<root>'} 找到{media_label} URL。",
                f"API Aggregator: {media_label} URL not found at {path or '<root>'}.",
            ),
            text_for(language_mode, f"提取值类型：{value_type}", f"Extracted value type: {value_type}"),
            f"Content-Type: {content_type or '<empty>'}",
            text_for(language_mode, f"预览：{preview or '<empty>'}", f"Preview: {preview or '<empty>'}"),
        ]
    )


def validate_import_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("import data must be a JSON object")
    if "groups" in raw and not isinstance(raw.get("groups"), list):
        raise ValueError("groups must be a list")
    if "apis" in raw and not isinstance(raw.get("apis"), list):
        raise ValueError("apis must be a list")
    settings = raw.get("settings")
    if settings is not None and not isinstance(settings, dict):
        raise ValueError("settings must be an object")
    if isinstance(settings, dict) and "triggers" in settings and not isinstance(settings.get("triggers"), list):
        raise ValueError("settings.triggers must be a list")
    return normalize_data(raw)


def merge_import_data(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    current_groups = {
        str(item.get("id") or "").strip(): dict(item)
        for item in current.get("groups", [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    incoming_groups = [
        dict(item)
        for item in incoming.get("groups", [])
        if isinstance(item, dict)
    ]
    for group in incoming_groups:
        group_id = str(group.get("id") or "").strip()
        if group_id:
            current_groups[group_id] = group

    current_apis = {
        str(item.get("id") or "").strip(): dict(item)
        for item in current.get("apis", [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    incoming_apis = [
        dict(item)
        for item in incoming.get("apis", [])
        if isinstance(item, dict)
    ]
    for api in incoming_apis:
        api_id = str(api.get("id") or "").strip()
        if api_id:
            current_apis[api_id] = api

    merged_settings = dict(current.get("settings", {}))
    incoming_settings = dict(incoming.get("settings", {}))
    if "strategy" in incoming_settings:
        merged_settings["strategy"] = incoming_settings["strategy"]
    merged_cursors = dict(current.get("settings", {}).get("cursors", {}))
    merged_cursors.update(dict(incoming_settings.get("cursors", {})))
    merged_settings["cursors"] = merged_cursors

    current_triggers = {
        str(item.get("id") or "").strip(): dict(item)
        for item in current.get("settings", {}).get("triggers", [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    incoming_triggers = [
        dict(item)
        for item in incoming_settings.get("triggers", [])
        if isinstance(item, dict)
    ]
    for trigger in incoming_triggers:
        trigger_id = str(trigger.get("id") or "").strip()
        if trigger_id:
            current_triggers[trigger_id] = trigger
    merged_settings["triggers"] = list(current_triggers.values())

    return normalize_data(
        {
            "version": DATA_VERSION,
            "groups": list(current_groups.values()),
            "apis": list(current_apis.values()),
            "test_logs": list(current.get("test_logs", [])),
            "settings": merged_settings,
        }
    )


@pydantic_dataclass
class ApiAggregatorCallTool(FunctionTool[AstrAgentContext]):
    plugin: Any = PydanticField(default=None, repr=False)
    name: str = "api_aggregator_call"
    description: str = "Call an enabled API group managed by API Aggregator and return the selected API result."
    parameters: dict[str, Any] = PydanticField(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "group": {
                    "type": "string",
                    "description": "Visible API group name to call. Use all to call all enabled APIs.",
                    "default": "all",
                },
                "strategy": {
                    "type": "string",
                    "description": "Aggregation strategy.",
                    "enum": ["first-ok", "round-robin", "random"],
                    "default": "first-ok",
                },
                "response_path": {
                    "type": "string",
                    "description": "Optional dot path to extract from the selected JSON response, for example data.link.",
                    "default": "",
                },
                "response_default": {
                    "type": "string",
                    "description": "Optional fallback value used when response_path is empty or does not match.",
                    "default": "",
                },
                "response_transform": {
                    "type": "string",
                    "description": "Optional response transform.",
                    "enum": ["raw", "string", "json", "join-comma", "join-lines", "int", "float", "bool"],
                    "default": "raw",
                },
                "template_vars": {
                    "type": "object",
                    "description": "Optional template variables exposed as vars.* for URL, query, headers, and body rendering.",
                    "default": {},
                },
            },
            "required": [],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs: Any,
    ) -> ToolExecResult:
        _ = context
        if self.plugin is None:
            return json.dumps(
                {"ok": False, "error": "API Aggregator plugin is unavailable"},
                ensure_ascii=False,
            )

        group_name = str(kwargs.get("group") or "all").strip() or "all"
        strategy = str(kwargs.get("strategy") or "first-ok").strip()
        response_path = str(kwargs.get("response_path") or "").strip()
        response_default = str(kwargs.get("response_default") or "")
        response_transform = normalize_response_transform(kwargs.get("response_transform"))
        template_context = {
            "vars": pick_template_vars(kwargs.get("template_vars")),
        }
        result, state_or_error, status = await self.plugin.run_aggregate_by_group_name(
            group_name,
            strategy,
            template_context=template_context,
        )
        if status != 200 or not isinstance(result, dict):
            return json.dumps(
                {
                    "ok": False,
                    "group": group_name,
                    "strategy": strategy,
                    "error": str(state_or_error),
                },
                ensure_ascii=False,
            )

        value = None
        if result.get("ok"):
            value = response_value(result, response_path)
            if value is None and response_default:
                value = response_default
            value = transform_response_value(value, response_transform)
        return json.dumps(
            {
                "ok": bool(result.get("ok")),
                "group": group_name,
                "strategy": strategy,
                "response_path": response_path,
                "response_default": response_default,
                "response_transform": response_transform,
                "value": value,
                "selected": result.get("selected"),
                "failure_reason": result.get("failure_reason") or "",
                "attempts": result.get("attempts") or [],
            },
            ensure_ascii=False,
        )


@register(
    PLUGIN_NAME,
    "OpenAI",
    "Clean AstrBot WebUI plugin for managing and testing aggregated APIs.",
    VERSION,
    "",
)
class ApiAggregatorPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.language_mode = normalize_language_mode(self.config.get("language_mode"))
        self.response_read_limit_bytes = clamp_int(
            self.config.get("response_read_limit_bytes"),
            DEFAULT_RESPONSE_READ_LIMIT_BYTES,
            1,
            MAX_RESPONSE_READ_LIMIT_BYTES,
        )
        self.default_timeout_seconds = clamp_int(
            self.config.get("default_timeout_seconds"),
            DEFAULT_TIMEOUT_SECONDS,
            1,
            MAX_TIMEOUT_SECONDS,
        )
        self.test_log_limit = clamp_int(
            self.config.get("test_log_limit"),
            DEFAULT_TEST_LOG_LIMIT,
            1,
            MAX_TEST_LOG_LIMIT,
        )
        self.preview_max_chars = clamp_int(
            self.config.get("preview_max_chars"),
            DEFAULT_PREVIEW_MAX_CHARS,
            1,
            MAX_PREVIEW_MAX_CHARS,
        )
        self.proxy_mode, self.proxy_url, self.proxy_config_status, self.proxy_error = resolve_proxy_config(
            self.config.get("proxy_mode"),
            self.config.get("proxy_url"),
        )
        if self.proxy_config_status == "invalid":
            logger.warning(
                "[api_aggregator] invalid proxy_url configuration ignored in custom mode; expected http:// or https://"
            )
        self.root = Path(__file__).resolve().parent
        self.store = ApiAggregatorStore(Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME)
        self._registered = False
        self._register_web_apis()
        self.context.add_llm_tools(ApiAggregatorCallTool(plugin=self))

    def _register_web_apis(self) -> None:
        if self._registered:
            return
        for route, handler, methods, desc in self._web_api_routes():
            self.context.register_web_api(route, handler, methods, desc)
        self._registered = True

    def _web_api_routes(self):
        return [
            (f"/{PLUGIN_NAME}/state", self.state, ["GET"], "Get API Aggregator state"),
            (f"/{PLUGIN_NAME}/groups/create", self.group_create, ["POST"], "Create group"),
            (f"/{PLUGIN_NAME}/groups/update", self.group_update, ["POST"], "Update group"),
            (f"/{PLUGIN_NAME}/groups/delete", self.group_delete, ["POST"], "Delete group"),
            (f"/{PLUGIN_NAME}/apis/create", self.api_create, ["POST"], "Create API"),
            (f"/{PLUGIN_NAME}/apis/update", self.api_update, ["POST"], "Update API"),
            (f"/{PLUGIN_NAME}/apis/delete", self.api_delete, ["POST"], "Delete API"),
            (f"/{PLUGIN_NAME}/apis/toggle", self.api_toggle, ["POST"], "Toggle API"),
            (f"/{PLUGIN_NAME}/apis/test", self.api_test, ["POST"], "Test API"),
            (f"/{PLUGIN_NAME}/apis/test-all", self.api_test_all, ["POST"], "Test enabled APIs"),
            (f"/{PLUGIN_NAME}/aggregate/call", self.aggregate_call, ["POST"], "Call aggregated APIs"),
            (f"/{PLUGIN_NAME}/settings/save", self.save_settings, ["POST"], "Save API Aggregator settings"),
            (f"/{PLUGIN_NAME}/triggers/create", self.trigger_create, ["POST"], "Create trigger"),
            (f"/{PLUGIN_NAME}/triggers/update", self.trigger_update, ["POST"], "Update trigger"),
            (f"/{PLUGIN_NAME}/triggers/delete", self.trigger_delete, ["POST"], "Delete trigger"),
            (f"/{PLUGIN_NAME}/triggers/toggle", self.trigger_toggle, ["POST"], "Toggle trigger"),
            (f"/{PLUGIN_NAME}/triggers/preview-response-path", self.trigger_preview_response_path, ["POST"], "Preview trigger response path"),
            (f"/{PLUGIN_NAME}/import", self.import_data, ["POST"], "Import JSON data"),
            (f"/{PLUGIN_NAME}/export", self.export_data, ["GET"], "Export JSON data"),
        ]

    async def state(self):
        data = await self.store.read()
        return ok(
            {
                "plugin": PLUGIN_NAME,
                "version": VERSION,
                "runtime_config": {
                    "default_timeout_seconds": self.default_timeout_seconds,
                    "language_mode": self.language_mode,
                    "response_read_limit_bytes": self.response_read_limit_bytes,
                    "test_log_limit": self.test_log_limit,
                    "preview_max_chars": self.preview_max_chars,
                    "proxy_mode": self.proxy_mode,
                    "proxy_enabled": self.proxy_mode != "direct",
                    "proxy_config_status": self.proxy_config_status,
                    "proxy_error": self.proxy_error,
                },
                **data,
            }
        )

    async def group_create(self):
        body = await read_json_body()
        name = str(body.get("name") or "").strip()
        if not name:
            return fail("group name is required")
        group = {
            "id": new_id("group"),
            "name": name,
            "description": str(body.get("description") or ""),
            "created_at": now_ms(),
            "updated_at": now_ms(),
        }

        def mutate(data):
            if group_name_exists(data, name):
                raise ValueError("group name already exists")
            data["groups"].append(group)
            return group

        try:
            result, data = await self.store.mutate(mutate)
            return ok({"group": result, "state": data})
        except ValueError as exc:
            return fail(str(exc), 400)

    async def group_update(self):
        body = await read_json_body()
        group_id = str(body.get("id") or "").strip()
        if not group_id:
            return fail("group id is required")

        def mutate(data):
            for group in data["groups"]:
                if group["id"] == group_id:
                    next_name = str(body.get("name", group["name"]) or group["name"]).strip()
                    if not next_name:
                        raise ValueError("group name is required")
                    if group_name_exists(data, next_name, exclude_id=group_id):
                        raise ValueError("group name already exists")
                    group["name"] = next_name
                    group["description"] = str(body.get("description", group.get("description", "")) or "")
                    group["updated_at"] = now_ms()
                    return group
            raise LookupError("group not found")

        try:
            result, data = await self.store.mutate(mutate)
            return ok({"group": result, "state": data})
        except ValueError as exc:
            return fail(str(exc), 400)
        except LookupError as exc:
            return fail(str(exc), 404)

    async def group_delete(self):
        body = await read_json_body()
        group_id = str(body.get("id") or "").strip()
        if group_id == "default":
            return fail("default group cannot be deleted")

        def mutate(data):
            groups = data["groups"]
            if not any(group["id"] == group_id for group in groups):
                raise LookupError("group not found")
            fallback = groups[0]["id"] if groups[0]["id"] != group_id else "default"
            data["groups"] = [group for group in groups if group["id"] != group_id]
            if not data["groups"]:
                data["groups"].append(ApiAggregatorStore(self.root).default_data()["groups"][0])
                fallback = "default"
            for api in data["apis"]:
                if api.get("group_id") == group_id:
                    api["group_id"] = fallback
                    api["updated_at"] = now_ms()
            for rule in data.get("settings", {}).get("triggers", []):
                if rule.get("group_id") == group_id:
                    rule["group_id"] = fallback
                    rule["updated_at"] = now_ms()
            return {"deleted": group_id}

        try:
            result, data = await self.store.mutate(mutate)
            return ok({"result": result, "state": data})
        except LookupError as exc:
            return fail(str(exc), 404)

    async def api_create(self):
        body = await read_json_body()

        def mutate(data):
            payload = {
                "timeout_seconds": self.default_timeout_seconds,
                **body,
            }
            api = parse_api_payload(payload)
            group_ids = {group["id"] for group in data["groups"]}
            if api["group_id"] not in group_ids:
                api["group_id"] = data["groups"][0]["id"]
            data["apis"].append(api)
            return api

        try:
            result, data = await self.store.mutate(mutate)
            return ok({"api": result, "state": data})
        except ValueError as exc:
            return fail(str(exc))

    async def api_update(self):
        body = await read_json_body()
        api_id = str(body.get("id") or "").strip()
        if not api_id:
            return fail("api id is required")

        def mutate(data):
            for index, api in enumerate(data["apis"]):
                if api["id"] == api_id:
                    updated = parse_api_payload(body, api)
                    group_ids = {group["id"] for group in data["groups"]}
                    if updated["group_id"] not in group_ids:
                        updated["group_id"] = data["groups"][0]["id"]
                    data["apis"][index] = updated
                    return updated
            raise LookupError("api not found")

        try:
            result, data = await self.store.mutate(mutate)
            return ok({"api": result, "state": data})
        except LookupError as exc:
            return fail(str(exc), 404)
        except ValueError as exc:
            return fail(str(exc))

    async def api_delete(self):
        body = await read_json_body()
        api_id = str(body.get("id") or "").strip()

        def mutate(data):
            before = len(data["apis"])
            data["apis"] = [api for api in data["apis"] if api["id"] != api_id]
            if len(data["apis"]) == before:
                raise LookupError("api not found")
            return {"deleted": api_id}

        try:
            result, data = await self.store.mutate(mutate)
            return ok({"result": result, "state": data})
        except LookupError as exc:
            return fail(str(exc), 404)

    async def api_toggle(self):
        body = await read_json_body()
        api_id = str(body.get("id") or "").strip()
        enabled = bool(body.get("enabled"))

        def mutate(data):
            for api in data["apis"]:
                if api["id"] == api_id:
                    api["enabled"] = enabled
                    api["updated_at"] = now_ms()
                    return api
            raise LookupError("api not found")

        try:
            result, data = await self.store.mutate(mutate)
            return ok({"api": result, "state": data})
        except LookupError as exc:
            return fail(str(exc), 404)

    async def api_test(self):
        body = await read_json_body()
        api_id = str(body.get("id") or "").strip()
        template_context = {
            "vars": pick_template_vars(body.get("template_vars")),
        }
        data = await self.store.read()
        api = next((item for item in data["apis"] if item["id"] == api_id), None)
        if not api:
            return fail("api not found", 404)
        result = await execute_api_request(
            api,
            ignore_cooldown=True,
            response_read_limit_bytes=self.response_read_limit_bytes,
            proxy_mode=self.proxy_mode,
            proxy_url=self.proxy_url,
            template_context=template_context,
        )

        def mutate(next_data):
            record_test_result(next_data, result, test_log_limit=self.test_log_limit)
            return result

        saved_result, state = await self.store.mutate(mutate)
        return ok({"result": saved_result, "state": state})

    async def api_test_all(self):
        body = await read_json_body()
        template_context = {
            "vars": pick_template_vars(body.get("template_vars")),
        }
        data = await self.store.read()
        apis = [api for api in data["apis"] if api.get("enabled")]
        results = [
            await execute_api_request(
                api,
                ignore_cooldown=True,
                response_read_limit_bytes=self.response_read_limit_bytes,
                proxy_mode=self.proxy_mode,
                proxy_url=self.proxy_url,
                template_context=template_context,
            )
            for api in apis
        ]

        def mutate(next_data):
            result_by_id = {item["api_id"]: item for item in results}
            for result in result_by_id.values():
                record_test_result(next_data, result, test_log_limit=self.test_log_limit)
            return results

        saved_results, state = await self.store.mutate(mutate)
        return ok({"results": saved_results, "state": state})

    async def aggregate_call(self):
        body = await read_json_body()
        template_context = {
            "vars": pick_template_vars(body.get("template_vars")),
        }
        aggregate_result, state_or_error, status = await self.run_aggregate(
            str(body.get("group_id") or "all").strip() or "all",
            str(body.get("strategy") or "").strip() or None,
            template_context=template_context,
        )
        if status != 200:
            return fail(str(state_or_error), status)
        return ok({"result": aggregate_result, "state": state_or_error})

    async def run_aggregate_by_group_name(
        self,
        group_name: str,
        strategy: str | None = None,
        *,
        template_context: dict[str, Any] | None = None,
    ):
        data = await self.store.read()
        group_id, error = resolve_group_name(data, group_name)
        if error:
            return None, error, 404
        return await self.run_aggregate(str(group_id or "all"), strategy, template_context=template_context)

    async def run_aggregate(
        self,
        group_id: str,
        strategy: str | None = None,
        *,
        template_context: dict[str, Any] | None = None,
    ):
        data = await self.store.read()
        strategy = strategy or str(data.get("settings", {}).get("strategy") or "first-ok")
        if strategy not in AGGREGATION_STRATEGIES:
            return None, "unknown aggregation strategy", 400
        if group_id != "all" and not any(group["id"] == group_id for group in data["groups"]):
            return None, "group not found", 404

        candidates, start_index = ordered_candidates(data, group_id, strategy)
        if not candidates:
            return None, "no enabled API candidates", 404

        results: list[dict[str, Any]] = []
        selected: dict[str, Any] | None = None
        failure_reason = ""
        for api in candidates:
            result = await execute_api_request(
                api,
                response_read_limit_bytes=self.response_read_limit_bytes,
                proxy_mode=self.proxy_mode,
                proxy_url=self.proxy_url,
                template_context=template_context,
            )
            results.append(result)
            if result.get("ok"):
                selected = result
                break
            failure_reason = str(result.get("error") or "request failed")

        def mutate(next_data):
            for result in results:
                record_test_result(next_data, result, test_log_limit=self.test_log_limit)
            if strategy == "round-robin" and start_index is not None:
                cursors = next_data.setdefault("settings", {}).setdefault("cursors", {})
                cursors[group_id] = (start_index + 1) % len(candidates)
            return {
                "group_id": group_id,
                "strategy": strategy,
                "ok": bool(selected),
                "selected": selected,
                "attempts": results,
                "failure_reason": failure_reason if not selected else "",
            }

        aggregate_result, state = await self.store.mutate(mutate)
        return aggregate_result, state, 200

    async def save_settings(self):
        body = await read_json_body()
        strategy = str(body.get("strategy") or "first-ok")
        if strategy not in AGGREGATION_STRATEGIES:
            return fail("unknown aggregation strategy")

        def mutate(data):
            data.setdefault("settings", {})["strategy"] = strategy
            return data["settings"]

        settings, state = await self.store.mutate(mutate)
        return ok({"settings": settings, "state": state})

    def parse_trigger_payload(
        self,
        body: dict[str, Any],
        existing: dict[str, Any] | None = None,
        group_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        base = dict(existing or {})
        phrase = str(body.get("trigger", base.get("trigger", "")) or "").strip()
        if not phrase:
            raise ValueError("trigger is required")
        mode = str(body.get("match_mode", base.get("match_mode", "contains")) or "contains").strip()
        if mode not in TRIGGER_MATCH_MODES:
            raise ValueError("unknown match mode")
        strategy = str(body.get("strategy", base.get("strategy", "first-ok")) or "first-ok").strip()
        if strategy not in AGGREGATION_STRATEGIES:
            raise ValueError("unknown aggregation strategy")
        response_type = str(body.get("response_type", base.get("response_type", "summary")) or "summary").strip()
        if response_type not in RESPONSE_TYPES:
            raise ValueError("unknown response type")
        group_id = str(body.get("group_id", base.get("group_id", "all")) or "all").strip() or "all"
        if group_id != "all" and group_ids is not None and group_id not in group_ids:
            raise ValueError("group not found")
        ts = now_ms()
        return {
            **base,
            "id": str(base.get("id") or body.get("id") or new_id("trigger")),
            "enabled": bool(body.get("enabled", base.get("enabled", True))),
            "trigger": phrase,
            "match_mode": mode,
            "group_id": group_id,
            "strategy": strategy,
            "preview_api_id": str(
                body.get("preview_api_id", base.get("preview_api_id", "")) or ""
            ).strip(),
            "stop_event": bool(body.get("stop_event", base.get("stop_event", True))),
            "response_type": response_type,
            "response_path": str(body.get("response_path", base.get("response_path", "")) or "").strip(),
            "response_default": str(body.get("response_default", base.get("response_default", "")) or ""),
            "response_transform": normalize_response_transform(
                body.get("response_transform", base.get("response_transform", "raw"))
            ),
            "created_at": int(base.get("created_at") or ts),
            "updated_at": ts,
        }

    async def trigger_create(self):
        body = await read_json_body()

        def mutate(data):
            group_ids = {group["id"] for group in data["groups"]}
            rule = self.parse_trigger_payload(body, group_ids=group_ids)
            data.setdefault("settings", {}).setdefault("triggers", []).append(rule)
            return rule

        try:
            rule, state = await self.store.mutate(mutate)
            return ok({"trigger": rule, "state": state})
        except ValueError as exc:
            return fail(str(exc))

    async def trigger_update(self):
        body = await read_json_body()
        trigger_id = str(body.get("id") or "").strip()
        if not trigger_id:
            return fail("trigger id is required")

        def mutate(data):
            triggers = data.setdefault("settings", {}).setdefault("triggers", [])
            group_ids = {group["id"] for group in data["groups"]}
            for index, rule in enumerate(triggers):
                if rule["id"] == trigger_id:
                    updated = self.parse_trigger_payload(body, rule, group_ids)
                    triggers[index] = updated
                    return updated
            raise LookupError("trigger not found")

        try:
            rule, state = await self.store.mutate(mutate)
            return ok({"trigger": rule, "state": state})
        except LookupError as exc:
            return fail(str(exc), 404)
        except ValueError as exc:
            return fail(str(exc))

    async def trigger_delete(self):
        body = await read_json_body()
        trigger_id = str(body.get("id") or "").strip()

        def mutate(data):
            triggers = data.setdefault("settings", {}).setdefault("triggers", [])
            before = len(triggers)
            data["settings"]["triggers"] = [rule for rule in triggers if rule["id"] != trigger_id]
            if len(data["settings"]["triggers"]) == before:
                raise LookupError("trigger not found")
            return {"deleted": trigger_id}

        try:
            result, state = await self.store.mutate(mutate)
            return ok({"result": result, "state": state})
        except LookupError as exc:
            return fail(str(exc), 404)

    async def trigger_toggle(self):
        body = await read_json_body()
        trigger_id = str(body.get("id") or "").strip()
        enabled = bool(body.get("enabled"))

        def mutate(data):
            triggers = data.setdefault("settings", {}).setdefault("triggers", [])
            for rule in triggers:
                if rule["id"] == trigger_id:
                    rule["enabled"] = enabled
                    rule["updated_at"] = now_ms()
                    return rule
            raise LookupError("trigger not found")

        try:
            rule, state = await self.store.mutate(mutate)
            return ok({"trigger": rule, "state": state})
        except LookupError as exc:
            return fail(str(exc), 404)

    async def trigger_preview_response_path(self):
        body = await read_json_body()
        api_id = str(body.get("api_id") or "").strip()
        path = str(body.get("response_path") or "").strip()
        sample_message = str(body.get("sample_message") or "")
        data = await self.store.read()
        api = next((item for item in data["apis"] if item["id"] == api_id), None)
        if not api:
            return fail("api not found", 404)
        template_context = build_message_template_context(
            sample_message,
            None,
            None,
            pick_template_vars(body.get("template_vars")),
        )
        result = await execute_api_request(
            api,
            ignore_cooldown=True,
            response_read_limit_bytes=self.response_read_limit_bytes,
            proxy_mode=self.proxy_mode,
            proxy_url=self.proxy_url,
            template_context=template_context,
        )

        def mutate(next_data):
            record_test_result(next_data, result, test_log_limit=self.test_log_limit)
            return preview_response_path(
                {"selected": result},
                path,
                language_mode=self.language_mode,
            )

        preview, state = await self.store.mutate(mutate)
        return ok({"preview": preview, "result": result, "state": state})

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        message = str(getattr(event, "message_str", "") or "")
        if not message:
            return
        data = await self.store.read()
        all_triggers = data.get("settings", {}).get("triggers", [])
        triggers = [rule for rule in all_triggers if rule.get("enabled")]
        logger.info(
            f"[api_aggregator] message received: text={message!r}, enabled_triggers={len(triggers)}, total_triggers={len(all_triggers)}"
        )
        for rule in triggers:
            if not trigger_matches(rule, message):
                continue
            logger.info(
                f"[api_aggregator] trigger matched: id={rule.get('id')}, trigger={rule.get('trigger')!r}, group={rule.get('group_id')}, strategy={rule.get('strategy')}"
            )
            captured = capture_trigger_arguments(rule, message)
            template_context = build_message_template_context(
                message,
                event,
                rule,
                trigger_args=captured,
            )
            result, state_or_error, status = await self.run_aggregate(
                str(rule.get("group_id") or "all"),
                str(rule.get("strategy") or data.get("settings", {}).get("strategy") or "first-ok"),
                template_context=template_context,
            )
            if status == 200 and isinstance(result, dict):
                logger.info(
                    f"[api_aggregator] aggregate success: ok={result.get('ok')}, attempts={len(result.get('attempts') or [])}"
                )
                if not result.get("ok"):
                    logger.info(f"[api_aggregator] aggregate attempts failed: {result.get('attempts')}")
                    yield event.plain_result(
                        format_aggregate_reply(
                            result,
                            preview_max_chars=self.preview_max_chars,
                            language_mode=self.language_mode,
                        )
                    )
                    if rule.get("stop_event", True):
                        event.stop_event()
                    return
                response_type = str(rule.get("response_type") or "summary")
                if response_type == "image":
                    response_path = str(rule.get("response_path") or "")
                    image_url = resolve_trigger_media_url(rule, result)
                    if image_url.startswith(("http://", "https://")):
                        yield event.image_result(image_url)
                    else:
                        logger.info(
                            f"[api_aggregator] image url extraction failed: path={response_path!r}, value={image_url!r}, selected={result.get('selected')}"
                        )
                        yield event.plain_result(
                            format_missing_url_reply(
                                "image",
                                response_path,
                                result,
                                language_mode=self.language_mode,
                            )
                        )
                elif response_type == "audio":
                    response_path = str(rule.get("response_path") or "")
                    audio_url = resolve_trigger_media_url(rule, result)
                    if audio_url.startswith(("http://", "https://")):
                        yield event.chain_result([Comp.Record(file=audio_url, url=audio_url)])
                    else:
                        logger.info(
                            f"[api_aggregator] audio url extraction failed: path={response_path!r}, value={audio_url!r}, selected={result.get('selected')}"
                        )
                        yield event.plain_result(
                            format_missing_url_reply(
                                "audio",
                                response_path,
                                result,
                                language_mode=self.language_mode,
                            )
                        )
                elif response_type == "video":
                    response_path = str(rule.get("response_path") or "")
                    video_url = resolve_trigger_media_url(rule, result)
                    if video_url.startswith(("http://", "https://")):
                        yield event.chain_result([Comp.Video.fromURL(url=video_url)])
                    else:
                        logger.info(
                            f"[api_aggregator] video url extraction failed: path={response_path!r}, value={video_url!r}, selected={result.get('selected')}"
                        )
                        yield event.plain_result(
                            format_missing_url_reply(
                                "video",
                                response_path,
                                result,
                                language_mode=self.language_mode,
                            )
                        )
                elif response_type == "text":
                    try:
                        value = resolve_trigger_response_value(rule, result)
                    except Exception as exc:
                        value = text_for(
                            self.language_mode,
                            f"API Aggregator：响应转换失败：{exc}",
                            f"API Aggregator: response transform failed: {exc}",
                        )
                    if isinstance(value, (dict, list)):
                        yield event.plain_result(json.dumps(value, ensure_ascii=False))
                    else:
                        yield event.plain_result(str(value))
                else:
                    yield event.plain_result(
                        format_aggregate_reply(
                            result,
                            preview_max_chars=self.preview_max_chars,
                            language_mode=self.language_mode,
                        )
                    )
            else:
                logger.info(f"[api_aggregator] aggregate failed: {state_or_error}")
                yield event.plain_result(
                    text_for(
                        self.language_mode,
                        f"API Aggregator：{state_or_error}",
                        f"API Aggregator: {state_or_error}",
                    )
                )
            if rule.get("stop_event", True):
                event.stop_event()
            return
        logger.info("[api_aggregator] no trigger matched")

    async def import_data(self):
        body = await read_json_body()
        raw_data = body.get("data") if "data" in body else body
        strategy = str(body.get("strategy") or "replace").strip().lower()
        try:
            imported = validate_import_payload(raw_data)
        except ValueError as exc:
            return fail(str(exc), 400)

        if strategy == "validate":
            return ok(
                {
                    "valid": True,
                    "strategy": strategy,
                    "summary": {
                        "groups": len(imported.get("groups", [])),
                        "apis": len(imported.get("apis", [])),
                        "triggers": len(imported.get("settings", {}).get("triggers", [])),
                    },
                }
            )
        if strategy == "replace":
            saved = await self.store.write(imported)
            return ok({"state": saved, "strategy": strategy})
        if strategy == "merge":
            current = await self.store.read()
            saved = await self.store.write(merge_import_data(current, imported))
            return ok({"state": saved, "strategy": strategy})
        return fail("unknown import strategy", 400)

    async def export_data(self):
        data = await self.store.read()
        return ok(data)
