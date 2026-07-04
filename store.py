from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .constants import (
    AGGREGATION_STRATEGIES,
    DATA_VERSION,
    DEFAULT_RESPONSE_READ_LIMIT_BYTES,
    DEFAULT_TEST_LOG_LIMIT,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_COOLDOWN_SECONDS,
    MAX_RESPONSE_READ_LIMIT_BYTES,
    MAX_RETRY_COUNT,
    MAX_TEST_LOG_LIMIT,
    MAX_TIMEOUT_SECONDS,
    RESPONSE_TYPES,
    RESPONSE_TRANSFORMS,
    TRIGGER_MATCH_MODES,
)
from .utils import clamp_int, new_id, now_ms


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
    settings_strategy = str(settings.get("strategy") or "first-ok")
    if settings_strategy not in AGGREGATION_STRATEGIES:
        settings_strategy = "first-ok"
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
        trigger_strategy = str(item.get("strategy") or "first-ok").strip()
        if trigger_strategy not in AGGREGATION_STRATEGIES:
            trigger_strategy = "first-ok"
        cleaned_triggers.append(
            {
                "id": trigger_id,
                "enabled": bool(item.get("enabled", True)),
                "trigger": phrase,
                "match_mode": mode,
                "group_id": str(item.get("group_id") or "all").strip() or "all",
                "strategy": trigger_strategy,
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
            "strategy": settings_strategy,
            "cursors": cleaned_cursors,
            "triggers": cleaned_triggers,
        },
    }


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


def normalize_response_transform(value: Any) -> str:
    transform = str(value or "raw").strip().lower()
    return transform if transform in RESPONSE_TRANSFORMS else "raw"


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
