"""X3D JSON encoding to XML: the conversion rules, one test each."""

import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lxml import etree

from validation.defuse import reorder_defs_xml
from validation.semantic import semantic_diagnostics
from validation.validate import validate_xml
from x3d_utils.json2xml import JsonConversionError, json_to_xml

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _doc(*children, head=None, **root) -> str:
    x3d = {"encoding": "UTF-8", "@profile": "Immersive", "@version": "4.0"}
    x3d.update(root)
    if head is not None:
        x3d["head"] = head
    x3d["Scene"] = {"-children": list(children)}
    return json.dumps({"X3D": x3d})


def _tree(text: str) -> etree._Element:
    return etree.fromstring(json_to_xml(text).encode())


# -- attributes --------------------------------------------------------------

def test_attribute_text_encodings():
    root = _tree(_doc({"Material": {
        "@diffuseColor": [0.8, 0.1, 0], "@shininess": 0.25, "@DEF": "M", "@metadata": None}}))
    m = root.find("Scene/Material")
    assert m.get("diffuseColor") == "0.8 0.1 0" and m.get("shininess") == "0.25" and m.get("DEF") == "M"
    assert "metadata" not in m.attrib                           # null members are skipped


def test_booleans_and_nested_arrays_flatten():
    root = _tree(_doc({"Billboard": {"@axisOfRotation": [0, 1, 0]}},
                      {"IndexedFaceSet": {"@solid": False, "@coordIndex": [[0, 1, 2], [-1]]}}))
    assert root.find("Scene/IndexedFaceSet").get("solid") == "false"
    assert root.find("Scene/IndexedFaceSet").get("coordIndex") == "0 1 2 -1"


def test_mfstring_values_are_quoted_and_escaped():
    root = _tree(_doc({"ImageTexture": {"@url": ["a.png", 'b "c".png', "d\\e.png"]}}))
    assert root.find("Scene/ImageTexture").get("url") == '"a.png" "b \\"c\\".png" "d\\\\e.png"'


def test_xml_special_characters_in_attributes_are_escaped():
    root = _tree(_doc({"WorldInfo": {"@title": 'a<b & "c"\n'}}))
    assert root.find("Scene/WorldInfo").get("title") == 'a<b & "c"\n'


# -- root --------------------------------------------------------------------

def test_root_boilerplate_names_the_schema_of_the_declared_version():
    for version in ("4.0", "4.1"):
        text = json_to_xml(_doc(**{"@version": version}))
        assert f"specifications/x3d-{version}.xsd" in text
        assert "xmlns:xsd='http://www.w3.org/2001/XMLSchema-instance'" in text


def test_file_format_members_are_not_carried_over():
    text = json_to_xml(_doc(**{"JSON schema": "https://example.org/s.json",
                               "@xsd:noNamespaceSchemaLocation": "https://example.org/x.xsd"}))
    root = etree.fromstring(text.encode())
    assert "example.org" not in text and root.get("profile") == "Immersive"


# -- containerField ----------------------------------------------------------

def test_container_field_only_when_it_differs_from_the_default():
    root = _tree(_doc({"Shape": {"-appearance": {"Appearance": {
        "-material": {"Material": {}}, "-texture": {"ImageTexture": {"@url": ["a.png"]}}}}}},
        {"Transform": {"-children": [{"Shape": {}}]}}))
    assert root.find("Scene/Shape/Appearance").get("containerField") is None
    assert root.find("Scene/Shape/Appearance/Material").get("containerField") is None
    assert root.find("Scene/Transform/Shape").get("containerField") is None


def test_physical_material_texture_gets_its_container_field():
    root = _tree(_doc({"Shape": {"-appearance": {"Appearance": {"-material": {"PhysicalMaterial": {
        "-baseTexture": {"ImageTexture": {"@url": ["a.png"]}}}}}}}}))
    assert root.find(".//ImageTexture").get("containerField") == "baseTexture"


def test_use_references_keep_their_container_field():
    root = _tree(_doc({"Shape": {"-appearance": {"Appearance": {
        "-material": {"PhysicalMaterial": {"@DEF": "P",
                                           "-baseTexture": {"ImageTexture": {"@DEF": "T", "@url": ["a.png"]}}}}}}}},
        {"Shape": {"-appearance": {"Appearance": {"-material": {"PhysicalMaterial": {
            "-normalTexture": {"ImageTexture": {"@USE": "T"}}}}}}}}))
    assert root.findall(".//ImageTexture")[1].get("containerField") == "normalTexture"


# -- order where the XSD fixes one --------------------------------------------

def test_x3d_children_are_head_then_scene():
    text = json.dumps({"X3D": {"Scene": {"-children": []}, "head": {"meta": [{"@name": "a", "@content": "b"}]},
                               "@profile": "Core", "@version": "4.0", "encoding": "UTF-8"}})
    assert [c.tag for c in etree.fromstring(json_to_xml(text).encode())] == ["head", "Scene"]


def test_head_children_are_component_unit_meta():
    head = {"meta": [{"@name": "a", "@content": "b"}],
            "unit": [{"@category": "angle", "@name": "deg", "@conversionFactor": 0.0174}],
            "component": [{"@name": "Geospatial", "@level": 1}]}
    assert [c.tag for c in _tree(_doc(head=head)).find("head")] == ["component", "unit", "meta"]


def test_is_and_metadata_precede_other_children():
    body = {"-children": [{"Shape": {}}], "-metadata": {"MetadataString": {"@name": "n", "@value": ["v"]}},
            "IS": {"connect": [{"@nodeField": "translation", "@protoField": "t"}]}}
    kids = [c.tag for c in _tree(_doc({"Transform": body})).find("Scene/Transform")]
    assert kids == ["IS", "MetadataString", "Shape"]
    # and the XSD accepts it; written the other way round, it would not
    xml = json_to_xml(_doc({"ProtoDeclare": {
        "@name": "P", "ProtoBody": {"-children": [{"Transform": body}]},
        "ProtoInterface": {"field": [{"@name": "t", "@type": "SFVec3f", "@accessType": "inputOutput"}]}}}))
    assert validate_xml(xml)["valid"], validate_xml(xml)["errors"]


def test_proto_interface_precedes_proto_body():
    text = _doc({"ProtoDeclare": {"@name": "P", "ProtoBody": {"-children": [{"Group": {}}]},
                                  "ProtoInterface": {"field": []}}})
    assert [c.tag for c in _tree(text).find("Scene/ProtoDeclare")] == ["ProtoInterface", "ProtoBody"]


@pytest.mark.parametrize("name", ["Script", "ComposedShader", "ShaderProgram", "PackagedShader"])
def test_fields_precede_is_in_script_and_shaders(name):
    node = {name: {"IS": {"connect": [{"@nodeField": "a", "@protoField": "p"}]},
                   "field": [{"@name": "a", "@type": "SFFloat", "@accessType": "inputOutput"}]}}
    assert [c.tag for c in _tree(_doc(node)).find(f"Scene/{name}")] == ["field", "IS"]


def test_script_with_is_and_field_is_valid_against_the_xsd():
    node = {"Script": {"IS": {"connect": [{"@nodeField": "a", "@protoField": "p"}]},
                       "field": [{"@name": "a", "@type": "SFFloat", "@accessType": "inputOutput"}]}}
    xml = json_to_xml(_doc({"ProtoDeclare": {
        "@name": "P", "ProtoInterface": {"field": [{"@name": "p", "@type": "SFFloat", "@accessType": "inputOutput"}]},
        "ProtoBody": {"-children": [node]}}}))
    assert validate_xml(xml)["valid"], validate_xml(xml)["errors"]


def test_array_item_order_is_never_changed():
    text = _doc({"Group": {"-children": [{"Box": {}}, {"WorldInfo": {}}, {"Sphere": {}}, {"Viewpoint": {}}]}})
    assert [c.tag for c in _tree(text).find("Scene/Group")] == ["Box", "WorldInfo", "Sphere", "Viewpoint"]


def test_canonical_false_keeps_member_order_as_written():
    text = json.dumps({"X3D": {"Scene": {"-children": []}, "@profile": "Core", "@version": "4.0",
                               "head": {"meta": [{"@name": "a", "@content": "b"}]}, "encoding": "UTF-8"}})
    kept = etree.fromstring(json_to_xml(text, canonical=False).encode())
    assert [c.tag for c in kept] == ["Scene", "head"]


def test_member_order_does_not_change_the_scene():
    """JSON object members are unordered: any permutation of them gives a scene as valid as the original."""
    for name in ("optimised_bench_int-04", "optimised_bench_geo-04"):
        doc = json.loads((FIXTURES / "json" / f"{name}.x3dj").read_text())

        def shuffled(o, rng):
            if isinstance(o, dict):
                items = [(k, shuffled(v, rng)) for k, v in o.items()]
                rng.shuffle(items)
                return dict(items)
            return [shuffled(v, rng) for v in o] if isinstance(o, list) else o

        for seed in range(5):
            xml = reorder_defs_xml(json_to_xml(shuffled(doc, random.Random(seed))))
            assert validate_xml(xml)["valid"], (name, seed, validate_xml(xml)["errors"][:1])
            assert not [d for d in semantic_diagnostics(xml) if d.level == "error"]


# -- Script and shader source ---------------------------------------------------

def test_source_code_becomes_cdata():
    script = {"Script": {"@DEF": "S", "field": [{"@name": "go", "@type": "SFBool", "@accessType": "inputOnly"}],
                         "#sourceCode": ["ecmascript:", "function go(v) { if (a < b && c) {} }"]}}
    text = json_to_xml(_doc(script))
    assert "<![CDATA[ecmascript:\nfunction go(v) { if (a < b && c) {} }]]>" in text
    node = etree.fromstring(text.encode()).find("Scene/Script")
    assert node.find("field").get("name") == "go" and "a < b && c" in node[-1].tail
    assert validate_xml(text)["valid"]


def test_source_code_string_form_and_cdata_terminator():
    text = json_to_xml(_doc({"Script": {"#sourceCode": "var s = ']]>';"}}))
    assert etree.fromstring(text.encode()).find("Scene/Script").text.strip() == "var s = ']]>';"


# -- statements ---------------------------------------------------------------------

def test_route_statements():
    xml = json_to_xml(_doc({"TimeSensor": {"@DEF": "T"}}, {"PositionInterpolator": {"@DEF": "P"}},
                           {"ROUTE": {"@fromNode": "T", "@fromField": "fraction_changed",
                                      "@toNode": "P", "@toField": "set_fraction"}}))
    assert etree.fromstring(xml.encode()).find("Scene/ROUTE").get("toNode") == "P"


def test_comments_are_dropped():
    xml = json_to_xml(_doc({"#comment": "hi"}, {"Group": {"#comment": "x", "-children": [{"#comment": "y"}]}}))
    assert "hi" not in xml and etree.fromstring(xml.encode()).find("Scene/Group") is not None


# -- errors name the JSON path ----------------------------------------------------------

@pytest.mark.parametrize("text, fragment", [
    ("{broken", "invalid JSON"),
    ("[]", "no top-level X3D"),
    ('{"X3D": []}', "X3D: is not an object"),
    (_doc({"Shape": []}), "Scene/-children[0]/Shape: Shape is a list, not an object"),
    (_doc({"Group": {"-children": {"Box": {}}}}), "-children is not an array"),
    (_doc({"Group": {"-children": [3]}}), "child is a int, not an object"),
    (_doc({"Group": {"@a b": 1}}), "not a valid XML name"),
    (_doc({"Gro<up": {}}), "not a valid XML name"),
    (_doc({"WorldInfo": {"@title": "a\u0001b"}}), "character XML 1.0 cannot carry"),
    (_doc({"WorldInfo": {"@title": {"x": 1}}}), "unsupported attribute value of type dict"),
    (_doc({"Script": {"#sourceCode": [1]}}), "#sourceCode is not a string"),
])
def test_errors_name_the_json_path(text, fragment):
    with pytest.raises(JsonConversionError) as err:
        json_to_xml(text)
    assert fragment in str(err.value)


def test_non_finite_numbers_are_refused():
    with pytest.raises(JsonConversionError, match="not an X3D number"):
        json_to_xml('{"X3D": {"@profile": "Core", "@version": "4.0", "Scene": {"-children": '
                    '[{"Viewpoint": {"@position": [0, 0, 1e999]}}]}}}')


def test_nesting_depth_is_capped():
    inner = {"Box": {}}
    for _ in range(300):
        inner = {"Group": {"-children": [inner]}}
    with pytest.raises(JsonConversionError, match="nested deeper"):
        json_to_xml(_doc(inner))


def test_json_nested_beyond_the_parser_is_an_error_not_a_crash():
    with pytest.raises(JsonConversionError):
        json_to_xml("[" * 100000)


# -- real scenes ----------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(p.stem for p in (FIXTURES / "json").glob("*.x3dj")))
def test_real_scene_converts_to_valid_xml(name):
    xml = reorder_defs_xml(json_to_xml((FIXTURES / "json" / f"{name}.x3dj").read_text()))
    result = validate_xml(xml)
    assert result["valid"], result["errors"][:2]
    assert not [d for d in semantic_diagnostics(xml) if d.level == "error"]


def test_canonical_helloworld_converts():
    xml = json_to_xml((FIXTURES / "HelloWorld.json").read_text())
    assert validate_xml(xml)["valid"]


# -- hostile documents -------------------------------------------------------

def _raw(*children) -> str:
    return '{"X3D": {"@profile": "Core", "@version": "4.0", "Scene": {"-children": [%s]}}}' % ",".join(children)


@pytest.mark.parametrize("text, member", [
    ('{"X3D": {"Scene": {}}, "X3D": {"Scene": {"-children": [{"Box": {}}]}}}', "X3D"),
    ('{"X3D": {"@profile": "Core", "@profile": "Full", "Scene": {}}}', "@profile"),
    (_raw('{"Shape": {}, "Shape": {"@DEF": "a"}}'), "Shape"),
    (_raw('{"Viewpoint": {"@position": [0, 0, 1], "@position": [9, 9, 9]}}'), "@position"),
])
def test_a_member_written_twice_is_refused(text, member):
    """json.loads would keep the last one; another reader keeps the first."""
    with pytest.raises(JsonConversionError, match=f"member '{member}' is written twice"):
        json_to_xml(text)


def test_repeated_comment_members_are_dropped_not_refused():
    text = _raw('{"Group": {"#comment": "a", "#comment": "b"}}')
    assert etree.fromstring(json_to_xml(text).encode()).find("Scene/Group") is not None


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
def test_non_finite_numbers_are_refused_wherever_they_sit(literal):
    for value in (f"[{literal}, 0, 0]", f"[[0, 0, 1], [{literal}, 0, 0]]", f"[true, {literal}]", literal):
        with pytest.raises(JsonConversionError, match="is not an X3D number"):
            json_to_xml(_raw('{"Transform": {"@translation": %s}}' % value))


def test_numbers_beyond_the_integer_digit_limit_are_a_json_error():
    with pytest.raises(JsonConversionError, match="invalid JSON"):
        json_to_xml(_raw('{"Transform": {"@scale": [%s, 1, 1]}}' % ("9" * 5000)))


def test_numeric_arrays_keep_their_text_form():
    """The fast path for number arrays and the general path write the same text."""
    root = _tree(_doc({"Transform": {"@translation": [1, -0.5, 1e-07], "@scale": [1e+22, 2.5, 3]}},
                      {"Transform": {"@translation": [[1, -0.5], [1e-07]], "@scale": [True, 2.5, 3]}}))
    a, b = root.findall("Scene/Transform")
    assert a.get("translation") == b.get("translation") == "1 -0.5 1e-07"
    assert a.get("scale") == "1e+22 2.5 3" and b.get("scale") == "true 2.5 3"


@pytest.mark.parametrize("title", ["\ud800", "x\udfffy", "￾", "\x00"])
def test_characters_xml_cannot_carry_are_refused(title):
    with pytest.raises(JsonConversionError, match="character XML 1.0 cannot carry"):
        json_to_xml(_doc({"WorldInfo": {"@title": title}}))


def test_a_lone_surrogate_written_as_an_escape_never_reaches_the_xml():
    with pytest.raises(JsonConversionError, match="cannot carry"):
        json_to_xml(_raw('{"WorldInfo": {"@title": "\\ud800"}}'))
    xml = json_to_xml(_raw('{"WorldInfo": {"@title": "\\ud83d\\ude00"}}'))     # a pair is one character
    assert chr(0x1F600) in xml
    xml.encode("utf-8")


@pytest.mark.parametrize("count", [3000, 100_000])
def test_deeply_nested_json_is_an_error_not_a_crash(count):
    for text in ('{"X3D":{"Scene":' + '{"-children":[{"Group":' * count + "{}" + "}]}" * count + "}}",
                 '{"a":' * count + "1" + "}" * count):
        with pytest.raises(JsonConversionError):
            json_to_xml(text)
    arrays = '{"X3D":{"Scene":{"-children":[{"Transform":{"@scale":' + "[" * count + "1" + "]" * count + "}}]}}}"
    try:
        assert json_to_xml(arrays).count('scale="1"') == 1     # nested arrays flatten iteratively, or the parser's limit is reported
    except JsonConversionError as exc:
        assert "nested too deeply" in str(exc)


def test_nesting_just_inside_the_cap_converts_and_the_error_path_stays_short():
    def nested(n):
        inner = {"Box": {}}
        for _ in range(n):
            inner = {"Group": {"-children": [inner]}}
        return _doc(inner)
    xml = json_to_xml(nested(190))
    assert xml.count("<Group") == 190 and "".join(xml.split()).endswith("</Scene></X3D>")
    with pytest.raises(JsonConversionError) as err:
        json_to_xml(nested(400))
    assert len(str(err.value)) < 400 and "..." in str(err.value)


def test_indentation_stops_growing_so_deep_documents_do_not_balloon():
    inner = {"Group": {"-children": [{"Group": {} } for _ in range(2000)]}}
    for _ in range(150):
        inner = {"Group": {"-children": [inner]}}
    text = _doc(inner)
    assert len(json_to_xml(text)) < 6 * len(text)
    assert all(len(line) - len(line.lstrip()) <= 2 * 24 + 2 for line in json_to_xml(text).splitlines())


def test_the_element_count_is_capped(monkeypatch):
    import x3d_utils.json2xml as mod
    monkeypatch.setattr(mod, "MAX_ELEMENTS", 50)
    json_to_xml(_doc(*[{"Group": {}}] * 40))
    with pytest.raises(JsonConversionError, match="more than 50 elements"):
        json_to_xml(_doc(*[{"Group": {}}] * 60))


def test_a_container_field_given_twice_is_refused():
    """A '-normal' member on a node whose default is something else writes containerField itself."""
    scene = _doc({"Shape": {"-geometry": {"IndexedFaceSet": {"-coord": {"Normal": {"@containerField": "x"}}}}}})
    with pytest.raises(JsonConversionError, match="@containerField is given and the member -coord sets it too"):
        json_to_xml(scene)
    xml = etree.fromstring(json_to_xml(_doc({"Shape": {"-geometry": {"IndexedFaceSet": {"-coord": {"Normal": {}}}}}})).encode())
    assert xml.find(".//Normal").get("containerField") == "coord"


@pytest.mark.parametrize("scene, fragment", [
    (_doc({"Group": {"@foo:bar": 1}}), "namespace prefix 'foo'"),
    (_doc({"foo:Group": {}}), "namespace prefix 'foo'"),
    (_doc({"Group": {"-": {"Box": {}}}}), "'-' names no field"),
])
def test_names_that_make_ill_formed_xml_are_refused(scene, fragment):
    with pytest.raises(JsonConversionError, match=fragment):
        json_to_xml(scene)


def test_declared_prefixes_pass_and_the_result_is_well_formed():
    xml = json_to_xml(_doc({"Group": {"@xml:lang": "en", "@xsd:foo": "1"}}))
    etree.fromstring(xml.encode())


@pytest.mark.parametrize("seed", range(40))
def test_random_garbage_never_escapes_as_another_exception(seed):
    """Whatever the document, the answer is XML or a JsonConversionError."""
    rng = random.Random(seed)
    pool = ['{"Box": {}}', '{"Group": {"-children": []}}', '{"Shape": []}', "3", "null", '"s"', "[[]]", "NaN",
            '{"": 1}', '{"@a": {"b": 1}}', '{"-x": 3}', '{"#comment": 1}', '{"Script": {"#sourceCode": 5}}']
    text = _raw(*[rng.choice(pool) for _ in range(rng.randint(0, 6))])
    try:
        etree.fromstring(json_to_xml(text).encode())
    except JsonConversionError:
        pass
