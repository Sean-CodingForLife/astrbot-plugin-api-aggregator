from __future__ import annotations

import time
import uuid
from typing import Any

from .constants import DEFAULT_LANGUAGE_MODE, LANGUAGE_MODES


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


def runtime_language(value: Any) -> str:
    mode = normalize_language_mode(value)
    return "en-US" if mode == "auto" else mode


def text_for(language: str, zh: str, en: str) -> str:
    return zh if runtime_language(language) == "zh-CN" else en
