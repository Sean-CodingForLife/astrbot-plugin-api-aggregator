# Changelog

## Unreleased

### Changed

- Redesigned the official AstrBot plugin Page dashboard with a responsive operations-console layout while keeping the existing `window.AstrBotPluginPage` bridge integration and backend API actions.

### Added

- Added AstrBot plugin configuration for default API timeout, response read limit, test log retention, and aggregate preview length.
- Added `language_mode` configuration for Chinese/English dashboard text and localized trigger replies.
- Added official AstrBot plugin i18n resources under `.astrbot-plugin/i18n/` and wired the dashboard to `bridge.t()` / `bridge.onContext()`.
- Exposed runtime configuration to the dashboard state so newly created APIs use the configured default timeout.
