# API Aggregator for AstrBot

`API Aggregator` 是一个 AstrBot 插件，用于在插件页中集中管理外部 HTTP API，并按分组执行聚合调用。

它提供三类能力：

- WebUI 中管理 API、分组、触发器、导入导出和测试日志
- 将一组 API 组合成统一的聚合调用入口
- 向 AstrBot Agent 注册 `api_aggregator_call` 工具，供模型按组调用

## 功能概览

- 支持 `GET`、`POST`、`PUT`、`PATCH`、`DELETE`
- 支持请求 URL、Query、Headers、Body 模板变量渲染
- 支持 `first-ok`、`round-robin`、`random`、`priority` 四种聚合策略
- 支持消息触发器，将聊天消息路由到指定 API 分组
- 支持 `summary`、`text`、`image`、`audio`、`video` 五种触发器响应类型
- 支持 JSON 路径提取，例如 `data.url`
- 支持响应默认值和响应转换
- 支持 API 配置导入、导出和校验
- 支持异步 HTTP 请求执行
- 支持插件级代理配置和请求诊断日志

## 运行要求

- AstrBot `>=4.26,<5`
- Python 运行环境由 AstrBot 提供
- 运行依赖：
  - `httpx>=0.27,<1`

## 安装

1. 将目录放入 AstrBot 插件目录，目录名保持为 `astrbot-plugin-api-aggregator`
2. 确认依赖已安装
3. 重启 AstrBot，或在插件管理中重新加载插件
4. 打开 AstrBot WebUI 的插件页，进入 `API Aggregator`

## 插件配置

插件配置由 [_conf_schema.json](/C:/Users/Administrator/Desktop/api-aggregator/astrbot-plugin-api-aggregator/_conf_schema.json) 定义。

| 配置项 | 默认值 | 范围 | 说明 |
| --- | --- | --- | --- |
| `language_mode` | `auto` | `auto` / `zh-CN` / `en-US` | 插件页语言模式；`auto` 跟随 AstrBot WebUI 语言 |
| `default_timeout_seconds` | `12` | `1-120` | 新建 API 时使用的默认超时秒数 |
| `response_read_limit_bytes` | `4096` | `1-1048576` | 单次请求最多读取的上游响应字节数 |
| `test_log_limit` | `100` | `1-1000` | 保留的测试日志条数上限 |
| `preview_max_chars` | `1200` | `1-10000` | 聚合摘要中预览文本的最大字符数 |
| `proxy_mode` | `direct` | `direct` / `custom` / `environment` | 请求代理模式 |
| `proxy_url` | `""` | `http://...` / `https://...` | 自定义代理地址，仅在 `proxy_mode=custom` 时生效 |

## 使用说明

### 管理 API

每个 API 条目支持以下字段：

- `name`
- `group_id`
- `url`
- `method`
- `query`
- `headers`
- `body`
- `priority`
- `timeout_seconds`
- `retry_count`
- `cooldown_seconds`
- `enabled`
- `description`

说明：

- `priority` 越小，优先级越高
- `retry_count` 范围为 `0-5`
- `cooldown_seconds` 范围为 `0-3600`
- Body 目前是原始文本，由请求头决定上游如何解释

### 模板变量

URL、Query、Headers 和 Body 支持模板语法：

```text
{{path.to.value}}
```

常见变量：

- `{{time.unix}}`
- `{{time.unix_ms}}`
- `{{time.iso}}`
- `{{random.int}}`
- `{{random.hex}}`
- `{{random.uuid}}`
- `{{env.OPENAI_API_KEY}}`
- `{{api.name}}`
- `{{api.method}}`
- `{{message.text}}`
- `{{message.command}}`
- `{{message.args_text}}`
- `{{message.args.0}}`
- `{{session.id}}`
- `{{session.user_id}}`
- `{{session.room_id}}`
- `{{trigger.phrase}}`
- `{{trigger.match_mode}}`
- `{{trigger.args_text}}`
- `{{trigger.args.0}}`
- `{{vars.foo}}`

说明：

- `env.*` 从 AstrBot 进程环境变量读取
- `message.*`、`session.*`、`trigger.*` 主要用于消息触发场景
- `vars.*` 用于 Web API 或 LLM 工具调用时传入额外变量
- 缺失模板变量会导致请求失败，并记录为 `template_error`

### 聚合策略

- `first-ok`：按当前候选顺序依次尝试，直到首个成功
- `round-robin`：按分组游标轮询起点
- `random`：随机打乱候选顺序
- `priority`：按 `priority` 从小到大排序尝试

只有启用状态的 API 会参与聚合调用。

### 消息触发器

触发器支持三种匹配模式：

- `contains`
- `exact`
- `command`

触发器支持五种响应类型：

- `summary`
- `text`
- `image`
- `audio`
- `video`

触发器还支持：

- `response_path`
- `response_default`
- `response_transform`
- `preview_api_id`
- `stop_event`

当响应类型为 `image`、`audio`、`video` 时，`response_path` 应提取到一个 `http://` 或 `https://` URL。

### 响应提取与转换

支持的响应转换：

- `raw`
- `string`
- `json`
- `join-comma`
- `join-lines`
- `int`
- `float`
- `bool`

说明：

- `response_path` 仅对 JSON 响应做路径提取
- 文本响应不做路径提取，直接使用原始文本
- 如果 `response_path` 未命中且配置了 `response_default`，则回退到默认值

### LLM 工具

插件注册的工具名：

```text
api_aggregator_call
```

参数：

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `group` | string | `all` | 可见分组名称，`all` 表示所有已启用 API |
| `strategy` | string | `first-ok` | `first-ok`、`round-robin`、`random`、`priority` |
| `response_path` | string | `""` | 可选 JSON 路径，例如 `data.link` |
| `response_default` | string | `""` | 路径未命中时的默认值 |
| `response_transform` | string | `raw` | 响应转换方式 |
| `template_vars` | object | `{}` | 注入到 `vars.*` 的模板变量 |

返回值是 JSON 字符串，包含：

- `ok`
- `group`
- `strategy`
- `response_path`
- `response_default`
- `response_transform`
- `value`
- `selected`
- `failure_reason`
- `attempts`

## 数据存储

插件运行数据写入 AstrBot 插件数据目录：

```text
<astrbot_plugin_data_path>/astrbot_plugin_api_aggregator/api_aggregator.json
```

数据结构包含：

- `groups`
- `apis`
- `test_logs`
- `settings.strategy`
- `settings.cursors`
- `settings.triggers`

导入策略：

- `replace`
- `merge`
- `validate`

## 项目结构

```text
astrbot-plugin-api-aggregator/
├── __init__.py
├── main.py
├── constants.py
├── utils.py
├── store.py
├── template_utils.py
├── request_utils.py
├── aggregation.py
├── metadata.yaml
├── requirements.txt
├── _conf_schema.json
├── .astrbot-plugin/
│   └── i18n/
│       ├── en-US.json
│       └── zh-CN.json
└── pages/
    └── dashboard/
        ├── index.html
        ├── app.js
        └── style.css
```

## 诊断与行为说明

- 请求执行使用异步 `httpx.AsyncClient`
- 默认会对 HTTP `4xx/5xx` 视为失败
- 请求日志会记录请求摘要、重试尝试、失败类型和最终结果
- 触发器日志会记录匹配结果、参数捕获和聚合执行结果
- 自定义代理地址无效时，会记录 warning，并回退为直连
- `response_read_limit_bytes` 只控制最大读取字节数，不适合下载大文件

## 开发说明

- 插件入口为 [main.py](/C:/Users/Administrator/Desktop/api-aggregator/astrbot-plugin-api-aggregator/main.py)
- 前端页面位于 [pages/dashboard/index.html](/C:/Users/Administrator/Desktop/api-aggregator/astrbot-plugin-api-aggregator/pages/dashboard/index.html) 和 [pages/dashboard/app.js](/C:/Users/Administrator/Desktop/api-aggregator/astrbot-plugin-api-aggregator/pages/dashboard/app.js)
- 国际化资源位于 [.astrbot-plugin/i18n/zh-CN.json](/C:/Users/Administrator/Desktop/api-aggregator/astrbot-plugin-api-aggregator/.astrbot-plugin/i18n/zh-CN.json) 和 [.astrbot-plugin/i18n/en-US.json](/C:/Users/Administrator/Desktop/api-aggregator/astrbot-plugin-api-aggregator/.astrbot-plugin/i18n/en-US.json)

