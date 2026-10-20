# Grafana 告警转发

AstrBot 插件：接收 Grafana 统一告警（Unified Alerting）的 Webhook 请求，按标签路由并转发到指定会话。

## 功能

- 通过 AstrBot 现有 HTTP 服务接收 Webhook，无需额外端口。
- 按 Grafana 标签把告警路由到不同会话，支持默认会话兜底。
- 触发与恢复消息模板可配置。
- 支持 severity 过滤、恢复通知、按会话静默。
- 提供 `/grafana test`、`/grafana bind`、`/grafana mute`、`/grafana status` 命令。

## 要求

- AstrBot >= 4.10.4。
- Grafana 9+ 统一告警（Unified Alerting）。
- Grafana 能访问 AstrBot 的 Dashboard 端口（默认 6185）。

## 安装

1. 把插件目录放到 `data/plugins/astrbot_plugin_grafana_alert`。
2. 重启 AstrBot，或在 WebUI 插件页重载。
3. 在 WebUI 插件配置中填写目标会话，或在目标群里执行 `/grafana bind`。

## 创建 API Key

1. 打开 AstrBot WebUI 的「设置」页。
2. 在 API Key 管理区域创建一个 Key。
3. scope 勾选 `plugin`（或使用 `*`）。
4. 复制生成的明文 Key，填入 Grafana。

插件本身不保存 API Key，鉴权由 AstrBot 完成。

## 配置 Grafana Contact Point

Grafana → Alerting → Contact points → 新建 Contact point：

- Integration：Webhook
- URL：

```text
http://<AstrBot 地址>:6185/api/v1/plugins/extensions/astrbot_plugin_grafana_alert/webhook
```

- Method：POST
- Custom header：`X-API-Key: <API Key>`

保存后，在 Notification policies 中把需要转发的告警路由到该 Contact Point。

如果 Grafana 版本不支持自定义请求头，可以把 Key 放在查询参数里：

```text
.../webhook?api_key=<API Key>
```

查询参数会进入访问日志，仅在无法设置请求头时使用。

## 配置项

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | true | 总开关 |
| `default_session` | string | 空 | 未命中路由时的目标会话 UMO |
| `routes` | template_list | 空 | 标签路由规则 |
| `severity_filter` | list | 空 | severity 白名单；空表示全部 |
| `forward_resolved` | bool | true | 是否转发恢复通知 |
| `firing_template` | text | 内置模板 | 触发消息模板 |
| `resolved_template` | text | 内置模板 | 恢复消息模板 |
| `timezone` | string | Asia/Shanghai | 时间显示时区 |
| `max_message_chars` | int | 3000 | 单条消息最大字符数 |

### 路由规则

每条规则包含：

- `name`：规则名，用于 `/grafana bind <规则名>`。
- `match`：`k=v` 形式的标签匹配，逗号分隔；留空匹配全部。
- `session`：目标会话 UMO。

规则按列表顺序匹配，第一条命中的生效。示例：

| name | match | session |
|---|---|---|
| db | `team=db` | `aiocqhttp:GroupMessage:111111` |
| prod | `env=prod,severity=critical` | `aiocqhttp:GroupMessage:222222` |
| fallback | 空 | `aiocqhttp:GroupMessage:333333` |

## 消息模板

模板使用 Python `str.format` 语法，支持以下占位符：

`status`、`alertname`、`severity`、`instance`、`job`、`summary`、`description`、`labels`、`annotations`、`starts_at`、`ends_at`、`generator_url`、`fingerprint`。

缺失字段渲染为空字符串。同一目标的多条告警合并为一条消息，超过 `max_message_chars` 时截断。模板包含未知占位符或属性访问时，插件会记录错误并回退到内置模板。

## 命令

命令需要 AstrBot 管理员权限，推荐在目标群里执行：

| 命令 | 说明 |
|---|---|
| `/grafana test` | 向当前会话发送测试告警 |
| `/grafana bind` | 把当前会话设为默认目标 |
| `/grafana bind <规则名>` | 把当前会话绑定到指定路由规则 |
| `/grafana mute [时长]` | 静默当前会话，默认 1h；支持 30m、2h、7d |
| `/grafana unmute` | 解除当前会话静默 |
| `/grafana status` | 查看配置与静默状态 |

## 手动测试

在能访问 AstrBot 的机器上执行：

```bash
curl -X POST "http://127.0.0.1:6185/api/v1/plugins/extensions/astrbot_plugin_grafana_alert/webhook" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <API Key>" \
  -d '{"status":"firing","alerts":[{"status":"firing","labels":{"alertname":"ManualTest","severity":"warning","instance":"demo"},"annotations":{"summary":"manual test"},"startsAt":"2026-10-21T00:00:00Z","endsAt":"0001-01-01T00:00:00Z","generatorURL":"http://grafana","fingerprint":"manual"}]}'
```

预期响应：

```json
{"status":"ok","alerts":1,"delivered":1,"skipped":0,"failed":0}
```

## 排错

- `401 Invalid API key`：API Key 错误、已撤销或未带在请求头中。
- `403 Insufficient API key scope`：Key 的 scope 不包含 `plugin`。
- `404`：插件未启用、Dashboard 未启动，或插件名/路径不一致。
- `400 invalid payload`：请求体不是 Alertmanager 格式，确认使用的是 Grafana 统一告警。
- 响应 `ok` 但没收到消息：检查 `default_session`/`routes` 是否命中、当前会话是否被静默、平台适配器是否在线。
- 日志关键字：`Grafana webhook processed`、`No platform found for session`、`Invalid firing_template`。

## 安全

- 为插件单独创建 API Key，scope 只勾选 `plugin`，不要复用管理员 Key。
- 不要把 Key 写入公开仓库、截图或日志。
- 优先使用 `X-API-Key` 请求头，避免查询参数进入访问日志。
- 定期轮换 Key。

## 开发与测试

在 AstrBot 仓库根目录执行：

```bash
python data/plugins/astrbot_plugin_grafana_alert/tests/run_tests.py
python -m py_compile data/plugins/astrbot_plugin_grafana_alert/main.py
```

如安装了 pytest，也可以执行：

```bash
python -m pytest data/plugins/astrbot_plugin_grafana_alert/tests -q
```

## 许可证

见 [LICENSE](./LICENSE)。
