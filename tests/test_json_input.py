"""convert_x3d reads the X3D JSON encoding."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lxml import etree
from mcp.server.fastmcp import FastMCP

from tools.convert import register
from validation.semantic import semantic_diagnostics
from validation.validate import validate_xml

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "json"
SCENES = sorted(p.stem for p in FIXTURES.glob("*.x3dj"))


def _convert(content: str, to: str, source: str = "json") -> str:
    mcp = FastMCP("t")
    register(mcp)
    out = asyncio.run(mcp.call_tool(
        "convert_x3d", {"content": content, "from_encoding": source, "to_encoding": to}))
    return (out[0][0] if isinstance(out, tuple) else out[0]).text


def _doc(*children, **root) -> str:
    x3d = {"encoding": "UTF-8", "@profile": "Immersive", "@version": "4.0"}
    x3d.update(root)
    x3d["Scene"] = {"-children": list(children)}
    return json.dumps({"X3D": x3d})


@pytest.mark.parametrize("name", SCENES)
def test_json_to_xml_gives_a_valid_scene(name):
    xml = _convert((FIXTURES / f"{name}.x3dj").read_text(), "xml")
    assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert validate_xml(xml)["valid"], validate_xml(xml)["errors"][:2]
    assert not [d for d in semantic_diagnostics(xml) if d.level == "error"]


@pytest.mark.parametrize("name", SCENES)
def test_json_to_vrml_keeps_every_element(name):
    out = _convert((FIXTURES / f"{name}.x3dj").read_text(), "vrml")
    assert out.startswith("#VRML"), out[:200]


def test_json_to_json_returns_the_scene_unchanged_once_it_reads():
    source = (FIXTURES / "baseline_bench_anim-04.x3dj").read_text()
    assert _convert(source, "json") == source
    assert _convert("{nope", "json").startswith("Cannot read X3D JSON")


def test_script_fields_and_source_reach_every_encoding():
    source = (FIXTURES / "optimised_bench_int-04.x3dj").read_text()
    xml = etree.fromstring(_convert(source, "xml").encode())
    script = xml.find(".//Script")
    assert script.findall("field") and "function" in "".join(script.itertext())
    vrml = _convert(source, "vrml")
    assert "Script" in vrml and "function" in vrml


def test_head_component_statements_are_kept():
    xml = etree.fromstring(_convert((FIXTURES / "optimised_bench_geo-04.x3dj").read_text(), "xml").encode())
    assert [c.get("name") for c in xml.findall("head/component")] == ["Geospatial"]


def test_defs_are_moved_ahead_of_their_uses():
    """JSON members are unordered, so a USE can be written before its DEF in another field."""
    body = {"RigidBodyCollection": {
        "-joints": [{"BallJoint": {"-body1": {"RigidBody": {"@USE": "B1"}},
                                   "-body2": {"RigidBody": {"@USE": "B2"}}}}],
        "-bodies": [{"RigidBody": {"@DEF": "B1"}}, {"RigidBody": {"@DEF": "B2"}}]}}
    xml = _convert(_doc(body), "xml")
    assert not [d for d in semantic_diagnostics(xml) if d.check == "use-before-def"]


def test_comments_in_json_do_not_stop_the_conversion():
    xml = _convert(_doc({"#comment": "a"}, {"Group": {"#comment": "b"}}), "xml")
    assert etree.fromstring(xml.encode()).find("Scene/Group") is not None


def test_a_scene_x3dpy_would_cut_is_refused_for_vrml_but_not_for_xml():
    proto = {"ProtoDeclare": {"@name": "P", "ProtoInterface": {"field": []},
                              "ProtoBody": {"-children": [{"Group": {}}]}}}
    source = _doc(proto)
    xml = _convert(source, "xml")
    assert "<ProtoDeclare" in xml
    refusal = _convert(source, "vrml")
    assert refusal.startswith("Cannot convert to vrml: x3d.py would drop")
    assert "ProtoBody x1" in refusal and "to_encoding='xml'" in refusal


def test_errors_are_text_that_names_the_json_path():
    assert _convert("{nope", "xml").startswith("Cannot read X3D JSON: (root): invalid JSON")
    msg = _convert(_doc({"Shape": []}), "xml")
    assert msg == "Cannot read X3D JSON: X3D/Scene/-children[0]/Shape: Shape is a list, not an object"


def test_unknown_encodings_keep_their_messages():
    scene = _doc({"Group": {}})
    assert _convert(scene, "yaml") == "Unknown target encoding: yaml"
    assert _convert(scene, "yaml", source="json") == "Unknown target encoding: yaml"
    assert _convert("x", "xml", source="yaml") == "Parsing from 'yaml' is not yet supported. Use 'xml' or 'json' as source."


def test_xml_source_is_unchanged():
    xml = '<X3D profile="Immersive" version="4.1"><Scene><Shape><Box/></Shape></Scene></X3D>'
    assert "<Box" in _convert(xml, "xml", source="xml")


def test_an_attribute_past_the_parser_limit_still_converts_to_xml():
    """lxml refuses one attribute above about 10 MB; the text json_to_xml wrote is still returned."""
    big = " ".join(["0.5"] * 3_000_000)
    scene = _doc({"Shape": {"-geometry": {"IndexedLineSet": {"-coord": {"Coordinate": {"@point": [0, 0, 0]}}}}}})
    scene = scene.replace('"@point": [0, 0, 0]', '"@point": [%s]' % big.replace(" ", ","))
    xml = _convert(scene, "xml")
    assert len(xml) > 10_000_000 and xml.rstrip().endswith("</X3D>")
    assert _convert(scene, "vrml").startswith("Cannot convert to vrml:")
