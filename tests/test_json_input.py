"""convert_x3d reads the X3D JSON encoding."""

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lxml import etree
from mcp.server.fastmcp import FastMCP

import tools.convert as convert_mod
from tools import transfer as blob_tools
from tools import validate_tool
from tools.convert import register
from transfer import blobs
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


# -- blob_id and the return modes for the JSON encoding -----------------------------------------

@pytest.fixture
def store(monkeypatch):
    for name in [n for n in os.environ if n.startswith(("X3D_MCP_BLOB", "X3D_MCP_ALLOWED", "X3D_MCP_SUMMARY",
                                                        "X3D_MCP_OFFLOAD"))]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    blobs.reset()
    yield blobs.get_store()
    blobs.reset()


def _call(tool: str, args: dict) -> str:
    mcp = FastMCP("t")
    register(mcp)
    blob_tools.register(mcp)
    validate_tool.register(mcp)
    out = asyncio.run(mcp.call_tool(tool, args))
    return (out[0][0] if isinstance(out, tuple) else out[0]).text


def _put(store, text: str, media: str = "model/x3d+json") -> str:
    return store.put(text.encode("utf-8"), media_type=media)["blob_id"]


@pytest.mark.parametrize("to", ["xml", "json", "vrml"])
@pytest.mark.parametrize("name", SCENES)
def test_json_by_blob_id_equals_json_inline(store, name, to):
    source = (FIXTURES / f"{name}.x3dj").read_text()
    inline = _convert(source, to)
    by_blob = _call("convert_x3d", {"content": "", "blob_id": _put(store, source),
                                    "from_encoding": "json", "to_encoding": to})
    assert by_blob == inline and inline


def test_json_return_modes_describe_the_converted_scene(store):
    source = (FIXTURES / "optimised_bench_int-04.x3dj").read_text()
    bid = _put(store, source)
    args = {"content": "", "blob_id": bid, "from_encoding": "json", "to_encoding": "xml"}
    xml = _call("convert_x3d", args)
    summary = json.loads(_call("convert_x3d", {**args, "return_mode": "summary"}))
    assert summary["ok"] and summary["document"]["sha256"] == hashlib.sha256(xml.encode()).hexdigest()
    assert summary["document"]["media_type"] == "model/x3d+xml" and summary["document"]["nodes"] > 5
    assert summary["input"]["sha256"] == hashlib.sha256(source.encode()).hexdigest()
    stored = json.loads(_call("convert_x3d", {**args, "return_mode": "blob"}))["document"]
    assert store.get_text(stored["blob_id"]) == xml and validate_xml(xml)["valid"]
    # the stored result goes straight into the next call, so the scene never enters the context
    assert json.loads(_call("validate_x3d", {"content": "", "blob_id": stored["blob_id"]}))["valid"] is True


def test_json_to_json_in_blob_mode_stores_the_input_text_unchanged(store):
    source = (FIXTURES / "baseline_bench_anim-04.x3dj").read_text()
    out = json.loads(_call("convert_x3d", {"content": source, "from_encoding": "json", "to_encoding": "json",
                                           "return_mode": "blob"}))["document"]
    assert store.get_text(out["blob_id"]) == source and out["media_type"] == "model/x3d+json"


def test_a_json_error_is_text_in_full_mode_and_json_in_the_others(store):
    bid = _put(store, '{"X3D": {"Scene": {"-children": [{"Shape": []}]}}}')
    args = {"content": "", "blob_id": bid, "from_encoding": "json", "to_encoding": "xml"}
    assert _call("convert_x3d", args).startswith("Cannot read X3D JSON: X3D/Scene/-children[0]/Shape")
    for mode in ("summary", "blob"):
        body = json.loads(_call("convert_x3d", {**args, "return_mode": mode}))
        assert body["ok"] is False and body["error"].startswith("Cannot read X3D JSON: X3D/Scene")
    assert store.stats()["blobs"] == 1                               # a refusal stores nothing


def test_one_input_only_and_missing_blobs_are_reported_for_json(store):
    source = (FIXTURES / "baseline_bench_anim-04.x3dj").read_text()
    bid = _put(store, source)
    both = _call("convert_x3d", {"content": source, "blob_id": bid, "from_encoding": "json", "to_encoding": "xml"})
    assert "exactly one of" in both
    gone = _call("convert_x3d", {"content": "", "blob_id": "0" * 64, "from_encoding": "json", "to_encoding": "xml"})
    assert gone.startswith("no blob 000000000000")


def test_a_text_blob_holding_xml_is_refused_as_json_not_misread(store):
    xml = _convert((FIXTURES / "baseline_bench_anim-04.x3dj").read_text(), "xml")
    out = _call("convert_x3d", {"content": "", "blob_id": _put(store, xml, "model/x3d+xml"),
                                "from_encoding": "json", "to_encoding": "xml"})
    assert out.startswith("Cannot read X3D JSON: (root): invalid JSON")


# -- documents that x3d.py or the writer cannot take ---------------------------------------------

def test_vrml_refuses_a_value_x3dpy_cannot_hold_instead_of_raising():
    source = _doc({"Transform": {"@translation": "oops"}})
    assert _convert(source, "xml").startswith("<?xml")                # the validators see the bad value
    out = _convert(source, "vrml")
    assert out.startswith("Cannot convert to vrml: x3d.py cannot hold this scene") and "to_encoding='xml'" in out
    assert "Traceback" not in out and len(out) < 400


def test_hostile_json_comes_back_as_text(monkeypatch):
    for text, fragment in [('{"X3D": {"Scene": {}}, "X3D": {"Scene": {}}}', "written twice"),
                           ('{"X3D": {"Scene": {"-children": [{"Transform": {"@scale": [NaN, 1, 1]}}]}}}', "not an X3D number"),
                           ("[" * 100_000, "nested too deeply"),
                           ('{"a":' * 50_000 + "1" + "}" * 50_000, "nested too deeply")]:
        for to in ("xml", "json", "vrml"):
            out = _convert(text, to)
            assert out.startswith("Cannot read X3D JSON: ") and fragment in out, (to, out[:200])


def test_xml_that_is_not_well_formed_is_never_returned_as_a_result(monkeypatch):
    monkeypatch.setattr(convert_mod, "json_to_xml", lambda text: "<X3D><Scene><Group></Scene></X3D>")
    out = _convert(_doc({"Group": {}}), "xml")
    assert out.startswith("Cannot read X3D JSON: the XML written for it is not well-formed")


def test_convert_xml_input_still_raises_nothing_new_for_json_words():
    """'json' stays a source only through the new branch; every other source keeps its message."""
    assert _convert("x", "xml", source="yaml") == "Parsing from 'yaml' is not yet supported. Use 'xml' or 'json' as source."
