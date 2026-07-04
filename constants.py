from __future__ import annotations

import re


PLUGIN_NAME = "astrbot_plugin_api_aggregator"
VERSION = "0.2.0"
DATA_VERSION = 1
AGGREGATION_STRATEGIES = {"first-ok", "round-robin", "random", "priority"}
TRIGGER_MATCH_MODES = {"contains", "exact", "command"}
RESPONSE_TYPES = {"summary", "text", "image", "audio", "video"}
LANGUAGE_MODES = {"auto", "zh-CN", "en-US"}
PROXY_MODES = {"direct", "custom", "environment"}
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
