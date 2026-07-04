from __future__ import annotations

import json
import random
from typing import Any

from .constants import (
    AGGREGATION_STRATEGIES,
    DATA_VERSION,
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_PREVIEW_MAX_CHARS,
    DEFAULT_TEST_LOG_LIMIT,
    MAX_COOLDOWN_SECONDS,
    MAX_PREVIEW_MAX_CHARS,
    MAX_TEST_LOG_LIMIT,
)
from .request_utils import execute_api_request
from .store import normalize_response_transform
from .template_utils import resolve_data_path
from .utils import clamp_int, now_ms, text_for


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
                    f"API Aggregator: all {len(attempts)} attempt(s) failed.",
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
                f"API Aggregator: {selected.get('api_name')} OK",
                f"API Aggregator: {selected.get('api_name')} OK",
            ),
            text_for(language_mode, f"Status: {selected.get('status')}", f"Status: {selected.get('status')}"),
            text_for(language_mode, f"Elapsed: {selected.get('elapsed_ms')} ms", f"Elapsed: {selected.get('elapsed_ms')} ms"),
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
            f"Path extraction only supports JSON responses. Current body kind: {body_kind or 'unknown'}.",
            f"Path extraction only supports JSON responses. Current body kind: {body_kind or 'unknown'}.",
        )
    else:
        message = text_for(
            language_mode,
            "Path matched response data." if matched else f"Path did not match the latest {source_type} payload.",
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
        {"image": "image", "audio": "audio", "video": "video"}.get(media_type, media_type),
        media_type,
    )
    return "\n".join(
        [
            text_for(
                language_mode,
                f"API Aggregator: {media_label} URL not found at {path or '<root>'}.",
                f"API Aggregator: {media_label} URL not found at {path or '<root>'}.",
            ),
            text_for(language_mode, f"Extracted value type: {value_type}", f"Extracted value type: {value_type}"),
            f"Content-Type: {content_type or '<empty>'}",
            text_for(language_mode, f"Preview: {preview or '<empty>'}", f"Preview: {preview or '<empty>'}"),
        ]
    )


def validate_import_payload(raw: Any) -> dict[str, Any]:
    from .store import normalize_data

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
    from .store import normalize_data

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
