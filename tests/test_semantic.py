"""Tests for the semantic validation pipeline.

Mirrors the 19-test inventory in
https://github.com/niknarra/x3d-mcp/blob/main/tests/test_semantic_check.py
(Nikhil Narra, Nicholas Polys -- Virginia Tech / Web3D Consortium).
"""

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
