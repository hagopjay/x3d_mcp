"""Degrees written where radians are meant, an Extrusion with no path, a light pointing nowhere.

Written from the misses of the blind structure test in x3d.loom flywheel/sprint-20261126/fault-court-2b
(creaseAngle 30, fieldOfView 45, rotation 90, one spine point, direction 0 0 0), so these are regression
tests, not a held-out score."""
from validation.semantic import semantic_diagnostics

HEAD = "<X3D profile='Interchange' version='4.0'><Scene>"
TAIL = "</Scene></X3D>"
NAMES = ("angle-looks-like-degrees", "extrusion-too-few-points", "light-direction-zero")


def checks(body, head=""):
    text = f"<X3D profile='Interchange' version='4.0'>{head}<Scene>{body}{TAIL}"
    return [(d.level, d.check) for d in semantic_diagnostics(text) if d.check in NAMES]


DEG = [("warning", "angle-looks-like-degrees")]


def test_rotation_of_ninety_reads_as_degrees():
    assert checks("<Transform rotation='0 1 0 90'><Shape><Box/></Shape></Transform>") == DEG


def test_rotation_in_radians_is_quiet():
    assert checks("<Transform rotation='0 1 0 1.5708'><Shape><Box/></Shape></Transform>") == []


def test_a_whole_turn_and_a_bit_in_radians_is_quiet():
    # 7.5 and 12.566 are over two pi but are not whole multiples of five degrees
    assert checks("<Transform rotation='0 1 0 7.5'/><Transform rotation='0 1 0 12.566'/>") == []


def test_ten_radians_is_left_alone():
    # below 15 a whole number is as likely a deliberate spin as a slip
    assert checks("<Transform rotation='0 1 0 10'><Shape><Box/></Shape></Transform>") == []


def test_negative_degrees():
    assert checks("<Transform rotation='1 0 0 -45'><Shape><Box/></Shape></Transform>") == DEG


def test_crease_angle_thirty():
    assert checks("<Shape><IndexedFaceSet creaseAngle='30' coordIndex='0 1 2 -1'>"
                  "<Coordinate point='0 0 0 1 0 0 0 1 0'/></IndexedFaceSet></Shape>") == DEG


def test_crease_angle_in_radians_is_quiet():
    assert checks("<Shape><IndexedFaceSet creaseAngle='0.5236' coordIndex='0 1 2 -1'>"
                  "<Coordinate point='0 0 0 1 0 0 0 1 0'/></IndexedFaceSet></Shape>") == []


def test_field_of_view_forty_five():
    assert checks("<Viewpoint fieldOfView='45'/>") == DEG


def test_spot_cut_off_in_degrees():
    assert checks("<SpotLight cutOffAngle='30' beamWidth='20'/>") == DEG * 2


def test_unit_statement_for_angles_silences_it():
    head = "<head><unit category='angle' name='degree' conversionFactor='0.017453292519943295'/></head>"
    assert checks("<Transform rotation='0 1 0 90'><Shape><Box/></Shape></Transform>", head) == []


def test_use_is_not_reported_twice():
    assert checks("<Transform DEF='T' rotation='0 1 0 90'/><Transform USE='T'/>") == DEG


def test_extrusion_one_spine_point():
    assert checks("<Shape><Extrusion spine='0 0 0'/></Shape>") == [("error", "extrusion-too-few-points")]


def test_extrusion_one_cross_section_point():
    assert checks("<Shape><Extrusion crossSection='1 1' spine='0 0 0 0 1 0'/></Shape>") == [("error", "extrusion-too-few-points")]


def test_extrusion_defaults_and_two_points_are_quiet():
    assert checks("<Shape><Extrusion/></Shape><Shape><Extrusion spine='0 0 0 0 2 0'/></Shape>") == []


def test_directional_light_pointing_nowhere():
    assert checks("<DirectionalLight direction='0 0 0'/>") == [("warning", "light-direction-zero")]


def test_spot_light_pointing_nowhere():
    assert checks("<SpotLight direction='0 0 0'/>") == [("warning", "light-direction-zero")]


def test_lights_with_a_direction_and_point_lights_are_quiet():
    assert checks("<DirectionalLight direction='0 -1 0'/><DirectionalLight/><PointLight/>") == []
