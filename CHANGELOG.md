# Changelog

## Unreleased

### Added

- Added API body mode support for `json`, `raw`, `form-data`, and `x-www-form-urlencoded`.
- Added `form-data` file field support in the dashboard with file-object storage in `Body Form JSON`.
- Added global upstream API proxy configuration with `proxy_url`.
- Added AstrBot plugin configuration for default API timeout, response read limit, test log retention, and aggregate preview length.
- Added `language_mode` configuration for Chinese/English dashboard text and localized trigger replies.
- Added official AstrBot plugin i18n resources under `.astrbot-plugin/i18n/` and wired the dashboard to `bridge.t()` / `bridge.onContext()`.

### Changed

- Switched upstream HTTP request execution from synchronous `urllib` to asynchronous `httpx`.
- Redesigned the official AstrBot plugin Page dashboard with a responsive operations-console layout while keeping the existing `window.AstrBotPluginPage` bridge integration and backend API actions.
- Exposed runtime configuration to the dashboard state so newly created APIs use the configured default timeout.

### Fixed

- Fixed JSON-like text responses with incorrect `Content-Type` so they can still be parsed as JSON for `response_path` extraction.
- Fixed `api_aggregator_call` tool parameter metadata to expose the `priority` aggregation strategy.
- Fixed `json` body mode to validate request body JSON before sending the request.
