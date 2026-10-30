"""Tests for the semantic validation pipeline.

Mirrors the 19-test inventory in
https://github.com/niknarra/x3d-mcp/blob/main/tests/test_semantic_check.py
(Nikhil Narra, Nicholas Polys -- Virginia Tech / Web3D Consortium).
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from validation.semantic import validate_semantic


def _wrap(scene_body: str, profile: str = "Immersive") -> str:
    """Wrap scene body XML in an X3D 4.1 document."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<X3D profile="{profile}" version="4.1">\n'
        f'  <Scene>{scene_body}</Scene>\n'
        f'</X3D>'
    )


# ---- Shape completeness ----

def test_shape_no_geometry():
    xml = _wrap("<Shape><Appearance><Material/></Appearance></Shape>")
    report = validate_semantic(xml)
    assert "shape-no-geometry" in report
    assert "Warning" in report or "warning" in report


def test_shape_no_appearance():
    xml = _wrap("<Shape><Box/></Shape>")
    report = validate_semantic(xml)
    assert "shape-no-appearance" in report


def test_complete_shape_no_warning():
    xml = _wrap("<Shape><Appearance><Material/></Appearance><Box/></Shape>")
    report = validate_semantic(xml)
    assert "shape-no-geometry" not in report
    assert "shape-no-appearance" not in report


# ---- Empty grouping nodes ----

def test_empty_transform():
    xml = _wrap("<Transform/>")
    report = validate_semantic(xml)
    assert "empty-group" in report


def test_transform_with_children_ok():
    xml = _wrap("<Transform><Shape><Box/></Shape></Transform>")
    report = validate_semantic(xml)
    assert "empty-group" not in report


# ---- DEF/USE consistency ----

def test_use_references_undefined_def():
    xml = _wrap('<Shape USE="GhostShape"/>')
    report = validate_semantic(xml)
    assert "use-undefined-def" in report
    assert "GhostShape" in report


def test_unused_def_info():
    xml = _wrap('<Shape DEF="Lonely"><Box/></Shape>')
    report = validate_semantic(xml)
    assert "unused-def" in report
    assert "Lonely" in report


def test_consistent_def_use():
    xml = _wrap(
        '<Shape DEF="Master"><Box/></Shape>'
        '<Transform><Shape USE="Master"/></Transform>'
    )
    report = validate_semantic(xml)
    assert "use-undefined-def" not in report
    assert "unused-def" not in report


# ---- Duplicate DEFs ----

def test_duplicate_def_names():
    xml = _wrap(
        '<Shape DEF="Twin"><Box/></Shape>'
        '<Shape DEF="Twin"><Sphere/></Shape>'
    )
    report = validate_semantic(xml)
    assert "duplicate-def" in report
    assert "Twin" in report


def test_unique_defs_ok():
    xml = _wrap(
        '<Shape DEF="A"><Box/></Shape>'
        '<Shape DEF="B"><Sphere/></Shape>'
    )
    report = validate_semantic(xml)
    assert "duplicate-def" not in report


# ---- ROUTE validity ----

def test_route_nonexistent_from_node():
    xml = _wrap(
        '<TimeSensor DEF="Clock" cycleInterval="2"/>'
        '<Transform DEF="Spinner"/>'
        '<ROUTE fromNode="GhostNode" fromField="fraction_changed" '
        'toNode="Spinner" toField="set_rotation"/>'
    )
    report = validate_semantic(xml)
    assert "route-missing-from-node" in report


def test_route_nonexistent_to_node():
    xml = _wrap(
        '<TimeSensor DEF="Clock" cycleInterval="2"/>'
        '<ROUTE fromNode="Clock" fromField="cycleTime" '
        'toNode="GhostNode" toField="set_fraction"/>'
    )
    report = validate_semantic(xml)
    assert "route-missing-to-node" in report


def test_route_invalid_field():
    xml = _wrap(
        '<TimeSensor DEF="Clock" cycleInterval="2"/>'
        '<Transform DEF="Spinner"><Shape><Box/></Shape></Transform>'
        '<ROUTE fromNode="Clock" fromField="totally_made_up_field" '
        'toNode="Spinner" toField="set_translation"/>'
    )
    report = validate_semantic(xml)
    assert "route-invalid-from-field" in report


def test_valid_route_no_error():
    xml = _wrap(
        '<TimeSensor DEF="Clock" cycleInterval="2" loop="true"/>'
        '<Transform DEF="Spinner"><Shape><Box/></Shape></Transform>'
        '<ROUTE fromNode="Clock" fromField="cycleTime" '
        'toNode="Spinner" toField="rotation"/>'
    )
    report = validate_semantic(xml)
    assert "route-missing-from-node" not in report
    assert "route-missing-to-node" not in report
    assert "route-invalid-from-field" not in report
    assert "route-invalid-to-field" not in report


# ---- Missing viewpoint ----

def test_no_viewpoint_info():
    xml = _wrap("<Shape><Box/></Shape>")
    report = validate_semantic(xml)
    assert "no-viewpoint" in report


def test_with_viewpoint_ok():
    xml = _wrap(
        '<Viewpoint position="0 0 10"/>'
        '<Shape><Appearance><Material/></Appearance><Box/></Shape>'
    )
    report = validate_semantic(xml)
    assert "no-viewpoint" not in report


# ---- Overall report shape ----

def test_clean_scene_structure():
    xml = _wrap(
        '<Viewpoint position="0 0 10"/>'
        '<Shape><Appearance><Material/></Appearance><Box/></Shape>'
    )
    report = validate_semantic(xml)
    assert "All Clear" in report


def test_multiple_issues_all_reported():
    xml = _wrap(
        '<Shape DEF="Twin"><Box/></Shape>'        # no appearance -> info
        '<Shape DEF="Twin"><Sphere/></Shape>'     # duplicate DEF -> error
        '<Shape><Appearance><Material/></Appearance></Shape>'  # no geometry -> warning
        '<Transform/>'                             # empty group -> warning
        '<Shape USE="GhostShape"/>'                # USE without DEF -> error
    )
    report = validate_semantic(xml)
    assert "duplicate-def" in report
    assert "shape-no-geometry" in report
    assert "empty-group" in report
    assert "use-undefined-def" in report
    assert "no-viewpoint" in report
    assert "## Errors" in report
    assert "## Warnings" in report
    assert "## Info" in report


def test_invalid_xml_source():
    report = validate_semantic("<broken")
    assert "Parse Error" in report


# ---- ROUTEs into dynamic interfaces (Script <field> + ProtoInstance) ----
# A Script's user-declared fields and a ProtoInstance's interface are not in
# X3DUOM, so the route-validity check must read them from the scene -- otherwise
# every valid ROUTE into a Script/proto field is false-flagged route-invalid.

def test_route_to_script_declared_field_is_valid():
    xml = _wrap(
        '<TimeSensor DEF="T" cycleInterval="2" loop="true"/>'
        '<Script DEF="S" directOutput="true">'
        '<field name="arrived" type="SFTime" accessType="inputOnly"/></Script>'
        '<ROUTE fromNode="T" fromField="cycleTime" toNode="S" toField="arrived"/>')
    report = validate_semantic(xml)
    assert "route-invalid-to-field" not in report   # 'arrived' is a declared field


def test_route_to_protoinstance_interface_field_is_valid():
    xml = _wrap(
        '<ProtoDeclare name="Mover"><ProtoInterface>'
        '<field name="set_time" type="SFTime" accessType="inputOnly"/>'
        '</ProtoInterface><ProtoBody><Transform/></ProtoBody></ProtoDeclare>'
        '<TimeSensor DEF="T" cycleInterval="2"/>'
        '<Mover DEF="M"/>'
        '<ROUTE fromNode="T" fromField="cycleTime" toNode="M" toField="set_time"/>')
    report = validate_semantic(xml)
    assert "route-invalid-to-field" not in report   # 'set_time' is in the interface


def test_route_to_truly_missing_script_field_still_flagged():
    xml = _wrap(
        '<TimeSensor DEF="T" cycleInterval="2"/>'
        '<Script DEF="S"><field name="arrived" type="SFTime" accessType="inputOnly"/></Script>'
        '<ROUTE fromNode="T" fromField="cycleTime" toNode="S" toField="nonexistent"/>')
    report = validate_semantic(xml)
    assert "route-invalid-to-field" in report        # a real typo is still caught


@pytest.mark.parametrize("shader", ["ComposedShader", "PackagedShader", "ShaderProgram"])
def test_route_to_shader_declared_field_is_valid(shader):
    # A shader's <field>s are its uniforms; animating one by ROUTE is the
    # standard way to drive GPU effects declaratively.
    xml = _wrap(
        '<TimeSensor DEF="T" cycleInterval="4" loop="true"/>'
        '<ScalarInterpolator DEF="SI" key="0 1" keyValue="0 1"/>'
        '<Shape><Appearance><Material/>'
        f'<{shader} DEF="S" language="GLSL">'
        '<field name="glow" type="SFFloat" accessType="inputOutput" value="0"/>'
        f'</{shader}></Appearance><Sphere/></Shape>'
        '<ROUTE fromNode="T" fromField="fraction_changed" toNode="SI" toField="set_fraction"/>'
        '<ROUTE fromNode="SI" fromField="value_changed" toNode="S" toField="glow"/>')
    report = validate_semantic(xml)
    assert "route-invalid-to-field" not in report   # 'glow' is a declared uniform


def test_route_to_missing_shader_field_still_flagged():
    xml = _wrap(
        '<TimeSensor DEF="T" cycleInterval="4" loop="true"/>'
        '<Shape><Appearance><Material/>'
        '<ComposedShader DEF="S" language="GLSL">'
        '<field name="glow" type="SFFloat" accessType="inputOutput" value="0"/>'
        '</ComposedShader></Appearance><Sphere/></Shape>'
        '<ROUTE fromNode="T" fromField="fraction_changed" toNode="S" toField="glw"/>')
    report = validate_semantic(xml)
    assert "route-invalid-to-field" in report        # a typo is still caught


# ---- Naming conventions (X3D Scene Authoring Hints) ----
# Periods break ClassicVRML ROUTE syntax; hyphens become subtraction
# operators in generated identifiers. x3d.py's NMTOKEN check catches
# periods on the construction path only and accepts hyphens everywhere,
# so this Level-4 check is the sole guard covering raw-XML editing.

def test_def_with_period_warned():
    report = validate_semantic(_wrap('<Transform DEF="my.name"><Shape/></Transform>'))
    assert "naming-convention" in report
    assert "ClassicVRML ROUTE" in report


def test_def_with_hyphen_warned():
    report = validate_semantic(_wrap('<Transform DEF="my-name"><Shape/></Transform>'))
    assert "naming-convention" in report
    assert "subtraction" in report


def test_use_and_name_attrs_also_checked():
    report = validate_semantic(_wrap(
        '<Transform DEF="Ok"><Shape/></Transform>'
        '<Transform USE="bad-ref"/>'))
    assert "naming-convention" in report

    report2 = validate_semantic(_wrap(
        '<HAnimHumanoid name="humanoid.1" containerField="children"/>'))
    assert "naming-convention" in report2


def test_leading_digit_is_error():
    report = validate_semantic(_wrap('<Transform DEF="3start"><Shape/></Transform>'))
    assert "naming-convention" in report
    assert "starts with a digit" in report


def test_clean_names_not_flagged():
    report = validate_semantic(_wrap(
        '<Transform DEF="CamelCaseName"><Shape>'
        '<Appearance><Material/></Appearance><Box/></Shape></Transform>'
        '<Viewpoint description="v"/>'))
    assert "naming-convention" not in report


# ---- ROUTE-aware unused-DEF ----

def test_routed_def_is_not_reported_unused():
    xml = _wrap(
        '<TimeSensor DEF="Clock" cycleInterval="2" loop="true"/>'
        '<OrientationInterpolator DEF="Spin" key="0 1" keyValue="0 1 0 0 0 1 0 3.14"/>'
        '<Transform DEF="Spinner"><Shape><Box/></Shape></Transform>'
        '<ROUTE fromNode="Clock" fromField="fraction_changed" '
        'toNode="Spin" toField="set_fraction"/>'
        '<ROUTE fromNode="Spin" fromField="value_changed" '
        'toNode="Spinner" toField="set_rotation"/>'
    )
    report = validate_semantic(xml)
    assert "unused-def" not in report


def test_unrouted_def_still_reported_beside_routed_ones():
    xml = _wrap(
        '<TimeSensor DEF="Clock" cycleInterval="2" loop="true"/>'
        '<Transform DEF="Spinner"><Shape><Box/></Shape></Transform>'
        '<Transform DEF="Lonely"><Shape><Box/></Shape></Transform>'
        '<ROUTE fromNode="Clock" fromField="cycleTime" '
        'toNode="Spinner" toField="rotation"/>'
    )
    report = validate_semantic(xml)
    assert "unused-def" in report
    assert "Lonely" in report
    assert "'Clock'" not in report
    assert "'Spinner'" not in report


def test_exported_def_is_not_reported_unused():
    xml = _wrap(
        '<Transform DEF="Shared"><Shape><Box/></Shape></Transform>'
        '<EXPORT localDEF="Shared" AS="SharedOut"/>'
    )
    assert "unused-def" not in validate_semantic(xml)


def test_shape_use_reference_is_not_incomplete():
    """A <Shape USE='x'/> reuses the DEF'd Shape; it must not be reported as
    missing geometry or appearance (a chess board of USEd tiles produced 62
    warnings before this)."""
    src = _wrap(
        "<Shape DEF='Tile'><Appearance><Material/></Appearance><Box size='1 0.1 1'/></Shape>"
        + "".join(f"<Transform translation='{i} 0 0'><Shape USE='Tile'/></Transform>"
                  for i in range(1, 8))
    )
    report = validate_semantic(src)
    assert "shape-no-geometry" not in report
    assert "shape-no-appearance" not in report


def test_shape_with_def_and_no_geometry_still_reported():
    src = _wrap("<Shape DEF='Empty'><Appearance><Material/></Appearance></Shape>")
    assert "shape-no-geometry" in validate_semantic(src)


# ---- MF tuple counts (the XSD pattern accepts "1 2 34 5 6" as two SFVec3f) ----

def test_tuple_count_split_digits():
    xml = _wrap('<Shape><IndexedLineSet coordIndex="0 1 -1"><Coordinate point="1 2 34 5 6"/></IndexedLineSet></Shape>')
    assert "tuple-count" in validate_semantic(xml)


def test_tuple_count_whole_tuples_ok():
    xml = _wrap('<Shape><IndexedLineSet coordIndex="0 1 -1"><Coordinate point="1 2 3, 4 5 6"/></IndexedLineSet></Shape>'
                '<OrientationInterpolator key="0 1" keyValue="0 1 0 0 0 1 0 3.14"/>')
    assert "tuple-count" not in validate_semantic(xml)


# ---- GaussianSplats (X3D 4.1 draft) ----

def _splats(**attrs) -> str:
    base = {
        "positions": "0 0 0 1 1 1",
        "scales": "0.1 0.1 0.1 0.2 0.2 0.2",
        "orientations": "0 0 0 1 0 0 0.707107 0.707107",
        "opacities": "0.5 1",
        "sphericalHarmonicsDegree0Coef0": "0.5 0.5 0.5 1 0 0",
    }
    base.update(attrs)
    body = " ".join(f'{k}="{v}"' for k, v in base.items() if v is not None)
    return _wrap(f'<Viewpoint/><GaussianSplats DEF="Splats" {body}/>')


def test_gaussian_splats_consistent_ok():
    report = validate_semantic(_splats())
    for check in ("splat-", "tuple-count"):
        assert check not in report, report


def test_gaussian_splats_array_length_mismatch():
    report = validate_semantic(_splats(opacities="0.5 1 0.25"))
    assert "splat-array-length" in report and "opacities holds 3 splats" in report


def test_gaussian_splats_split_digit_positions():
    assert "tuple-count" in validate_semantic(_splats(positions="0 0 0 11 1"))


def test_gaussian_splats_log_scales():
    assert "splat-scale-space" in validate_semantic(_splats(scales="-2.3 -2.3 -2.3 -1.6 -1.6 -1.6"))


def test_gaussian_splats_logit_opacity():
    assert "splat-opacity-space" in validate_semantic(_splats(opacities="2.2 -4.6"))


def test_gaussian_splats_partial_sh_degree():
    report = validate_semantic(_splats(sphericalHarmonicsDegree1Coef0="0 0 0 0 0 0",
                                       sphericalHarmonicsDegree1Coef1="0 0 0 0 0 0"))
    assert "splat-sh-partial" in report and "sphericalHarmonicsDegree1Coef2" in report


def test_gaussian_splats_zero_quaternion():
    assert "splat-quaternion-norm" in validate_semantic(_splats(orientations="0 0 0 0 0 0 0 1"))


def _many_splats(quat: str, n: int = 120) -> str:
    return _splats(positions=" ".join(f"{i} 0 0" for i in range(n)), scales=" ".join(["0.1 0.1 0.1"] * n),
                   orientations=" ".join([quat] * n), opacities=" ".join(["0.5"] * n),
                   sphericalHarmonicsDegree0Coef0=" ".join(["0.5 0.5 0.5"] * n))


def test_gaussian_splats_quaternion_order_wxyz():
    assert "splat-quaternion-order" in validate_semantic(_many_splats("0.99 0.1 0.05 0.02"))


def test_gaussian_splats_quaternion_order_xyzw_ok():
    assert "splat-quaternion-order" not in validate_semantic(_many_splats("0.1 0.05 0.02 0.99"))


def test_gaussian_splats_quaternion_order_needs_enough_splats():
    assert "splat-quaternion-order" not in validate_semantic(_many_splats("0.99 0.1 0.05 0.02", n=10))


def test_gaussian_splats_no_base_colour():
    assert "splat-no-color" in validate_semantic(_splats(sphericalHarmonicsDegree0Coef0=None))


def test_attribute_over_10_mb_parses():
    # libxml2 refuses one attribute over 10,000,000 bytes unless huge_tree is set;
    # a GaussianSplats node passes that at about 18,000 SH degree 3 splats.
    xml = _wrap(f'<Viewpoint/><WorldInfo title="big" info=\'"{"x" * 10_500_000}"\'/>')
    report = validate_semantic(xml)
    assert "Parse Error" not in report


# ---- Routes that can never fire ----

_TOGGLE = (
    '<Shape><Box/></Shape><TouchSensor DEF="Touch"/>'
    '<BooleanToggle DEF="Toggle"/><BooleanFilter DEF="Filter"/>'
    '<IntegerTrigger DEF="Trigger" integerKey="0"/><Switch DEF="Pick" whichChoice="0"><Group/></Switch>'
    '<ROUTE fromNode="Touch" fromField="isActive" toNode="Toggle" toField="set_boolean"/>'
    '<ROUTE fromNode="Toggle" fromField="toggle_changed" toNode="Filter" toField="set_boolean"/>'
    '<ROUTE fromNode="Filter" fromField="{field}" toNode="Trigger" toField="set_boolean"/>'
    '<ROUTE fromNode="Trigger" fromField="triggerValue" toNode="Pick" toField="set_whichChoice"/>'
)


def _toggle(field: str, version: str = "4.1") -> str:
    return _wrap(_TOGGLE.format(field=field)).replace('version="4.1"', f'version="{version}"')


def test_input_false_into_integer_trigger_never_fires():
    assert "route-never-fires" in validate_semantic(_toggle("inputFalse"))


def test_input_negate_into_integer_trigger_ok():
    assert "route-never-fires" not in validate_semantic(_toggle("inputNegate"))


def test_input_false_into_integer_trigger_ok_in_3_3():
    # X3D 3.3 browsers (X_ITE for files declared <= 3.3) trigger on any set_boolean value.
    assert "route-never-fires" not in validate_semantic(_toggle("inputFalse", version="3.3"))


# ---- Regression: false positives found auditing the X3DJSONLD example mirror ----
# (audit: research/x3d-archive-audit). ProtoInstance is a stand-in for any node,
# Metadata* nodes are X3DMetadataObject via AdditionalInheritance, and a
# <ProtoInstance name="P"> exposes the interface of ProtoDeclare P.

_PROTO = ('<ProtoDeclare name="MyBox"><ProtoInterface>'
          '<field name="set_go" type="SFBool" accessType="inputOnly"/>'
          '<field name="done" type="SFBool" accessType="outputOnly"/>'
          '<field name="size" type="SFVec3f" accessType="inputOutput" value="1 1 1"/>'
          '</ProtoInterface><ProtoBody><Shape><Box/></Shape></ProtoBody></ProtoDeclare>')


def test_protoinstance_is_accepted_wherever_a_node_goes():
    xml = _wrap(_PROTO + '<Transform><ProtoInstance DEF="PI" name="MyBox"/></Transform>')
    assert "containerfield-type-mismatch" not in validate_semantic(xml)


def test_protoinstance_accepted_in_layer_children_and_shape_appearance():
    xml = _wrap(_PROTO + '<LayerSet activeLayer="1" order="0 1"><Layer>'
                '<ProtoInstance name="MyBox"/></Layer></LayerSet>')
    assert "containerfield-type-mismatch" not in validate_semantic(xml)


def test_route_to_and_from_protoinstance_element_form_is_valid():
    xml = _wrap(_PROTO + '<ProtoInstance DEF="PI" name="MyBox"/>'
                '<BooleanFilter DEF="BF"/><BooleanToggle DEF="BT"/>'
                '<ROUTE fromNode="BF" fromField="inputTrue" toNode="PI" toField="set_go"/>'
                '<ROUTE fromNode="PI" fromField="done" toNode="BT" toField="set_boolean"/>'
                '<ROUTE fromNode="BF" fromField="inputTrue" toNode="PI" toField="size"/>')
    report = validate_semantic(xml)
    assert "route-invalid-to-field" not in report
    assert "route-invalid-from-field" not in report


def test_route_to_protoinstance_missing_field_still_flagged():
    xml = _wrap(_PROTO + '<ProtoInstance DEF="PI" name="MyBox"/><BooleanFilter DEF="BF"/>'
                '<ROUTE fromNode="BF" fromField="inputTrue" toNode="PI" toField="nope"/>')
    assert "route-invalid-to-field" in validate_semantic(xml)


def test_route_to_protoinstance_with_unknown_proto_not_flagged():
    # ExternProto/unresolvable interface: cannot know the fields, so stay quiet.
    xml = _wrap('<ProtoInstance DEF="PI" name="NotDeclaredHere"/><BooleanFilter DEF="BF"/>'
                '<ROUTE fromNode="BF" fromField="inputTrue" toNode="PI" toField="whatever"/>')
    assert "route-invalid" not in validate_semantic(xml)


@pytest.mark.parametrize("meta", ["MetadataString", "MetadataInteger", "MetadataFloat",
                                  "MetadataDouble", "MetadataBoolean", "MetadataSet"])
def test_metadata_nodes_are_x3dmetadataobject(meta):
    xml = _wrap(f'<WorldInfo><{meta} containerField="metadata" name="m"/></WorldInfo>')
    assert "containerfield-type-mismatch" not in validate_semantic(xml)


def test_metadataset_value_accepts_nested_metadata():
    xml = _wrap('<WorldInfo><MetadataSet containerField="metadata" name="outer">'
                '<MetadataString containerField="value" name="s" value="&quot;x&quot;"/>'
                '<MetadataSet containerField="value" name="inner"/></MetadataSet></WorldInfo>')
    assert "containerfield-type-mismatch" not in validate_semantic(xml)


def test_real_containerfield_mismatch_still_flagged():
    xml = _wrap('<Shape><Box containerField="appearance"/></Shape>')   # Box is not an appearance
    assert "containerfield-type-mismatch" in validate_semantic(xml)


# ---- RigidBodyPhysics ----

def _rbc(inner: str, attrs: str = "") -> str:
    return _wrap(f"<RigidBodyCollection {attrs}>{inner}</RigidBodyCollection>")


def _joint(kind: str = "BallJoint", b1: str = "B1", b2: str = "B2", attrs: str = "") -> str:
    return (f"<{kind} DEF='J' {attrs}>"
            f"<RigidBody USE='{b1}' containerField='body1'/>"
            f"<RigidBody USE='{b2}' containerField='body2'/></{kind}>")


_TWO_BODIES = "<RigidBody DEF='B1'/><RigidBody DEF='B2'/>"


def _checks(report: str) -> set[str]:
    return set(re.findall(r"\*\*\[(physics-[a-z-]+)\]\*\*", report))


def test_physics_clean_scene_passes():
    report = validate_semantic(_rbc(_TWO_BODIES + _joint(
        "SingleAxisHingeJoint", attrs="axis='0 0 1'")))
    assert not _checks(report)


@pytest.mark.parametrize("mass", ["-2", "0"])
def test_physics_nonpositive_mass_is_an_error(mass):
    assert "physics-mass" in _checks(validate_semantic(
        _rbc(f"<RigidBody DEF='B1' mass='{mass}'/>")))


def test_physics_zero_mass_ok_when_fixed():
    assert "physics-mass" not in _checks(validate_semantic(
        _rbc("<RigidBody DEF='B1' mass='0' fixed='true'/>")))


def test_physics_negative_mass_is_an_error_even_when_fixed():
    assert "physics-mass" in _checks(validate_semantic(
        _rbc("<RigidBody DEF='B1' mass='-1' fixed='true'/>")))


@pytest.mark.parametrize("kind,attr", [
    ("SingleAxisHingeJoint", "axis"), ("SliderJoint", "axis"),
    ("UniversalJoint", "axis1"), ("DoubleAxisHingeJoint", "axis2"),
])
def test_physics_zero_length_joint_axis(kind, attr):
    report = validate_semantic(_rbc(_TWO_BODIES + _joint(kind, attrs=f"{attr}='0 0 0'")))
    assert "physics-zero-axis" in _checks(report)


def test_physics_zero_axis_body_orientation():
    assert "physics-zero-axis" in _checks(validate_semantic(
        _rbc("<RigidBody DEF='B1' orientation='0 0 0 1.5'/>")))


def test_physics_joint_missing_body_is_a_warning():
    report = validate_semantic(_rbc(
        "<RigidBody DEF='B1'/><BallJoint><RigidBody USE='B1' containerField='body1'/></BallJoint>"))
    assert "physics-joint-body" in _checks(report)
    assert "body2" in report


def test_physics_joint_same_body_is_an_error():
    report = validate_semantic(_rbc(_TWO_BODIES + _joint(b2="B1")))
    assert "to itself" in report and "physics-joint-body" in _checks(report)


def test_physics_joint_body_of_wrong_type():
    report = validate_semantic(_rbc(
        "<RigidBody DEF='B1'/><CollisionCollection DEF='CC' containerField='collider'/>"
        + _joint(b2="CC")))
    assert "not a RigidBody" in report


def test_physics_joint_body_in_another_collection():
    report = validate_semantic(_wrap(
        "<RigidBodyCollection><RigidBody DEF='BX'/></RigidBodyCollection>"
        f"<RigidBodyCollection><RigidBody DEF='B1'/>{_joint(b2='BX')}</RigidBodyCollection>"))
    assert "physics-joint-cross-collection" in _checks(report)


def test_physics_body_and_joint_outside_a_collection():
    report = validate_semantic(_wrap(
        "<RigidBody DEF='B1'/><RigidBody DEF='B2'/>" + _joint()))
    checks = _checks(report)
    assert "physics-not-in-collection" in checks
    assert report.count("is not a child of a RigidBodyCollection") == 3


@pytest.mark.parametrize("attr,value", [
    ("mass", "nan"), ("position", "0 inf 0"), ("linearVelocity", "1 -Infinity 0"),
])
def test_physics_non_finite_field(attr, value):
    report = validate_semantic(_rbc(f"<RigidBody DEF='B1' {attr}='{value}'/>"))
    assert "physics-non-finite" in _checks(report)


def test_physics_non_finite_in_collection_attribute():
    assert "physics-non-finite" in _checks(validate_semantic(
        _rbc("<RigidBody DEF='B1'/>", attrs="gravity='0 nan 0'")))


# ---- Spec value checks found by the data-spine mutation sweep (spine-mut v1) ----

def test_undeclared_proto_instance_flagged():
    assert "proto-undeclared" in validate_semantic(_wrap('<ProtoInstance name="Nowhere"/>'))


def test_declared_and_extern_protos_not_flagged():
    xml = _wrap(_PROTO + '<ExternProtoDeclare name="Far" url=\'"far.x3d#Far"\'/>'
                '<ProtoInstance name="MyBox"/><ProtoInstance name="Far"/>')
    assert "proto-undeclared" not in validate_semantic(xml)


def test_zero_axis_rotation_flagged_only_with_an_angle():
    assert "rotation-zero-axis" in validate_semantic(_wrap('<Transform rotation="0 0 0 1.57"><Group/></Transform>'))
    assert "rotation-zero-axis" not in validate_semantic(_wrap('<Transform rotation="0 0 0 0"><Group/></Transform>'))
    assert "rotation-zero-axis" not in validate_semantic(_wrap('<Transform rotation="0 1 0 1.57"><Group/></Transform>'))


def test_cycle_interval_zero_is_a_warning():
    report = validate_semantic(_wrap('<TimeSensor cycleInterval="0"/>'))
    assert "cycle-interval-zero" in report and "## Errors" not in report


@pytest.mark.parametrize("geom", ['<Box size="-1 2 2"/>', '<Sphere radius="0"/>',
                                  '<Cylinder height="-2"/>', '<Cone bottomRadius="-1"/>'])
def test_non_positive_sizes_flagged(geom):
    assert "value-out-of-range" in validate_semantic(_wrap(f'<Shape>{geom}</Shape>'))


def test_positive_sizes_ok():
    assert "value-out-of-range" not in validate_semantic(_wrap('<Shape><Box size="1 2 3"/></Shape><Shape><Sphere radius="0.5"/></Shape>'))


@pytest.mark.parametrize("node", ['<Fog fogType="CLOUDY"/>', '<NavigationInfo transitionType=\'"WARP"\'/>',
                                  '<Shape><Text string=\'"a"\'><FontStyle justify=\'"CENTRE"\'/></Text></Shape>',
                                  '<TextureProperties boundaryModeS="WRAP"/>'])
def test_bad_enum_flagged(node):
    assert "enum-invalid" in validate_semantic(_wrap(node))


def test_good_enums_ok():
    xml = _wrap('<Fog fogType="EXPONENTIAL"/><NavigationInfo transitionType=\'"ANIMATE" "LINEAR"\'/>'
                '<Shape><Text string=\'"a"\'><FontStyle justify=\'"MIDDLE" "MIDDLE"\' style="BOLD"/></Text></Shape>')
    assert "enum-invalid" not in validate_semantic(xml)


def test_coord_index_past_end_flagged():
    xml = _wrap('<Shape><IndexedFaceSet coordIndex="0 1 3 -1"><Coordinate point="0 0 0 1 0 0 0 1 0"/>'
                '</IndexedFaceSet></Shape>')
    assert "index-out-of-range" in validate_semantic(xml)


def test_coord_index_in_range_and_used_coordinate_ok():
    xml = _wrap('<Shape><IndexedFaceSet coordIndex="0 1 2 -1"><Coordinate DEF="C" point="0 0 0 1 0 0 0 1 0"/>'
                '</IndexedFaceSet></Shape><Shape><IndexedLineSet coordIndex="2 0 -1"><Coordinate USE="C"/>'
                '</IndexedLineSet></Shape>')
    assert "index-out-of-range" not in validate_semantic(xml)


def test_triangle_set_index_cannot_use_minus_one():
    xml = _wrap('<Shape><IndexedTriangleSet index="0 1 -1"><Coordinate point="0 0 0 1 0 0 0 1 0"/>'
                '</IndexedTriangleSet></Shape>')
    assert "index-out-of-range" in validate_semantic(xml)


def test_interpolator_keys_out_of_order_flagged():
    # the pattern found in 13 archive H-Anim files: "0 0.76 0.8 0.82 0.87 0.86 1"
    xml = _wrap('<OrientationInterpolator key="0 0.87 0.86 1" keyValue="0 1 0 0 0 1 0 1 0 1 0 2 0 1 0 3"/>')
    assert "interpolator-key-order" in validate_semantic(xml)


def test_interpolator_repeated_keys_ok():
    xml = _wrap('<ScalarInterpolator key="0 0.5 0.5 1" keyValue="0 1 2 3"/>')
    assert "interpolator-key-order" not in validate_semantic(xml)


# ---- spine-mut v2 blind spots: attribute indices and tied array counts ----

@pytest.mark.parametrize("ifs", [
    '<IndexedFaceSet coordIndex="0 1 2 -1" texCoordIndex="0 1 7 -1"><Coordinate point="0 0 0 1 0 0 0 1 0"/>'
    '<TextureCoordinate point="0 0 1 0 0 1"/></IndexedFaceSet>',
    '<IndexedFaceSet colorPerVertex="false" coordIndex="0 1 2 -1" colorIndex="4"><Coordinate point="0 0 0 1 0 0 0 1 0"/>'
    '<Color color="1 0 0"/></IndexedFaceSet>',
    '<IndexedFaceSet coordIndex="0 1 2 -1" normalIndex="0 0 9 -1"><Coordinate point="0 0 0 1 0 0 0 1 0"/>'
    '<Normal vector="0 0 1"/></IndexedFaceSet>',
])
def test_attribute_index_past_end_flagged(ifs):
    assert "index-out-of-range" in validate_semantic(_wrap(f"<Shape>{ifs}</Shape>"))


def test_attribute_indices_in_range_ok():
    xml = _wrap('<Shape><IndexedFaceSet coordIndex="0 1 2 -1" texCoordIndex="0 1 2 -1" colorIndex="0 0 1 -1" '
                'normalIndex="0 0 0 -1"><Coordinate point="0 0 0 1 0 0 0 1 0"/><TextureCoordinate point="0 0 1 0 0 1"/>'
                '<ColorRGBA color="1 0 0 1 0 1 0 1"/><Normal vector="0 0 1"/></IndexedFaceSet></Shape>'
                '<Shape><IndexedLineSet coordIndex="0 1 -1" colorIndex="0 0 -1"><Coordinate point="0 0 0 1 0 0"/>'
                '<Color DEF="K" color="1 1 1"/></IndexedLineSet></Shape>')
    assert "index-out-of-range" not in validate_semantic(xml)


@pytest.mark.parametrize("node", [
    '<Shape><ElevationGrid xDimension="3" zDimension="3" height="0 0 0 0 1 0 0 0"/></Shape>',
    '<Shape><IndexedTriangleSet index="0 1 2 0"><Coordinate point="0 0 0 1 0 0 0 1 0"/></IndexedTriangleSet></Shape>',
    '<Background skyAngle="0.8 1.4" skyColor="0 0 1 0.5 0.5 1"/>',
    '<Background groundAngle="1.5" groundColor="0.2 0.2 0.2"/>',
    '<Background skyAngle="1.2" skyColor="0 0 1"/>',
])
def test_tied_array_counts_flagged(node):
    assert "array-count" in validate_semantic(_wrap(node))


def test_tied_array_counts_ok():
    xml = _wrap('<Shape><ElevationGrid xDimension="2" zDimension="2" height="0 1 1 0"/></Shape>'
                '<Shape><IndexedTriangleSet index="0 1 2"><Coordinate point="0 0 0 1 0 0 0 1 0"/></IndexedTriangleSet></Shape>'
                '<Background skyAngle="0.8 1.4" skyColor="0 0 1 0.5 0.5 1 1 1 1" groundAngle="1.5" '
                'groundColor="0.1 0.1 0.1 0.2 0.2 0.2"/><Background/>')
    assert "array-count" not in validate_semantic(xml)


def test_lod_range_decreasing_flagged():
    assert "lod-range-order" in validate_semantic(_wrap(
        '<LOD range="50 10"><Shape><Box/></Shape><Shape><Sphere/></Shape><Group/></LOD>'))


def test_lod_range_increasing_ok():
    assert "lod-range-order" not in validate_semantic(_wrap(
        '<LOD range="10 50"><Shape><Box/></Shape><Shape><Sphere/></Shape><Group/></LOD>'))


def test_extra_sky_colours_only_warn():
    report = validate_semantic(_wrap('<Background skyColor="0 0 1 0.5 0.5 1 1 1 1"/>'))
    assert "array-count-extra" in report and "[array-count]" not in report


def test_per_face_minus_one_tolerated():
    xml = _wrap('<Shape><IndexedFaceSet normalPerVertex="false" coordIndex="0 1 2 -1" normalIndex="0 -1">'
                '<Coordinate point="0 0 0 1 0 0 0 1 0"/><Normal vector="0 0 1"/></IndexedFaceSet></Shape>')
    assert "index-out-of-range" not in validate_semantic(xml)
