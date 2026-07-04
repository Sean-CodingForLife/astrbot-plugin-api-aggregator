# API Aggregator for AstrBot

API Aggregator 是一个 AstrBot WebUI 插件，用于在插件页中集中管理多个 HTTP API，并按分组聚合调用。它可以把外部 API 封装成消息触发器，也会向 AstrBot Agent 注册一个 LLM 工具，方便模型按组调用外部接口。

## 功能特性

- 在 AstrBot 插件页管理 API、分组、启用状态和测试结果
- 支持 GET、POST、PUT、PATCH、DELETE 请求
- 支持 Query、Headers、Body、超时、重试和冷却时间配置
- 支持 `first-ok`、`round-robin`、`random`、`priority` 四种聚合策略
- 支持通过 AstrBot 插件设置调整外部 API 响应读取上限
- 支持消息触发器，匹配后自动调用 API 分组
- 触发器可返回摘要、文本、图片、音频或视频
- 支持 JSON 响应字段路径提取，例如 `data.url`
- 支持响应默认值与基础响应转换
- 支持命令触发器参数捕获并注入模板变量
- 支持导入、导出插件配置 JSON
- 注册 LLM 工具 `api_aggregator_call`

## 适用场景

- 把多个备用 API 聚合成一个高可用调用入口
- 给 AstrBot 添加可视化管理的外部 API 能力
- 通过关键词或命令触发 HTTP API，并把结果直接发回会话
- 让 Agent 在对话中按分组调用已配置的工具 API

## 安装

1. 将本目录放入 AstrBot 的插件目录，目录名建议保持为 `astrbot-plugin-api-aggregator`。
2. 重启 AstrBot 或在插件管理中重新加载插件。
3. 打开 AstrBot WebUI 的插件页，进入 API Aggregator 面板。
4. 添加 API 分组和 API 条目，使用 `Test` 或 `Test Enabled APIs` 验证配置。

插件要求：

- AstrBot `>=4.26,<5`
- Python 运行环境由 AstrBot 提供
- 无额外第三方 Python 依赖

## 插件配置

插件支持在 AstrBot 插件设置中调整以下配置：

| 配置项 | 默认值 | 范围 | 说明 |
| --- | --- | --- | --- |
| `language_mode` | `auto` | `auto` / `zh-CN` / `en-US` | 语言模式；`auto` 下插件页跟随浏览器语言，后端触发器回复保持英文 |
| `default_timeout_seconds` | `12` | `1-120` | 新增 API 时使用的默认请求超时时间；单个 API 仍可单独覆盖 |
| `response_read_limit_bytes` | `4096` | `1-1048576` | 每次请求最多读取的外部 API 响应字节数 |
| `test_log_limit` | `100` | `1-1000` | 测试日志最多保留条数 |
| `preview_max_chars` | `1200` | `1-10000` | 聚合摘要消息中响应预览的最大字符数 |
| `proxy_mode` | `direct` | `direct` / `custom` / `environment` | 代理模式；`direct` 直接访问目标 API，`custom` 使用 `proxy_url`，`environment` 使用 AstrBot 进程环境中的代理设置 |
| `proxy_url` | `""` | `http://...` / `https://...` | 仅在 `proxy_mode=custom` 时生效 |

语言和配置项文案使用 AstrBot 官方插件国际化目录 `.astrbot-plugin/i18n/`。当 `language_mode` 为 `auto` 时，插件页跟随 AstrBot WebUI 当前语言；触发器后端回复因没有 WebUI locale 上下文，会保持英文。

## 使用说明

### 1. 管理 API

在插件页中点击 `Add API` 添加接口。每个 API 可配置：

- 名称、分组、URL、请求方法
- Query JSON
- Headers JSON
- Body
- Priority：范围 `1-1000`，数值越小优先级越高
- Timeout Seconds，范围 `1-120`
- Retry Count，范围 `0-5`
- Cooldown Seconds，范围 `0-3600`
- 是否启用

URL、Query、Headers 和 Body 支持变量模板，语法为 `{{path.to.value}}`。

常用变量示例：

- `{{time.unix}}` / `{{time.unix_ms}}` / `{{time.iso}}`
- `{{random.int}}` / `{{random.hex}}` / `{{random.uuid}}`
- `{{env.OPENAI_API_KEY}}`
- `{{api.name}}` / `{{api.method}}`
- `{{message.text}}` / `{{message.command}}` / `{{message.args_text}}`
- `{{message.args.0}}` / `{{message.args.1}}`
- `{{session.id}}` / `{{session.user_id}}` / `{{session.room_id}}`
- `{{trigger.phrase}}` / `{{trigger.match_mode}}`
- `{{vars.foo}}` / `{{vars.payload.user_id}}`

说明：

- `env.*` 从 AstrBot 进程环境变量读取，适合密钥注入。
- `message.*`、`session.*`、`trigger.*` 主要在消息触发器场景下可用。
- `vars.*` 用于 Web API / LLM 工具调用时传入额外模板变量。
- 缺失变量会导致本次请求直接失败，并在测试结果中标记为 `template_error`。

插件默认读取外部 API 响应的前 4096 字节，并按 `Content-Type` 自动归类为：

- `json`
- `text`
- `binary`

JSON 和文本响应会保存预览内容；二进制响应会保存大小和内容类型等元数据。

可以在 AstrBot 插件设置中调整 `response_read_limit_bytes`。该值控制每次请求最多读取多少响应内容。如果需要解析较大的 JSON 或字段较靠后的响应，可以适当调高；如果 API 可能返回大文件，建议保持较小值。

### 2. 聚合调用

在 `Aggregator` 区域选择分组和策略后点击 `Call Group`。

策略说明：

- `first-ok`：按当前列表顺序调用，遇到第一个成功响应即停止
- `round-robin`：按分组维护轮询游标，从不同 API 开始尝试
- `random`：随机打乱候选 API 后尝试
- `priority`：按 API `priority` 从小到大尝试

只有启用状态的 API 会参与聚合调用。

### 3. 消息触发器

在 `Message Triggers` 区域添加触发器。触发器支持三种匹配模式：

- `contains`：消息包含触发词
- `exact`：消息完全等于触发词
- `command`：消息第一个空格前的命令等于触发词

当匹配模式为 `command` 时，可在模板中使用：

- `{{trigger.args_text}}`：命令后的原始参数文本
- `{{trigger.args.0}}`、`{{trigger.args.1}}`
- `{{command.name}}`、`{{command.args_text}}`、`{{command.args.0}}`

触发器可以配置响应类型：

- `summary`：返回调用摘要
- `text`：返回响应正文或指定字段
- `image`：从响应字段中提取图片 URL 并发送图片
- `audio`：从响应字段中提取音频 URL 并发送音频
- `video`：从响应字段中提取视频 URL 并发送视频

文本响应还支持：

- `Response Default`：`response_path` 未命中时的回退值
- `Response Transform`：`raw`、`string`、`json`、`join-comma`、`join-lines`、`int`、`float`、`bool`

当响应类型为图片、音频或视频时，`Response Path` 应指向一个 `http://` 或 `https://` URL。可以先选择 `Preview API` 并点击 `Preview Path` 验证字段路径。

### 4. LLM 工具

插件会注册工具：

```text
api_aggregator_call
```

参数：

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `group` | string | `all` | 可见分组名称，传 `all` 表示所有启用 API |
| `strategy` | string | `first-ok` | `first-ok`、`round-robin`、`random` 或 `priority` |
| `response_path` | string | 空 | 可选 JSON 点路径，例如 `data.link` |
| `response_default` | string | 空 | 当 `response_path` 未命中时使用的默认值 |
| `response_transform` | string | `raw` | `raw`、`string`、`json`、`join-comma`、`join-lines`、`int`、`float`、`bool` |
| `template_vars` | object | `{}` | 可选模板变量，会暴露为 `vars.*` |

工具返回 JSON 字符串，包含 `ok`、`selected`、`attempts`、`failure_reason` 和提取后的 `value`。

示例返回：

```json
{
  "ok": true,
  "group": "Default",
  "strategy": "first-ok",
  "response_path": "data.link",
  "value": "https://example.com/result.png",
  "selected": {
    "api_id": "api_xxx",
    "api_name": "Example API",
    "ok": true,
    "status": 200,
    "body_kind": "json"
  },
  "failure_reason": "",
  "attempts": []
}
```

## 数据存储

运行数据不存放在插件仓库内。插件会写入 AstrBot 插件数据目录：

```text
<astrbot_plugin_data_path>/astrbot_plugin_api_aggregator/api_aggregator.json
```

该文件包含：

- `groups`
- `apis`
- `test_logs`
- `settings.strategy`
- `settings.cursors`
- `settings.triggers`

WebUI 的导入/导出功能使用同一份 JSON 数据结构。导入策略包括：

- `replace`：替换当前数据
- `merge`：合并导入数据
- `validate`：只校验导入数据，不写入

## 项目结构

```text
astrbot-plugin-api-aggregator/
├── main.py                  # 插件后端、Web API、触发器和 LLM 工具
├── metadata.yaml            # AstrBot 插件元数据
├── _conf_schema.json        # AstrBot 插件设置 Schema
├── .astrbot-plugin/
│   └── i18n/
│       ├── en-US.json       # 官方插件国际化资源
│       └── zh-CN.json
├── pages/
│   └── dashboard/
│       ├── index.html       # 插件页 HTML
│       ├── app.js           # 插件页交互逻辑
│       └── style.css        # 插件页样式
├── README.md
├── CHANGELOG.md
├── .gitignore
└── .gitattributes
```

## 开发说明

本插件主要由 `main.py` 提供后端能力：

- 注册 AstrBot Web API
- 读写插件数据 JSON
- 执行 HTTP 请求并规范化结果
- 实现聚合策略
- 注册消息事件处理器
- 注册 `api_aggregator_call` 工具

插件页通过 `window.AstrBotPluginPage` 调用后端接口，页面资源位于 `pages/dashboard/`。

开发时建议不要提交以下内容：

- `__pycache__/`
- `*.pyc`
- 本地虚拟环境
- 临时测试导出文件
- 编辑器和系统缓存文件

## 注意事项

- 外部 API 响应读取上限由插件设置 `response_read_limit_bytes` 控制，默认 4096 字节；适合状态检查、短文本、JSON 和 URL 提取，不适合直接下载大文件。
- 代理模式由 `proxy_mode` 控制：`direct` 不使用代理直接访问目标 API，`custom` 使用 `proxy_url`，`environment` 使用 AstrBot 进程环境中的代理设置。
- 当 `proxy_mode=custom` 且 `proxy_url` 配置了不支持的地址时，插件启动时会记录 warning，并在插件页状态中标记为无效；请求会回退为直连。
- 模板变量渲染发生在真正发起 HTTP 请求之前，适用于 URL、Query、Headers 和 Body。
- 聚合 `priority` 策略依赖 API 的 `priority` 字段；数值越小越优先。
- `response_path` 只对 JSON 对象做字段提取；文本响应会直接返回文本。
- 图片、音频、视频触发器只会发送从响应中提取到的 HTTP(S) URL。
- API Headers、Body 和导入导出文件可能包含密钥，请谨慎分享。
