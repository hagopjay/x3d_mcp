"""Tests for the profile and component conformance check."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lxml import etree
from validation.profile import check_profile, component_statements, profile_nodes
from validation.semantic import validate_semantic


def _doc(profile: str, body: str, head: str = "") -> str:
    head_xml = f"<head>{head}</head>" if head else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<X3D profile="{profile}" version="4.0">
  {head_xml}
  <Scene>
    {body}
  </Scene>
</X3D>"""


def _report(xml: str):
    return check_profile(etree.fromstring(xml.encode("utf-8")))


def test_profile_tables_come_from_x3duom():
    tables = profile_nodes()
    assert "Box" in tables["Interchange"]
    assert "Rectangle2D" not in tables["Interchange"]
    assert "Rectangle2D" in tables["Immersive"]
    assert tables["Full"] is None          # everything allowed
    assert "Box" not in tables["Core"]


def test_interchange_box_is_conformant():
    report = _report(_doc("Interchange", "<Shape><Box/></Shape>"))
    assert report.ok
    assert report.missing == []


def test_rectangle2d_outside_interchange():
    report = _report(_doc("Interchange", "<Shape><Rectangle2D size='2 2'/></Shape>"))
    assert not report.ok
    [m] = report.missing
    assert m["node"] == "Rectangle2D"
    assert m["component"] == "Geometry2D"
    assert m["level"] == 1
    assert component_statements(report) == ["<component name='Geometry2D' level='1'/>"]


def test_component_statement_makes_it_conformant():
    xml = _doc("Interchange", "<Shape><Rectangle2D size='2 2'/></Shape>",
               head="<component name='Geometry2D' level='1'/>")
    report = _report(xml)
    assert report.ok
    assert report.components == {"Geometry2D": 1}


def test_component_level_too_low_is_reported():
    # PointLight is Lighting level 2; a level-1 statement does not cover it.
    xml = _doc("Interchange", "<PointLight/>", head="<component name='Lighting' level='1'/>")
    report = _report(xml)
    assert not report.ok
    [m] = report.missing
    assert m["node"] == "PointLight"
    assert m["declared_level"] == 1
    assert component_statements(report) == ["<component name='Lighting' level='2'/>"]


def test_immersive_and_full_cover_more():
    body = "<Shape><Rectangle2D size='2 2'/></Shape><PointLight/>"
    assert _report(_doc("Immersive", body)).ok
    assert _report(_doc("Full", body + "<HAnimHumanoid/>")).ok


def test_hanim_outside_immersive_needs_component():
    report = _report(_doc("Immersive", "<HAnimHumanoid name='h'/>"))
    assert not report.ok
    assert component_statements(report) == ["<component name='HAnim' level='1'/>"]
    assert _report(_doc("Immersive", "<HAnimHumanoid name='h'/>",
                        head="<component name='HAnim' level='1'/>")).ok


def test_statements_and_prototypes_are_ignored():
    body = ("<ProtoDeclare name='P'><ProtoInterface><field name='f' type='SFBool' "
            "accessType='inputOutput' value='true'/></ProtoInterface><ProtoBody>"
            "<Group/></ProtoBody></ProtoDeclare><ProtoInstance name='P'/>"
            "<TimeSensor DEF='T'/><Transform DEF='X'/>"
            "<ROUTE fromNode='T' fromField='fraction_changed' toNode='X' toField='set_scale'/>")
    report = _report(_doc("Interchange", body))
    assert report.ok, report.missing


def test_semantic_report_names_the_fix():
    report = validate_semantic(_doc("Interchange", "<Shape><Rectangle2D size='2 2'/></Shape>"))
    assert "profile-component" in report
    assert "Rectangle2D" in report
    assert "<component name='Geometry2D' level='1'/>" in report


def test_semantic_report_clean_for_conformant_scene():
    report = validate_semantic(_doc("Interchange", "<Viewpoint/><Shape><Appearance><Material/></Appearance><Box/></Shape>"))
    assert "profile-component" not in report
