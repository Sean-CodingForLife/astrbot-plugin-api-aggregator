from __future__ import annotations

import json
import os
import random
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from astrbot.api.event import AstrMessageEvent

from constants import TEMPLATE_PATTERN


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
    from store import pick_string_map

    raw_map = pick_string_map(value)
    return {
        key: render_template_string(item, context)
        for key, item in raw_map.items()
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
    from store import pick_template_vars

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
