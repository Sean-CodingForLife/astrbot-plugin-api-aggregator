from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from constants import (
    DEFAULT_RESPONSE_READ_LIMIT_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_RESPONSE_READ_LIMIT_BYTES,
    MAX_RETRY_COUNT,
    MAX_TIMEOUT_SECONDS,
    SUPPORTED_PROXY_SCHEMES,
)
from template_utils import build_template_context, render_string_map_templates, render_template_string
from utils import clamp_int, now_ms


def build_request(api: dict[str, Any], template_context: dict[str, Any] | None = None) -> Request:
    context = build_template_context(api, template_context)
    url = render_template_string(api.get("url"), context).strip()
    query = render_string_map_templates(api.get("query"), context)
    headers = render_string_map_templates(api.get("headers"), context)
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


def normalize_proxy_mode(value: Any, *, proxy_url_value: Any = "") -> str:
    from constants import PROXY_MODES

    mode = str(value or "").strip()
    if mode in PROXY_MODES:
        return mode
    return "direct"


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
