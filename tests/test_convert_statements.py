"""convert_x3d's XML reader keeps what a node holds besides child nodes.

The reader rebuilds a scene as x3d.py objects; anything it does not map is dropped
without a message, and an XML comment used to raise TypeError.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lxml import etree

from tools.convert import _parse_xml_to_model
from validation.validate import validate_xml

_SCRIPTED = """<?xml version="1.0" encoding="UTF-8"?>
<X3D profile="Immersive" version="4.1">
  <head>
    <component name="Geospatial" level="1"/>
    <unit category="angle" name="degrees" conversionFactor="0.0174532925"/>
    <meta name="title" content="t"/>
  </head>
  <Scene>
    <!-- a comment between statements -->
    <Script DEF="S" directOutput="true">
      <field name="go" type="SFBool" accessType="inputOnly"/>
      <field name="done" type="SFBool" accessType="outputOnly"/>
      <![CDATA[ecmascript:
function go(v) { done = v; }
]]>
    </Script>
    <Shape><!-- inside a node --><Box/></Shape>
  </Scene>
</X3D>"""


def _roundtrip(xml: str) -> etree._Element:
    return etree.fromstring(_parse_xml_to_model(xml).XML().split("?>", 1)[1].split("\n", 1)[1].encode())


def test_comments_are_skipped_not_fatal():
    root = _roundtrip(_SCRIPTED)
    assert root.find("Scene/Shape/Box") is not None


def test_head_component_and_unit_survive():
    head = _roundtrip(_SCRIPTED).find("head")
    assert [(c.tag, c.get("name")) for c in head] == [
        ("component", "Geospatial"), ("unit", "degrees"), ("meta", "title")]
    assert head.find("component").get("level") == "1"
    assert float(head.find("unit").get("conversionFactor")) == 0.0174532925


def test_script_fields_and_source_survive():
    script = _roundtrip(_SCRIPTED).find("Scene/Script")
    assert [(f.get("name"), f.get("accessType")) for f in script.findall("field")] == [
        ("go", "inputOnly"), ("done", "outputOnly")]
    assert "function go(v)" in script.text + "".join(c.tail or "" for c in script)


def test_is_connections_survive():
    xml = """<X3D profile="Immersive" version="4.1"><Scene>
      <Script DEF="S"><field name="a" type="SFFloat" accessType="inputOutput"/>
        <IS><connect nodeField="a" protoField="p"/></IS></Script></Scene></X3D>"""
    connect = _roundtrip(xml).find("Scene/Script/IS/connect")
    assert (connect.get("nodeField"), connect.get("protoField")) == ("a", "p")


def test_mf_vec2_and_colorrgba_values_are_read():
    xml = """<X3D profile="Immersive" version="4.1"><Scene>
      <Shape><Appearance><Material/></Appearance>
        <Extrusion crossSection="2 2, 2 -2, -2 -2, -2 2, 2 2" spine="0 0 0, 0 1 0"/></Shape>
      <Shape><IndexedFaceSet coordIndex="0 1 2 -1"><Coordinate point="0 0 0 1 0 0 0 1 0"/>
        <ColorRGBA color="1 0 0 1, 0 1 0 1, 0 0 1 1"/></IndexedFaceSet></Shape>
    </Scene></X3D>"""
    root = _roundtrip(xml)
    assert len(root.find(".//Extrusion").get("crossSection").split()) == 10
    assert len(root.find(".//ColorRGBA").get("color").split()) == 12


def test_scripted_scene_is_still_valid_after_conversion():
    out = _parse_xml_to_model(_SCRIPTED).XML()
    assert validate_xml(out)["valid"] is True


def test_field_named_like_a_python_keyword_is_read():
    xml = """<X3D profile="Immersive" version="4.1"><Scene>
      <DirectionalLight global="true" intensity="0.5"/></Scene></X3D>"""
    light = _roundtrip(xml).find("Scene/DirectionalLight")
    assert light.get("global") == "true" and light.get("intensity") == "0.5"
