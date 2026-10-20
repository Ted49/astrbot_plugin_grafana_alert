"""AstrBot plugin that forwards Grafana alerts to chat sessions."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.config.astrbot_config import AstrBotConfig

from .core import (
    DEFAULT_FIRING_TEMPLATE,
    DEFAULT_RESOLVED_TEMPLATE,
    Alert,
    PayloadError,
    TemplateError,
    format_time,
    is_resolved,
    merge_messages,
    parse_duration,
    parse_payload,
    parse_routes,
    render_alert,
    route_alert,
    severity_allowed,
    validate_routes,
    validate_template,
)

PLUGIN_NAME = "astrbot_plugin_grafana_alert"
WEBHOOK_ROUTE = f"/{PLUGIN_NAME}/webhook"
DEFAULT_TIMEZONE = "Asia/Shanghai"


class GrafanaAlertPlugin(Star):
    """Forward Grafana webhook alerts to configured AstrBot sessions."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self._template_errors: set[str] = set()
        context.register_web_api(
            WEBHOOK_ROUTE,
            self.handle_webhook,
            ["POST"],
            "Receive Grafana alert webhooks",
        )
        self._log_config_warnings()

    def _log_config_warnings(self) -> None:
        """Log invalid templates, routes, and timezone once at startup."""
        for key, default in (
            ("firing_template", DEFAULT_FIRING_TEMPLATE),
            ("resolved_template", DEFAULT_RESOLVED_TEMPLATE),
        ):
            template = self.config.get(key)
            if not isinstance(template, str) or not template.strip():
                self.logger.warning(
                    "Config %s is empty; using the default template.", key
                )
                continue
            for error in validate_template(template):
                self.logger.error("Invalid %s: %s", key, error)
        for warning in validate_routes(parse_routes(self.config.get("routes"))):
            self.logger.warning("%s", warning)
        timezone_name = str(self.config.get("timezone") or DEFAULT_TIMEZONE)
        try:
            ZoneInfo(timezone_name)
        except Exception:
            self.logger.warning(
                "Unknown timezone %r; times fall back to UTC.", timezone_name
            )

    def _template(self, key: str, default: str) -> str:
        """Return the configured template, falling back to the default.

        Args:
            key: Config key holding the template.
            default: Built-in template used when config is empty or invalid.

        Returns:
            A template that passed placeholder validation.
        """
        template = self.config.get(key)
        if not isinstance(template, str) or not template.strip():
            return default
        errors = validate_template(template)
        if errors:
            if key not in self._template_errors:
                self._template_errors.add(key)
                self.logger.error(
                    "Invalid %s (%s); falling back to the default template.",
                    key,
                    "; ".join(errors),
                )
            return default
        self._template_errors.discard(key)
        return template

    async def _mute_until(self, umo: str) -> float:
        """Return the active mute deadline for a session.

        Args:
            umo: Unified message origin of the target session.

        Returns:
            Unix timestamp of the mute deadline, or 0.0 when not muted.
        """
        key = f"mute_until:{umo}"
        value = await self.get_kv_data(key, 0.0)
        if not isinstance(value, (int, float)):
            return 0.0
        if float(value) <= time.time():
            await self.delete_kv_data(key)
            return 0.0
        return float(value)

    async def handle_webhook(self):
        """Receive one Grafana webhook notification and forward its alerts.

        Returns:
            JSON response with alert, delivery, skip, and failure counts.
        """
        if not bool(self.config.get("enabled", True)):
            return json_response({"status": "disabled"})

        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            self.logger.warning("Rejected Grafana webhook: body is not a JSON object.")
            return error_response("invalid payload")

        try:
            alerts = parse_payload(payload)
        except PayloadError as exc:
            self.logger.warning("Rejected Grafana webhook: %s", exc)
            return error_response("invalid payload")

        routes = parse_routes(self.config.get("routes"))
        default_session = str(self.config.get("default_session") or "").strip()
        severity_filter = self.config.get("severity_filter")
        forward_resolved = bool(self.config.get("forward_resolved", True))

        grouped: dict[str, list[Alert]] = {}
        skipped = 0
        for alert in alerts:
            if not severity_allowed(alert, severity_filter):
                skipped += 1
                continue
            if is_resolved(alert) and not forward_resolved:
                skipped += 1
                continue
            target = route_alert(alert, routes, default_session)
            if target is None:
                skipped += 1
                continue
            grouped.setdefault(target, []).append(alert)

        timezone_name = str(self.config.get("timezone") or DEFAULT_TIMEZONE)
        try:
            max_chars = max(200, int(self.config.get("max_message_chars", 3000)))
        except (TypeError, ValueError):
            max_chars = 3000

        messages: list[tuple[str, str]] = []
        now = time.time()
        for target, target_alerts in grouped.items():
            if await self._mute_until(target) > now:
                skipped += len(target_alerts)
                continue
            rendered: list[str] = []
            for alert in target_alerts:
                if is_resolved(alert):
                    template = self._template(
                        "resolved_template", DEFAULT_RESOLVED_TEMPLATE
                    )
                else:
                    template = self._template(
                        "firing_template", DEFAULT_FIRING_TEMPLATE
                    )
                try:
                    rendered.append(render_alert(alert, template, timezone_name))
                except TemplateError as exc:
                    self.logger.error(
                        "Failed to render alert %r: %s", alert.fingerprint, exc
                    )
                    rendered.append(
                        f"[{alert.status}] {alert.labels.get('alertname', 'alert')}"
                    )
            messages.append((target, merge_messages(rendered, max_chars)))

        delivered = 0
        failed = 0
        if messages:
            results = await asyncio.gather(
                *(
                    self.context.send_message(target, MessageChain([Plain(text)]))
                    for target, text in messages
                ),
                return_exceptions=True,
            )
            for (target, _), result in zip(messages, results, strict=True):
                if isinstance(result, BaseException):
                    failed += 1
                    self.logger.error("Failed to send alert to %s: %s", target, result)
                elif result is False:
                    failed += 1
                    self.logger.error("No platform found for session %s", target)
                else:
                    delivered += 1

        self.logger.info(
            "Grafana webhook processed: alerts=%d delivered=%d skipped=%d failed=%d",
            len(alerts),
            delivered,
            skipped,
            failed,
        )
        return json_response(
            {
                "status": "ok",
                "alerts": len(alerts),
                "delivered": delivered,
                "skipped": skipped,
                "failed": failed,
            }
        )

    async def terminate(self):
        """Remove the webhook route when the plugin unloads."""
        self.context.registered_web_apis[:] = [
            item
            for item in self.context.registered_web_apis
            if item[0] != WEBHOOK_ROUTE
        ]

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("grafana")
    async def grafana(
        self,
        event: AstrMessageEvent,
        sub: str = "",
        arg: str = "",
    ):
        """Handle /grafana subcommands.

        Args:
            event: Incoming command event.
            sub: Subcommand name.
            arg: Remaining single argument.

        Yields:
            Plain-text command replies.
        """
        command = (sub or "").strip().lower()
        argument = (arg or "").strip()
        umo = event.unified_msg_origin
        timezone_name = str(self.config.get("timezone") or DEFAULT_TIMEZONE)

        if command == "test":
            sample = Alert(
                status="firing",
                labels={
                    "alertname": "GrafanaWebhookTest",
                    "severity": "warning",
                    "instance": "demo",
                },
                annotations={"summary": "This is a test alert from AstrBot."},
                starts_at=datetime.now(timezone.utc).isoformat(),
            )
            text = render_alert(
                sample,
                self._template("firing_template", DEFAULT_FIRING_TEMPLATE),
                timezone_name,
            )
            try:
                sent = await self.context.send_message(
                    umo,
                    MessageChain([Plain(text)]),
                )
            except Exception as exc:
                self.logger.error("Test alert failed: %s", exc)
                yield event.plain_result(f"测试告警发送失败：{exc}")
                return
            yield event.plain_result(
                "测试告警已发送。" if sent else "测试告警发送失败：未找到对应平台。"
            )
            return

        if command == "bind":
            if argument:
                routes = self.config.get("routes")
                if not isinstance(routes, list):
                    routes = []
                rule = next(
                    (
                        item
                        for item in routes
                        if isinstance(item, dict)
                        and str(item.get("name") or "").strip() == argument
                    ),
                    None,
                )
                if rule is None:
                    yield event.plain_result(f"未找到路由规则：{argument}")
                    return
                rule["session"] = umo
            else:
                self.config["default_session"] = umo
            try:
                self.config.save_config()
            except Exception as exc:
                self.logger.error("Failed to save plugin config: %s", exc)
                yield event.plain_result(f"保存配置失败：{exc}")
                return
            scope = f"路由规则 {argument}" if argument else "默认会话"
            yield event.plain_result(f"已将当前会话绑定为{scope}。")
            return

        if command == "mute":
            seconds = parse_duration(argument) if argument else 3600
            if seconds is None:
                yield event.plain_result(
                    "时长格式无效。示例：/grafana mute 30m、/grafana mute 2h"
                )
                return
            until = time.time() + seconds
            await self.put_kv_data(f"mute_until:{umo}", until)
            deadline = format_time(
                datetime.fromtimestamp(until, timezone.utc).isoformat(),
                timezone_name,
            )
            yield event.plain_result(f"已静默当前会话，解除时间：{deadline}")
            return

        if command == "unmute":
            await self.delete_kv_data(f"mute_until:{umo}")
            yield event.plain_result("已解除当前会话的静默。")
            return

        if command == "status":
            routes = parse_routes(self.config.get("routes"))
            warnings = validate_routes(routes)
            severity_filter = self.config.get("severity_filter")
            if isinstance(severity_filter, list) and severity_filter:
                severity_text = ", ".join(str(item) for item in severity_filter)
            else:
                severity_text = "全部"
            default_session = (
                str(self.config.get("default_session") or "").strip() or "(未设置)"
            )
            mute_until = await self._mute_until(umo)
            if mute_until > 0:
                mute_text = "静默至 " + format_time(
                    datetime.fromtimestamp(mute_until, timezone.utc).isoformat(),
                    timezone_name,
                )
            else:
                mute_text = "未静默"
            lines = [
                "Grafana 告警转发状态",
                f"开关：{'开启' if self.config.get('enabled', True) else '关闭'}",
                f"默认会话：{default_session}",
                f"路由规则：{len(routes)} 条",
                f"severity 过滤：{severity_text}",
                f"转发恢复通知：{'是' if self.config.get('forward_resolved', True) else '否'}",
                f"当前会话：{mute_text}",
            ]
            if warnings:
                lines.append("配置警告：")
                lines.extend(f"- {item}" for item in warnings)
            yield event.plain_result("\n".join(lines))
            return

        yield event.plain_result(
            "Grafana 告警转发命令：\n"
            "/grafana test - 向当前会话发送测试告警\n"
            "/grafana bind [规则名] - 绑定当前会话为默认会话或指定路由\n"
            "/grafana mute [时长] - 静默当前会话，默认 1h\n"
            "/grafana unmute - 解除静默\n"
            "/grafana status - 查看配置与静默状态"
        )
