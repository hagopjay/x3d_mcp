"""Corrections applied in memory to the bundled X3D 4.0 JSON Schema."""

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from validation.validate import (
    JSON_SCHEMA_PATH, _fix_navigationinfo_type, load_json_schema, validate_json,
)

# SHA-1 of the upstream file as published; the fixes must not edit it.
UPSTREAM_SHA1 = "c6765a4cbc75fecf0cb843b7c828fda8a17a176b"


def _scene(nav_type) -> str:
    return json.dumps({"X3D": {
        "encoding": "UTF-8", "@profile": "Interchange", "@version": "4.0",
        "Scene": {"-children": [{"NavigationInfo": {"@type": nav_type}}]},
    }})


def test_navigationinfo_type_accepts_the_mfstring_array():
    result = validate_json(_scene(["EXAMINE", "ANY"]))
    assert result["valid"] is True, result["errors"][:3]


def test_navigationinfo_type_rejects_a_bare_string():
    result = validate_json(_scene("EXAMINE"))
    assert result["valid"] is False
    assert any("@type" in e for e in result["errors"])


def test_navigationinfo_type_items_must_be_strings():
    assert validate_json(_scene(["EXAMINE", 3]))["valid"] is False


def test_bundled_schema_file_is_untouched():
    assert hashlib.sha1(JSON_SCHEMA_PATH.read_bytes()).hexdigest() == UPSTREAM_SHA1
    raw = json.loads(JSON_SCHEMA_PATH.read_text())
    upstream = raw["$defs"]["NavigationInfo"]["oneOf"][1]["properties"]["@type"]
    assert upstream["type"] == "string"                      # the bug is still in the file


def test_loaded_schema_carries_the_fix_and_is_a_fresh_copy():
    first = load_json_schema()
    prop = first["$defs"]["NavigationInfo"]["oneOf"][1]["properties"]["@type"]
    assert prop["type"] == "array" and prop["items"] == {"type": "string"}
    prop["type"] = "mutated"
    again = load_json_schema()["$defs"]["NavigationInfo"]["oneOf"][1]["properties"]["@type"]
    assert again["type"] == "array"


def test_fix_is_a_no_op_once_upstream_corrects_it():
    schema = load_json_schema()
    before = json.dumps(schema, sort_keys=True)
    _fix_navigationinfo_type(schema)
    assert json.dumps(schema, sort_keys=True) == before
