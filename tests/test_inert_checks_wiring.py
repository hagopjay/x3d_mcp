"""Wired legally, does nothing: sensors, clocks and outputs (validation/inert_checks.py).

Written with the checks from the nine misses of the blind behaviour test in x3d.loom
flywheel/sprint-20261126/fault-court-2, so these are regression tests, not a held-out score."""
from lxml import etree

from validation.inert_checks import *  # noqa: F401,F403

def S(body):
    return etree.fromstring(f"<X3D><Scene>{body}</Scene></X3D>").find("Scene")
def run(f, body):
    return [(d.level, d.check) for d in f(S(body))]
SH = "<Shape><Box/></Shape>"

# sensor
def test_s_lonely_group():
    assert ("warning", "sensor-senses-nothing") in run(check_sensor_senses_nothing, f"{SH}<Group><TouchSensor DEF='t'/></Group>")
def test_s_direct_scene():
    assert ("warning", "sensor-senses-nothing") in run(check_sensor_senses_nothing, "<TouchSensor DEF='t'/><Transform/>")
def test_s_disabled():
    r = run(check_sensor_senses_nothing, f"<Group>{SH}<TouchSensor DEF='t' enabled='false'/></Group>")
    assert ("warning", "sensor-disabled-never-enabled") in r
def test_s_ok_sibling_and_nested():
    assert run(check_sensor_senses_nothing, f"<Transform><TouchSensor/><Group>{SH}</Group></Transform>") == []
def test_s_disabled_but_routed():
    b = f"<Group>{SH}<TouchSensor DEF='t' enabled='false'/></Group><BooleanToggle DEF='b'/><ROUTE fromNode='b' fromField='toggle' toNode='t' toField='set_enabled'/>"
    assert run(check_sensor_senses_nothing, b) == []
def test_s_script_info():
    assert run(check_sensor_senses_nothing, "<Script/><Group><PlaneSensor/></Group>") == [("info", "sensor-senses-nothing")]
def test_s_use_group():
    assert run(check_sensor_senses_nothing, f"<Group DEF='g'>{SH}</Group><Group><TouchSensor/><Group USE='g'/></Group>") == []
def test_s_is():
    assert run(check_sensor_senses_nothing, "<Group><TouchSensor><IS><connect nodeField='enabled' protoField='e'/></IS></TouchSensor></Group>") == []

# clock
R = lambda a, af, b, bf: f"<ROUTE fromNode='{a}' fromField='{af}' toNode='{b}' toField='{bf}'/>"
FEED = "<PositionInterpolator DEF='p' key='0 1' keyValue='0 0 0 1 1 1'/>" + R("c", "fraction_changed", "p", "set_fraction")
def test_c_never():
    assert ("warning", "timesensor-never-starts") in run(check_clock_never_starts, "<TimeSensor DEF='c'/>" + FEED)
def test_c_loop_ok():
    assert run(check_clock_never_starts, "<TimeSensor DEF='c' loop='true'/>" + FEED) == []
def test_c_started_by_route():
    assert run(check_clock_never_starts, "<TimeSensor DEF='c'/><TouchSensor DEF='t'/>" + FEED + R("t", "touchTime", "c", "startTime")) == []
def test_c_unused_quiet():
    assert run(check_clock_never_starts, "<TimeSensor DEF='c'/>") == []
def test_c_stop_ignored():
    assert ("info", "timesensor-stop-ignored") in run(check_clock_never_starts, "<TimeSensor DEF='c' startTime='5' stopTime='2'/>")
def test_c_proto_info():
    assert run(check_clock_never_starts, "<ProtoInstance name='x'/><TimeSensor DEF='c'/>" + FEED) == [("info", "timesensor-never-starts")]
def test_c_enabled_alias():
    assert run(check_clock_never_starts, "<TimeSensor DEF='c'/>" + FEED + R("z", "x", "c", "enabled")) == []

# output
def test_o_nowhere():
    b = "<TimeSensor DEF='c' loop='true'/><BooleanToggle DEF='b'/>" + R("c", "isActive", "b", "set_boolean")
    assert ("warning", "output-goes-nowhere") in run(check_output_goes_nowhere, b)
def test_o_no_input():
    b = "<Transform DEF='t'/><PositionInterpolator DEF='p' key='0 1' keyValue='0 0 0 1 1 1'/>" + R("p", "value_changed", "t", "set_translation")
    assert ("warning", "input-from-nowhere") in run(check_output_goes_nowhere, b)
def test_o_ok_chain():
    b = "<Transform DEF='t'/><TimeSensor DEF='c' loop='true'/><PositionInterpolator DEF='p'/>" + R("c", "fraction_changed", "p", "set_fraction") + R("p", "value_changed", "t", "translation")
    assert run(check_output_goes_nowhere, b) == []
def test_o_script_info():
    b = "<Script/><BooleanToggle DEF='b'/>" + R("x", "y", "b", "set_boolean")
    assert run(check_output_goes_nowhere, b) == [("info", "output-goes-nowhere")]
def test_o_is_exempt():
    b = "<BooleanToggle DEF='b'><IS><connect nodeField='toggle' protoField='q'/></IS></BooleanToggle>" + R("x", "y", "b", "set_boolean")
    assert run(check_output_goes_nowhere, b) == []
def test_o_alias_same_field():
    b = "<Transform DEF='t'/><BooleanToggle DEF='b'/>" + R("a", "x", "b", "set_toggle") + R("b", "toggle_changed", "t", "visible")
    assert run(check_output_goes_nowhere, b) == []

def test_s_many_nodes_gc_regression():
    joints = "".join("<Transform><Group/></Transform>" for _ in range(300))
    assert run(check_sensor_senses_nothing, f"<Group><TouchSensor/><Transform>{joints}{SH}</Transform></Group>") == []

def test_c_disabled_is_info():
    assert run(check_clock_never_starts, "<TimeSensor DEF='c' loop='true' enabled='false'/>" + FEED) == [("info", "timesensor-never-starts")]
