"""Regressions from running the glTF and USD parity checks on x3d.downey's 20 scenes (26 Nov 2026).

mesh-colocated-points answers VG.016, a Mesh rule: closed polylines and tied scatter data repeat points on purpose.
coordinate-precision past 2^38 says the value is suspect, not only that the mesh will jitter."""
from validation.semantic import semantic_diagnostics

HEAD = "<X3D profile='Interchange' version='4.0'><Scene>"


def checks(body, name):
    return [d for d in semantic_diagnostics(HEAD + body + "</Scene></X3D>") if d.check == name]


def test_closed_lineset_is_not_a_weld_candidate():
    assert not checks("<Shape><LineSet vertexCount='4'><Coordinate point='0 0 0 1 0 0 1 1 0 0 0 0'/></LineSet></Shape>",
                      "mesh-colocated-points")


def test_tied_pointset_is_not_a_weld_candidate():
    assert not checks("<Shape><PointSet><Coordinate point='1 2 0 1 2 0 3 4 0'/></PointSet></Shape>", "mesh-colocated-points")


def test_mesh_with_repeated_point_is_still_reported():
    assert checks("<Shape><IndexedFaceSet coordIndex='0 1 2 -1'><Coordinate point='0 0 0 1 0 0 0 0 0'/></IndexedFaceSet></Shape>",
                  "mesh-colocated-points")


def test_line_coordinate_used_by_a_mesh_is_still_reported():
    assert checks("<Shape><LineSet vertexCount='3'><Coordinate DEF='P' point='0 0 0 1 0 0 0 0 0'/></LineSet></Shape>"
                  "<Shape><IndexedFaceSet coordIndex='0 1 2 -1'><Coordinate USE='P'/></IndexedFaceSet></Shape>",
                  "mesh-colocated-points")


def test_far_value_is_called_suspect():
    d = checks("<Shape><LineSet vertexCount='3'><Coordinate point='0 0 0 1 26736625293011112551251968 0 2 0 0'/></LineSet></Shape>",
               "coordinate-precision")
    assert len(d) == 1 and "past 2^38" in d[0].message and "overflowed or cancelled" in d[0].message


def test_near_jitter_message_unchanged():
    d = checks("<Shape><IndexedFaceSet coordIndex='0 1 2 -1'><Coordinate point='100000 0 0 100001 0 0 100000 1 0'/>"
               "</IndexedFaceSet></Shape>", "coordinate-precision")
    assert len(d) == 1 and "jitter" in d[0].message
