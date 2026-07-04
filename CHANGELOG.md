# Changelog

## 0.2.0

### Changed

- 将后端逻辑从 `main.py` 拆分为独立模块：`constants.py`、`utils.py`、`store.py`、`template_utils.py`、`request_utils.py`、`aggregation.py`
- 将 HTTP 请求执行从同步 `urllib + asyncio.to_thread` 改为异步 `httpx.AsyncClient`
- 明确声明运行依赖 `httpx>=0.27,<1`
- 统一插件元数据、入口导出、文档和国际化文案
- 移除前端对旧占位内容的兼容逻辑

### Added

- 新增插件级配置：`language_mode`
- 新增插件级配置：`default_timeout_seconds`
- 新增插件级配置：`response_read_limit_bytes`
- 新增插件级配置：`test_log_limit`
- 新增插件级配置：`preview_max_chars`
- 新增插件级配置：`proxy_mode`
- 新增插件级配置：`proxy_url`
- 新增更完整的请求、触发器和聚合诊断日志

### Fixed

- 修复拆分模块后插件包加载失败的问题，补充 `__init__.py` 并改为包相对导入
- 修复 `main.py` 中缺失常量和工具函数导入导致的运行错误
- 修复 `api_aggregator_call` 工具参数与实际支持策略不一致的问题
- 修复 `settings.strategy` 在数据规范化过程中被触发器策略覆盖的问题
- 修复多处乱码和文案不一致问题

