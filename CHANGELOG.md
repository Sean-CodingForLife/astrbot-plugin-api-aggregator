# Changelog

## Unreleased

### 新增

- 增加全局上游 API 代理配置 `proxy_url`，支持通过 AstrBot 插件设置让本插件请求统一走代理。

### 优化

- 优化仪表盘为更清爽的顶部导航布局，移除插件内二级侧栏，放宽 API 列表和非 API 页面内容空间。

### 修复

- 修复上游返回错误 `Content-Type` 时 JSON 文本被识别为普通文本的问题；现在文本响应内容以 `{` 或 `[` 开头且可成功解析为 JSON 时，会按 JSON 响应处理，以便 `response_path` 正常提取。

### Changed

- Redesigned the official AstrBot plugin Page dashboard with a responsive operations-console layout while keeping the existing `window.AstrBotPluginPage` bridge integration and backend API actions.

### Added

- Added AstrBot plugin configuration for default API timeout, response read limit, test log retention, and aggregate preview length.
- Added `language_mode` configuration for Chinese/English dashboard text and localized trigger replies.
- Added official AstrBot plugin i18n resources under `.astrbot-plugin/i18n/` and wired the dashboard to `bridge.t()` / `bridge.onContext()`.
- Exposed runtime configuration to the dashboard state so newly created APIs use the configured default timeout.
