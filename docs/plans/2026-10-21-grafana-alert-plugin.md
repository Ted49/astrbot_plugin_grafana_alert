# Grafana Alert Forwarding Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax (- [ ]) for tracking.

**Goal:** Build the astrbot_plugin_grafana_alert plugin so Grafana Unified Alerting webhooks are parsed, routed by labels, rendered with configurable templates, and forwarded to AstrBot sessions.

**Architecture:** The plugin registers one POST route on AstrBot's existing dashboard HTTP server and relies on AstrBot API-key auth. Pure functions in core.py own payload parsing, routing, filtering, rendering, and truncation; main.py owns AstrBot wiring, KV-backed mute state, command handling, and message delivery.

**Tech Stack:** Python 3.10+ syntax, AstrBot plugin API (Star, context.register_web_api, filter.command), standard library only. No third-party runtime dependencies.

**Spec:** docs/specs/2026-10-21-grafana-alert-plugin-design.md

## Global Constraints

- Python 3.10+ compatible; target AstrBot >= 4.10.4 because the config uses template_list.
- No third-party runtime dependencies; do not add requirements.txt.
- Code comments, docstrings, and log messages are English. User-facing messages and documentation are Chinese.
- Every webhook request must return a response; the handler must not leak uncaught exceptions.
- Line length 88 for Python; keep imports ordered for ruff.
- Work inside the nested plugin repository: git -C data/plugins/astrbot_plugin_grafana_alert ...
- Do not edit data/config/... or any generated runtime config.

## File Map

| File | Responsibility |
|---|---|
| core.py | Dataclasses plus pure parsing, routing, filtering, template, time, and truncation helpers. |
| main.py | Plugin class, webhook route, mute KV state, /grafana commands, message delivery. |
| _conf_schema.json | WebUI configuration schema. |
| metadata.yaml | Plugin metadata; add the AstrBot version floor. |
| README.md | Deployment, Grafana setup, configuration, commands, troubleshooting. |
| tests/conftest.py | Adds the plugin directory to sys.path for pytest. |
| tests/run_tests.py | Dependency-free runner used when pytest is unavailable. |
| tests/test_core.py | Unit tests for core.py. |
| tests/test_schema.py | Validates _conf_schema.json key shape. |

---

### Task 1: Core parsing, routing, and filtering

**Files:**
- Create: data/plugins/astrbot_plugin_grafana_alert/core.py
- Create: data/plugins/astrbot_plugin_grafana_alert/tests/conftest.py
- Create: data/plugins/astrbot_plugin_grafana_alert/tests/run_tests.py
- Create: data/plugins/astrbot_plugin_grafana_alert/tests/test_core.py

**Interfaces:**
- Produces Alert(status, labels, annotations, starts_at, ends_at, generator_url, fingerprint)
- Produces RouteRule(name, match, session)
- Produces PayloadError(ValueError)
- Produces parse_payload(payload) -> list[Alert]
- Produces parse_routes(raw) -> list[RouteRule]
- Produces parse_match(match) -> dict | None
- Produces validate_routes(routes) -> list[str]
- Produces route_alert(alert, routes, default_session) -> str | None
- Produces severity_allowed(alert, severity_filter) -> bool
- Produces is_resolved(alert) -> bool
- Produces parse_duration(value) -> int | None

- [ ] **Step 1: Write the failing tests**

Create tests/conftest.py:

~~~python
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))
~~~

Create tests/run_tests.py:

~~~python
"""Run every test function without requiring pytest."""

import importlib
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent
for path in (str(PLUGIN_DIR), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)


def _load_tests():
    tests = []
    for path in sorted(HERE.glob("test_*.py")):
        module = importlib.import_module(path.stem)
        for name, value in vars(module).items():
            if name.startswith("test_") and callable(value):
                tests.append((f"{path.stem}.{name}", value))
    return tests


def main() -> int:
    failures = 0
    for name, test in _load_tests():
        try:
            test()
        except Exception:
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"PASS {name}")
    return failures


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
~~~

Create tests/test_core.py with these tests:

~~~python
from core import (
    Alert,
    PayloadError,
    RouteRule,
    is_resolved,
    parse_duration,
    parse_match,
    parse_payload,
    parse_routes,
    route_alert,
    severity_allowed,
    validate_routes,
)


def _alert(**overrides):
    values = {
        "status": "firing",
        "labels": {
            "alertname": "HighCPU",
            "severity": "critical",
            "instance": "web-1",
            "team": "web",
        },
        "annotations": {"summary": "CPU usage is high"},
        "starts_at": "2026-10-21T00:00:00Z",
    }
    values.update(overrides)
    return Alert(**values)


def _expect_payload_error(payload):
    try:
        parse_payload(payload)
    except PayloadError:
        return
    raise AssertionError(f"expected PayloadError for {payload!r}")


def test_parse_payload_maps_alertmanager_fields():
    alerts = parse_payload(
        {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "HighCPU", "severity": "critical"},
                    "annotations": {"summary": "CPU high"},
                    "startsAt": "2026-10-21T00:00:00Z",
                    "endsAt": "0001-01-01T00:00:00Z",
                    "generatorURL": "http://grafana/d/abc",
                    "fingerprint": "abc123",
                }
            ],
        }
    )
    assert len(alerts) == 1
    assert alerts[0].status == "firing"
    assert alerts[0].labels["alertname"] == "HighCPU"
    assert alerts[0].annotations["summary"] == "CPU high"
    assert alerts[0].starts_at == "2026-10-21T00:00:00Z"
    assert alerts[0].ends_at == "0001-01-01T00:00:00Z"
    assert alerts[0].generator_url == "http://grafana/d/abc"
    assert alerts[0].fingerprint == "abc123"


def test_parse_payload_skips_non_object_alert_entries():
    alerts = parse_payload({"alerts": ["bad", {"status": "FIRING"}]})
    assert len(alerts) == 1
    assert alerts[0].status == "firing"


def test_parse_payload_rejects_non_object():
    _expect_payload_error([])


def test_parse_payload_rejects_missing_alerts():
    _expect_payload_error({"status": "firing"})


def test_parse_payload_rejects_alerts_not_list():
    _expect_payload_error({"alerts": {}})


def test_parse_match_empty_is_catch_all():
    assert parse_match("") == {}


def test_parse_match_parses_pairs():
    assert parse_match("team=db, env=prod") == {"team": "db", "env": "prod"}


def test_parse_match_rejects_missing_equals():
    assert parse_match("team") is None
    assert parse_match("team=db,env") is None


def test_parse_routes_ignores_non_dict_items():
    routes = parse_routes(
        [
            "bad",
            {
                "name": "db",
                "match": "team=db",
                "session": "umo-db",
                "__template_key": "route",
            },
        ]
    )
    assert routes == [RouteRule(name="db", match="team=db", session="umo-db")]


def test_validate_routes_reports_bad_match_and_empty_session():
    warnings = validate_routes(
        [
            RouteRule(name="db", match="team", session="umo-db"),
            RouteRule(name="empty", match="", session=""),
        ]
    )
    assert any("db" in warning for warning in warnings)
    assert any("empty" in warning for warning in warnings)


def test_route_alert_first_match_wins():
    routes = [
        RouteRule(name="web", match="team=web", session="umo-web"),
        RouteRule(name="all", match="", session="umo-all"),
    ]
    assert route_alert(_alert(), routes, "") == "umo-web"


def test_route_alert_uses_catch_all_rule():
    routes = [RouteRule(name="all", match="", session="umo-all")]
    assert route_alert(_alert(labels={"alertname": "X"}), routes, "") == "umo-all"


def test_route_alert_falls_back_to_default_session():
    routes = [RouteRule(name="db", match="team=db", session="umo-db")]
    assert route_alert(_alert(), routes, " umo-default ") == "umo-default"


def test_route_alert_returns_none_without_target():
    assert route_alert(_alert(), [], "") is None


def test_route_alert_skips_invalid_rules():
    routes = [
        RouteRule(name="bad", match="team", session="umo-bad"),
        RouteRule(name="no-session", match="team=web", session=""),
        RouteRule(name="web", match="team=web", session="umo-web"),
    ]
    assert route_alert(_alert(), routes, "") == "umo-web"


def test_severity_allowed_empty_filter_allows_everything():
    assert severity_allowed(_alert(), []) is True


def test_severity_allowed_is_case_insensitive():
    assert severity_allowed(_alert(), ["CRITICAL"]) is True
    assert severity_allowed(_alert(), [" WARNING "]) is False


def test_severity_allowed_rejects_missing_severity_when_filtered():
    assert severity_allowed(_alert(labels={"alertname": "X"}), ["critical"]) is False


def test_is_resolved_is_case_insensitive():
    assert is_resolved(_alert(status="RESOLVED")) is True
    assert is_resolved(_alert(status="firing")) is False


def test_parse_duration_supports_units():
    assert parse_duration("30") == 30 * 60
    assert parse_duration("90s") == 90
    assert parse_duration("30m") == 30 * 60
    assert parse_duration("2h") == 2 * 3600
    assert parse_duration("7d") == 7 * 86400


def test_parse_duration_rejects_invalid_values():
    assert parse_duration("") is None
    assert parse_duration("0m") is None
    assert parse_duration("31d") is None
    assert parse_duration("abc") is None
~~~

- [ ] **Step 2: Run tests to verify they fail**

Run from the AstrBot repository root:

~~~powershell
python data/plugins/astrbot_plugin_grafana_alert/tests/run_tests.py
~~~

Expected: every test fails with ModuleNotFoundError for core.

- [ ] **Step 3: Write the implementation**

Create core.py:

~~~python
"""Pure parsing, routing, and filtering helpers for Grafana alerts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

MAX_MUTE_SECONDS = 30 * 24 * 60 * 60
_DURATION_RE = re.compile(r"^(\d+)\s*([smhd])?$", re.IGNORECASE)
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class PayloadError(ValueError):
    """Raised when a Grafana webhook payload has an unusable shape."""


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
    """Normalize an Alertmanager-style webhook body into alerts."""
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
    """Convert the routes config list into strongly typed rules."""
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
    """Parse a comma-separated key=value match expression."""
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
    """Return human-readable warnings for invalid route rules."""
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
    """Return the first matching target session for an alert."""
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
    """Check an alert against the configured severity whitelist."""
    if not isinstance(severity_filter, list) or not severity_filter:
        return True
    allowed = {
        str(item).strip().lower()
        for item in severity_filter
        if str(item).strip()
    }
    if not allowed:
        return True
    severity = alert.labels.get("severity", "").strip().lower()
    return bool(severity) and severity in allowed


def is_resolved(alert: Alert) -> bool:
    """Return whether an alert is in the resolved state."""
    return alert.status.strip().lower() == "resolved"


def parse_duration(value: str) -> int | None:
    """Parse a mute duration such as 30, 30m, 2h, or 7d."""
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
~~~

- [ ] **Step 4: Run tests to verify they pass**

~~~powershell
python data/plugins/astrbot_plugin_grafana_alert/tests/run_tests.py
~~~

Expected: every test prints PASS and the command exits 0.

- [ ] **Step 5: Commit**

~~~powershell
git -C data/plugins/astrbot_plugin_grafana_alert add core.py tests
git -C data/plugins/astrbot_plugin_grafana_alert commit -m "feat: parse and route Grafana alerts"
~~~

---

### Task 2: Templates, time formatting, and truncation

**Files:**
- Modify: data/plugins/astrbot_plugin_grafana_alert/core.py
- Modify: data/plugins/astrbot_plugin_grafana_alert/tests/test_core.py

**Interfaces:**
- Produces DEFAULT_FIRING_TEMPLATE and DEFAULT_RESOLVED_TEMPLATE
- Produces TemplateError(ValueError)
- Produces format_labels(values) -> str
- Produces format_time(value, timezone_name) -> str
- Produces render_alert(alert, template, timezone_name) -> str
- Produces validate_template(template) -> list[str]
- Produces merge_messages(messages, max_chars) -> str

- [ ] **Step 1: Append failing tests**

Add these imports to tests/test_core.py:

~~~python
from core import DEFAULT_FIRING_TEMPLATE, DEFAULT_RESOLVED_TEMPLATE
from core import format_time, merge_messages, render_alert, validate_template
~~~

Append these tests:

~~~python
def test_validate_template_accepts_default_templates():
    assert validate_template(DEFAULT_FIRING_TEMPLATE) == []
    assert validate_template(DEFAULT_RESOLVED_TEMPLATE) == []


def test_validate_template_rejects_unknown_and_attribute_access():
    assert validate_template("{unknown}") != []
    assert validate_template("{labels.__class__}") != []
    assert validate_template("{alertname") != []


def test_render_alert_fills_known_fields():
    text = render_alert(
        _alert(),
        "warning {alertname} {severity} {instance} {summary}",
        "Asia/Shanghai",
    )
    assert text == "warning HighCPU critical web-1 CPU usage is high"


def test_render_alert_renders_sorted_labels():
    text = render_alert(_alert(), "{labels}", "Asia/Shanghai")
    assert text == "alertname=HighCPU, instance=web-1, severity=critical, team=web"


def test_render_alert_missing_fields_are_empty():
    alert = _alert(labels={"alertname": "X"}, annotations={})
    assert render_alert(alert, "{summary}|{instance}", "UTC") == "|"


def test_format_time_converts_to_timezone():
    assert format_time("2026-10-21T00:00:00Z", "Asia/Shanghai") == "2026-10-21 08:00:00"


def test_format_time_handles_zero_and_invalid_values():
    assert format_time("0001-01-01T00:00:00Z", "UTC") == ""
    assert format_time("not-a-time", "UTC") == "not-a-time"


def test_merge_messages_joins_and_truncates():
    text = merge_messages(["a" * 150, "b" * 150], 200)
    assert len(text) <= 200
    assert text.endswith("内容已截断）")
    assert merge_messages(["first", "second"], 3000) == "first\n\nsecond"
~~~

- [ ] **Step 2: Run tests to verify they fail**

~~~powershell
python data/plugins/astrbot_plugin_grafana_alert/tests/run_tests.py
~~~

Expected: ImportError for the new template names.

- [ ] **Step 3: Implement templates and rendering**

Replace the core.py import block with:

~~~python
from __future__ import annotations

import re
import string
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo
~~~

Add these constants after the duration constants:

~~~python
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
TRUNCATED_SUFFIX = "\n…（内容已截断）"
_ZERO_TIME_PREFIX = "0001-01-01"
~~~

Add the exception after PayloadError:

~~~python
class TemplateError(ValueError):
    """Raised when a message template cannot be rendered."""
~~~

Append these helpers to core.py:

~~~python
def format_labels(values: dict[str, str]) -> str:
    """Render labels as a sorted key=value string."""
    return ", ".join(f"{key}={values[key]}" for key in sorted(values))


def format_time(value: str, timezone_name: str) -> str:
    """Convert an ISO 8601 timestamp into the configured timezone."""
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
    """Render one alert through a validated template."""
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
    """Check a template against the placeholder whitelist."""
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
            errors.append(
                f"field {field_name!r} may not use attribute or index access"
            )
        elif field_name not in PLACEHOLDER_KEYS:
            errors.append(f"unknown placeholder {field_name!r}")
    return errors


def merge_messages(messages: list[str], max_chars: int) -> str:
    """Join rendered alerts and truncate to a character budget."""
    limit = max(200, int(max_chars))
    text = "\n\n".join(message for message in messages if message)
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(TRUNCATED_SUFFIX))
    return text[:keep].rstrip() + TRUNCATED_SUFFIX
~~~

- [ ] **Step 4: Run tests to verify they pass**

~~~powershell
python data/plugins/astrbot_plugin_grafana_alert/tests/run_tests.py
~~~

Expected: all Task 1 and Task 2 tests print PASS.

- [ ] **Step 5: Commit**

~~~powershell
git -C data/plugins/astrbot_plugin_grafana_alert add core.py tests/test_core.py
git -C data/plugins/astrbot_plugin_grafana_alert commit -m "feat: render Grafana alert templates"
~~~

---

### Task 3: Configuration schema and metadata

**Files:**
- Create: data/plugins/astrbot_plugin_grafana_alert/_conf_schema.json
- Create: data/plugins/astrbot_plugin_grafana_alert/tests/test_schema.py
- Modify: data/plugins/astrbot_plugin_grafana_alert/metadata.yaml

- [ ] **Step 1: Write the failing schema test**

Create tests/test_schema.py:

~~~python
import json
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "_conf_schema.json"

REQUIRED_KEYS = {
    "enabled",
    "default_session",
    "routes",
    "severity_filter",
    "forward_resolved",
    "firing_template",
    "resolved_template",
    "timezone",
    "max_message_chars",
}


def test_schema_is_valid_json_and_has_required_keys():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert REQUIRED_KEYS <= set(schema)
    assert schema["routes"]["type"] == "template_list"
    assert "route" in schema["routes"]["templates"]
    assert schema["severity_filter"]["type"] == "list"
~~~

Run the test runner and confirm it fails because the file is missing.

- [ ] **Step 2: Write the schema**

Create _conf_schema.json with these keys:

~~~json
{
  "enabled": {"description": "总开关", "type": "bool", "default": true},
  "default_session": {
    "description": "默认目标会话",
    "type": "string",
    "default": "",
    "hint": "未命中任何路由规则时使用。可在群里执行 /grafana bind 自动填写。"
  },
  "routes": {
    "description": "标签路由规则",
    "type": "template_list",
    "default": [],
    "hint": "按顺序匹配，第一条命中的规则生效；match 留空表示匹配全部。",
    "templates": {
      "route": {
        "name": "标签路由",
        "hint": "match 形如 team=db,env=prod；session 填目标会话 UMO。",
        "display_item": "name",
        "items": {
          "name": {"description": "规则名称", "type": "string", "default": ""},
          "match": {"description": "标签匹配", "type": "string", "default": ""},
          "session": {"description": "目标会话 UMO", "type": "string", "default": ""}
        }
      }
    }
  },
  "severity_filter": {
    "description": "severity 白名单",
    "type": "list",
    "default": [],
    "hint": "留空表示转发全部；配置后只转发匹配的 severity。"
  },
  "forward_resolved": {"description": "转发恢复通知", "type": "bool", "default": true},
  "firing_template": {
    "description": "触发告警模板",
    "type": "text",
    "default": "🚨 告警触发 | {alertname}\n级别：{severity}\n实例：{instance}\n摘要：{summary}\n开始：{starts_at}\n详情：{generator_url}"
  },
  "resolved_template": {
    "description": "恢复告警模板",
    "type": "text",
    "default": "✅ 告警恢复 | {alertname}\n级别：{severity}\n实例：{instance}\n恢复：{ends_at}\n详情：{generator_url}"
  },
  "timezone": {
    "description": "时间显示时区",
    "type": "string",
    "default": "Asia/Shanghai",
    "hint": "IANA 时区名称，例如 Asia/Shanghai、UTC。"
  },
  "max_message_chars": {
    "description": "单条消息最大字符数",
    "type": "int",
    "default": 3000,
    "slider": {"min": 500, "max": 8000, "step": 100}
  }
}
~~~

- [ ] **Step 3: Declare the AstrBot version floor**

Append to metadata.yaml:

~~~yaml
astrbot_version: ">=4.10.4"
~~~

- [ ] **Step 4: Run tests**

~~~powershell
python data/plugins/astrbot_plugin_grafana_alert/tests/run_tests.py
~~~

Expected: schema test and core tests all PASS.

- [ ] **Step 5: Commit**

~~~powershell
git -C data/plugins/astrbot_plugin_grafana_alert add _conf_schema.json metadata.yaml tests/test_schema.py
git -C data/plugins/astrbot_plugin_grafana_alert commit -m "feat: add plugin configuration schema"
~~~

---

### Task 4: Plugin class, webhook handler, and commands

**Files:**
- Create: data/plugins/astrbot_plugin_grafana_alert/main.py

**Interfaces:**
- Consumes every public helper from Tasks 1 and 2.
- Produces WEBHOOK_ROUTE = "/astrbot_plugin_grafana_alert/webhook".
- Produces GrafanaAlertPlugin(context, config) with handle_webhook and /grafana commands.

- [ ] **Step 1: Write main.py**

Use this structure and behavior:

1. Imports: asyncio, time, datetime/timezone, ZoneInfo, AstrBot event/filter/MessageChain/Plain/Star/Context/web helpers/AstrBotConfig, and the core helpers.
2. Class GrafanaAlertPlugin(Star):
   - __init__ stores config, creates a template-error set, calls context.register_web_api(WEBHOOK_ROUTE, self.handle_webhook, ["POST"], "Receive Grafana alert webhooks"), then logs config warnings.
   - _log_config_warnings validates both templates, routes, and timezone.
   - _template(key, default) validates and falls back to the default, logging once per key.
   - _mute_until(umo) reads KV key mute_until:<umo>, clears expired values, returns the deadline or 0.0.
   - handle_webhook():
     * Return {"status": "disabled"} when enabled is false.
     * Read body with await request.json(default=None); reject non-dict with 400 error_response("invalid payload").
     * parse_payload; catch PayloadError and return the same 400.
     * For each alert: apply severity_allowed, drop resolved when forward_resolved is false, route with route_alert, group by target session, count skipped.
     * For each target: skip when _mute_until(target) is in the future, render each alert with the firing or resolved template, join with merge_messages(max(200, int(config max_message_chars))), collect (target, text).
     * Send with asyncio.gather over context.send_message(target, MessageChain([Plain(text)])), return_exceptions=True. Count delivered, failed (exception or False), log failures.
     * Log one info line and return {"status": "ok", "alerts": N, "delivered": X, "skipped": Y, "failed": Z}.
   - terminate() removes WEBHOOK_ROUTE from context.registered_web_apis.
   - grafana(event, sub="", arg="") decorated with filter.permission_type(PermissionType.ADMIN) and filter.command("grafana"):
     * test: build a sample Alert, render the firing template, send to event.unified_msg_origin, reply success or failure.
     * bind [rule]: set default_session or the first matching route item session, call config.save_config(), reply.
     * mute [duration]: parse_duration or 3600, store mute_until as epoch, reply with formatted deadline.
     * unmute: delete the KV key, reply.
     * status: show enabled, default session, route count, severity filter, forward_resolved, current-session mute state, and route warnings.
     * anything else: reply with usage text.

Key handler loop:

~~~python
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
~~~

Key send block:

~~~python
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
~~~

- [ ] **Step 2: Check syntax**

~~~powershell
python -m py_compile data/plugins/astrbot_plugin_grafana_alert/main.py data/plugins/astrbot_plugin_grafana_alert/core.py
~~~

Expected: exit 0, no output.

- [ ] **Step 3: Run unit tests**

~~~powershell
python data/plugins/astrbot_plugin_grafana_alert/tests/run_tests.py
~~~

Expected: all PASS.

- [ ] **Step 4: Commit**

~~~powershell
git -C data/plugins/astrbot_plugin_grafana_alert add main.py
git -C data/plugins/astrbot_plugin_grafana_alert commit -m "feat: add Grafana webhook handler and commands"
~~~

---

### Task 5: README and end-to-end verification

**Files:**
- Modify: data/plugins/astrbot_plugin_grafana_alert/README.md

- [ ] **Step 1: Replace the template README**

Document, in Chinese:

1. What the plugin does and the four main features.
2. Requirements: AstrBot >= 4.10.4, Grafana 9+ Unified Alerting, Grafana can reach the dashboard port.
3. API key creation: WebUI Settings, scope plugin, copy plaintext once.
4. Grafana contact point: Webhook, POST, URL http://HOST:6185/api/v1/plugins/extensions/astrbot_plugin_grafana_alert/webhook, custom header X-API-Key. Mention the ?api_key= fallback and its logging risk.
5. Configuration table for the nine schema keys.
6. Route rule example table with three rules.
7. Template placeholder list and truncation behavior.
8. Command table.
9. A curl example with a full Alertmanager payload and expected JSON response.
10. Troubleshooting for 401, 403, 404, 400, ok-but-no-message.
11. Security notes: dedicated key, rotation, no query string unless necessary.
12. Local test commands.

- [ ] **Step 2: Run full local verification**

~~~powershell
python data/plugins/astrbot_plugin_grafana_alert/tests/run_tests.py
python -m py_compile data/plugins/astrbot_plugin_grafana_alert/main.py data/plugins/astrbot_plugin_grafana_alert/core.py
~~~

Expected: all tests PASS; compile exits 0.

- [ ] **Step 3: Review the final diff**

~~~powershell
git -C data/plugins/astrbot_plugin_grafana_alert status --short
git -C data/plugins/astrbot_plugin_grafana_alert diff --stat
~~~

Expected: only the plugin files listed in this plan are changed.

- [ ] **Step 4: Commit**

~~~powershell
git -C data/plugins/astrbot_plugin_grafana_alert add README.md
git -C data/plugins/astrbot_plugin_grafana_alert commit -m "docs: document Grafana alert plugin usage"
~~~

---

## Self-Review Notes

- Spec coverage: transport (Task 4), config (Task 3), routing/filtering/mute (Tasks 1 and 4), templates/truncation (Task 2), commands (Task 4), errors/logging (Task 4), tests/docs (Tasks 1, 2, 3, 5).
- Names and signatures are consistent across core.py, main.py, and the tests.
- No third-party dependency is introduced; the test runner works without pytest.
