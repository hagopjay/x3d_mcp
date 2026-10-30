"""Exact-fix hints (validation.hints): one structured hint per validator error, and fixes that apply."""

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from validation.hints import apply_fix, gate, repair_with_hints, semantic_hints  # noqa: E402
from validation.validate import validate_xml, validate_xml_with_hints  # noqa: E402

BASE = """<?xml version="1.0" encoding="UTF-8"?>
<X3D profile="Interactive" version="4.0"><head><meta name="title" content="t"/></head><Scene>
  <Transform DEF="Spinner"><Shape><Appearance><Material diffuseColor="0 0.4 1"/></Appearance><Box/></Shape></Transform>
  <TimeSensor DEF="Clock" cycleInterval="4" loop="true"/>
  <OrientationInterpolator DEF="Spin" key="0 0.5 1" keyValue="0 1 0 0 0 1 0 3.14159 0 1 0 6.28318"/>
  <ROUTE fromNode="Clock" fromField="fraction_changed" toNode="Spin" toField="set_fraction"/>
  <ROUTE fromNode="Spin" fromField="value_changed" toNode="Spinner" toField="set_rotation"/>
</Scene></X3D>"""
ROUTE1 = '<ROUTE fromNode="Clock" fromField="fraction_changed" toNode="Spin" toField="set_fraction"/>'


def test_base_scene_is_valid():
    assert gate(BASE)["valid"]


# (case name, broken document, expected rule, expected op)
CASES = [
    ("geometry outside its Shape", BASE.replace("<Box/></Shape>", "</Shape><Box/>"), "wrong-parent", "move"),
    ("geometry with no Shape", BASE.replace("<Shape><Appearance><Material diffuseColor=\"0 0.4 1\"/></Appearance><Box/></Shape>",
                                            "<Box/>"), "wrong-parent", "wrap"),
    ("material in the Shape", BASE.replace('<Appearance><Material diffuseColor="0 0.4 1"/></Appearance>',
                                           '<Appearance/><Material diffuseColor="0 0.4 1"/>'), "wrong-parent", "move"),
    ("transform inside a Shape", BASE.replace("<Box/></Shape>", "<Box/><Transform/></Shape>"), "wrong-parent", "move"),
    ("bad enumeration", BASE.replace('profile="Interactive"', 'profile="interactive"'), "bad-enum", "set_attribute"),
    ("missing version", BASE.replace(' version="4.0"', ""), "missing-attribute", "set_attribute"),
    ("missing profile", BASE.replace(' profile="Interactive"', ""), "missing-attribute", "set_attribute"),
    ("colour on 0..255", BASE.replace('"0 0.4 1"', '"0 102 255"'), "colour-out-of-range", "set_attribute"),
    ("negative colour", BASE.replace('"0 0.4 1"', '"0 -0.4 1"'), "colour-out-of-range", "set_attribute"),
    ("ROUTE in a Shape", BASE.replace(ROUTE1, "").replace("<Box/>", "<Box/>" + ROUTE1), "misplaced-route", "move"),
    ("ROUTE in head", BASE.replace(ROUTE1, "").replace("</head>", ROUTE1 + "</head>"), "misplaced-route", "move"),
    ("ROUTE without toField", BASE.replace(' toField="set_fraction"', ""), "missing-attribute", "set_attribute"),
    ("PBR field on Material", BASE.replace("<Material ", '<Material roughness="0.5" '), "unknown-attribute",
     "remove_attribute"),
    ("misspelt field", BASE.replace("diffuseColor", "diffuseColour"), "unknown-attribute", "rename_attribute"),
    ("bool in capitals", BASE.replace('loop="true"', 'loop="TRUE"'), "bad-value", "set_attribute"),
    ("below minimum", BASE.replace('cycleInterval="4"', 'cycleInterval="-4"'), "out-of-range", "set_attribute"),
    ("unknown node, synonym", BASE.replace("<Box/>", "<Cube/>"), "unknown-node", "rename_element"),
    ("unknown node, lower case", BASE.replace("<Box/>", "<box/>"), "unknown-node", "rename_element"),
    ("meta in the Scene", BASE.replace("<Box/>", "<Box/><meta name='a' content='b'/>"), "misplaced-statement", "move"),
    ("no Scene element", BASE.replace("<Scene>", "").replace("</Scene>", ""), "wrong-parent", "wrap"),
]


@pytest.mark.parametrize("name,doc,rule,op", CASES, ids=[c[0] for c in CASES])
def test_each_error_gets_a_hint_whose_fix_makes_the_scene_valid(name, doc, rule, op):
    v = validate_xml_with_hints(doc)
    assert not v["valid"]
    hint = v["hints"][0]
    assert hint["rule"] == rule and hint["suggested_fix"]["op"] == op
    assert hint["error_index"] == 0 and hint["source"] == "xsd" and hint["message"]
    assert gate(apply_fix(doc, hint["suggested_fix"]))["valid"] or repair_with_hints(doc)["valid"]
    assert repair_with_hints(doc)["valid"], name


def test_hint_names_allowed_parents_from_the_verified_table():
    doc = BASE.replace("<Box/></Shape>", "</Shape><Box/>")
    hint = validate_xml_with_hints(doc)["hints"][0]
    assert hint["node"] == "Box" and hint["parent"] == "Transform"
    assert "Shape.geometry" in hint["allowed_parents"]
    assert "Transform.children" not in hint["allowed_parents"]


def test_enumeration_hint_lists_the_allowed_values():
    hint = validate_xml_with_hints(BASE.replace('profile="Interactive"', 'profile="Interactiv"'))["hints"][0]
    assert "Immersive" in hint["allowed_values"] and hint["suggested_fix"]["value"] == "Interactive"


def test_route_field_is_inferred_from_types():
    hint = validate_xml_with_hints(BASE.replace(' toField="set_fraction"', ""))["hints"][0]
    assert hint["suggested_fix"]["value"] == "set_fraction"


def test_validate_xml_output_unchanged_by_default():
    bad = BASE.replace("<Box/>", "<Cube/>")
    assert "hints" not in validate_xml(bad)
    assert "hints" not in validate_xml_with_hints(BASE)


def test_semantic_hints_cover_containerfield_route_and_use_order():
    wrong_cf = BASE.replace("<Box/>", '<Box containerField="children"/>')
    hints = semantic_hints(wrong_cf)
    assert any(h["check"].startswith("containerfield-") and h["suggested_fix"]["value"] == "geometry" for h in hints)
    typo = BASE.replace('toField="set_fraction"', 'toField="set_fractio"')
    h = next(h for h in semantic_hints(typo) if h["rule"] == "route-field")
    assert h["suggested_fix"]["value"] == "set_fraction"
    use_first = BASE.replace('<Box/>', '<Box USE="B"/>').replace(
        "<TimeSensor", '<Shape><Box DEF="B"/></Shape><TimeSensor')
    h = next(h for h in gate(use_first)["hints"] if h["rule"] == "use-before-def")
    assert gate(apply_fix(use_first, h["suggested_fix"]))["valid"]


def test_profile_component_hint_adds_the_component():
    doc = BASE.replace("</Scene>", '<Fog fogType="LINEAR"/></Scene>')
    g = gate(doc)
    h = next(h for h in g["hints"] if h["rule"] == "profile-component")
    assert h["suggested_fix"]["components"] == [{"name": "EnvironmentalEffects", "level": 2}]
    assert gate(apply_fix(doc, h["suggested_fix"]))["valid"]


def test_apply_fix_keeps_the_doctype():
    doc = BASE.replace('<X3D profile', '<!DOCTYPE X3D PUBLIC "ISO//Web3D//DTD X3D 4.0//EN" '
                       '"https://www.web3d.org/specifications/x3d-4.0.dtd">\n<X3D profile', 1)
    fixed = apply_fix(doc.replace('loop="true"', 'loop="TRUE"'),
                      {"op": "set_attribute", "path": "/X3D/Scene/TimeSensor", "attribute": "loop", "value": "true"})
    assert "<!DOCTYPE X3D" in fixed and 'loop="true"' in fixed


def test_long_list_values_do_not_hang_the_pattern_check():
    values = " ".join(["0.5 0.5 1.5"] * 400)
    doc = BASE.replace("<Box/>", f'<IndexedFaceSet coordIndex="0 1 2 -1"><Color color="{values}"/>'
                                 '<Coordinate point="0 0 0 1 0 0 0 1 0"/></IndexedFaceSet>')
    t0 = time.time()
    v = validate_xml_with_hints(doc)
    assert time.time() - t0 < 5 and not v["valid"]


def _call(tool: str, **args) -> str:
    from tools import validate_tool
    mcp = FastMCP("t")
    validate_tool.register(mcp)
    out = asyncio.run(mcp.call_tool(tool, args))
    return (out[0] if isinstance(out, tuple) else out)[0].text


def test_mcp_exact_fix_hints_tool():
    bad = BASE.replace("<Box/></Shape>", "</Shape><Box/>")
    out = json.loads(_call("exact_fix_hints", content=bad))
    assert out["valid"] is False and out["stage"] == "xsd" and out["hints"][0]["rule"] == "wrong-parent"
    fixed = json.loads(_call("exact_fix_hints", content=bad, apply=True))
    assert fixed["valid_after"] is True and fixed["applied"] and gate(fixed["fixed"])["valid"]
    assert json.loads(_call("exact_fix_hints", content=BASE))["valid"] is True


def test_existing_tool_output_is_unchanged():
    bad = BASE.replace("<Box/></Shape>", "</Shape><Box/>")
    assert json.loads(_call("validate_x3d", content=bad)) == validate_xml(bad)


def test_route_before_def_hint_moves_the_route_and_repair_passes():
    doc = BASE.replace(ROUTE1 + "\n", "").replace("<Scene>\n", "<Scene>\n  " + ROUTE1 + "\n")
    g = gate(doc)
    assert not g["valid"] and any("route-before-def" in e for e in g["errors"])
    hint = next(h for h in g["hints"] if h["check"] == "route-before-def")
    assert hint["suggested_fix"]["op"] == "move" and hint["suggested_fix"]["to_path"] == "/X3D/Scene"
    assert repair_with_hints(doc)["valid"]


def test_colour_hint_for_a_value_the_xsd_lets_through():
    doc = BASE.replace('<Box/></Shape></Transform>',
                       '<Box/></Shape></Transform><Background skyColor="0 0 0 -0.2 0.5 0.5" skyAngle="1"/>')
    assert validate_xml(doc)["valid"]             # the MFColor pattern allows the minus sign
    g = gate(doc)
    assert not g["valid"] and any("colour-out-of-range" in e for e in g["errors"])
    hint = next(h for h in g["hints"] if h["check"] == "colour-out-of-range")
    assert hint["suggested_fix"]["value"] == "0 0 0 0 0.5 0.5"
    assert repair_with_hints(doc)["valid"]
