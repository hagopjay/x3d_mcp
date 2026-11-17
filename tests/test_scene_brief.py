"""scene_brief: a budgeted, trust-tiered scene description that never carries text the scene's author wrote.

Fixtures: tests/fixtures/scene_brief (README.md there: the adversarial strings are inert test data).
"""

import asyncio
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path

import pytest
from lxml import etree
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tools import scene_brief as brief_tools
from transfer import blobs
from validation.semantic import Diagnostic, semantic_diagnostics
from validation.validate import validate_xml
from x3d_utils import scene_brief as sb

FIX = Path(__file__).resolve().parent / "fixtures" / "scene_brief"
INJECTION = (FIX / "injection_scene.x3d").read_text(encoding="utf-8")
NO_VALIDATORS = "census,bounds,camera,lights,materials,routes,text"


def scene(body: str, version: str = "4.0", profile: str = "Immersive") -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<X3D profile="{profile}" version="{version}"><Scene>{body}</Scene></X3D>'


def blocks(n: int) -> str:
    return scene("".join(f'<Transform DEF="T{i}" translation="{i} 0 0"><Shape DEF="S{i}"><Appearance>'
                         f'<Material DEF="M{i}" diffuseColor="0.5 0.2 0.3"/></Appearance>'
                         f'<Box size="1 1 1"/></Shape></Transform>\n' for i in range(n)))


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in [n for n in os.environ if n.startswith(("X3D_MCP_BLOB", "X3D_MCP_ALLOWED", "X3D_MCP_SUMMARY", "X3D_MCP_OFFLOAD"))]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    blobs.reset()
    yield
    blobs.reset()


@pytest.fixture(scope="module")
def mcp():
    m = FastMCP("t")
    brief_tools.register(m)
    return m


def call(mcp, **args):
    out = asyncio.run(mcp.call_tool("scene_brief", args))
    content = out[0] if isinstance(out, tuple) else out
    return json.loads(content[0].text), content[0].text


def facets_of(xml, **kw):
    return sb.scene_brief(xml, facets=NO_VALIDATORS.split(","), **kw)["facets"]


def bounds(body, **kw):
    return facets_of(scene(body), **kw)["bounds"]


def body_without_timing(b: dict) -> dict:
    return {k: v for k, v in b.items() if k != "timing_ms"}


# registration ------------------------------------------------------------------------------------

def test_registered_with_optional_inputs_no_output_schema_and_a_short_menu_entry(mcp):
    (tool,) = [t for t in asyncio.run(mcp.list_tools()) if t.name == "scene_brief"]
    props = tool.inputSchema["properties"]
    assert "ctx" not in props and not tool.inputSchema.get("required")
    assert tool.outputSchema is None
    assert len(tool.description.encode()) < 3000


def test_server_registers_the_tool_and_the_instructions_name_it():
    import server
    assert "scene_brief" in {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert "scene_brief" in server.INSTRUCTIONS


# the brief, its determinism and its inputs --------------------------------------------------------

def test_deterministic_and_the_hash_excludes_timing():
    a, b = sb.scene_brief(INJECTION), sb.scene_brief(INJECTION)
    assert a["brief_sha256"] == b["brief_sha256"] and body_without_timing(a) == body_without_timing(b)
    body = {k: v for k, v in a.items() if k not in ("timing_ms", "brief_sha256")}
    assert a["brief_sha256"] == hashlib.sha256(sb._canon(body).encode()).hexdigest()
    assert sb.scene_brief(INJECTION.replace("Crate", "Crate2"))["brief_sha256"] != a["brief_sha256"]


def test_content_path_and_blob_agree(mcp, tmp_path):
    p = tmp_path / "s.x3d"
    p.write_text(INJECTION, encoding="utf-8")
    by_content, _ = call(mcp, content=INJECTION)
    by_path, _ = call(mcp, path=str(p))
    meta = blobs.get_store().put(INJECTION.encode(), media_type="model/x3d+xml", name="s.x3d")
    by_blob, _ = call(mcp, blob_id=meta["blob_id"])
    assert by_content["brief_sha256"] == by_path["brief_sha256"] == by_blob["brief_sha256"]


@pytest.mark.parametrize("args", [{}, {"content": "<X3D/>", "path": "/x"}, {"content": "x", "current_scene": True}])
def test_exactly_one_input_is_required(mcp, args):
    out, _ = call(mcp, **args)
    assert out["ok"] is False and "error" in out


def test_current_scene_equals_the_same_scene_by_content(mcp, monkeypatch):
    from x3d_utils.scene import SceneManager
    sm = SceneManager()
    shape, box = sm.create_node("Shape"), sm.create_node("Box")
    sm.add_child(shape, box)
    monkeypatch.setattr(brief_tools, "_scene_for", lambda ctx: sm)
    xml = sm.to_xml()
    by_scene, _ = call(mcp, current_scene=True, facets=NO_VALIDATORS)
    by_content, _ = call(mcp, content=xml, facets=NO_VALIDATORS)
    assert by_scene["brief_sha256"] == by_content["brief_sha256"]


def test_unknown_facet_names_the_allowed_set(mcp):
    out, _ = call(mcp, content=INJECTION, facets="census,leaks")
    assert out["ok"] is False and out["error"] == "unknown-facet" and out["allowed"] == list(sb.FACETS)


def test_facets_without_validators_carry_no_verdict(mcp):
    out, raw = call(mcp, content=INJECTION, facets=NO_VALIDATORS)
    assert "validators" not in out["facets"] and '"xsd"' not in raw and '"semantic"' not in raw


# the budget ------------------------------------------------------------------------------------

@pytest.mark.parametrize("budget", [1024, 1536, 2048, 4096, 65536])
@pytest.mark.parametrize("xml", ["injection", "blocks"])
def test_budget_is_a_hard_cap(budget, xml):
    text = INJECTION if xml == "injection" else blocks(120)
    b = sb.scene_brief(text, budget_bytes=budget, text_mode="quoted", ids="names")
    raw = sb.render(b)
    assert len(raw.encode()) <= budget and len(raw.encode()) <= b["budget"]["used"] <= budget
    assert json.loads(raw)["ok"] is True
    if budget <= 1536:
        assert b["budget"]["dropped"], "a tight budget must name what it dropped"


@pytest.mark.parametrize("n", [1, 3000])
def test_the_documented_floor_still_holds_with_the_full_input_digest(n):
    # The 64-hex input.sha256_full is part of the fixed answer; the 1,024-byte floor documented in docs/scene-brief.md must stay satisfiable.
    for mode in ("counts", "quoted"):
        b = sb.scene_brief(blocks(n), budget_bytes=sb.MIN_BUDGET, text_mode=mode, ids="names")
        assert len(sb.render(b).encode()) <= sb.MIN_BUDGET and b["input"]["sha256_full"].startswith(b["input"]["sha256"])


def test_a_budget_below_the_floor_is_clamped_and_says_so():
    b = sb.scene_brief(INJECTION, budget_bytes=10)
    assert b["budget"]["bytes"] == sb.MIN_BUDGET == 1024 and b["budget"]["clamped"] is True
    assert len(sb.render(b).encode()) <= sb.MIN_BUDGET


def test_a_budget_nothing_can_fit_in_is_refused_with_the_size_needed(monkeypatch):
    monkeypatch.setattr(sb, "MIN_BUDGET", 64)
    with pytest.raises(sb.BriefError) as e:
        sb.scene_brief(INJECTION, budget_bytes=64)
    assert e.value.code == "budget-too-small" and e.value.extra["min_bytes"] > 64


def test_the_brief_is_flat_in_scene_size():
    small = len(sb.render(sb.scene_brief(blocks(60), facets=NO_VALIDATORS.split(","))).encode())
    large = len(sb.render(sb.scene_brief(blocks(6000), facets=NO_VALIDATORS.split(","))).encode())
    assert large < 1.2 * small and large < 4096


# census and the other measured facets ---------------------------------------------------------------

def test_census_matches_an_independent_lxml_count():
    root = etree.fromstring(INJECTION.encode())
    sc = root.find("Scene")
    els = [e for e in sc.iter() if isinstance(e.tag, str)]
    c = facets_of(INJECTION)["census"]
    assert c["nodes"] == len(els) - 1 - sum(1 for e in els if e.tag == "ROUTE")
    assert c["defs"] == len({e.get("DEF") for e in els if e.get("DEF")})
    assert c["uses"] == sum(1 for e in els if e.get("USE"))


def test_lights_camera_and_materials_flags():
    f = facets_of(scene('<NavigationInfo headlight="false"/><Viewpoint position="1 2 3" fieldOfView="1"/>'
                        '<PointLight/><DirectionalLight/><Shape><Appearance><Material transparency="0.5"/></Appearance><Box/></Shape>'
                        '<Shape><Box/></Shape>'))
    assert f["camera"]["headlight"] is False and f["camera"]["first"]["position"] == [1.0, 2.0, 3.0] and f["camera"]["first"]["fov"] == 1.0
    assert f["lights"]["count"] == 2 and f["lights"]["by_type"] == {"DirectionalLight": 1, "PointLight": 1}
    assert f["materials"] == {"tier": "measured", "shapes": 2, "without_appearance": 1, "material_types": {"Material": 1},
                              "textures": 0, "transparent": 1}
    assert facets_of(scene("<Shape><Box/></Shape>"))["camera"]["headlight"] is True      # no NavigationInfo: the default


def test_routes_facet():
    f = facets_of(scene('<TimeSensor DEF="C"/><Transform DEF="A"/><Transform DEF="B"/><Script DEF="S"><field name="go" type="SFBool" accessType="inputOnly"/></Script>'
                        '<ROUTE fromNode="C" fromField="fraction_changed" toNode="A" toField="set_translation"/>'
                        '<ROUTE fromNode="C" fromField="fraction_changed" toNode="B" toField="set_translation"/>'
                        '<ROUTE fromNode="C" fromField="isActive" toNode="S" toField="go"/>'
                        '<ROUTE fromNode="C" fromField="isActive" toNode="Ghost" toField="x"/>'))["routes"]
    assert f["count"] == 4 and f["dangling"] == 1 and f["script_endpoints"] == 1 and f["max_fan_out"] == 2
    assert f["top_edges"]["TimeSensor.fraction_changed>Transform.set_translation"] == 2
    assert f["top_edges"]["TimeSensor.isActive>Script.?"] == 1       # a Script's own field names are author text


# bounds ------------------------------------------------------------------------------------------

def test_bounds_analytic_cases():
    b = bounds('<Transform translation="1 2 3"><Shape><Box size="2 2 2"/></Shape></Transform>')
    assert (b["min"], b["max"], b["size"], b["center"]) == ([0.0, 1.0, 2.0], [2.0, 3.0, 4.0], [2.0, 2.0, 2.0], [1.0, 2.0, 3.0])
    b = bounds('<Transform scale="2 1 3" translation="10 0 0"><Shape><Box size="1 1 1"/></Shape></Transform>')
    assert (b["min"], b["max"]) == ([9.0, -0.5, -1.5], [11.0, 0.5, 1.5])
    b = bounds(f'<Transform rotation="0 1 0 {math.pi / 2}"><Shape><Box size="4 2 2"/></Shape></Transform>')      # a quarter turn swaps x and z
    assert b["size"] == pytest.approx([2.0, 2.0, 4.0], abs=0.0021)
    b = bounds('<Transform translation="1 0 0"><Transform translation="0 2 0" scale="2 2 2"><Shape><Sphere radius="1"/></Shape></Transform></Transform>')
    assert (b["min"], b["max"]) == ([-1.0, 0.0, -2.0], [3.0, 4.0, 2.0])
    b = bounds('<Transform scale="-1 1 1" translation="5 0 0"><Shape><Box size="2 2 2"/></Shape></Transform>')    # a mirror still gives min <= max
    assert (b["min"], b["max"]) == ([4.0, -1.0, -1.0], [6.0, 1.0, 1.0])
    b = bounds('<Shape><IndexedFaceSet coordIndex="0 1 2 -1"><Coordinate point="0 0 0 4 0 0 0 3 0"/></IndexedFaceSet></Shape>')
    assert (b["min"], b["max"]) == ([0.0, 0.0, 0.0], [4.0, 3.0, 0.0])
    b = bounds('<Transform center="1 0 0" rotation="0 0 1 3.141592653589793"><Shape><Box size="2 2 2"/></Shape></Transform>')
    assert b["center"] == pytest.approx([2.0, 0.0, 0.0], abs=0.001)  # a half turn about a centre one unit out lands the box two units out; outward endpoint quantization can move the displayed centre


def test_bounds_45_degree_turn_is_the_exact_box_of_the_rotated_corners():
    b = bounds(f'<Transform rotation="0 1 0 {math.pi / 4}"><Shape><Box size="2 2 2"/></Shape></Transform>')
    assert 2 * math.sqrt(2) <= b["size"][0] == b["size"][2] < 2 * math.sqrt(2) + 0.0021
    assert b["conservative"] is True and b["static"] is True


def test_bounds_switch_lod_and_use():
    b = bounds('<Switch whichChoice="1"><Shape><Box size="2 2 2"/></Shape><Transform translation="10 0 0"><Shape><Box size="2 2 2"/></Shape></Transform></Switch>')
    assert b["min"][0] == 9.0
    assert bounds('<Switch whichChoice="-1"><Shape><Box/></Shape></Switch>')["bounded_shapes"] == 0
    b = bounds('<LOD range="5"><Shape><Box size="2 2 2"/></Shape><Transform translation="100 0 0"><Shape><Box/></Shape></Transform></LOD>')
    assert b["max"][0] == 1.0                         # the first level only
    b = bounds('<Shape DEF="S"><Box size="2 2 2"/></Shape><Transform translation="5 0 0"><Shape USE="S"/></Transform>')
    assert (b["min"][0], b["max"][0], b["bounded_shapes"]) == (-1.0, 6.0, 2)


def test_bounds_reports_coverage_and_never_guesses():
    b = bounds('<Shape><Text string=\'"hi"\'/></Shape><Inline url=\'"a.x3d"\'/><Shape><Box size="oops"/></Shape><Shape/>'
               '<Transform translation="1 two 3"><Shape><Box/></Shape></Transform>')
    assert b["bounded_shapes"] == 0 and "min" not in b
    assert b["unbounded"] == {"invalid-value": 2, "no-geometry": 1, "unsupported:Inline": 1, "unsupported:Text": 1}
    b = bounds('<Transform bboxSize="1000 1000 1000"><Shape><Box size="2 2 2"/></Shape></Transform>')
    assert b["size"] == [2.0, 2.0, 2.0]               # an authored bbox is a claim, never read as evidence


def test_bounds_hanim_geo_and_collision_proxy():
    b = bounds('<HAnimHumanoid><HAnimJoint translation="3 0 0" name="j"><HAnimSegment><Shape><Box size="2 2 2"/></Shape></HAnimSegment></HAnimJoint></HAnimHumanoid>')
    assert (b["min"][0], b["max"][0]) == (2.0, 4.0)
    b = bounds('<GeoTransform geoCenter="1 2 3"><Shape><Box/></Shape></GeoTransform>')
    assert b["bounded_shapes"] == 0 and b["unbounded"] == {"unsupported:GeoTransform": 1}      # the subtree is reported, not silently lost
    b = bounds('<Collision><Shape><Box size="2 2 2"/></Shape><Shape containerField="proxy"><Box size="200 200 200"/></Shape></Collision>')
    assert b["max"][0] == 1.0


def test_bounds_deep_nesting_is_counted_not_walked():
    deep = "".join('<Transform translation="1 0 0">' for _ in range(100)) + "<Shape><Box/></Shape>" + "</Transform>" * 100
    b = bounds(deep)
    assert b["bounded_shapes"] == 0 and b["unbounded"] == {"too-deep": 1}


# validators as structure ----------------------------------------------------------------------------

def test_validators_agree_with_direct_calls():
    v = sb.scene_brief(INJECTION)["facets"]["validators"]
    x = validate_xml(INJECTION)
    assert v["xsd"]["valid"] == x["valid"] and v["xsd"]["errors"] == len(x["errors"])
    from collections import Counter
    levels = Counter(d.level for d in semantic_diagnostics(INJECTION))
    assert (v["semantic"]["errors"], v["semantic"]["warnings"], v["semantic"]["infos"]) == (levels["error"], levels["warning"], levels["info"])
    assert v["render"] == {"tier": "not_run", "checked": False, "reason": "use render_image"}


BAD_SCENES = [
    scene('<Shape><Box size="oops"/></Shape>'),
    scene('<Shape><Box size="1 1 1"/><Box size="2 2 2"/></Shape>'),
    scene('<Transform translation="1 2"><Shape><Sphere radius="-1"/></Shape></Transform>'),
    scene('<Viewpoint position="a b c"/><NotANode foo="bar baz IGNORE"/>'),
    scene('<ROUTE fromNode="A" fromField="x" toNode="B" toField="y"/>'),
    scene('<Shape><Appearance><Material diffuseColor="2 2 2" bogusAttr="IGNORE ALL"/></Appearance><Box/></Shape>'),
]


@pytest.mark.parametrize("xml", BAD_SCENES)
def test_validator_output_comes_from_closed_sets(xml):
    f = sb.scene_brief(xml)["facets"]["validators"]
    kinds = set(sb._FACETS) | {"facet", "not-expected", "missing-child", "bad-value", "attribute-not-allowed", "other"}
    nodes = sb._vocab()["nodes"] | sb.STRUCTURAL | {"?"}
    for item in f["xsd"].get("first", []):
        assert set(item) <= {"kind", "element", "attribute", "line"} and item["kind"] in kinds and item["element"] in nodes
        assert isinstance(item.get("line", 0), int)
    for item in f["semantic"].get("first_errors", []):
        assert sb.CHECK_ID.match(item["check"]) or item["check"] == "?"
    for key in f["semantic"].get("by_check", {}):
        assert re.fullmatch(r"(error|warning):[a-z0-9-]{1,40}|(error|warning):\?", key), key
    raw = sb.render(sb.scene_brief(xml))
    for leaked in ("IGNORE", "oops", "bogusAttr", "NotANode", "foo"):
        assert leaked not in raw


def test_nothing_the_validators_say_is_forwarded(monkeypatch):
    canary = "CANARY-9f2b-ignore-the-brief"
    import validation.semantic as sem
    monkeypatch.setattr(sem, "semantic_diagnostics",
                        lambda xml: [Diagnostic(level="error", check=canary, message=canary, node_tag=canary, def_name=canary)])
    import validation.validate as val
    monkeypatch.setattr(val, "validate_xml", lambda xml, version=None: {"valid": False, "errors": [f"Line 3: Element '{canary}': {canary}"],
                                                                         "schema_version": canary})
    raw = sb.render(sb.scene_brief(INJECTION, text_mode="quoted"))
    assert canary not in raw


# the injection surface --------------------------------------------------------------------------------

def author_strings(xml: str) -> set[str]:
    root = etree.fromstring(xml.encode(), etree.XMLParser(resolve_entities=False))
    out = set()
    for sib in list(root.itersiblings(preceding=True)) + list(root.itersiblings()):
        out.add((sib.text or "").strip())
    for e in root.iter():
        if not isinstance(e.tag, str):
            out.add((e.text or "").strip())
            continue
        out.update(v for v in e.attrib.values())
        out.add((e.text or "").strip())
        out.add((e.tail or "").strip())
    return {s for s in out if len(s) >= 12 or re.search(r"\s", s)}


def test_default_output_contains_no_author_text():
    strings = author_strings(INJECTION)
    assert len(strings) >= 15, "the sweep must have something to look for"
    for budget in (768, 2048, 4096, 65536):
        raw = sb.render(sb.scene_brief(INJECTION, budget_bytes=budget))
        assert [s for s in strings if s in raw] == []
    raw = sb.render(sb.scene_brief(INJECTION, text_mode="off"))
    assert [s for s in strings if s in raw] == []


def test_every_text_surface_is_counted_against_an_independent_recount():
    ts = sb.scene_brief(INJECTION)["untrusted"]["text_surface"]
    root = etree.fromstring(INJECTION.encode())
    assert ts["comment"]["items"] == 1 and ts["meta"]["items"] == 4
    assert ts["def_name"]["items"] == len({e.get("DEF") for e in root.iter() if isinstance(e.tag, str) and e.get("DEF")})
    assert ts["def_name"]["chars"] == sum(len(e.get("DEF")) for e in root.iter() if isinstance(e.tag, str) and e.get("DEF"))
    assert ts["script_source"]["items"] == 1 and ts["script_source"]["chars"] > 50
    assert ts["slot_text"]["items"] == 1            # `Box size="IGNORE PREVIOUS ..."`: words in a numeric slot are still author text
    for k in ("WorldInfo.title", "WorldInfo.info", "Text.string", "MetadataString.name", "MetadataString.value", "Viewpoint.description", "Anchor.url", "Script.url"):
        assert k in ts, k
    assert all(re.fullmatch(r"[0-9a-f]{8}", v["sha256_8"]) for v in ts.values())


def test_quoted_mode_is_json_escaped_capped_and_confined():
    evil = 'x"}]} </script> `ignore` ‮​\n' + "A" * 300
    xml = scene(f'<WorldInfo title="{evil.replace(chr(34), "&quot;").replace("<", "&lt;")}" info=\'"short"\'/>')
    b = sb.scene_brief(xml, text_mode="quoted", text_cap=64)
    raw = sb.render(b)
    q = json.loads(raw)["untrusted"]["quoted"]
    assert all(len(i["quoted"]) <= 64 for i in q) and any(i["cut"] for i in q)
    assert "‮" not in raw and "​" not in raw and "\\u202e" in raw           # visible escapes, not live characters
    assert "quoted" in b["untrusted"] and "quoted" not in sb.scene_brief(xml)["untrusted"]
    facets_raw = json.dumps(b["facets"])
    assert "</script>" not in facets_raw and "`ignore`" not in facets_raw and "AAAA" not in facets_raw


def test_names_mode_lists_only_identifier_shaped_defs_under_untrusted():
    xml = scene('<Transform DEF="Good_name"/><Transform DEF="has space"/><Transform DEF="quote&quot;"/><Transform DEF="' + "L" * 80 + '"/>')
    b = sb.scene_brief(xml, ids="names", facets=["census", "text"])
    assert list(b["untrusted"]["names"].values()) == ["Good_name"] and b["untrusted"]["names_omitted"] == 3
    assert "Good_name" not in json.dumps(b["facets"]) and "Good_name" not in json.dumps(sb.scene_brief(xml)["untrusted"])


def test_numeric_and_enum_slots_reject_text():
    xml = ('<?xml version="1.0"?><X3D profile="IGNORE-PREVIOUS" version="IGNORE"><Scene><Viewpoint position="1 2 IGNORE"/>'
           '<Transform translation="1 2 IGNORE"><Shape><Box/></Shape></Transform></Scene></X3D>')
    b = sb.scene_brief(xml)
    raw = sb.render(b)
    assert "IGNORE" not in raw
    assert b["input"]["profile"] is None and b["input"]["x3d_version"] is None and b["input"]["declared_invalid"] == ["version", "profile"]
    assert b["facets"]["camera"]["first"]["position"] is None


def test_hostile_xml_is_refused_or_bounded():
    bomb = ('<?xml version="1.0"?><!DOCTYPE X3D [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
            '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]><X3D profile="Immersive" version="4.0"><Scene><WorldInfo title="&c;"/></Scene></X3D>')
    assert len(sb.render(sb.scene_brief(bomb)).encode()) < 4096
    xxe = ('<?xml version="1.0"?><!DOCTYPE X3D [<!ENTITY x SYSTEM "file:///etc/passwd">]><X3D profile="Immersive" version="4.0">'
           '<Scene><WorldInfo title="&x;"/></Scene></X3D>')
    try:
        assert "root:" not in sb.render(sb.scene_brief(xxe))        # an unresolved external entity is refused or left out, never fetched
    except sb.BriefError as e:
        assert e.code == "not-well-formed"
    big = scene('<WorldInfo title="' + "A" * 1_000_000 + '"/>')
    assert len(sb.render(sb.scene_brief(big, facets=["census", "text"])).encode()) < 4096
    deep = '<?xml version="1.0"?><X3D version="4.0"><Scene>' + "<Group>" * 5000 + "</Group>" * 5000 + "</Scene></X3D>"
    try:
        out = sb.scene_brief(deep, facets=["census", "bounds"])
        assert out["ok"] is True
    except sb.BriefError as e:
        assert e.code == "not-well-formed"


@pytest.mark.parametrize("xml,code", [("not xml at all <<", "not-well-formed"), ("<html><body/></html>", "not-x3d"),
                                       ('<X3D version="4.0"><head/></X3D>', "no-scene")])
def test_unreadable_input_returns_a_code_and_a_line_only(mcp, xml, code):
    out, raw = call(mcp, content=xml)
    assert out["ok"] is False and out["error"] == code and set(out) <= {"schema", "ok", "error", "line"}
    assert "html" not in raw.replace("not-x3d", "")


# the tool's modes -------------------------------------------------------------------------------------

def test_return_modes(mcp):
    full, raw = call(mcp, content=INJECTION)
    summary, sraw = call(mcp, content=INJECTION, return_mode="summary")
    assert summary["brief_sha256"] == full["brief_sha256"] and len(sraw.encode()) < 800 and summary["xsd_valid"] is False
    stored, _ = call(mcp, content=INJECTION, return_mode="blob")
    text = blobs.get_store().get_text(stored["brief_blob_id"])
    assert json.loads(text)["brief_sha256"] == full["brief_sha256"] and stored["return_mode"] == "blob"
    bad, _ = call(mcp, content=INJECTION, return_mode="everything")
    assert bad["ok"] is False and "return_mode" in bad["error"]
