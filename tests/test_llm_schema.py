"""The node-restricted JSON Schema for constrained LLM decoding (validation.llm_schema)."""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from validation.llm_schema import PRESETS, node_def_names, restricted_schema, schema_report
from validation.validate import load_json_schema, validate_json


def _doc(*children):
    return {"X3D": {"encoding": "UTF-8", "@profile": "Full", "@version": "4.0",
                    "Scene": {"-children": list(children)}}}


def _shape(geometry):
    return {"Shape": {"-geometry": geometry}}


def _valid(schema, doc) -> bool:
    return Draft202012Validator(schema).is_valid(doc)


@pytest.mark.parametrize("lean", [False, True])
def test_vocabulary_is_enforced_and_typing_survives(lean):
    s = restricted_schema(PRESETS["starter"], lean=lean)
    assert _valid(s, _doc(_shape({"Box": {"@size": [1, 2, 3]}}), {"WorldInfo": {"@title": "t"}}))
    assert not _valid(s, _doc(_shape({"Torus": {}})))
    assert not _valid(s, _doc({"PointLight": {}}))                 # valid X3D, outside the vocabulary
    assert validate_json(json.dumps(_doc({"PointLight": {}})))["valid"]
    assert not _valid(s, _doc(_shape({"Box": {"@size": [1, 2]}})))


def test_use_form_comes_with_the_node():
    s = restricted_schema(PRESETS["starter"])
    assert _valid(s, _doc({"Transform": {"@DEF": "T"}}, {"Transform": {"@USE": "T"}}))


def test_lean_drops_class_id_style_and_is_smaller():
    full = restricted_schema(PRESETS["starter"], lean=False)
    lean = restricted_schema(PRESETS["starter"], lean=True)
    assert not _valid(lean, _doc({"Transform": {"@class": "x"}}))
    assert _valid(full, _doc({"Transform": {"@class": "x"}}))
    assert schema_report(lean)["bytes"] < schema_report(full)["bytes"]


def test_unknown_node_names_raise():
    with pytest.raises(ValueError, match="Teapot"):
        restricted_schema(["Box", "Teapot"])


def test_navigationinfo_fix_carries_through():
    s = restricted_schema(PRESETS["starter"])
    assert _valid(s, _doc({"NavigationInfo": {"@type": ["EXAMINE", "ANY"]}}))
    assert not _valid(s, _doc({"NavigationInfo": {"@type": "EXAMINE"}}))


def test_scene_node_count_and_size_budget():
    assert len(node_def_names(load_json_schema())) == 261
    rep = schema_report(restricted_schema(PRESETS["starter"]))
    assert rep["bytes"] < rep["full_bytes"] / 20


def test_presets_name_real_nodes():
    known = set(node_def_names(load_json_schema()))
    for names in PRESETS.values():
        assert set(names) <= known


def test_the_tool_returns_the_schema_and_refuses_an_unbounded_request():
    from tools import validate_tool
    mcp = FastMCP("t")
    validate_tool.register(mcp)

    def call(**args):
        out = asyncio.run(mcp.call_tool("x3d_json_schema", args))
        return json.loads((out[0] if isinstance(out, tuple) else out)[0].text)

    ok = call(preset="starter")
    assert ok["report"]["defs"] == len(ok["schema"]["$defs"]) and _valid(ok["schema"], _doc(_shape({"Box": {}})))
    assert call(nodes="Box,Sphere,WorldInfo")["report"]["bytes"] < ok["report"]["bytes"]
    assert "error" in call()                                       # no vocabulary named
    assert "Teapot" in call(nodes="Teapot")["error"]
