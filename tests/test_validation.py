"""Tests for the validation pipeline."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from validation.validate import validate_xml, validate_json


def test_valid_minimal_scene():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<X3D profile="Interchange" version="4.1">
  <Scene>
    <Shape>
      <Appearance>
        <Material/>
      </Appearance>
      <Box/>
    </Shape>
  </Scene>
</X3D>"""
    result = validate_xml(xml)
    assert result["valid"] is True
    assert result["errors"] == []


def test_valid_scene_with_xsi():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<X3D profile="Interchange" version="4.1"
     xmlns:xsd="https://www.w3.org/2001/XMLSchema-instance"
     xsd:noNamespaceSchemaLocation="https://www.web3d.org/specifications/x3d-4.0.xsd">
  <Scene>
    <Shape>
      <Box/>
    </Shape>
  </Scene>
</X3D>"""
    result = validate_xml(xml)
    assert result["valid"] is True


def test_invalid_unknown_node():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<X3D profile="Interchange" version="4.1">
  <Scene>
    <FakeNode/>
  </Scene>
</X3D>"""
    result = validate_xml(xml)
    assert result["valid"] is False
    assert any("FakeNode" in e for e in result["errors"])


def test_invalid_malformed_xml():
    result = validate_xml("<broken")
    assert result["valid"] is False
    assert len(result["errors"]) > 0


def test_invalid_missing_profile():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<X3D version="4.1">
  <Scene/>
</X3D>"""
    result = validate_xml(xml)
    # XSD requires profile attribute
    assert result["valid"] is False


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _minimal_valid_json() -> str:
    """Canonical-shape minimal X3D JSON: encoding + Scene.-children populated.

    Note: `-geometry` and `-appearance` are SFNode containers (single object),
    not arrays. Web3D's HelloWorld.json shows the canonical shape.
    """
    return json.dumps({
        "X3D": {
            "encoding": "UTF-8",
            "@profile": "Interchange",
            "@version": "4.0",
            "Scene": {"-children": [{"Shape": {"-geometry": {"Box": {}}}}]},
        }
    })


def test_valid_json():
    result = validate_json(_minimal_valid_json())
    assert result["valid"] is True
    assert result["errors"] == []


def test_valid_canonical_helloworld():
    helloworld = (FIXTURES / "HelloWorld.json").read_text()
    result = validate_json(helloworld)
    assert result["valid"] is True, result["errors"][:5]


def test_invalid_json_parse():
    result = validate_json("{broken")
    assert result["valid"] is False
    assert "parse error" in result["errors"][0].lower()


def test_invalid_json_missing_x3d():
    result = validate_json('{"Scene": {}}')
    assert result["valid"] is False
    assert any("X3D" in e for e in result["errors"])


def test_invalid_json_missing_fields():
    result = validate_json('{"X3D": {}}')
    assert result["valid"] is False
    joined = " ".join(result["errors"])
    assert "@version" in joined
    assert "@profile" in joined
    assert "Scene" in joined
    assert "encoding" in joined


def test_invalid_json_misspelled_head():
    """Misspelled `head` -- John Carlson's example. Surface check missed this; schema catches it."""
    bad = json.dumps({"X3D": {
        "encoding": "UTF-8", "@profile": "Interchange", "@version": "4.0",
        "haed": {"meta": []},
        "Scene": {"-children": [{"Shape": {"-geometry": {"Box": {}}}}]},
    }})
    result = validate_json(bad)
    assert result["valid"] is False
    assert any("haed" in e for e in result["errors"])


def test_invalid_json_misspelled_meta():
    """Misspelled `meta` inside head."""
    bad = json.dumps({"X3D": {
        "encoding": "UTF-8", "@profile": "Interchange", "@version": "4.0",
        "head": {"mteta": []},
        "Scene": {"-children": [{"Shape": {"-geometry": {"Box": {}}}}]},
    }})
    result = validate_json(bad)
    assert result["valid"] is False
    assert any("mteta" in e for e in result["errors"])


def test_invalid_json_unknown_node_in_scene():
    """Unknown node name inside Scene.-children fails schema validation."""
    bad = json.dumps({"X3D": {
        "encoding": "UTF-8", "@profile": "Interchange", "@version": "4.0",
        "Scene": {"-children": [{"FakeNode": {}}]},
    }})
    result = validate_json(bad)
    assert result["valid"] is False


def test_invalid_json_wrong_version_type():
    """@version must be a string, not an integer."""
    bad = json.dumps({"X3D": {
        "encoding": "UTF-8", "@profile": "Interchange", "@version": 4,
        "Scene": {"-children": [{"Shape": {"-geometry": {"Box": {}}}}]},
    }})
    result = validate_json(bad)
    assert result["valid"] is False
    assert any("@version" in e or "version" in e for e in result["errors"])


def test_json_error_format_includes_path():
    """Error strings carry a 'path/to/field: message' shape so the LLM can act."""
    bad = json.dumps({"X3D": {
        "encoding": "UTF-8", "@profile": "Interchange", "@version": "4.0",
        "head": {"mteta": []},
        "Scene": {"-children": [{"Shape": {"-geometry": {"Box": {}}}}]},
    }})
    result = validate_json(bad)
    assert result["valid"] is False
    assert any(":" in e for e in result["errors"])
    assert any(e.startswith("X3D/head") or "head" in e for e in result["errors"])


def _scene_with(node_xml: str, version: str | None) -> str:
    version_attr = f' version="{version}"' if version is not None else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<X3D profile="Immersive"{version_attr}>
  <Scene>
    {node_xml}
  </Scene>
</X3D>"""


def test_version_40_uses_40_schema():
    result = validate_xml(_scene_with("<Shape><Box/></Shape>", "4.0"))
    assert result["valid"] is True
    assert result["schema_version"] == "4.0"
    assert "notes" not in result


def test_version_41_uses_41_schema():
    result = validate_xml(_scene_with("<Shape><Box/></Shape>", "4.1"))
    assert result["valid"] is True
    assert result["schema_version"] == "4.1"


def test_41_only_node_rejected_under_40_with_hint():
    # EnvironmentLight exists only in the X3D 4.1 draft.
    result = validate_xml(_scene_with('<EnvironmentLight intensity="0.4"/>', "4.0"))
    assert result["valid"] is False
    assert result["schema_version"] == "4.0"
    assert any("EnvironmentLight" in e for e in result["errors"])
    assert any("added in X3D 4.1" in n for n in result["notes"])


def test_41_only_node_accepted_under_41():
    result = validate_xml(_scene_with('<EnvironmentLight intensity="0.4"/>', "4.1"))
    assert result["valid"] is True
    assert result["schema_version"] == "4.1"


def test_version_override_argument_wins():
    xml = _scene_with('<EnvironmentLight intensity="0.4"/>', "4.0")
    assert validate_xml(xml, version="4.1")["valid"] is True


def test_missing_version_defaults_to_41_with_note():
    result = validate_xml(_scene_with("<Shape><Box/></Shape>", None))
    assert result["schema_version"] == "4.1"
    assert any("No version attribute" in n for n in result["notes"])


def test_legacy_33_validated_against_40_with_note():
    result = validate_xml(_scene_with("<Shape><Box/></Shape>", "3.3"))
    assert result["valid"] is True
    assert result["schema_version"] == "4.0"
    assert any("X3D 3.3" in n for n in result["notes"])


def test_unknown_version_falls_back_with_note():
    result = validate_xml(_scene_with("<Shape><Box/></Shape>", "9.9"))
    assert result["schema_version"] == "4.1"
    assert any("Unknown version" in n for n in result["notes"])


_GEOLOD = ("<GeoLOD geoSystem='&quot;GD&quot; &quot;WE&quot;' center='37.2 -80.4 630' "
           "range='200' child1Url='&quot;a.x3d&quot;'>"
           "<Group containerField='rootNode'><Shape><Box/></Shape></Group></GeoLOD>")


def test_geolod_missing_child_error_gets_content_model_hint():
    doc = ('<X3D profile="Immersive" version="4.0"><head>'
           '<component name="Geospatial" level="1"/></head>'
           f'<Scene>{_GEOLOD}</Scene></X3D>')
    res = validate_xml(doc)
    assert res["valid"] is False
    assert any("Missing child element" in e and "GeoLOD" in e for e in res["errors"])
    hint = next(n for n in res["notes"] if n.startswith("GeoLOD at line"))
    assert "GeoOrigin" in hint and "rootUrl" in hint
    assert "geoCoords='37.2 -80.4 630'" in hint        # taken from the GeoLOD's center


def test_geolod_with_geoorigin_validates_without_hint():
    doc = ('<X3D profile="Immersive" version="4.0"><head>'
           '<component name="Geospatial" level="1"/></head><Scene>'
           + _GEOLOD.replace("</GeoLOD>",
                             "<GeoOrigin containerField='geoOrigin' geoCoords='37.2 -80.4 630'/></GeoLOD>")
           + '</Scene></X3D>')
    res = validate_xml(doc)
    assert res["valid"] is True
    assert "notes" not in res
