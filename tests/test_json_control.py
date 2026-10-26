"""The 65-scene round-trip control, when the data is at hand.

X3D_JSON_CONTROL_DIR names a directory with xml/*.x3d (scenes valid as XML) and json/*.x3dj
(the same scenes written by x3d-tidy). The scenes are not part of the repository, so
without the variable (or without the directories) every test here is skipped.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jsonschema import Draft202012Validator
from mcp.server.fastmcp import FastMCP

from tools.convert import _convert, _dropped_elements, _parse_xml_to_model, register
from validation.defuse import reorder_defs_xml
from validation.semantic import semantic_diagnostics
from validation.validate import validate_json, validate_xml
from x3d_utils.gen_schema import build_generation_schema
from x3d_utils.json2xml import json_to_xml

ROOT = os.environ.get("X3D_JSON_CONTROL_DIR", "")
XML = sorted(Path(ROOT, "xml").glob("*.x3d")) if ROOT else []
JSON = sorted(Path(ROOT, "json").glob("*.x3dj")) if ROOT else []

pytestmark = pytest.mark.skipif(not XML or len(XML) != len(JSON),
                                reason="set X3D_JSON_CONTROL_DIR to a directory with xml/ and json/ scene pairs")


def _errors(xml: str) -> list:
    return [d for d in semantic_diagnostics(xml) if d.level == "error"]


@pytest.mark.parametrize("path", JSON, ids=lambda p: p.stem)
def test_json_converts_to_valid_xml_and_vrml_and_back_to_itself(path):
    text = path.read_text()
    xml, ok = _convert(text, "json", "xml")
    assert ok and validate_xml(xml)["valid"] and not _errors(xml)
    vrml, ok = _convert(text, "json", "vrml")
    assert ok and vrml.startswith("#VRML"), vrml[:200]
    assert _convert(text, "json", "json") == (text, True)


@pytest.mark.parametrize("path", XML, ids=lambda p: p.stem)
def test_the_xml_reader_keeps_every_element_and_stays_valid(path):
    xml = path.read_text()
    assert validate_xml(xml)["valid"]
    written = reorder_defs_xml(_parse_xml_to_model(xml).XML())
    assert validate_xml(written)["valid"] and not _errors(written)
    assert _dropped_elements(xml, written) == {}


def test_validate_json_accepts_every_scene_once_the_version_is_pinned_to_4_0():
    rejected = []
    for path in JSON:
        doc = json.loads(path.read_text())
        doc["X3D"]["@version"] = "4.0"              # x3d-tidy stamps 4.1; the bundled schema stops at 4.0
        if not validate_json(json.dumps(doc))["valid"]:
            rejected.append(path.stem)
    assert rejected == []


def test_a_schema_that_names_the_scenes_nodes_accepts_every_scene():
    for profile in ("Interchange", "Interactive", "Immersive"):
        accepted = 0
        for path in JSON:
            doc = json.loads(path.read_text())
            used = sorted({k for k in _node_names(doc["X3D"])})
            schema = build_generation_schema(profile, used, 500, "4.1")["schema"]
            Draft202012Validator.check_schema(schema)
            for member in ("JSON schema", "@xsd:noNamespaceSchemaLocation"):
                doc["X3D"].pop(member, None)
            doc["X3D"]["@profile"] = profile
            accepted += Draft202012Validator(schema).is_valid(doc)
        assert accepted == len(JSON), profile


def _node_names(value):
    """Names of the {NodeName: body} entries in an X3D JSON tree (keys that start with a capital letter)."""
    if isinstance(value, dict):
        for key, inner in value.items():
            if key[:1].isupper() and key not in ("Scene",) and isinstance(inner, dict):
                yield key
            yield from _node_names(inner)
    elif isinstance(value, list):
        for item in value:
            yield from _node_names(item)


def test_the_tool_gives_the_same_xml_as_the_function():
    mcp = FastMCP("t")
    register(mcp)
    text = JSON[0].read_text()
    out = asyncio.run(mcp.call_tool("convert_x3d", {"content": text, "from_encoding": "json", "to_encoding": "xml"}))
    got = (out[0][0] if isinstance(out, tuple) else out[0]).text
    assert got == _convert(text, "json", "xml")[0] and json_to_xml(text).strip() in got.replace("\r", "")
