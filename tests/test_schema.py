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
