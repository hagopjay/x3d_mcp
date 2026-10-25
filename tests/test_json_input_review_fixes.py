"""Regressions from the review of the JSON input work: statement values, byte-order
marks, the element budget, names, containerField, VRML size, and blob read limits."""

import asyncio
import json
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lxml import etree
from mcp.server.fastmcp import FastMCP

import tools.convert as convert_mod
import x3d_utils.json2xml as j2x
from tools import convert, validate_tool
from transfer import blobs, tooling
from x3d_utils.json2xml import JsonConversionError, json_to_xml


@pytest.fixture(autouse=True)
def _clean():
    blobs.reset()
    yield
    blobs.reset()


def _call(tool: str, **args) -> str:
    mcp = FastMCP("t")
    convert.register(mcp)
    validate_tool.register(mcp)
    out = asyncio.run(mcp.call_tool(tool, args))
    return (out[0] if isinstance(out, tuple) else out)[0].text


def _scene(*children) -> str:
    return json.dumps({"X3D": {"@profile": "Full", "@version": "4.0", "Scene": {"-children": list(children)}}})


def _field(name, typ, value):
    return {"@name": name, "@type": typ, "@accessType": "initializeOnly", "@value": value}


TYPED = [("SFVec3f", "1 2 3"), ("SFColor", "1 0 0"), ("MFString", '"a" "b"'), ("MFInt32", "1 2 3"),
         ("SFRotation", "0 1 0 1.5"), ("SFBool", "true"), ("SFFloat", "0.5"), ("SFString", "hi")]


# ---- statement values: typed values must neither crash nor be rewritten as Python reprs

@pytest.mark.parametrize("to", ["xml", "vrml"])
def test_json_script_fields_of_any_type_convert(to):
    script = {"Script": {"@DEF": "S", "field": [_field(f"f{i}", t, v) for i, (t, v) in enumerate(TYPED)],
                         "#sourceCode": "x"}}
    out = _call("convert_x3d", content=_scene(script), from_encoding="json", to_encoding=to)
    assert not out.startswith("Cannot") and "Error" not in out[:80], out[:300]
    out = _call("convert_x3d", content=_scene(script), from_encoding="json", to_encoding=to, return_mode="summary")
    assert json.loads(out)["ok"] is True


def _typed_xml(wrapper: str) -> str:
    fields = "".join(f"<field name='f{i}' type='{t}' accessType='initializeOnly' value='{v}'/>"
                     for i, (t, v) in enumerate(TYPED))
    return wrapper.replace("@", fields)


XML_SCRIPT = ("<X3D profile='Full' version='4.0'><Scene><Script DEF='S'>@<![CDATA[x]]></Script></Scene></X3D>")
XML_PROTO = ("<X3D profile='Full' version='4.0'><Scene><ProtoDeclare name='P'><ProtoInterface>@"
             "</ProtoInterface><ProtoBody><Group/></ProtoBody></ProtoDeclare></Scene></X3D>")


@pytest.mark.parametrize("wrapper", [XML_SCRIPT, XML_PROTO], ids=["script", "proto"])
@pytest.mark.parametrize("to", ["xml", "json", "vrml"])
def test_xml_source_with_typed_field_values_does_not_raise(wrapper, to):
    out, ok = convert_mod._convert(_typed_xml(wrapper), "xml", to)
    assert ok, out[:300]


def test_xml_field_values_are_written_back_as_written():
    out, ok = convert_mod._convert(_typed_xml(XML_SCRIPT), "xml", "xml")
    root = etree.fromstring(out.split("?>", 1)[1].split("\n", 1)[1].encode())
    got = {f.get("type"): f.get("value") for f in root.iter("field")}
    assert got == dict(TYPED)


def test_json_script_field_values_survive_to_xml_and_vrml():
    script = {"Script": {"@DEF": "S", "field": [_field("a", "SFVec3f", "1 2 3")], "#sourceCode": "x"}}
    out, ok = convert_mod._convert(_scene(script), "json", "vrml")
    assert ok and "value 1 2 3" in out and "(1.0" not in out


def test_an_x3d_py_refusal_on_xml_source_is_a_message_not_an_exception(monkeypatch):
    def boom(self):
        raise convert_mod.x3d_mod.X3DError("no")
    monkeypatch.setattr(convert_mod.X3D, "VRML", boom)
    out, ok = convert_mod._convert("<X3D profile='Full' version='4.0'><Scene/></X3D>", "xml", "vrml")
    assert not ok and out.startswith("Cannot convert to vrml")


# ---- byte-order mark

def test_a_byte_order_mark_is_ignored():
    text = _scene({"Group": {}})
    assert json_to_xml("﻿" + text) == json_to_xml(text)
    out = _call("convert_x3d", content="﻿" + text, from_encoding="json", to_encoding="xml")
    assert out.startswith("<?xml")


def test_a_stored_blob_with_a_byte_order_mark_converts():
    bid = blobs.get_store().put(b"\xef\xbb\xbf" + _scene({"Group": {}}).encode())["blob_id"]
    out = _call("convert_x3d", content="", blob_id=bid, from_encoding="json", to_encoding="xml")
    assert out.startswith("<?xml")


# ---- element budget is applied before a wide array is collected

def test_the_element_cap_fires_before_a_wide_array_is_walked(monkeypatch):
    monkeypatch.setattr(j2x, "MAX_ELEMENTS", 100)
    calls = []
    real = j2x._check_name
    monkeypatch.setattr(j2x, "_check_name", lambda *a: calls.append(1) or real(*a))
    with pytest.raises(JsonConversionError, match="more than 100 elements"):
        json_to_xml(_scene(*[{"Group": {}}] * 5000))
    assert len(calls) < 50
    calls.clear()
    head = {"X3D": {"head": {"meta": [{"@name": "a", "@content": "b"}] * 5000}, "Scene": {}}}
    with pytest.raises(JsonConversionError, match="more than 100 elements"):
        json_to_xml(json.dumps(head))
    assert len(calls) < 50


def test_a_scene_at_the_cap_is_still_accepted(monkeypatch):
    monkeypatch.setattr(j2x, "MAX_ELEMENTS", 105)
    json_to_xml(_scene(*[{"Group": {}}] * 100))      # X3D + Scene + 100 Groups


# ---- names

@pytest.mark.parametrize("scene", [
    {"X3D": {"Scene": {"-children": [{"Group\n": {}}]}}},
    {"X3D": {"Scene": {"-children": [{"Group": {"@a\n": "x"}}]}}},
    {"X3D": {"Scene": {"-children": [{"Group": {"@DEF": "a", "@DEF\n": "b"}}]}}},
    {"X3D": {"Scene": {"-children": [{"xmlns:foo": {}}]}}},
])
def test_names_with_a_trailing_newline_or_the_xmlns_prefix_are_refused(scene):
    with pytest.raises(JsonConversionError, match="not a valid XML name|namespace prefix"):
        json_to_xml(json.dumps(scene))


def test_an_xmlns_attribute_is_still_accepted():
    assert 'xmlns:foo="u"' in json_to_xml(json.dumps({"X3D": {"Scene": {"-children": [{"Group": {"@xmlns:foo": "u"}}]}}}))


# ---- containerField

def test_containerfield_with_a_field_member_is_refused_even_at_the_default():
    scene = {"X3D": {"Scene": {"-children": [{"Shape": {"-geometry": {"Box": {"@containerField": "appearance"}}}}]}}}
    with pytest.raises(JsonConversionError, match="@containerField is given"):
        json_to_xml(json.dumps(scene))
    scene["X3D"]["Scene"]["-children"][0]["Shape"]["-geometry"]["Box"]["@containerField"] = "geometry"
    with pytest.raises(JsonConversionError, match="@containerField is given"):
        json_to_xml(json.dumps(scene))


# ---- VRML size and offloading

def test_vrml_output_is_capped_on_depth_times_size(monkeypatch):
    inner = [{"Box": {}}] * 50
    for _ in range(60):
        inner = [{"Group": {"-children": inner}}]
    doc = _scene(*inner)
    assert convert_mod._convert(doc, "json", "vrml")[1] is True
    monkeypatch.setattr(convert_mod, "MAX_VRML_BYTES", 10_000)
    out, ok = convert_mod._convert(doc, "json", "vrml")
    assert not ok and "to_encoding='xml'" in out
    assert convert_mod._convert(doc, "json", "xml")[1] is True


def test_the_vrml_estimate_is_close_to_the_real_size():
    inner = [{"Box": {}}] * 500
    for _ in range(100):
        inner = [{"Group": {"-children": inner}}]
    doc = _scene(*inner)
    stats: dict = {}
    xml = json_to_xml(doc, stats=stats)
    real = len(convert_mod._convert(doc, "json", "vrml")[0])
    est = len(xml) + stats["indent_units"] * convert_mod._VRML_BYTES_PER_DEPTH
    assert 0.8 * real <= est <= 1.5 * real, (real, est)


def test_json_conversion_runs_in_a_worker_thread_whatever_its_size(monkeypatch):
    seen = []
    real = convert_mod._convert
    monkeypatch.setattr(convert_mod, "_convert", lambda *a: seen.append(threading.current_thread()) or real(*a))
    _call("convert_x3d", content=_scene({"Group": {}}), from_encoding="json", to_encoding="xml")
    assert seen and seen[0] is not threading.main_thread()


# ---- blob reads: decoded size limit and concurrency

def _config(**kw):
    cfg = replace(blobs.get_config(), **kw)
    blobs.configure(cfg)
    return cfg


def test_a_tool_refuses_a_blob_decoding_over_its_limit_but_blob_read_still_pages_it():
    _config(tool_max_bytes=2000)
    bid = blobs.get_store().put(("<X3D profile='Full'><Scene>" + "<Group/>" * 500 + "</Scene></X3D>").encode())["blob_id"]
    out = json.loads(_call("validate_x3d", blob_id=bid))
    assert "X3D_MCP_TOOL_MAX_BYTES" in out["errors"][0] and "blob_read" in out["errors"][0]
    assert "X3D_MCP_TOOL_MAX_BYTES" in _call("validate_semantic", blob_id=bid)
    assert blobs.get_store().read(bid, 0, 10)


def test_the_limit_is_on_decoded_size_not_stored_size():
    import zstandard
    _config(tool_max_bytes=100_000)
    raw = ("<X3D profile='Full'><Scene>" + "<Group/>" * 100_000 + "</Scene></X3D>").encode()
    bid = blobs.get_store().put(zstandard.ZstdCompressor(level=19).compress(raw), content_encoding="zstd")["blob_id"]
    meta = blobs.get_store().stat(bid)
    assert meta["stored_size"] < 10_000 < 100_000 < meta["size"]
    assert "X3D_MCP_TOOL_MAX_BYTES" in _call("validate_semantic", blob_id=bid)


def test_blob_calls_are_bounded_by_the_concurrency_limit():
    _config(tool_concurrency=2)
    live = peak = 0

    async def one():
        nonlocal live, peak
        async with tooling.blob_slot("a" * 64):
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.05)
            live -= 1

    async def free():
        async with tooling.blob_slot(""):       # no blob: never waits
            return True

    async def main():
        results = await asyncio.gather(*[one() for _ in range(6)], free())
        return results[-1]

    assert asyncio.run(main()) is True
    assert peak == 2


def test_with_blob_slot_keeps_the_tool_schema():
    mcp = FastMCP("t")
    convert.register(mcp)
    tool = asyncio.run(mcp.list_tools())
    schema = next(t for t in tool if t.name == "convert_x3d").inputSchema
    assert list(schema["properties"]) == ["content", "from_encoding", "to_encoding", "blob_id", "return_mode"]
    assert schema["required"] == ["content", "from_encoding", "to_encoding"]
