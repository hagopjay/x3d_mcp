"""Tests for repair_x3d: the validators' fixes, applied and re-validated."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lxml import etree

from validation.repair import repair_x3d
from validation.profile import check_profile
from validation.validate import validate_xml
from validation.semantic import validate_semantic


def _doc(body: str, profile: str = "Interchange", version: str = "4.0",
         head: str = "", doctype: str = "", attrs: str = "") -> str:
    prof = f' profile="{profile}"' if profile else ""
    ver = f' version="{version}"' if version else ""
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n{doctype}'
            f'<X3D{prof}{ver}{attrs}>{head}<Scene>{body}</Scene></X3D>')


_BOX = "<Shape><Appearance><Material/></Appearance><Box/></Shape>"


def _kinds(res: dict) -> list[str]:
    return [c["kind"] for c in res["changes"]]


def test_clean_document_unchanged():
    res = repair_x3d(_doc(_BOX, head="<head></head>"))
    assert res["changes"] == [] and res["unfixable"] == []
    assert res["remaining"]["schema_valid"] and res["remaining"]["profile_ok"]


def test_version_raised_to_lowest_that_has_every_node():
    src = _doc("<EnvironmentLight/>" + _BOX, profile="Immersive")
    assert validate_xml(src)["valid"] is False           # EnvironmentLight is 4.1-only
    res = repair_x3d(src)
    root = etree.fromstring(res["fixed"].encode())
    assert root.get("version") == "4.1"
    change = next(c for c in res["changes"] if c["kind"] == "version")
    assert change["from"] == "4.0" and change["to"] == "4.1"
    assert "EnvironmentLight" in change["reason"]
    assert res["remaining"]["schema_valid"] is True
    assert "<EnvironmentLight" in res["fixed"]            # content never removed


def test_version_not_raised_when_not_needed():
    res = repair_x3d(_doc(_BOX))
    assert "version" not in _kinds(res)
    assert 'version="4.0"' in res["fixed"]


def test_component_statements_added_to_head():
    src = _doc("<Shape><Appearance><Material/></Appearance><Rectangle2D/></Shape>",
               head="<head><meta name='title' content='t'/></head>")
    assert not check_profile(etree.fromstring(src.encode())).ok
    res = repair_x3d(src)
    root = etree.fromstring(res["fixed"].encode())
    head = root.find("head")
    comps = [c for c in head if c.tag == "component"]
    assert [(c.get("name"), c.get("level")) for c in comps] == [("Geometry2D", "1")]
    assert head[0].tag == "component"                     # before meta statements
    assert res["remaining"]["profile_ok"] is True
    assert "profile-component" not in validate_semantic(res["fixed"])


def test_head_created_when_absent_and_placed_before_scene():
    res = repair_x3d(_doc("<Shape><Rectangle2D/></Shape>"))
    root = etree.fromstring(res["fixed"].encode())
    assert [child.tag for child in root] == ["head", "Scene"]
    assert res["remaining"]["schema_valid"] is True


def test_existing_component_level_raised():
    src = _doc("<PointLight/>" + _BOX,
               head="<head><component name='Lighting' level='1'/></head>")
    res = repair_x3d(src)
    change = next(c for c in res["changes"] if c["kind"] == "component")
    assert change["name"] == "Lighting" and change["from"] == "1" and change["to"] == "2"
    root = etree.fromstring(res["fixed"].encode())
    assert len(root.find("head").findall("component")) == 1


def test_missing_profile_and_version_set():
    res = repair_x3d(_doc(_BOX, profile="", version=""))
    root = etree.fromstring(res["fixed"].encode())
    assert root.get("profile") == "Immersive"
    assert root.get("version") == "4.0"                   # lowest that fits
    assert {"profile", "version"} <= set(_kinds(res))
    assert res["remaining"]["schema_valid"] is True


def test_doctype_follows_version():
    doctype = ('<!DOCTYPE X3D PUBLIC "ISO//Web3D//DTD X3D 4.0//EN" '
               '"https://www.web3d.org/specifications/x3d-4.0.dtd">\n')
    res = repair_x3d(_doc("<EnvironmentLight/>" + _BOX, profile="Immersive", doctype=doctype))
    assert 'DTD X3D 4.1//EN' in res["fixed"] and "x3d-4.1.dtd" in res["fixed"]
    assert "doctype" in _kinds(res)


def test_x3dpy_https_xsi_namespace_corrected():
    from x3d.x3d import X3D, Scene, Shape, Box
    src = X3D(profile="Interchange", version="4.0",
              Scene=Scene(children=[Shape(geometry=Box())])).XML()
    assert "https://www.w3.org/2001/XMLSchema-instance" in src
    res = repair_x3d(src)
    root = etree.fromstring(res["fixed"].encode())
    assert all("https://www.w3.org" not in a for a in root.attrib)
    assert root.get("{http://www.w3.org/2001/XMLSchema-instance}noNamespaceSchemaLocation") \
        == "https://www.web3d.org/specifications/x3d-4.0.xsd"
    # strict XSD (no XSI stripping) now accepts it
    from validation.validate import _get_schema
    assert _get_schema("4.0").validate(root)


def test_containerfield_fix_included_with_kind():
    src = _doc("<Shape><Appearance><PhysicalMaterial>"
               "<ImageTexture url='&quot;t.png&quot;'/>"
               "</PhysicalMaterial></Appearance><Box/></Shape>", profile="Full", version="4.1")
    res = repair_x3d(src)
    change = next(c for c in res["changes"] if c["kind"] == "containerField")
    assert change["to"] == "baseTexture"


def test_unknown_node_reported_not_removed():
    res = repair_x3d(_doc("<Teapot/>" + _BOX))
    assert any(u["kind"] == "unknown-node" and u["node"] == "Teapot" for u in res["unfixable"])
    assert "<Teapot" in res["fixed"]
    assert res["remaining"]["schema_valid"] is False


def test_parse_error_reported():
    res = repair_x3d("<X3D><Scene>")
    assert "error" in res and res["fixed"] == ""


def test_experiment_scene_a_round_trip():
    """The cold-authored PBR scene: passes the 4.1 server check, fails 4.0 and X_ITE."""
    src = _doc(
        "<Background skyColor='0.1 0.1 0.12'/><Viewpoint position='0 1 5'/>"
        "<DirectionalLight direction='-0.4 -1 -0.6'/><PointLight location='2 3 2'/>"
        "<EnvironmentLight intensity='0.5'/>"
        "<Shape><Appearance><PhysicalMaterial baseColor='0.9 0.5 0.3'/></Appearance>"
        "<Sphere/></Shape>"
        "<Transform rotation='1 0 0 -1.5708'><Shape><Appearance><Material/></Appearance>"
        "<Rectangle2D size='6 6'/></Shape></Transform>",
        head="<head><meta name='title' content='a'/></head>")
    res = repair_x3d(src)
    assert res["remaining"] == {"schema_valid": True, "schema_errors": [],
                                "profile_ok": True, "unfixable": 0}
    names = sorted((c["name"], c["to"]) for c in res["changes"] if c["kind"] == "component")
    assert names == [("Geometry2D", "1"), ("Lighting", "2"), ("Shape", "2")]


def test_geolod_without_geoorigin_reported_with_options_not_changed():
    body = ("<GeoLOD geoSystem='&quot;GD&quot; &quot;WE&quot;' center='37.2 -80.4 630' "
            "range='200' child1Url='&quot;a.x3d&quot;'>"
            "<Group containerField='rootNode'><Shape><Box/></Shape></Group></GeoLOD>")
    res = repair_x3d(_doc(body, profile="Immersive",
                          head="<head><component name='Geospatial' level='1'/></head>"))
    entry = next(u for u in res["unfixable"] if u["kind"] == "geolod-content-model")
    assert "geoCoords='37.2 -80.4 630'" in entry["options"][0]
    assert "rootUrl" in entry["options"][1]
    assert "<GeoOrigin" not in res["fixed"]                 # nothing inserted
    assert res["remaining"]["schema_valid"] is False       # still a human decision


def test_geolod_with_geoorigin_not_reported():
    body = ("<GeoLOD geoSystem='&quot;GD&quot; &quot;WE&quot;' center='0 0 0' range='200'>"
            "<Group containerField='rootNode'><Shape><Box/></Shape></Group>"
            "<GeoOrigin containerField='geoOrigin'/></GeoLOD>")
    res = repair_x3d(_doc(body, profile="Immersive",
                          head="<head><component name='Geospatial' level='1'/></head>"))
    assert not any(u["kind"] == "geolod-content-model" for u in res["unfixable"])
    assert res["remaining"]["schema_valid"] is True
