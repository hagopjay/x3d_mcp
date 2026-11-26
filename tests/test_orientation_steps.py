"""OrientationInterpolator steps and the shortest path (Loom failure modes 357, 361, 451).

Found by the self-coding wild set (x3d.loom selfcode/wild): a turntable with keys 0, pi, 2 pi rocks, a writer that
rounded 3.14159265 to 3.141593 turned a half turn the other way, and a "spin" with keys 0 and 6.28319 does not move."""
from validation.semantic import semantic_diagnostics

HEAD = "<X3D profile='Interchange' version='4.0'><Scene>"
NAMES = ("orientation-half-turn-step", "orientation-step-long-way", "orientation-never-turns")


def checks(values, keys=None):
    n = len(values.split()) // 4
    keys = keys or " ".join(f"{i / (n - 1):g}" for i in range(n))
    text = f"{HEAD}<OrientationInterpolator DEF='O' key='{keys}' keyValue='{values}'/></Scene></X3D>"
    return [d.check for d in semantic_diagnostics(text) if d.check in NAMES]


def test_zero_pi_two_pi_with_eight_decimals_rocks():
    # 3.14159265 is under pi (forward), 6.28318531 - 3.14159265 is over it (back): the turntable rocks
    assert checks("0 1 0 0 0 1 0 3.14159265 0 1 0 6.28318531") == ["orientation-step-long-way", "orientation-half-turn-step"]


def test_pi_rounded_up_spins_backwards():
    assert checks("0 1 0 0 0 1 0 3.1416 0 1 0 6.2832") == ["orientation-step-long-way"] * 2


def test_pi_rounded_down_plays_as_written_and_is_only_a_note():
    text = f"{HEAD}<OrientationInterpolator DEF='O' key='0 0.5 1' keyValue='0 1 0 0 0 1 0 3.14159 0 1 0 6.28318'/></Scene></X3D>"
    d = [x for x in semantic_diagnostics(text) if x.check in NAMES]
    assert [(x.check, x.level) for x in d] == [("orientation-half-turn-step", "info")]


def test_six_decimal_pi_runs_the_other_way():
    assert checks("0 1 0 0 0 1 0 3.141593") == ["orientation-step-long-way"]


def test_whole_turn_in_one_step_never_turns():
    assert checks("0 1 0 0 0 1 0 6.28319") == ["orientation-never-turns"]


def test_step_past_pi_plays_the_other_way():
    assert checks("0 1 0 0 0 1 0 4.0") == ["orientation-step-long-way"]


def test_wrapped_angle_in_a_baked_animation_is_left_alone():
    # 6.2 back to 0.1 is a wrap: it plays 0.18 rad forward, the way its neighbours are written
    assert not checks("0 1 0 5.8 0 1 0 6.0 0 1 0 6.2 0 1 0 0.1 0 1 0 0.3")


def test_wrap_to_a_negative_angle_is_left_alone():
    assert not checks("0 1 0 1.5708 0 1 0 2.35619 0 1 0 3.1 0 1 0 -2.35619 0 1 0 -1.5708")


def test_nearly_a_whole_turn_never_turns():
    assert checks("0 1 0 0 0 1 0 6.28") == ["orientation-never-turns"]


def test_four_key_turntable_is_clean():
    assert not checks("0 1 0 0 0 1 0 2.0944 0 1 0 4.18879 0 1 0 6.28319")


def test_quarter_turn_and_hold_are_clean():
    assert not checks("0 1 0 0 0 1 0 1.5708 0 1 0 1.5708 0 1 0 0")


def test_opposite_axes_half_turn():
    assert checks("0 1 0 1.5707963 0 -1 0 1.5707963") == ["orientation-half-turn-step"]


def test_change_of_axis_is_clean():
    assert not checks("1 0 0 1.2 0 1 0 2.0")


def test_zero_axis_is_left_to_the_axis_check():
    assert not checks("0 0 0 0 0 1 0 1")
