"""Legal X3D that plays or draws wrong (validation/plays_checks.py).

Written from the 15 misses of blind court 3 in x3d.loom flywheel/court-3, so these are regression tests, not a
held-out score. Each check has the mistake, its corrected twin, and the cases it must leave alone."""
from validation.semantic import semantic_diagnostics

TAIL = "</Scene></X3D>"
NAMES = ("timesensor-start-in-past", "drag-limits-swapped", "follower-no-smoothing", "script-handler-missing",
         "loadsensor-watches-unloaded", "loadsensor-watches-nothing", "inline-never-loads", "anchor-viewpoint-missing",
         "anchor-target-not-viewpoint", "lod-level-never-shown", "lod-range-count", "mesh-inside-out",
         "proto-field-unknown", "extrusion-cross-section-crosses", "nurbs-knot-count", "nurbs-knot-order",
         "nurbs-order", "nurbs-too-few-control-points", "nurbs-control-count", "scene-unlit", "toggle-out-of-step")
BALL = "<Shape><Appearance><Material diffuseColor='1 0 0'/></Appearance><Sphere/></Shape>"


def checks(body, profile="Immersive"):
    text = f"<X3D profile='{profile}' version='4.0'><Scene>{body}{TAIL}"
    return [(d.level, d.check) for d in semantic_diagnostics(text) if d.check in NAMES]


def messages(body):
    text = f"<X3D profile='Immersive' version='4.0'><Scene>{body}{TAIL}"
    return [d.message for d in semantic_diagnostics(text) if d.check in NAMES]


# ------------------------------------------------------------------ clock in the past
def clock(attrs):
    return (f"<Transform DEF='T'>{BALL}</Transform><TimeSensor DEF='C' cycleInterval='1.5' {attrs}/>"
            "<PositionInterpolator DEF='P' key='0 1' keyValue='0 0 0 1 1 1'/>"
            "<ROUTE fromNode='C' fromField='fraction_changed' toNode='P' toField='set_fraction'/>"
            "<ROUTE fromNode='P' fromField='value_changed' toNode='T' toField='set_translation'/>")


def test_one_cycle_that_ended_in_1970():
    assert checks(clock("loop='false' startTime='1'")) == [("warning", "timesensor-start-in-past")]


def test_looping_clock_with_a_start_time_is_quiet():
    assert checks(clock("loop='true' startTime='1'")) == []


def test_clock_started_by_a_route_is_quiet():
    body = clock("loop='false' startTime='1'") + ("<Group><TouchSensor DEF='Touch'/>" + BALL + "</Group>"
           "<ROUTE fromNode='Touch' fromField='touchTime' toNode='C' toField='set_startTime'/>")
    assert checks(body) == []


def test_a_start_time_of_today_is_quiet():
    assert checks(clock("loop='false' startTime='1795000000'")) == []


# ------------------------------------------------------------------ drag limits
def drag(attrs):
    return f"<Group><PlaneSensor DEF='D' {attrs}/>{BALL}</Group>"


def test_plane_sensor_limits_swapped():
    assert checks(drag("minPosition='2 0' maxPosition='0 0'")) == [("warning", "drag-limits-swapped")]


def test_plane_sensor_limits_in_order_are_quiet():
    assert checks(drag("minPosition='0 0' maxPosition='2 0'")) == []


def test_a_maximum_of_minus_one_means_no_limit():
    assert checks(drag("minPosition='1 1' maxPosition='-1 -1'")) == []
    assert checks(drag("")) == []


def test_cylinder_sensor_angles_swapped():
    body = f"<Group><CylinderSensor minAngle='1.5' maxAngle='0.5'/>{BALL}</Group>"
    assert checks(body) == [("warning", "drag-limits-swapped")]
    assert checks(f"<Group><CylinderSensor minAngle='0.5' maxAngle='1.5'/>{BALL}</Group>") == []


# ------------------------------------------------------------------ followers
def test_damper_with_tau_zero():
    assert checks("<PositionDamper DEF='L' tau='0'/>") == [("warning", "follower-no-smoothing")]
    assert checks("<PositionDamper DEF='L' tau='0.5'/>") == []


def test_damper_with_order_zero_and_chaser_with_no_duration():
    assert checks("<ScalarDamper order='0'/>") == [("warning", "follower-no-smoothing")]
    assert checks("<OrientationChaser duration='0'/>") == [("warning", "follower-no-smoothing")]
    assert checks("<OrientationChaser duration='2'/><ScalarDamper order='2'/>") == []


# ------------------------------------------------------------------ script handlers
def script(fn, extra=""):
    return ("<TimeSensor DEF='C' loop='true'/><Script DEF='S'>"
            "<field name='tick' type='SFTime' accessType='inputOnly'/>"
            "<field name='label' type='MFString' accessType='outputOnly'/>"
            f"<![CDATA[ecmascript:\nvar n = 0;\n{fn}\n{extra}]]></Script>"
            "<Shape><Text DEF='R' string='\"0\"'/></Shape>"
            "<ROUTE fromNode='C' fromField='cycleTime' toNode='S' toField='tick'/>"
            "<ROUTE fromNode='S' fromField='label' toNode='R' toField='set_string'/>")


def test_script_function_named_differently_from_its_field():
    body = script("function onTick(v) { n++; label = new MFString('' + n); }")
    assert checks(body) == [("warning", "script-handler-missing")]
    assert "onTick" in messages(body)[0]


def test_script_function_named_after_its_field_is_quiet():
    assert checks(script("function tick(v) { n++; label = new MFString('' + n); }")) == []
    assert checks(script("tick = function (v) { n++; };")) == []
    assert checks(script("const tick = (v) => { n++; };")) == []


def test_script_with_events_processed_is_only_a_note():
    body = script("function onTick(v) { n++; }", "function eventsProcessed() { label = new MFString('x'); }")
    assert checks(body) == [("info", "script-handler-missing")]


def test_script_in_a_file_is_left_alone():
    body = ("<TimeSensor DEF='C' loop='true'/><Script DEF='S' url='\"count.js\"'>"
            "<field name='tick' type='SFTime' accessType='inputOnly'/></Script>"
            "<ROUTE fromNode='C' fromField='cycleTime' toNode='S' toField='tick'/>")
    assert checks(body) == []


# ------------------------------------------------------------------ loading
def test_load_sensor_watching_something_that_never_loads():
    body = ("<LoadSensor DEF='L'><Inline DEF='M' load='false' url='\"a.x3d\"'/></LoadSensor><PointLight DEF='P' on='false'/>"
            "<ROUTE fromNode='L' fromField='isLoaded' toNode='P' toField='set_on'/>")
    assert checks(body) == [("warning", "loadsensor-watches-unloaded")]
    assert checks(body.replace(" load='false'", "")) == []


def test_load_sensor_whose_child_is_loaded_by_a_route_is_quiet():
    body = ("<LoadSensor DEF='L'><Inline DEF='M' load='false' url='\"a.x3d\"'/></LoadSensor>"
            f"<Group><TouchSensor DEF='T'/>{BALL}</Group>"
            "<ROUTE fromNode='T' fromField='isActive' toNode='M' toField='set_load'/>")
    assert checks(body) == []


def test_load_sensor_with_nothing_to_watch():
    body = ("<LoadSensor DEF='L'/><PointLight DEF='P' on='false'/>"
            "<ROUTE fromNode='L' fromField='isLoaded' toNode='P' toField='set_on'/>")
    assert checks(body) == [("warning", "loadsensor-watches-nothing")]


def test_inline_left_unloaded_is_a_note():
    assert checks("<Inline load='false' url='\"a.x3d\"'/>") == [("info", "inline-never-loads")]


# ------------------------------------------------------------------ anchors
def anchor(url, vp="CloseUp"):
    return (f"<Viewpoint DEF='Wide' position='0 0 12'/><Viewpoint DEF='{vp}' position='0 0 3'/>"
            f"<Anchor url='{url}'>{BALL}</Anchor>")


def test_anchor_to_a_viewpoint_in_the_wrong_case():
    body = anchor('"#Closeup"')
    assert checks(body) == [("warning", "anchor-viewpoint-missing")]
    assert "'CloseUp'" in messages(body)[0]


def test_anchor_to_an_existing_viewpoint_is_quiet():
    assert checks(anchor('"#CloseUp"')) == []


def test_anchor_with_a_working_second_url_is_quiet():
    assert checks(anchor('"#Nowhere" "https://example.org/page.html"')) == []


def test_anchor_to_a_node_that_is_not_a_viewpoint():
    body = f"<Transform DEF='Door'>{BALL}</Transform><Anchor url='\"#Door\"'>{BALL}</Anchor>"
    assert checks(body) == [("warning", "anchor-target-not-viewpoint")]


# ------------------------------------------------------------------ LOD
def lod(rng, n=3):
    return f"<LOD range='{rng}'>" + BALL * n + "</LOD>"


def test_lod_with_as_many_ranges_as_children():
    assert checks(lod("0 5 15")) == [("warning", "lod-level-never-shown"), ("warning", "lod-range-count")]
    assert checks(lod("5 15 30")) == [("warning", "lod-range-count")]


def test_lod_with_one_range_fewer_than_children_is_quiet():
    assert checks(lod("5 15")) == []


def test_lod_with_children_no_range_reaches():
    assert checks(lod("5", n=4)) == [("warning", "lod-level-never-shown")]


def test_lod_without_a_range_is_left_to_the_browser():
    assert checks("<LOD>" + BALL * 3 + "</LOD>") == []


# ------------------------------------------------------------------ winding
CUBE_PTS = "-1 -1 -1, 1 -1 -1, 1 1 -1, -1 1 -1, -1 -1 1, 1 -1 1, 1 1 1, -1 1 1"
CUBE_OUT = "4 5 6 7 -1 1 0 3 2 -1 5 1 2 6 -1 0 4 7 3 -1 7 6 2 3 -1 0 1 5 4 -1"      # counter-clockwise seen from outside


def cube(attrs="", index=CUBE_OUT, before=""):
    return (f"{before}<Shape><IndexedFaceSet coordIndex='{index}' {attrs}><Coordinate point='{CUBE_PTS}'/>"
            "</IndexedFaceSet></Shape>")


def test_outward_cube_marked_clockwise_is_inside_out():
    assert checks(cube("ccw='false'")) == [("warning", "mesh-inside-out")]


def test_outward_cube_is_quiet():
    assert checks(cube()) == []


def test_inward_cube_marked_counter_clockwise_is_inside_out():
    inward = " ".join(" ".join(reversed(f.split())) + " -1" for f in CUBE_OUT.split("-1") if f.strip())
    assert checks(cube(index=inward)) == [("warning", "mesh-inside-out")]
    assert checks(cube("ccw='false'", index=inward)) == []


def test_two_sided_cube_and_open_mesh_are_left_alone():
    assert checks(cube("ccw='false' solid='false'")) == []
    assert checks(cube("ccw='false'", index="4 5 6 7 -1 1 0 3 2 -1")) == []


def test_a_room_seen_from_inside_is_left_alone():
    assert checks(cube("ccw='false'", before="<Viewpoint position='0 0 0.5'/>")) == []


def test_a_mirrored_cube_is_left_alone():
    body = "<Transform scale='-1 1 1'>" + cube("ccw='false'") + "</Transform>"
    assert checks(body) == []


# ------------------------------------------------------------------ prototypes
PROTO = ("<ProtoDeclare name='Tower'><ProtoInterface>"
         "<field accessType='initializeOnly' type='SFColor' name='towerColor' value='0.5 0.5 0.5'/>"
         "</ProtoInterface><ProtoBody><Shape><Appearance><Material>"
         "<IS><connect nodeField='diffuseColor' protoField='towerColor'/></IS></Material></Appearance><Box/></Shape>"
         "</ProtoBody></ProtoDeclare>")


def test_proto_instance_sets_a_field_the_prototype_lacks():
    body = PROTO + "<ProtoInstance name='Tower'><fieldValue name='towerColour' value='1 0 0'/></ProtoInstance>"
    assert checks(body) == [("error", "proto-field-unknown")]
    assert "'towerColor'" in messages(body)[0]


def test_proto_instance_with_declared_fields_is_quiet():
    assert checks(PROTO + "<ProtoInstance name='Tower'><fieldValue name='towerColor' value='1 0 0'/></ProtoInstance>") == []


def test_extern_proto_fields_are_checked_too():
    ext = ("<ExternProtoDeclare name='Lamp' url='\"lamp.x3d#Lamp\"'><field accessType='inputOutput' type='SFBool' name='lit'/>"
           "</ExternProtoDeclare>")
    assert checks(ext + "<ProtoInstance name='Lamp'><fieldValue name='lite' value='true'/></ProtoInstance>") == [
        ("error", "proto-field-unknown")]
    assert checks(ext + "<ProtoInstance name='Lamp'><fieldValue name='lit' value='true'/></ProtoInstance>") == []


# ------------------------------------------------------------------ extrusion outline
def test_bow_tie_cross_section():
    body = "<Shape><Extrusion crossSection='1 1, -1 -1, -1 1, 1 -1, 1 1' spine='0 -1 0, 0 1 0'/></Shape>"
    assert checks(body) == [("warning", "extrusion-cross-section-crosses")]


def test_square_and_open_cross_sections_are_quiet():
    assert checks("<Shape><Extrusion crossSection='1 1, 1 -1, -1 -1, -1 1, 1 1' spine='0 -1 0, 0 1 0'/></Shape>") == []
    assert checks("<Shape><Extrusion crossSection='0 0, 1 0.2, 2 0, 3 0.2' spine='0 -1 0, 0 1 0'/></Shape>") == []
    assert checks("<Shape><Extrusion/></Shape>") == []


# ------------------------------------------------------------------ NURBS
GRID16 = ", ".join(f"{x} 0 {z}" for x in range(4) for z in range(4))


def patch(uknot, vknot="0 0 0 1 2 2 2", pts=GRID16):
    return (f"<Shape><NurbsPatchSurface uDimension='4' vDimension='4' uOrder='3' vOrder='3' uKnot='{uknot}' vKnot='{vknot}'>"
            f"<Coordinate containerField='controlPoint' point='{pts}'/></NurbsPatchSurface></Shape>")


def test_knot_vector_one_short():
    assert checks(patch("0 0 0 1 2 2"), profile="Full") == [("warning", "nurbs-knot-count")]


def test_knot_vector_of_the_right_length_is_quiet():
    assert checks(patch("0 0 0 1 2 2 2"), profile="Full") == []
    assert checks("<Shape><NurbsPatchSurface uDimension='4' vDimension='4'>"
                  f"<Coordinate containerField='controlPoint' point='{GRID16}'/></NurbsPatchSurface></Shape>", profile="Full") == []


def test_knot_vector_that_goes_down():
    assert checks(patch("0 0 0 2 1 2 2"), profile="Full") == [("warning", "nurbs-knot-order")]


def test_surface_with_the_wrong_number_of_control_points():
    assert checks(patch("0 0 0 1 2 2 2", pts="0 0 0, 1 0 0, 2 0 0"), profile="Full") == [("error", "nurbs-control-count")]


def test_curve_knots_and_too_few_points():
    curve = "<Shape><NurbsCurve order='3' knot='{k}'><Coordinate containerField='controlPoint' point='{p}'/></NurbsCurve></Shape>"
    four = "0 0 0, 1 1 0, 2 0 0, 3 1 0"
    assert checks(curve.format(k="0 0 0 1 2 2", p=four), profile="Full") == [("warning", "nurbs-knot-count")]
    assert checks(curve.format(k="0 0 0 1 2 2 2", p=four), profile="Full") == []
    assert checks(curve.format(k="", p="0 0 0, 1 1 0"), profile="Full") == [("error", "nurbs-too-few-control-points")]


# ------------------------------------------------------------------ lighting
def test_headlight_off_and_no_light():
    assert checks(f"<NavigationInfo headlight='false'/>{BALL}") == [("warning", "scene-unlit")]


def test_headlight_off_with_a_light_is_quiet():
    assert checks(f"<NavigationInfo headlight='false'/><DirectionalLight/>{BALL}") == []


def test_headlight_off_with_every_light_switched_off():
    assert checks(f"<NavigationInfo headlight='false'/><PointLight on='false'/>{BALL}") == [("warning", "scene-unlit")]


def test_glowing_and_unlit_shapes_need_no_light():
    glow = "<Shape><Appearance><Material emissiveColor='1 1 0'/></Appearance><Box/></Shape>"
    bare = "<Shape><Box/></Shape>"
    assert checks(f"<NavigationInfo headlight='false'/>{glow}{bare}") == []


def test_a_shape_drawn_by_a_shader_needs_no_light():
    shaded = ("<Shape><Appearance><Material/><ComposedShader language='GLSL'><ShaderPart type='VERTEX'/>"
              "<ShaderPart type='FRAGMENT'/></ComposedShader></Appearance><Box/></Shape>")
    assert checks(f"<NavigationInfo headlight='false'/>{shaded}", profile="Full") == []


def test_a_scene_with_an_inline_is_not_judged_for_light():
    assert checks(f"<NavigationInfo headlight='false'/><Inline url='\"lights.x3d\"'/>{BALL}") == []


# ------------------------------------------------------------------ toggle
def toggle(clock_attrs="", toggle_attrs=""):
    return (f"<Transform DEF='Spinner'>{BALL}</Transform><Group><TouchSensor DEF='Touch'/>{BALL}</Group>"
            f"<BooleanToggle DEF='Flip' {toggle_attrs}/><TimeSensor DEF='Clock' loop='true' {clock_attrs}/>"
            "<OrientationInterpolator DEF='Turn' key='0 0.5 1' keyValue='0 1 0 0 0 1 0 3.14 0 1 0 6.28'/>"
            "<ROUTE fromNode='Touch' fromField='isActive' toNode='Flip' toField='set_boolean'/>"
            "<ROUTE fromNode='Flip' fromField='toggle_changed' toNode='Clock' toField='set_enabled'/>"
            "<ROUTE fromNode='Clock' fromField='fraction_changed' toNode='Turn' toField='set_fraction'/>"
            "<ROUTE fromNode='Turn' fromField='value_changed' toNode='Spinner' toField='set_rotation'/>")


def test_clock_running_before_the_toggle_that_should_start_it():
    assert checks(toggle()) == [("warning", "toggle-out-of-step")]


def test_toggle_and_clock_that_start_in_step_are_quiet():
    assert checks(toggle(clock_attrs="enabled='false'")) == []
    assert checks(toggle(toggle_attrs="toggle='true'")) == []
