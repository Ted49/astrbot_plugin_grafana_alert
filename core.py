"""Pure parsing, routing, and rendering helpers for Grafana alerts."""

from __future__ import annotations

import re
import string
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

MAX_MUTE_SECONDS = 30 * 24 * 60 * 60
_DURATION_RE = re.compile(r"^(\d+)\s*([smhd])?$", re.IGNORECASE)
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_ZERO_TIME_PREFIX = "0001-01-01"
TRUNCATED_SUFFIX = "\n…（内容已截断）"

DEFAULT_FIRING_TEMPLATE = (
    "🚨 告警触发 | {alertname}\n"
    "级别：{severity}\n"
    "实例：{instance}\n"
    "摘要：{summary}\n"
    "开始：{starts_at}\n"
    "详情：{generator_url}"
)
DEFAULT_RESOLVED_TEMPLATE = (
    "✅ 告警恢复 | {alertname}\n"
    "级别：{severity}\n"
    "实例：{instance}\n"
    "恢复：{ends_at}\n"
    "详情：{generator_url}"
)
PLACEHOLDER_KEYS = frozenset(
    {
        "status",
        "alertname",
        "severity",
        "instance",
        "job",
        "summary",
        "description",
        "labels",
        "annotations",
        "starts_at",
        "ends_at",
        "generator_url",
        "fingerprint",
    }
)


class PayloadError(ValueError):
    """Raised when a Grafana webhook payload has an unusable shape."""


class TemplateError(ValueError):
    """Raised when a message template cannot be rendered."""


@dataclass(frozen=True)
class Alert:
    """One alert from an Alertmanager-style Grafana webhook."""

    status: str
    labels: dict[str, str]
    annotations: dict[str, str]
    starts_at: str = ""
    ends_at: str = ""
    generator_url: str = ""
    fingerprint: str = ""


@dataclass(frozen=True)
class RouteRule:
    """One label-based routing rule."""

    name: str
    match: str
    session: str


def parse_payload(payload: Any) -> list[Alert]:
    """Normalize an Alertmanager-style webhook body into alerts.

    Args:
        payload: Parsed JSON body from Grafana.

    Returns:
        Parsed alerts; non-object alert entries are ignored.

    Raises:
        PayloadError: If the body is not an object or alerts is not a list.
    """
    if not isinstance(payload, dict):
        raise PayloadError("payload must be a JSON object")
    raw_alerts = payload.get("alerts")
    if not isinstance(raw_alerts, list):
        raise PayloadError("alerts must be a list")

    alerts: list[Alert] = []
    for item in raw_alerts:
        if not isinstance(item, dict):
            continue
        raw_labels = item.get("labels")
        labels = (
            {str(key): str(value) for key, value in raw_labels.items()}
            if isinstance(raw_labels, dict)
            else {}
        )
        raw_annotations = item.get("annotations")
        annotations = (
            {str(key): str(value) for key, value in raw_annotations.items()}
            if isinstance(raw_annotations, dict)
            else {}
        )
        alerts.append(
            Alert(
                status=str(item.get("status") or "").strip().lower(),
                labels=labels,
                annotations=annotations,
                starts_at=str(item.get("startsAt") or ""),
                ends_at=str(item.get("endsAt") or ""),
                generator_url=str(item.get("generatorURL") or ""),
                fingerprint=str(item.get("fingerprint") or ""),
            )
        )
    return alerts


def parse_routes(raw: Any) -> list[RouteRule]:
    """Convert the routes config list into strongly typed rules.

    Args:
        raw: Raw value from plugin config.

    Returns:
        Parsed rules; non-object entries are ignored.
    """
    if not isinstance(raw, list):
        return []
    routes: list[RouteRule] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        routes.append(
            RouteRule(
                name=str(item.get("name") or "").strip(),
                match=str(item.get("match") or "").strip(),
                session=str(item.get("session") or "").strip(),
            )
        )
    return routes


def parse_match(match: str) -> dict[str, str] | None:
    """Parse a comma-separated key=value match expression.

    Args:
        match: Raw match expression. An empty string is a catch-all rule.

    Returns:
        Parsed pairs, or None when any segment is malformed.
    """
    if not match.strip():
        return {}
    pairs: dict[str, str] = {}
    for part in match.split(","):
        if "=" not in part:
            return None
        key, value = part.split("=", 1)
        key = key.strip()
        if not key:
            return None
        pairs[key] = value.strip()
    return pairs


def validate_routes(routes: list[RouteRule]) -> list[str]:
    """Return human-readable warnings for invalid route rules.

    Args:
        routes: Parsed routing rules.

    Returns:
        Warning strings; an empty list means every rule is usable.
    """
    warnings: list[str] = []
    for route in routes:
        label = route.name or "<unnamed>"
        if not route.session:
            warnings.append(f"route {label}: session is empty")
        if route.match and parse_match(route.match) is None:
            warnings.append(
                f"route {label}: match must be k=v pairs separated by commas"
            )
    return warnings


def route_alert(
    alert: Alert,
    routes: list[RouteRule],
    default_session: str,
) -> str | None:
    """Return the first matching target session for an alert.

    Args:
        alert: Alert whose labels are matched.
        routes: Rules ordered by priority.
        default_session: Fallback UMO when no rule matches.

    Returns:
        Target UMO, or None when no route and no fallback exist.
    """
    for route in routes:
        if not route.session:
            continue
        pairs = parse_match(route.match)
        if pairs is None:
            continue
        if all(alert.labels.get(key) == value for key, value in pairs.items()):
            return route.session
    fallback = default_session.strip()
    return fallback or None


def severity_allowed(alert: Alert, severity_filter: Any) -> bool:
    """Check an alert against the configured severity whitelist.

    Args:
        alert: Alert to check.
        severity_filter: Configured list; empty or non-list means allow all.

    Returns:
        Whether the alert passes the filter.
    """
    if not isinstance(severity_filter, list) or not severity_filter:
        return True
    allowed = {
        str(item).strip().lower() for item in severity_filter if str(item).strip()
    }
    if not allowed:
        return True
    severity = alert.labels.get("severity", "").strip().lower()
    return bool(severity) and severity in allowed


def is_resolved(alert: Alert) -> bool:
    """Return whether an alert is in the resolved state.

    Args:
        alert: Alert to inspect.

    Returns:
        True when the normalized status is resolved.
    """
    return alert.status.strip().lower() == "resolved"


def parse_duration(value: str) -> int | None:
    """Parse a mute duration such as 30, 30m, 2h, or 7d.

    Args:
        value: Duration text. A bare number is interpreted as minutes.

    Returns:
        Duration in seconds, or None when invalid or above 30 days.
    """
    if not isinstance(value, str):
        return None
    matched = _DURATION_RE.fullmatch(value.strip())
    if matched is None:
        return None
    unit = (matched.group(2) or "m").lower()
    seconds = int(matched.group(1)) * _DURATION_UNITS[unit]
    if seconds <= 0 or seconds > MAX_MUTE_SECONDS:
        return None
    return seconds


def format_labels(values: dict[str, str]) -> str:
    """Render labels as a sorted key=value string.

    Args:
        values: Label or annotation mapping.

    Returns:
        Comma-separated pairs sorted by key.
    """
    return ", ".join(f"{key}={values[key]}" for key in sorted(values))


def format_time(value: str, timezone_name: str) -> str:
    """Convert an ISO 8601 timestamp into the configured timezone.

    Args:
        value: Raw timestamp from Grafana.
        timezone_name: IANA timezone name.

    Returns:
        Formatted local time, the raw value when unparsable, or an empty
        string for the Alertmanager zero timestamp.
    """
    if not value or value.startswith(_ZERO_TIME_PREFIX):
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        zone = ZoneInfo(timezone_name)
    except Exception:
        zone = timezone.utc
    return parsed.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")


def render_alert(alert: Alert, template: str, timezone_name: str) -> str:
    """Render one alert through a validated template.

    Args:
        alert: Alert to render.
        template: Template using the documented placeholder whitelist.
        timezone_name: IANA timezone used for timestamps.

    Returns:
        Rendered message text.

    Raises:
        TemplateError: If the template contains an unusable placeholder.
    """
    values = {
        "status": alert.status,
        "alertname": alert.labels.get("alertname", ""),
        "severity": alert.labels.get("severity", ""),
        "instance": alert.labels.get("instance", ""),
        "job": alert.labels.get("job", ""),
        "summary": alert.annotations.get("summary", ""),
        "description": alert.annotations.get("description", ""),
        "labels": format_labels(alert.labels),
        "annotations": format_labels(alert.annotations),
        "starts_at": format_time(alert.starts_at, timezone_name),
        "ends_at": format_time(alert.ends_at, timezone_name),
        "generator_url": alert.generator_url,
        "fingerprint": alert.fingerprint,
    }
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError, AttributeError) as exc:
        raise TemplateError(str(exc)) from exc


def validate_template(template: str) -> list[str]:
    """Check a template against the placeholder whitelist.

    Args:
        template: Candidate message template.

    Returns:
        Error strings; an empty list means the template is usable.
    """
    if not isinstance(template, str) or not template.strip():
        return ["template is empty"]
    errors: list[str] = []
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        return [f"invalid format string: {exc}"]
    for _, field_name, _, _ in parsed:
        if field_name is None:
            continue
        if any(char in field_name for char in ".[]"):
            errors.append(f"field {field_name!r} may not use attribute or index access")
        elif field_name not in PLACEHOLDER_KEYS:
            errors.append(f"unknown placeholder {field_name!r}")
    if errors:
        return errors
    try:
        template.format(**dict.fromkeys(PLACEHOLDER_KEYS, ""))
    except (KeyError, IndexError, ValueError, AttributeError) as exc:
        errors.append(f"cannot render template: {exc}")
    return errors


def merge_messages(messages: list[str], max_chars: int) -> str:
    """Join rendered alerts and truncate the result to a character budget.

    Args:
        messages: Rendered alert messages.
        max_chars: Maximum output length; clamped to at least 200.

    Returns:
        Joined text, with a suffix when truncation was required.
    """
    limit = max(200, int(max_chars))
    text = "\n\n".join(message for message in messages if message)
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(TRUNCATED_SUFFIX))
    return text[:keep].rstrip() + TRUNCATED_SUFFIX
