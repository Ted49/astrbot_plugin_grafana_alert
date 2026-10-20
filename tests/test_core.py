from core import (
    DEFAULT_FIRING_TEMPLATE,
    DEFAULT_RESOLVED_TEMPLATE,
    Alert,
    PayloadError,
    RouteRule,
    format_time,
    is_resolved,
    merge_messages,
    parse_duration,
    parse_match,
    parse_payload,
    parse_routes,
    render_alert,
    route_alert,
    severity_allowed,
    validate_routes,
    validate_template,
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
    alert = _alert(labels={"alertname": "X"})
    assert severity_allowed(alert, ["critical"]) is False


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


def test_validate_template_accepts_default_templates():
    assert validate_template(DEFAULT_FIRING_TEMPLATE) == []
    assert validate_template(DEFAULT_RESOLVED_TEMPLATE) == []


def test_validate_template_rejects_unknown_and_attribute_access():
    assert validate_template("{unknown}") != []
    assert validate_template("{labels.__class__}") != []
    assert validate_template("{alertname") != []
    assert validate_template("{alertname:bad}") != []


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
