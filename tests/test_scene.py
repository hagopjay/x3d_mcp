"""Tests for the scene state manager."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest
from x3d_utils.scene import SceneManager, SceneError


@pytest.fixture
def sm():
    return SceneManager()


def test_create_node(sm):
    node_id = sm.create_node("Box", size=(3, 3, 3))
    assert node_id is not None
    assert node_id in sm._nodes


def test_create_node_unknown_type(sm):
    with pytest.raises(SceneError, match="Unknown node type"):
        sm.create_node("FakeNode")


def test_set_field(sm):
    node_id = sm.create_node("Material")
    sm.set_field(node_id, "diffuseColor", (1, 0, 0))
    assert sm._nodes[node_id].diffuseColor == (1, 0, 0)


def test_set_field_invalid(sm):
    node_id = sm.create_node("Box")
    with pytest.raises(SceneError, match="has no field"):
        sm.set_field(node_id, "nonexistent", 42)


def test_add_child(sm):
    parent_id = sm.create_node("Transform")
    child_id = sm.create_node("Shape")
    sm.add_child(parent_id, child_id)
    assert child_id in sm._children[parent_id]
    assert sm._parent[child_id] == parent_id


def test_add_child_appearance_material(sm):
    shape_id = sm.create_node("Shape")
    app_id = sm.create_node("Appearance")
    mat_id = sm.create_node("Material", diffuseColor=(1, 0, 0))
    box_id = sm.create_node("Box")

    sm.add_child(shape_id, app_id)
    sm.add_child(app_id, mat_id)
    sm.add_child(shape_id, box_id)

    xml = sm.to_xml()
    assert "Material" in xml
    assert "diffuseColor" in xml
    assert "Box" in xml


def test_def_node(sm):
    node_id = sm.create_node("Shape")
    sm.def_node(node_id, "MyShape")
    assert sm._def_to_id["MyShape"] == node_id
    assert sm._id_to_def[node_id] == "MyShape"
    xml = sm.to_xml()
    assert "DEF='MyShape'" in xml


def test_def_node_duplicate(sm):
    id1 = sm.create_node("Shape")
    id2 = sm.create_node("Shape")
    sm.def_node(id1, "MyShape")
    with pytest.raises(SceneError, match="DEF name already in use"):
        sm.def_node(id2, "MyShape")


def test_use_node(sm):
    node_id = sm.create_node("Shape")
    sm.def_node(node_id, "MyShape")
    use_id = sm.use_node("MyShape")
    assert use_id != node_id
    xml = sm.to_xml()
    assert "DEF='MyShape'" in xml
    assert "USE='MyShape'" in xml


def test_use_node_unknown(sm):
    with pytest.raises(SceneError, match="No node with DEF name"):
        sm.use_node("NoSuchDef")


def test_add_route(sm):
    t_id = sm.create_node("TimeSensor")
    i_id = sm.create_node("PositionInterpolator")
    sm.def_node(t_id, "Clock")
    sm.def_node(i_id, "Mover")
    sm.add_route(t_id, "fraction_changed", i_id, "set_fraction")
    xml = sm.to_xml()
    assert "ROUTE" in xml
    assert "fromNode='Clock'" in xml
    assert "toNode='Mover'" in xml


def test_add_route_no_def(sm):
    t_id = sm.create_node("TimeSensor")
    i_id = sm.create_node("PositionInterpolator")
    with pytest.raises(SceneError, match="no DEF name"):
        sm.add_route(t_id, "fraction_changed", i_id, "set_fraction")


def test_remove_node(sm):
    node_id = sm.create_node("Box")
    sm.remove_node(node_id)
    assert node_id not in sm._nodes


def test_remove_node_with_parent(sm):
    parent_id = sm.create_node("Transform")
    child_id = sm.create_node("Shape")
    sm.add_child(parent_id, child_id)
    sm.remove_node(child_id)
    assert child_id not in sm._children[parent_id]


def test_add_meta(sm):
    sm.add_meta("title", "Test Scene")
    xml = sm.to_xml()
    assert "title" in xml
    assert "Test Scene" in xml


def test_set_profile(sm):
    sm.set_profile("Full")
    xml = sm.to_xml()
    assert "profile='Full'" in xml


def test_set_profile_invalid(sm):
    with pytest.raises(SceneError, match="Unknown profile"):
        sm.set_profile("FakeProfile")


def test_to_xml(sm):
    sm.create_node("Shape")
    xml = sm.to_xml()
    assert "<?xml" in xml
    assert "<X3D" in xml
    assert "<Scene>" in xml
    assert "<Shape" in xml


def test_to_json(sm):
    sm.create_node("Shape")
    json_out = sm.to_json()
    assert "X3D" in json_out
    assert "Scene" in json_out


def test_to_vrml(sm):
    sm.create_node("Shape")
    vrml = sm.to_vrml()
    assert "#VRML" in vrml
    assert "Shape" in vrml


def test_reset(sm):
    sm.create_node("Box")
    sm.create_node("Sphere")
    sm.add_meta("title", "Test")
    sm.reset()
    assert len(sm._nodes) == 0
    assert len(sm._metas) == 0
    assert len(sm._routes) == 0


def test_complete_scene(sm):
    """Build a complete scene and verify XML output."""
    sm.set_profile("Interchange")
    sm.add_meta("title", "Complete Test Scene")

    xform_id = sm.create_node("Transform", translation=(2, 0, 0))
    shape_id = sm.create_node("Shape")
    app_id = sm.create_node("Appearance")
    mat_id = sm.create_node("Material", diffuseColor=(0, 1, 0))
    sphere_id = sm.create_node("Sphere", radius=1.5)

    sm.add_child(app_id, mat_id)
    sm.add_child(shape_id, app_id)
    sm.add_child(shape_id, sphere_id)
    sm.add_child(xform_id, shape_id)

    sm.def_node(xform_id, "GreenSphere")

    xml = sm.to_xml()
    assert "DEF='GreenSphere'" in xml
    assert "diffuseColor='0 1 0'" in xml
    assert "Sphere" in xml
    assert "Interchange" in xml
    assert "Complete Test Scene" in xml


def test_containerfield_override_nondefault_placement():
    """Granular mode can place a node in a non-default field (the x3d.py gap)."""
    from validation.semantic import validate_semantic
    s = SceneManager()
    ap = s.create_node("Appearance")
    pm = s.create_node("PhysicalMaterial")
    tx = s.create_node("ImageTexture", url=["t.png"])
    s.add_child(ap, pm)
    s.add_child(pm, tx, "baseTexture")            # was impossible before
    xml = s.to_xml()
    assert "baseTexture" in xml
    assert "containerfield" not in validate_semantic(xml)


def test_containerfield_override_rejects_bad_field():
    s = SceneManager()
    pm = s.create_node("PhysicalMaterial")
    tx = s.create_node("ImageTexture")
    try:
        s.add_child(pm, tx, "nonexistentField")
        assert False, "expected SceneError"
    except SceneError:
        pass


def _jointed_scene() -> SceneManager:
    """Two bodies, a joint using both, and a collider: the shape x3d.py misorders."""
    s = SceneManager()
    rbc = s.create_node("RigidBodyCollection")
    b1 = s.create_node("RigidBody")
    b2 = s.create_node("RigidBody")
    s.def_node(b1, "B1")
    s.def_node(b2, "B2")
    s.add_child(rbc, b1)
    s.add_child(rbc, b2)
    s.add_child(rbc, s.create_node("CollisionCollection"))
    joint = s.create_node("BallJoint")
    s.add_child(rbc, joint)
    s.add_child(joint, s.use_node("B1"), "body1")
    s.add_child(joint, s.use_node("B2"), "body2")
    return s


def test_to_xml_defs_bodies_before_joints():
    """x3d.py writes joints before bodies; to_xml must put each DEF first (X-05)."""
    from validation.semantic import semantic_diagnostics
    xml = _jointed_scene().to_xml()
    assert xml.index("DEF=\"B1\"") < xml.index("USE=\"B1\"")
    assert xml.index("DEF=\"B2\"") < xml.index("USE=\"B2\"")
    assert not [d for d in semantic_diagnostics(xml) if d.check == "use-before-def"]


def test_to_xml_keeps_doctype_when_reordering():
    xml = _jointed_scene().to_xml()
    assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE X3D')


@pytest.mark.parametrize("value", [False, "false", "False", 0])
def test_set_field_sfbool_false_is_written(sm, value):
    """A false SFBool must reach the XML: it used to be accepted then dropped (X-08)."""
    node_id = sm.create_node("RigidBody")
    sm.set_field(node_id, "enabled", value)
    assert sm._nodes[node_id].enabled is False
    assert "enabled='false'" in sm.to_xml()


@pytest.mark.parametrize("value", [True, "true", 1])
def test_set_field_sfbool_true(sm, value):
    node_id = sm.create_node("RigidBody", enabled=False)
    sm.set_field(node_id, "enabled", value)
    assert sm._nodes[node_id].enabled is True


def test_set_field_sfbool_rejects_garbage(sm):
    node_id = sm.create_node("RigidBody")
    with pytest.raises(SceneError, match="SFBool"):
        sm.set_field(node_id, "enabled", "maybe")
    with pytest.raises(SceneError, match="SFBool"):
        sm.set_field(node_id, "enabled", 2)


@pytest.mark.parametrize("node_type,field", [
    ("BallJoint", "body1AnchorPoint"),      # outputOnly
    ("SliderJoint", "separation"),          # outputOnly
    ("Transform", "addChildren"),           # inputOnly
])
def test_set_field_rejects_event_only_fields(sm, node_type, field):
    """outputOnly/inputOnly fields never reach the XML, so say so (X-13)."""
    node_id = sm.create_node(node_type)
    with pytest.raises(SceneError, match="ROUTE"):
        sm.set_field(node_id, field, 1.0)


def test_set_field_still_sets_input_output_fields(sm):
    node_id = sm.create_node("SliderJoint")
    sm.set_field(node_id, "minSeparation", 0.5)
    assert sm._nodes[node_id].minSeparation == 0.5
