from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
from aggregation import (
    capture_trigger_arguments,
    enabled_apis_for_group,
    format_aggregate_reply,
    format_missing_url_reply,
    group_name_exists,
    merge_import_data,
    ordered_candidates,
    preview_response_path,
    record_test_result,
    resolve_group_name,
    resolve_trigger_media_url,
    resolve_trigger_response_value,
    response_value,
    transform_response_value,
    trigger_matches,
    validate_import_payload,
)
from constants import (
    DEFAULT_PREVIEW_MAX_CHARS,
    DEFAULT_RESPONSE_READ_LIMIT_BYTES,
    DEFAULT_TEST_LOG_LIMIT,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_COOLDOWN_SECONDS,
    MAX_PREVIEW_MAX_CHARS,
    MAX_RESPONSE_READ_LIMIT_BYTES,
    MAX_RETRY_COUNT,
    MAX_TEST_LOG_LIMIT,
    MAX_TIMEOUT_SECONDS,
    PLUGIN_NAME,
    VERSION,
)
from request_utils import execute_api_request, resolve_proxy_config
from store import ApiAggregatorStore, normalize_response_transform, pick_string_map, pick_template_vars
from template_utils import build_message_template_context
from utils import clamp_int, new_id, normalize_language_mode, now_ms


async def read_json_body() -> dict[str, Any]:
    data = await request.json(default={})
    return data if isinstance(data, dict) else {}


def ok(data: Any = None):
    return json_response(data if data is not None else {})


def fail(message: str, status: int = 400):
    return error_response(message, status_code=status)


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
