"""Wired legally, does nothing: Switch index, ROUTE circle, zero-size sensor, empty Script, open loop (validation/inert_checks.py).

Written with the checks from the nine misses of the blind behaviour test in x3d.loom
flywheel/sprint-20261126/fault-court-2, so these are regression tests, not a held-out score."""
from lxml import etree

from validation.inert_checks import *  # noqa: F401,F403

def S(body):
    return etree.fromstring(f"<X3D><Scene>{body}</Scene></X3D>")

SW3 = '<Switch DEF="Sw" whichChoice="0"><Box/><Sphere/><Cone/></Switch>'
def seq(v): return f'{SW3}<IntegerSequencer DEF="Q" key="0 .5 1" keyValue="{v}"/><ROUTE fromNode="Q" fromField="value_changed" toNode="Sw" toField="whichChoice"/>'
def test_sw_bad(): assert len(check_switch_index(S(seq("0 1 3")))) == 1
def test_sw_ok(): assert not check_switch_index(S(seq("0 1 2")))
def test_sw_minus1_ok(): assert not check_switch_index(S(seq("-1 1 2")))
def test_sw_static(): assert len(check_switch_index(S('<Switch whichChoice="2"><Box/><Sphere/></Switch>'))) == 1
def test_sw_trigger():
    b = SW3 + '<IntegerTrigger DEF="T" integerKey="5"/><ROUTE fromNode="T" fromField="triggerValue" toNode="Sw" toField="set_whichChoice"/>'
    assert len(check_switch_index(S(b))) == 1

T = '<Transform DEF="A"/><Transform DEF="B"/>'
R = '<ROUTE fromNode="%s" fromField="translation_changed" toNode="%s" toField="set_translation"/>'
def test_cycle_bad(): assert len(check_route_cycle(S(T + R % ("A", "B") + R % ("B", "A")))) == 1
def test_cycle_chain_ok(): assert not check_route_cycle(S(T + R % ("A", "B")))
def test_cycle_interpolator_ok():
    b = T + '<PositionInterpolator DEF="P" key="0 1" keyValue="0 0 0 1 1 1"/>' + R % ("A", "B") + \
        '<ROUTE fromNode="B" fromField="translation_changed" toNode="P" toField="set_fraction"/><ROUTE fromNode="P" fromField="value_changed" toNode="A" toField="set_translation"/>'
    assert not check_route_cycle(S(b))
def test_cycle_self(): assert len(check_route_cycle(S(T + R % ("A", "A")))) == 1

P = '<Transform DEF="T"/><ProximitySensor DEF="P" %s/><ROUTE fromNode="P" fromField="position_changed" toNode="T" toField="set_translation"/>'
def test_prox_default(): assert len(check_sensor_zero_size(S(P % ""))) == 1
def test_prox_zero(): assert len(check_sensor_zero_size(S(P % 'size="0 0 0"'))) == 1
def test_prox_sized_ok(): assert not check_sensor_zero_size(S(P % 'size="5 5 5"'))
def test_prox_unrouted_ok(): assert not check_sensor_zero_size(S('<ProximitySensor DEF="P"/>'))

SC = '<Script DEF="S" %s>%s<field name="o" type="SFFloat" accessType="outputOnly"/></Script><ROUTE fromNode="S" fromField="o" toNode="X" toField="set_f"/>'
def test_script_empty(): assert len(check_script_empty(S(SC % ("", "")))) == 1
def test_script_url_ok(): assert not check_script_empty(S(SC % ('url="a.js"', "")))
def test_script_cdata_ok():
    s = etree.fromstring('<X3D><Scene><Script DEF="S"><field name="o" type="SFFloat" accessType="outputOnly"/><![CDATA[function initialize(){}]]></Script><ROUTE fromNode="S" fromField="o" toNode="X" toField="f"/></Scene></X3D>')
    assert not check_script_empty(s)
def test_script_unrouted_ok(): assert not check_script_empty(S('<Script DEF="S"/>'))

def L(keys, loop="true", extra="", tgt="<Transform DEF='X'/>"):
    return f'{tgt}<TimeSensor DEF="C" loop="{loop}"/><OrientationInterpolator DEF="O" key="0 1" keyValue="{keys}"/>{extra}<ROUTE fromNode="C" fromField="fraction_changed" toNode="O" toField="set_fraction"/><ROUTE fromNode="O" fromField="value_changed" toNode="X" toField="set_rotation"/>'
def test_loop_bad(): assert len(check_loop_does_not_close(S(L("0 1 0 0  0 1 0 3.14")))) == 1
def test_loop_full_ok(): assert not check_loop_does_not_close(S(L("0 1 0 0  0 1 0 6.28319")))
def test_loop_false_ok(): assert not check_loop_does_not_close(S(L("0 1 0 0  0 1 0 3.14", loop="false")))
def test_loop_multiaxis_ok(): assert not check_loop_does_not_close(S(L("0 1 0 1  1 0 0 3.14")))
def test_loop_hanim_ok(): assert not check_loop_does_not_close(S(L("0 1 0 0  0 1 0 3.14", tgt="<HAnimJoint DEF='X'/>")))
def test_loop_info_with_script():
    r = check_loop_does_not_close(S(L("0 1 0 0  0 1 0 3.14", extra="<Script/>")))
    assert len(r) == 1 and r[0].level == "info"
def test_sw_placeholder_ok():
    b = '<Switch DEF="Sw" whichChoice="-1"><Group DEF="Placeholder"/></Switch><IntegerSequencer DEF="Q" key="0 1" keyValue="0 9"/><ROUTE fromNode="Q" fromField="value_changed" toNode="Sw" toField="whichChoice"/>'
    assert not check_switch_index(S(b))
def test_loop_baked_ok():
    ks = " ".join(f"0 1 0 {i*0.1}" for i in range(20)); keys = " ".join(str(i/19) for i in range(20))
    b = L(ks).replace('key="0 1"', f'key="{keys}"')
    assert not check_loop_does_not_close(S(b))


# a clock that feeds three or more tracks is a shared timeline: the open loop is a note, not a warning
def _spin(n_tracks):
    tracks = "".join(f"<PositionInterpolator DEF='P{i}' key='0 1' keyValue='0 0 0 1 0 0'/>"
                     f"<ROUTE fromNode='C' fromField='fraction_changed' toNode='P{i}' toField='set_fraction'/>"
                     for i in range(n_tracks))
    return S("<TimeSensor DEF='C' loop='true'/><Transform DEF='T'/>"
             "<OrientationInterpolator DEF='O' key='0 1' keyValue='0 1 0 0 0 1 0 2'/>"
             "<ROUTE fromNode='C' fromField='fraction_changed' toNode='O' toField='set_fraction'/>"
             "<ROUTE fromNode='O' fromField='value_changed' toNode='T' toField='set_rotation'/>" + tracks)


def test_loop_alone_warns(): assert [d.level for d in check_loop_does_not_close(_spin(0))] == ["warning"]
def test_loop_two_tracks_warns(): assert [d.level for d in check_loop_does_not_close(_spin(1))] == ["warning"]
def test_loop_shared_timeline_is_a_note(): assert [d.level for d in check_loop_does_not_close(_spin(2))] == ["info"]
