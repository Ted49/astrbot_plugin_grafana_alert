import asyncio
import importlib
import logging
import sys
import time
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
_PLUGIN = None
_CONTEXT = None


class _Plain:
    def __init__(self, text):
        self.text = text


class _MessageChain:
    def __init__(self, chain):
        self.chain = chain


class _Star:
    def __init__(self, context):
        self.context = context
        self.logger = logging.getLogger("test_grafana")
        self._kv = {}

    async def get_kv_data(self, key, default):
        return self._kv.get(key, default)

    async def put_kv_data(self, key, value):
        self._kv[key] = value

    async def delete_kv_data(self, key):
        self._kv.pop(key, None)


class _Context:
    def __init__(self):
        self.registered_web_apis = []
        self.sent = []

    def register_web_api(self, route, handler, methods, desc):
        self.registered_web_apis.append((route, handler, methods, desc))

    async def send_message(self, session, chain):
        self.sent.append((session, chain))
        return True


class _Response:
    def __init__(self, data, status_code=200):
        self.data = data
        self.status_code = status_code


class _Request:
    def __init__(self):
        self.payload = None

    async def json(self, default=None):
        return self.payload


def _json_response(data=None, *, status_code=200, headers=None):
    return _Response(data, status_code)


def _error_response(message, *, status_code=400, data=None, headers=None):
    return _Response(
        {"status": "error", "message": message, "data": data},
        status_code,
    )


def _passthrough(*args, **kwargs):
    return lambda func: func


class _PermissionType:
    ADMIN = "admin"


def _install_stubs():
    modules = {}
    saved = {}

    def add(name, **attrs):
        module = types.ModuleType(name)
        module.__path__ = []
        for key, value in attrs.items():
            setattr(module, key, value)
        saved[name] = sys.modules.get(name)
        sys.modules[name] = module
        modules[name] = module

    request = _Request()
    add("astrbot")
    add("astrbot.api")
    add(
        "astrbot.api.event",
        AstrMessageEvent=object,
        MessageChain=_MessageChain,
        filter=types.SimpleNamespace(
            command=_passthrough,
            permission_type=_passthrough,
            PermissionType=_PermissionType,
        ),
    )
    add("astrbot.api.message_components", Plain=_Plain)
    add("astrbot.api.star", Context=_Context, Star=_Star)
    add(
        "astrbot.api.web",
        error_response=_error_response,
        json_response=_json_response,
        request=request,
    )
    add("astrbot.core")
    add("astrbot.core.config")
    add("astrbot.core.config.astrbot_config", AstrBotConfig=dict)

    package = types.ModuleType("grafana_plugin_pkg")
    package.__path__ = [str(PLUGIN_DIR)]
    sys.modules["grafana_plugin_pkg"] = package
    main = importlib.import_module("grafana_plugin_pkg.main")
    main.request = request

    for name, original in saved.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original
    return main, request


def _load():
    global _PLUGIN, _CONTEXT
    if _PLUGIN is None:
        _PLUGIN, _CONTEXT = _install_stubs()
    return _PLUGIN, _CONTEXT


def _config(**overrides):
    values = {
        "enabled": True,
        "default_session": "aiocqhttp:GroupMessage:1",
        "routes": [],
        "severity_filter": [],
        "forward_resolved": True,
        "firing_template": "🚨 {alertname}",
        "resolved_template": "✅ {alertname}",
        "timezone": "Asia/Shanghai",
        "max_message_chars": 3000,
    }
    values.update(overrides)
    return values


def _payload(status="firing", severity="critical"):
    return {
        "status": status,
        "alerts": [
            {
                "status": status,
                "labels": {
                    "alertname": "HighCPU",
                    "severity": severity,
                    "instance": "web-1",
                },
                "annotations": {"summary": "CPU high"},
                "startsAt": "2026-10-21T00:00:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://grafana",
                "fingerprint": "abc",
            }
        ],
    }


def test_webhook_forwards_to_default_session():
    main, request = _load()
    context = _Context()
    plugin = main.GrafanaAlertPlugin(context, _config())
    request.payload = _payload()

    response = asyncio.run(plugin.handle_webhook())

    assert response.status_code == 200
    assert response.data == {
        "status": "ok",
        "alerts": 1,
        "delivered": 1,
        "skipped": 0,
        "failed": 0,
    }
    assert len(context.sent) == 1
    target, chain = context.sent[0]
    assert target == "aiocqhttp:GroupMessage:1"
    assert "HighCPU" in chain.chain[0].text


def test_webhook_skips_muted_target():
    main, request = _load()
    context = _Context()
    plugin = main.GrafanaAlertPlugin(context, _config())
    request.payload = _payload()
    asyncio.run(
        plugin.put_kv_data(
            "mute_until:aiocqhttp:GroupMessage:1",
            time.time() + 3600,
        )
    )

    response = asyncio.run(plugin.handle_webhook())

    assert response.data["delivered"] == 0
    assert response.data["skipped"] == 1
    assert context.sent == []


def test_webhook_rejects_invalid_payload():
    main, request = _load()
    plugin = main.GrafanaAlertPlugin(_Context(), _config())
    request.payload = []

    response = asyncio.run(plugin.handle_webhook())

    assert response.status_code == 400
    assert response.data["message"] == "invalid payload"


def test_webhook_respects_disabled_switch():
    main, request = _load()
    plugin = main.GrafanaAlertPlugin(_Context(), _config(enabled=False))
    request.payload = _payload()

    response = asyncio.run(plugin.handle_webhook())

    assert response.data == {"status": "disabled"}


def test_webhook_filters_severity():
    main, request = _load()
    context = _Context()
    plugin = main.GrafanaAlertPlugin(
        context,
        _config(severity_filter=["warning"]),
    )
    request.payload = _payload(severity="critical")

    response = asyncio.run(plugin.handle_webhook())

    assert response.data["skipped"] == 1
    assert context.sent == []
