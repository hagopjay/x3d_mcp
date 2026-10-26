"""The generation schema: a small strict subset of the X3D 4.0 JSON Schema."""

import asyncio
import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jsonschema import Draft202012Validator
from lxml import etree
from mcp.server.fastmcp import FastMCP

from tools.convert import register
from validation.profile import profile_nodes
from validation.validate import load_json_schema, validate_xml
from x3d_utils.gen_schema import (
    DEFAULT_MAX_NODES, SchemaRequestError, build_generation_schema,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "json"
OFFICIAL = Draft202012Validator(load_json_schema())


def _scene(*children, profile="Interchange", version="4.0", head=None) -> dict:
    x3d = {"encoding": "UTF-8", "@profile": profile, "@version": version,
           "Scene": {"-children": list(children)}}
    if head is not None:
        x3d["head"] = head
    return {"X3D": x3d}


def _validator(**kw) -> Draft202012Validator:
    return Draft202012Validator(build_generation_schema(**kw)["schema"])


def _objects(schema):
    """Every schema that describes an object."""
    stack = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "properties" in node and isinstance(node["properties"], dict) and node.get("type") == "object":
                yield node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


# -- shape of the schema ---------------------------------------------------------------

@pytest.mark.parametrize("profile", ["Core", "Interchange", "Interactive", "Immersive", "Full"])
def test_schema_is_valid_draft_2020_12(profile):
    built = build_generation_schema(profile)
    Draft202012Validator.check_schema(built["schema"])
    assert built["report"]["size"]["bytes"] == len(json.dumps(built["schema"], separators=(",", ":")))


def test_every_object_is_closed():
    for profile in ("Interchange", "Immersive"):
        schema = build_generation_schema(profile, ["Script"])["schema"]
        assert all(o.get("additionalProperties") is False for o in _objects(schema)), profile


def test_only_decoder_friendly_keywords_are_used():
    keywords = set(build_generation_schema("Immersive")["report"]["keywords"])
    assert keywords <= {"type", "enum", "items", "minItems", "maxItems", "properties", "required",
                        "additionalProperties", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                        "anyOf", "minProperties", "maxProperties", "$ref"}


def test_build_is_deterministic():
    one = json.dumps(build_generation_schema("Interactive", ["GeoLocation"], 60)["schema"])
    two = json.dumps(build_generation_schema("Interactive", ["GeoLocation"], 60)["schema"])
    assert one == two


# -- vocabulary -------------------------------------------------------------------------------

def test_default_schema_offers_the_whole_interchange_profile():
    report = build_generation_schema("Interchange")["report"]
    assert set(report["nodes"]) == profile_nodes()["Interchange"]
    assert report["outside_profile"] == {} and report["dropped_by_max_nodes"] == []
    assert report["size"]["bytes"] < 40_000


def test_profile_limits_the_nodes_and_rejects_the_rest():
    ok = _validator(profile="Interchange")
    assert ok.is_valid(_scene({"Shape": {"-geometry": {"Box": {}}}}))
    assert not ok.is_valid(_scene({"TouchSensor": {}}))              # Interactive
    assert not ok.is_valid(_scene({"Script": {}}))                   # Immersive
    assert not ok.is_valid(_scene({"FakeNode": {}}))
    assert _validator(profile="Interactive").is_valid(_scene({"TouchSensor": {}}, profile="Interactive"))


def test_named_nodes_are_added_and_their_components_reported():
    built = build_generation_schema("Interchange", ["TouchSensor", "NoSuchNode"])
    report = built["report"]
    assert "TouchSensor" in report["nodes"] and report["unknown_nodes"] == ["NoSuchNode"]
    assert report["outside_profile"]["TouchSensor"] == {"component": "PointingDeviceSensor", "level": 1}
    assert report["components_to_declare"] == {"PointingDeviceSensor": 1}


def test_nodes_added_for_a_profile_are_valid_with_their_component_statement():
    head = {"component": [{"@name": "Geospatial", "@level": 1}]}
    geo = {"GeoLocation": {"@geoCoords": [51.5, -0.1, 0], "-children": [{"Shape": {"-geometry": {"Box": {}}}}]}}
    assert _validator(profile="Interchange", nodes=["GeoLocation"]).is_valid(_scene(geo, head=head))
    assert not _validator(profile="Interchange").is_valid(_scene(geo, head=head))


def test_component_names_must_be_real():
    ok = _validator(profile="Interchange")
    assert not ok.is_valid(_scene({"Group": {}}, head={"component": [{"@name": "Appearance", "@level": 1}]}))
    assert ok.is_valid(_scene({"Group": {}}, head={"component": [{"@name": "Navigation", "@level": 2}]}))


def test_profile_and_version_are_pinned():
    ok = _validator(profile="Interchange", version="4.1")
    assert ok.is_valid(_scene({"Group": {}}, version="4.1"))
    assert not ok.is_valid(_scene({"Group": {}}, version="4.0"))
    assert not ok.is_valid(_scene({"Group": {}}, version="4.1", profile="Immersive"))


def test_max_nodes_caps_the_profile_but_never_the_named_nodes():
    capped = build_generation_schema("Immersive", ["Script"], 20)["report"]
    assert capped["node_count"] <= 20 and "Script" in capped["nodes"]
    assert capped["dropped_by_max_nodes"] and "Shape" in capped["nodes"]
    many = build_generation_schema("Immersive", ["Script", "Text", "Fog", "Anchor"], 2)["report"]
    assert {"Script", "Text", "Fog", "Anchor"} <= set(many["nodes"])


def test_companions_come_with_their_node():
    nodes = build_generation_schema("Interchange", ["IndexedFaceSet"], 1)["report"]["nodes"]
    assert {"IndexedFaceSet", "Coordinate"} <= set(nodes)


def test_size_grows_with_the_node_count():
    sizes = [build_generation_schema("Full", None, n)["report"]["size"]["bytes"] for n in (10, 30, 70)]
    assert sizes == sorted(sizes) and sizes[0] < 15_000 and sizes[2] < 60_000


@pytest.mark.parametrize("bad, fragment", [
    (dict(profile="Nope"), "Unknown profile 'Nope'"),
    (dict(version="3.3"), "Unsupported version '3.3'"),
    (dict(max_nodes=0), "max_nodes must be at least 1"),
])
def test_bad_requests_raise(bad, fragment):
    with pytest.raises(SchemaRequestError, match=fragment):
        build_generation_schema(**bad)


# -- fields -----------------------------------------------------------------------------------------

def test_navigationinfo_type_is_an_array_of_strings():
    built = build_generation_schema("Interchange")["schema"]
    nav = built["$defs"]["NavigationInfo"]["anyOf"][1]["properties"]["@type"]
    assert nav["type"] == "array" and nav["items"] == {"type": "string"}
    ok = _validator(profile="Interchange")
    assert ok.is_valid(_scene({"NavigationInfo": {"@type": ["EXAMINE", "ANY"]}}))
    assert not ok.is_valid(_scene({"NavigationInfo": {"@type": "EXAMINE"}}))


def test_use_may_replace_a_node_but_not_sit_beside_fields():
    ok = _validator(profile="Interchange")
    assert ok.is_valid(_scene({"Shape": {"-appearance": {"Appearance": {"-material": {"Material": {"@DEF": "M"}}}}}},
                              {"Shape": {"-appearance": {"Appearance": {"-material": {"Material": {"@USE": "M"}}}}}}))
    assert not ok.is_valid(_scene({"Shape": {"-appearance": {"Appearance": {"-material": {
        "Material": {"@USE": "M", "@shininess": 0.5}}}}}}))


def _shape_with_material(**fields) -> dict:
    return {"Shape": {"-appearance": {"Appearance": {"-material": {"Material": fields}}}}}


def test_vectors_keep_their_length_and_range():
    ok = _validator(profile="Interchange")
    assert ok.is_valid(_scene(_shape_with_material(**{"@diffuseColor": [0.1, 0.2, 0.3]})))
    assert not ok.is_valid(_scene(_shape_with_material(**{"@diffuseColor": [0.1, 0.2]})))
    assert not ok.is_valid(_scene(_shape_with_material(**{"@diffuseColor": [0.1, 0.2, 1.5]})))


def test_bounds_on_tuple_positions_are_kept():
    ok = _validator(profile="Interactive")
    assert ok.is_valid(_scene({"PointLight": {"@attenuation": [1, 0, 0]}}, profile="Interactive"))
    assert not ok.is_valid(_scene({"PointLight": {"@attenuation": [-1, 0, 0]}}, profile="Interactive"))


def test_fontstyle_keeps_its_own_style_field():
    ok = _validator(profile="Immersive", nodes=["FontStyle", "Text"])
    scene = _scene({"Shape": {"-geometry": {"Text": {"-fontStyle": {"FontStyle": {"@style": "BOLD"}}}}}}, profile="Immersive")
    assert ok.is_valid(scene)
    scene["X3D"]["Scene"]["-children"][0]["Shape"]["-geometry"]["Text"]["-fontStyle"]["FontStyle"]["@style"] = "BLINK"
    assert not ok.is_valid(scene)


def test_script_keeps_fields_and_source():
    ok = _validator(profile="Immersive")
    script = {"Script": {"field": [{"@name": "go", "@type": "SFBool", "@accessType": "inputOnly"}],
                         "#sourceCode": ["ecmascript:", "function go(v) {}"]}}
    assert ok.is_valid(_scene(script, profile="Immersive"))
    script["Script"]["field"][0]["@type"] = "SFBogus"
    assert not ok.is_valid(_scene(script, profile="Immersive"))


# -- relation to the official schema ----------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(p.stem for p in FIXTURES.glob("*.x3dj")))
def test_real_scenes_are_accepted_once_their_nodes_are_named(name):
    doc = json.loads((FIXTURES / f"{name}.x3dj").read_text())
    x3d = doc["X3D"]
    for member in ("JSON schema", "@xsd:noNamespaceSchemaLocation"):    # only the file format has them
        x3d.pop(member, None)
    used = set()

    def walk(o):
        if isinstance(o, dict):
            used.update(k for k in o if k[:1].isupper())
            [walk(v) for v in o.values()]
        elif isinstance(o, list):
            [walk(v) for v in o]
    walk(doc)
    schema = _validator(profile="Immersive", nodes=sorted(used), max_nodes=500, version=x3d["@version"])
    x3d["@profile"] = "Immersive"
    errors = [e.message[:100] for e in schema.iter_errors(doc)]
    assert not errors, errors[:2]


class _Sampler:
    """Draws instances from a generation schema."""

    def __init__(self, schema, rng, max_depth=6):
        self.defs, self.rng, self.max_depth = schema["$defs"], rng, max_depth

    def _leaf(self, name):
        node = self.defs[name]
        node = node["anyOf"][1] if "anyOf" in node else node
        return not any("$ref" in p for p in node.get("properties", {}).values())

    def draw(self, spec, depth=0):
        r = self.rng
        if "$ref" in spec:
            return self.draw(self.defs[spec["$ref"].split("/")[-1]], depth)
        if "anyOf" in spec:
            return self.draw(r.choice(spec["anyOf"]), depth)
        if "enum" in spec:
            return r.choice(spec["enum"])
        kind = spec.get("type")
        if kind == "object":
            props, required = spec["properties"], set(spec.get("required", []))
            if spec.get("minProperties") == 1:
                names = [n for n in props if depth < self.max_depth or self._leaf(n)] or list(props)
                name = r.choice(names)
                return {name: self.draw(props[name], depth + 1)}
            return {k: self.draw(p, depth + 1) for k, p in props.items()
                    if k in required or (depth < self.max_depth and r.random() < 0.3)}
        if kind == "array":
            low = spec.get("minItems", 0)
            high = max(low, min(spec.get("maxItems", low + 3), low + 3))
            count = low if depth >= self.max_depth else r.randint(max(low, 1) if high else 0, high)
            return [self.draw(spec.get("items", {"type": "number"}), depth + 1) for _ in range(count)]
        if kind == "string":
            return r.choice(["a", "b c"])
        if kind == "boolean":
            return r.choice([True, False])
        low = spec.get("minimum", spec.get("exclusiveMinimum", -2) + 0.5 if "exclusiveMinimum" in spec else -2)
        high = spec.get("maximum", low + 4)
        return r.randint(int(low) + 1, int(high)) if kind == "integer" else round(r.uniform(low, high), 3) or 0.5


@pytest.mark.parametrize("profile", ["Interchange", "Interactive", "Immersive", "Full"])
def test_instances_of_the_subset_are_valid_against_the_official_schema(profile):
    """Sampled, not proved. The component name TextureProjection is the XSD's; the JSON enum has TextureProjector."""
    built = build_generation_schema(profile, ["Script", "Text", "GeoLocation", "HAnimJoint", "Fog"], 40)
    schema = Draft202012Validator(built["schema"])
    for seed in range(60):
        instance = _Sampler(built["schema"], random.Random(f"{profile}{seed}")).draw(built["schema"])
        assert schema.is_valid(instance), (seed, [e.message[:90] for e in schema.iter_errors(instance)][:2])
        problems = [e.message for e in OFFICIAL.iter_errors(instance) if "TextureProjection" not in e.message]
        assert not problems, (profile, seed, problems[:2])


# -- the tool ---------------------------------------------------------------------------------------------

def _call(name, args):
    mcp = FastMCP("t")
    register(mcp)
    out = asyncio.run(mcp.call_tool(name, args))
    return (out[0][0] if isinstance(out, tuple) else out[0]).text


def test_tool_returns_schema_report_and_notes():
    result = json.loads(_call("x3d_generation_schema", {"profile": "Interchange"}))
    Draft202012Validator.check_schema(result["schema"])
    assert result["report"]["node_count"] == 49 and result["report"]["size"]["bytes"] < 40_000
    assert any("reject large schemas" in n for n in result["notes"])
    assert any("validate_semantic" in n for n in result["notes"])


def test_tool_can_return_the_report_alone():
    full = _call("x3d_generation_schema", {"profile": "Immersive"})
    report_only = _call("x3d_generation_schema", {"profile": "Immersive", "include_schema": False})
    assert "schema" not in json.loads(report_only) and len(report_only) < len(full) // 4
    assert json.loads(report_only)["report"] == json.loads(full)["report"]


def test_tool_mentions_components_unknown_nodes_and_version_limits():
    result = json.loads(_call("x3d_generation_schema", {
        "profile": "Interchange", "nodes": ["GeoLocation", "GaussianSplats"], "version": "4.1",
        "include_schema": False}))
    notes = " ".join(result["notes"])
    assert "Geospatial level 1" in notes and "GaussianSplats" in notes and "@version 4.1" in notes


def test_tool_reports_a_bad_request_as_json():
    assert "Unknown profile" in json.loads(_call("x3d_generation_schema", {"profile": "Nope"}))["error"]


def test_a_scene_the_schema_admits_converts_and_validates():
    schema = build_generation_schema("Interchange")["schema"]
    sampler = _Sampler(schema, random.Random(7), max_depth=3)
    scene = _scene({"Shape": {"-appearance": {"Appearance": {"-material": {"Material": {"@diffuseColor": [1, 0, 0]}}}},
                              "-geometry": {"Sphere": {"@radius": 2}}}},
                   {"Viewpoint": {"@position": [0, 0, 8]}})
    assert Draft202012Validator(schema).is_valid(scene)
    assert OFFICIAL.is_valid(scene)
    xml = _call("convert_x3d", {"content": json.dumps(scene), "from_encoding": "json", "to_encoding": "xml"})
    assert validate_xml(xml)["valid"] and etree.fromstring(xml.encode()).get("profile") == "Interchange"
    assert sampler is not None and DEFAULT_MAX_NODES >= 49


# -- return modes: the schema can stay on the server ------------------------------------------------

@pytest.fixture
def store(monkeypatch):
    import os
    from transfer import blobs
    for name in [n for n in os.environ if n.startswith(("X3D_MCP_BLOB", "X3D_MCP_ALLOWED", "X3D_MCP_SUMMARY",
                                                        "X3D_MCP_OFFLOAD"))]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    blobs.reset()
    yield blobs.get_store()
    blobs.reset()


def test_blob_mode_stores_the_schema_and_leaves_it_out_of_the_reply(store):
    import hashlib
    full = json.loads(_call("x3d_generation_schema", {"profile": "Interactive"}))
    out = json.loads(_call("x3d_generation_schema", {"profile": "Interactive", "return_mode": "blob"}))
    assert out["ok"] and "schema" not in out and out["report"] == full["report"] and out["notes"] == full["notes"]
    doc = out["document"]
    assert doc["media_type"] == "application/schema+json" and doc["bytes"] == full["report"]["size"]["bytes"]
    stored = store.get_text(doc["blob_id"])
    assert json.loads(stored) == full["schema"] and hashlib.sha256(stored.encode()).hexdigest() == doc["sha256"]
    Draft202012Validator.check_schema(json.loads(stored))
    assert len(json.dumps(out)) < len(json.dumps(full)) // 10


def test_summary_mode_gives_the_digest_without_storing(store):
    out = json.loads(_call("x3d_generation_schema", {"profile": "Interchange", "return_mode": "summary"}))
    assert "blob_id" not in out["document"] and out["document"]["bytes"] > 20_000
    assert store.stats()["blobs"] == 0


def test_blob_mode_with_include_schema_false_stores_nothing(store):
    out = json.loads(_call("x3d_generation_schema", {"include_schema": False, "return_mode": "blob"}))
    assert out["ok"] and "document" not in out and store.stats()["blobs"] == 0


def test_bad_requests_are_errors_in_every_mode(store):
    assert "Unknown profile" in json.loads(_call("x3d_generation_schema", {"profile": "Nope"}))["error"]
    for mode in ("summary", "blob"):
        body = json.loads(_call("x3d_generation_schema", {"profile": "Nope", "return_mode": mode}))
        assert body["ok"] is False and "Unknown profile" in body["error"]
    assert "return_mode must be" in json.loads(_call("x3d_generation_schema", {"return_mode": "zip"}))["error"]


def test_default_reply_is_unchanged_by_the_return_modes(store):
    """return_mode defaults to full: report, notes and schema as one compact JSON object."""
    text = _call("x3d_generation_schema", {})
    assert list(json.loads(text)) == ["report", "notes", "schema"] and ": " not in text[:60]


def test_the_tool_signature_gained_only_return_mode_after_the_original_parameters():
    m = FastMCP("sig")
    register(m)
    props = {t.name: t.inputSchema for t in asyncio.run(m.list_tools())}["x3d_generation_schema"]
    assert list(props["properties"]) == ["profile", "nodes", "max_nodes", "version", "include_schema", "return_mode"]
    assert props.get("required", []) == []


# -- hostile requests --------------------------------------------------------------------------------

def test_the_node_list_and_what_the_report_echoes_are_bounded():
    from x3d_utils.gen_schema import MAX_ASKED_NODES
    with pytest.raises(SchemaRequestError, match="at most"):
        build_generation_schema("Interchange", [f"N{i}" for i in range(MAX_ASKED_NODES + 1)])
    with pytest.raises(SchemaRequestError, match="list of node names"):
        build_generation_schema("Interchange", [1, 2])
    report = build_generation_schema("Interchange", ["N" * 100_000, "Box"])["report"]
    assert [len(n) for n in report["unknown_nodes"]] == [60]
    with pytest.raises(SchemaRequestError) as err:
        build_generation_schema("P" * 100_000)
    assert len(str(err.value)) < 300
    big = json.loads(_call("x3d_generation_schema", {"nodes": ["Z" * 5000] * 3 + [f"Q{i}" for i in range(900)],
                                                      "include_schema": False}))
    assert len(json.dumps(big)) < 120_000


@pytest.mark.parametrize("max_nodes", [-5, 0])
def test_a_non_positive_max_nodes_is_a_request_error_in_the_tool(max_nodes):
    assert "max_nodes" in json.loads(_call("x3d_generation_schema", {"max_nodes": max_nodes}))["error"]
