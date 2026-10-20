# Grafana 告警转发插件设计

- 日期：2026-10-21
- 状态：已与需求方确认，待实现
- 插件目录：`data/plugins/astrbot_plugin_grafana_alert`
- 目标版本：v0.1.0

## 1. 背景与目标

AstrBot 需要一个插件，把 Grafana 统一告警（Unified Alerting）的 Webhook 通知转发到即时通讯会话中，让值班群能够及时看到告警与恢复消息。

目标：

1. 通过 AstrBot 现有 Dashboard HTTP 服务接收 Grafana Webhook，不额外监听端口。
2. 按 Grafana 标签把告警路由到不同会话，并提供兜底会话。
3. 支持可配置的触发/恢复消息模板。
4. 支持 severity 过滤、恢复通知、按会话静默。
5. 提供管理员命令完成绑定会话、测试链路、静默与状态查看。
6. 错误可控：任何单条告警、单个目标或模板问题都不能导致插件崩溃或 AstrBot 崩溃。

## 2. 非目标

v1 明确不做：

- 旧版 Grafana Dashboard 告警（legacy payload，`title/message/state/evalMatches`）的解析。
- 告警去重、指纹缓存、重复通知抑制。
- 与 Grafana/Alertmanager Silence 的双向同步。
- 插件专属 WebUI 页面。
- 告警图片渲染、图表截图。
- 自定义 HTTP 端口或独立 HTTP 服务。
- 多语言界面（命令与消息默认中文）。

## 3. 术语

- **UMO**：AstrBot 的 unified message origin，会话唯一标识，例如 `aiocqhttp:GroupMessage:123456`。
- **告警（alert）**：Alertmanager payload 中 `alerts[]` 的一项。
- **通知（notification）**：Grafana 一次 Webhook 请求，包含一条或多条告警。
- **路由规则（route）**：把满足标签条件的告警指向某个 UMO 的配置项。

## 4. 架构与数据流

```text
Grafana Unified Alerting
  │  Contact point: Webhook
  │  POST http://<astrbot-host>:6185/api/v1/plugins/extensions/
  │       astrbot_plugin_grafana_alert/webhook
  │  Header: X-API-Key: <AstrBot API Key，scope 含 plugin>
  ▼
AstrBot Dashboard /api/v1 路由（核心完成 API Key 鉴权）
  ▼
插件 Web API handler
  1. 解析 Alertmanager payload，归一化为 Alert 列表
  2. severity 过滤、resolved 过滤
  3. 逐条告警匹配路由规则，得到目标 UMO
  4. 按 UMO 聚合告警
  5. 跳过静默中的 UMO
  6. 逐条告警渲染模板，同一 UMO 的告警合并为一条消息
  7. 并发调用 context.send_message 发送
  8. 返回统计 JSON
```

插件不接触 API Key 的校验逻辑，鉴权完全由 AstrBot 核心的 `require_plugin_scope` 完成。插件内不保存、不回显 API Key。

### 4.1 Webhook 接入点

注册路由（`context.register_web_api`）：

- 注册路径：`/astrbot_plugin_grafana_alert/webhook`
- 方法：`POST`
- 对外完整路径：`/api/v1/plugins/extensions/astrbot_plugin_grafana_alert/webhook`
- 鉴权：AstrBot API Key，scope 为 `plugin` 或 `*`；通过 `X-API-Key` 请求头传递。Grafana 不支持自定义请求头时，可退回 `?api_key=<key>` 查询参数，但 README 必须提示查询参数会进入访问日志。

路由注册在插件 `__init__` 中完成；插件 `terminate` 时从 `context.registered_web_apis` 移除该路由，避免禁用/卸载后仍能访问旧实例。

### 4.2 请求与响应

请求体为 Alertmanager Webhook JSON，至少包含 `alerts` 数组。插件只读取以下字段：

- 顶层：`status`、`alerts`、`groupLabels`、`commonLabels`、`commonAnnotations`、`externalURL`
- 单条告警：`status`、`labels`、`annotations`、`startsAt`、`endsAt`、`generatorURL`、`fingerprint`

响应：

| 情况 | HTTP | 响应体 |
|---|---|---|
| 正常处理 | 200 | `{"status":"ok","alerts":N,"delivered":X,"skipped":Y,"failed":Z}` |
| 插件停用 | 200 | `{"status":"disabled"}` |
| JSON 非法或不是对象 | 400 | `{"status":"error","message":"invalid payload","data":null}` |
| `alerts` 缺失或不是数组 | 400 | 同上 |
| 单个目标发送失败 | 200 | 正常统计，`failed` 加一 |

计数语义：

- `alerts`：解析出的告警条数。
- `delivered`：成功发送的聊天消息条数（每个目标会话一条）。
- `skipped`：因过滤、无路由、静默而未能转发的告警条数。
- `failed`：发送失败的目标数。

发送失败返回 200 而不是 5xx，避免 Grafana 自动重试造成重复消息；失败原因写入插件日志。

发送调用为 `context.send_message(umo, MessageChain([Plain(text)]))`；返回 `False`（找不到平台）视为该目标失败。多个目标通过 `asyncio.gather(..., return_exceptions=True)` 并发发送，单个目标异常不影响其他目标。

## 5. 配置模型

配置文件由 AstrBot 依据 `_conf_schema.json` 生成，位于 `data/config/astrbot_plugin_grafana_alert_config.json`。

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 总开关；关闭时 Webhook 直接返回 disabled |
| `default_session` | string | `""` | 未命中任何路由规则时的兜底 UMO |
| `routes` | template_list | `[]` | 标签路由规则，按列表顺序匹配 |
| `severity_filter` | list | `[]` | severity 白名单；空列表表示不过滤 |
| `forward_resolved` | bool | `true` | 是否转发恢复告警 |
| `firing_template` | text | 见 6.2 | 触发告警消息模板 |
| `resolved_template` | text | 见 6.2 | 恢复告警消息模板 |
| `timezone` | string | `Asia/Shanghai` | 时间显示时区，IANA 名称 |
| `max_message_chars` | int | `3000` | 单条消息最大字符数 |

### 5.1 路由规则

`routes` 使用 `template_list`，模板键为 `route`，字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | string | 规则名称，用于 `/grafana bind <name>` 与状态展示 |
| `match` | string | 标签匹配串，如 `team=db,env=prod`；留空表示匹配全部 |
| `session` | string | 目标会话 UMO |

匹配语义：

1. 规则按列表顺序逐条检查，第一条命中的规则生效，后面的规则不再检查。
2. `match` 按逗号切分，每段按第一个 `=` 切分为 key/value，去掉首尾空白后与告警的 labels 做**大小写敏感的精确字符串比较**；所有键值对都相等才算命中。
3. `match` 为空字符串表示 catch-all 规则。
4. 非空 `match` 中只要存在缺少 `=` 的片段，该规则视为无效：处理时跳过，不参与匹配，也不会退化成 catch-all；插件加载或执行 `/grafana status` 时记录 warning，Webhook 处理路径不重复刷日志。
5. 没有规则命中时使用 `default_session`；`default_session` 为空则丢弃该告警并计入 `skipped`。
6. 目标 `session` 为空字符串的规则视为无效，跳过；与无效 `match` 一样，在插件加载或 `/grafana status` 时记录 warning。

### 5.2 severity 过滤

- 读取每条告警的 `labels.severity`，去空白后与 `severity_filter` 做大小写不敏感比较。
- `severity_filter` 为空表示全部通过。
- 配置了过滤但告警没有 `severity` 标签时，该告警被过滤（严格模式），避免误放行。
- 过滤发生在路由之前。

### 5.3 resolved 过滤

- `forward_resolved` 为 `false` 时，`status` 为 `resolved` 的告警直接计入 `skipped`。
- 状态比较不区分大小写；除 `resolved` 外一律按触发处理。

## 6. 消息渲染

### 6.1 占位符

模板使用 Python `str.format` 语法，仅支持以下白名单键：

| 占位符 | 来源 | 说明 |
|---|---|---|
| `{status}` | alert.status | `firing` / `resolved` |
| `{alertname}` | labels.alertname | 告警名 |
| `{severity}` | labels.severity | 级别 |
| `{instance}` | labels.instance | 实例 |
| `{job}` | labels.job | 任务名 |
| `{summary}` | annotations.summary | 摘要 |
| `{description}` | annotations.description | 描述 |
| `{labels}` | labels | 渲染为 `k=v, k2=v2`，按键排序 |
| `{annotations}` | annotations | 渲染为 `k=v, k2=v2`，按键排序 |
| `{starts_at}` | startsAt | 转换到配置时区后格式化 |
| `{ends_at}` | endsAt | 转换到配置时区后格式化 |
| `{generator_url}` | generatorURL | Grafana 告警链接 |
| `{fingerprint}` | fingerprint | 告警指纹 |

规则：

- 缺失字段渲染为空字符串。
- 未知占位符视为模板错误，在插件加载时记录 error，并回退到内置默认模板；不会在每次请求时抛异常。
- 字段名中不允许出现 `.` 或 `[`，使用 `string.Formatter().parse` 校验，避免通过属性/下标访问泄露对象信息。
- 时间字段为 ISO 8601；解析失败时原样输出。零值时间（`0001-01-01T00:00:00Z`）输出为空字符串。
- `timezone` 非法时记录 warning，回退 `UTC`。

### 6.2 默认模板

触发：

```text
🚨 告警触发 | {alertname}
级别：{severity}
实例：{instance}
摘要：{summary}
开始：{starts_at}
详情：{generator_url}
```

恢复：

```text
✅ 告警恢复 | {alertname}
级别：{severity}
实例：{instance}
恢复：{ends_at}
详情：{generator_url}
```

### 6.3 合并与截断

- 同一目标 UMO 在一次 Webhook 中的多条告警，逐条渲染后用两个换行符（`\n\n`）拼接为一条消息。
- 拼接结果超过 `max_message_chars` 时截断，并确保截断后的内容加上 `\n…（内容已截断）` 后缀后不超过该上限。
- `max_message_chars` 小于 200 时按 200 使用，避免配置成不可读的值。
- 每个目标只发送一条消息，降低平台限流风险。

## 7. 命令

所有命令需要 AstrBot 管理员权限，实现方式：

```python
@filter.permission_type(filter.PermissionType.ADMIN)
@filter.command("grafana")
async def grafana(self, event: AstrMessageEvent, sub: str = "", arg: GreedyStr = "") -> None:
```

| 命令 | 行为 |
|---|---|
| `/grafana test` | 用示例告警向**当前会话**发送一条触发消息；绕过路由与静默；回复发送结果 |
| `/grafana bind` | 把当前会话写入 `default_session` 并保存配置 |
| `/grafana bind <规则名>` | 把当前会话写入第一条同名路由规则的 `session` 并保存配置；规则不存在时回复错误 |
| `/grafana mute [时长]` | 静默当前会话；省略时长默认 `1h` |
| `/grafana unmute` | 解除当前会话静默 |
| `/grafana status` | 展示默认会话、路由数量、severity 过滤、是否转发恢复、当前会话静默剩余时间 |
| 其他/缺省 | 回复命令用法 |

命令回复使用 `yield event.plain_result(...)`。配置修改通过 `self.config.save_config()` 持久化，WebUI 与命令看到的是同一份配置。

权限说明：`PermissionType.ADMIN` 依赖平台的群管理员身份，私聊通常不满足；因此 `bind` 的推荐用法是在目标群里由管理员执行。私聊或无法取得管理员身份时，可直接在 WebUI 插件配置中填写 UMO。

`/grafana bind <规则名>` 在存在重名规则时绑定第一条；`/grafana test` 绕过路由、静默、severity 过滤与 resolved 过滤，始终向当前会话发送。

时长格式：

- 正则 `^(\d+)\s*([smhd])?$`，单位缺省为分钟。
- 支持 `90s`、`30m`、`2h`、`7d`、`30`（30 分钟）。
- 上限 30 天；0 或非法值回复用法错误。
- `mute` 只影响被静默的会话，其他目标照常接收。

## 8. 静默状态

- 存储：插件 KV（`put_kv_data` / `get_kv_data` / `delete_kv_data`）。
- Key：`mute_until:<umo>`；Value：到期时间戳（float，Unix 秒）。
- 发送前检查：`now < expiry` 则跳过该会话，计入 `skipped`；已过期则删除 key。
- 插件重启后静默状态仍然有效。
- `/grafana status` 按当前会话查询并显示剩余分钟数。

## 9. 错误处理与可观测性

| 场景 | 行为 |
|---|---|
| JSON 非法 | 返回 400，记录 warning |
| 模板非法 | 加载时记录 error，回退默认模板 |
| 路由规则非法 | 加载和每次处理时跳过该规则，首次记录 warning |
| 目标 UMO 格式非法 | 发送异常被捕获，记录 error，`failed` 加一 |
| 平台发送异常 | 仅影响该目标，继续发送其他目标 |
| 插件停用 | Webhook 返回 `{"status":"disabled"}` |
| 配置对象不可用 | 使用安全默认值（enabled=false、无路由、无兜底），记录 error |

日志使用插件专属 logger（`self.logger`），不打印 API Key 等凭据。

每次 Webhook 记录一条 info 日志：告警数、目标数、成功数、跳过数、失败数。静默跳过使用 debug 级别，避免刷屏。

## 10. 安全

- 依赖 AstrBot API Key 鉴权；插件不实现第二套密钥，避免配置重复。
- README 明确要求为插件创建独立 API Key，scope 仅勾选 `plugin`，并提示定期轮换。
- 不记录、不回显 API Key。
- 模板渲染只允许白名单字段，不使用 `eval` / `format_map` 的任意属性访问。
- 路由匹配使用普通字符串比较，不使用正则，避免 ReDoS。
- 单次请求的告警数量不做硬限制，但消息长度有上限；超大 payload 由 Dashboard/反向代理的请求体限制兜底。

## 11. 文件结构

```text
data/plugins/astrbot_plugin_grafana_alert/
├── main.py              # 插件类、Webhook handler、命令、发送编排
├── core.py              # 纯函数：payload 解析、路由、模板渲染、时长解析
├── _conf_schema.json    # WebUI 配置 Schema
├── metadata.yaml        # 插件元数据（已由维护者填写）
├── README.md            # 部署、Grafana 配置、命令、排错
├── LICENSE              # 保留模板许可证
├── docs/specs/2026-10-21-grafana-alert-plugin-design.md
└── tests/test_core.py   # 纯函数单元测试
```

无第三方运行时依赖，不需要 `requirements.txt`。

`metadata.yaml` 增加 `astrbot_version: ">=4.10.4"`，因为 `template_list` 配置类型从该版本开始提供。

## 12. 测试计划

单元测试（`tests/test_core.py`，pytest）：

1. 解析合法 Alertmanager payload，字段映射正确。
2. 非对象 payload、缺失 `alerts`、`alerts` 非数组时抛出可识别异常。
3. 路由：首条命中优先、catch-all、无命中回退默认、无效规则被跳过。
4. severity 过滤：大小写不敏感、空列表放行、缺失 severity 被过滤。
5. 模板：占位符替换、缺失字段为空、非法占位符被识别、时间与时区格式化、零值时间。
6. 合并与截断：多条告警拼接、超长截断。
7. 静默时长解析：`30`、`30m`、`2h`、`7d`、非法值。
8. 告警状态归一化：`resolved` 过滤开关。

手工验证：

1. 启动 AstrBot，创建 scope 为 `plugin` 的 API Key。
2. 在群里执行 `/grafana bind` 与 `/grafana test`。
3. 用 curl 带 `X-API-Key` 向 Webhook 发送示例 payload，确认消息与响应统计。
4. 在 Grafana 中新建 Webhook Contact Point，点击 Test，确认收到测试告警。
5. 配置两条路由规则，验证标签分流与默认会话兜底。
6. `/grafana mute 1m` 后发送告警，确认该会话被跳过，其他会话不受影响；到期后恢复。

代码质量：

- `ruff format` 与 `ruff check` 插件目录。
- `uv run pytest data/plugins/astrbot_plugin_grafana_alert/tests -q`。

## 13. 部署步骤（README 摘要）

1. 在 AstrBot WebUI「设置」页创建 API Key，scope 勾选 `plugin`，保存明文 Key。
2. 安装并启用插件，在插件配置中填写 `default_session` 或使用 `/grafana bind`。
3. Grafana → Alerting → Contact points → 新建 Webhook：
   - URL：`http://<astrbot-host>:6185/api/v1/plugins/extensions/astrbot_plugin_grafana_alert/webhook`
   - Method：`POST`
   - Custom header：`X-API-Key: <API Key>`
4. 在 Notification policy 中把目标告警路由到该 Contact Point。
5. 使用 Contact Point 的 Test 按钮或 `/grafana test` 验证。

## 14. 验收标准

1. Grafana 统一告警的触发与恢复 Webhook 均能转发到配置的会话。
2. 标签路由按顺序生效，未命中时使用默认会话；无目标时返回成功但不发送。
3. 模板修改保存后，下一次 Webhook 请求使用最新配置；若 WebUI 保存触发插件重载，模板校验也会重新执行。
4. 静默按会话生效，重启后仍有效。
5. 非法 payload 返回 400，插件不抛未捕获异常。
6. 单元测试全部通过，ruff 无告警。
7. README 足以让新用户完成 API Key、Grafana Contact Point 和插件配置。
