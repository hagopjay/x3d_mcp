"""splat_inspect: formats, statistics, one test per rule, real files, hostile input."""

import gzip
import json
import struct

import numpy as np
import pytest

from . import splat_fixtures as spzfix
from . import splat_fixtures as plyfix
from . import test_splat_ply
from .splat_fixtures import SPLAT_PLY as SYNTH, make_cloud, needs_real, real
from transfer import chunk
from transfer.cloud import SplatError
from transfer.inspect_splats import LIMITS, detect_format, inspect_bytes, inspect_cloud
from transfer.x3dtext import x3d_document, splat_node, digit_specs


def codes(rep):
    return {r["code"] for r in rep["verdict"]["reasons"]}


def spz_bytes(cloud, **kw):
    return spzfix.write_spz(cloud, fractional_bits=kw.pop("fractional_bits", 10), **kw)


# ---- formats

def test_detect_format():
    assert detect_format(spz_bytes(make_cloud(10))) == "spz"
    assert detect_format(spz_bytes(make_cloud(10), version=3, wrap="none")) == "spz"
    assert detect_format(plyfix.write_ply(make_cloud(10))) == "ply"
    assert detect_format(b'<?xml version="1.0"?><X3D/>') == "x3d-xml"
    assert detect_format(b"  \n<X3D version='4.1'/>") == "x3d-xml"
    for junk in (b"", b"PK\x03\x04", b"{}", b"hello"):
        with pytest.raises(SplatError, match="unrecognised"):
            detect_format(junk)


def test_report_is_plain_json_for_every_format():
    cloud = make_cloud(800, sh_degree=1)
    node = splat_node(cloud, digit_specs("bridge", cloud))
    for data in (spz_bytes(cloud), plyfix.write_ply(cloud), x3d_document(node).encode()):
        rep = inspect_bytes(data)
        json.dumps(rep)
        assert rep["count"] == 800 and rep["sh_degree"] == 1 and rep["bbox"]["size"] and rep["memory"]["gpu_bytes_low"] > 0


def test_same_cloud_gives_the_same_statistics_in_every_format():
    cloud = make_cloud(1500, sh_degree=0)
    a = inspect_bytes(spz_bytes(cloud, fractional_bits=12))
    b = inspect_bytes(plyfix.write_ply(cloud))
    c = inspect_bytes(x3d_document(splat_node(cloud, digit_specs("exact", cloud))).encode())
    for other in (b, c):
        assert other["count"] == a["count"]
        assert np.allclose(other["bbox"]["size"], a["bbox"]["size"], atol=2e-3)
        assert other["opacity"]["median"] == pytest.approx(a["opacity"]["median"], abs=5e-3)


def test_spz_facts_come_from_the_header():
    rep = inspect_bytes(spz_bytes(make_cloud(300, sh_degree=2), fractional_bits=9, antialiased=True))
    assert rep["spz"] == {"version": 3, "antialiased": True, "fractional_bits": 9, "wrapped": "gzip",
                          "decoded_payload_bytes": 300 * (9 + 1 + 3 + 3 + 4 + 3 * 8)}
    assert rep["sh_degree"] == 2


def test_count_above_limit_reports_header_facts_without_decoding():
    big = gzip.compress(struct.pack("<IIIBBBB", 0x5053474E, 3, 9_000_000, 0, 12, 0, 0) + bytes(100))
    rep = inspect_bytes(big, max_points=1_000_000)
    assert rep["count"] == 9_000_000 and rep["verdict"]["level"] == "unusable" and "too-large" in codes(rep)
    big_ply = plyfix.write_ply(make_cloud(50)).replace(b"element vertex 50", b"element vertex 9000000")
    rep = inspect_bytes(big_ply, max_points=1_000_000)
    assert rep["verdict"]["level"] == "unusable" and rep["count"] == 9_000_000 and "ply-not-decoded" in codes(rep)


def test_non_splat_and_unsupported_ply_say_so():
    head = b"ply\nformat binary_little_endian 1.0\nelement vertex 2\nproperty float x\nproperty float y\nproperty float z\nend_header\n"
    rep = inspect_bytes(head + bytes(24))
    assert rep["verdict"]["level"] == "unusable" and "not a 3DGS layout" in rep["verdict"]["reasons"][0]["message"]
    rep = inspect_bytes(plyfix.write_ply(make_cloud(10)).replace(b"binary_little_endian", b"binary_big_endian"))
    assert "binary_big_endian PLY is not decoded" in rep["verdict"]["reasons"][0]["message"]
    two = test_splat_ply.ascii_ply(make_cloud(10), extra_element="element face 0\nproperty uchar x\n")
    assert "besides vertex" in inspect_bytes(two)["verdict"]["reasons"][0]["message"]


def test_ascii_ply_is_inspected_like_its_binary_twin():
    src = make_cloud(400, sh_degree=1)
    a = inspect_bytes(test_splat_ply.ascii_ply(src))
    b = inspect_bytes(plyfix.write_ply(src))
    assert a["format"] == "ply" and a["count"] == 400 and a["sh_degree"] == 1
    assert a["verdict"]["level"] == b["verdict"]["level"] and a["bbox"] == b["bbox"]
    assert a["ply"]["format"] == "ascii"


def test_ascii_ply_body_is_held_to_the_text_cap():
    from transfer.cloud import SplatError
    with pytest.raises(SplatError, match="above the limit"):
        inspect_bytes(test_splat_ply.ascii_ply(make_cloud(100)), max_text_bytes=1000)


def test_x3d_without_splats_is_an_error():
    with pytest.raises(SplatError, match="no GaussianSplats"):
        inspect_bytes(x3d_document('<Shape><Box/></Shape>').encode())


def test_x3d_text_over_the_size_cap_is_refused_before_parsing():
    cloud = make_cloud(500)
    text = x3d_document(splat_node(cloud, digit_specs("bridge", cloud))).encode()
    with pytest.raises(SplatError, match="above the limit"):
        inspect_bytes(text, max_text_bytes=len(text) - 1)
    assert inspect_bytes(text, max_text_bytes=len(text))["count"] == 500


def test_x3d_with_mismatched_arrays_is_an_error():
    cloud = make_cloud(50)
    text = x3d_document(splat_node(cloud, digit_specs("bridge", cloud)))
    bad = text.replace('opacities="', 'opacities="0.5 ', 1)
    with pytest.raises(SplatError, match="disagree"):
        inspect_bytes(bad.encode())


def test_x3d_concatenates_nodes_and_reads_the_chunk_envelope():
    from transfer import spz
    cloud = spz.read_spz(spz_bytes(make_cloud(3000, sh_degree=1)))
    files, man = chunk.build_envelope(cloud, target=700)
    total = 0
    for c in man["chunks"]:
        rep = inspect_bytes(files[c["file"]])
        assert rep["format"] == "x3d-xml" and rep["count"] == c["splats"] and rep["sh_degree"] == 1 and rep["nodes"] == 1
        total += rep["count"]
    assert total == 3000
    two = x3d_document("\n".join(splat_node(cloud.take(np.arange(i, i + 100)), digit_specs("bridge", cloud)) for i in (0, 100)))
    rep = inspect_bytes(two.encode())
    assert rep["count"] == 200 and rep["nodes"] == 2


def test_x3d_input_is_described_as_it_is():
    cloud = make_cloud(2000)
    text = x3d_document(splat_node(cloud, digit_specs("bridge", cloud))).encode()
    t = inspect_bytes(text)["x3d_text"]
    assert t["digits"] == "as-file" and t["estimated_bytes"] == len(text) and t["single_node_checkable"] is True
    assert 0 < t["max_start_tag_bytes"] < len(text)
    # a start tag over the limit is called out by name
    t_big = inspect_cloud(cloud, file_bytes=60_000_000, fmt="x3d-xml", extra={"nodes": 1, "max_start_tag_bytes": 60_000_000})
    assert t_big["x3d_text"]["single_node_checkable"] is False and "start-tag-over-limit" in codes(t_big)


# ---- sanity rules, one defect at a time

def defective(**changes):
    c = make_cloud(1000)
    for name, fn in changes.items():
        setattr(c, name, fn(getattr(c, name).copy()))
    return c


def test_clean_cloud_is_ok():
    rep = inspect_cloud(make_cloud(1000), file_bytes=1, fmt="spz")
    assert rep["verdict"]["level"] == "ok" and not {"warn", "error"} & {r["level"] for r in rep["verdict"]["reasons"]}


def test_logit_opacity_is_an_error():
    rep = inspect_cloud(defective(opacities=lambda a: a * 8 - 4), file_bytes=1, fmt="ply")
    assert rep["verdict"]["level"] == "unusable" and "opacity-range" in codes(rep)


def test_log_scale_is_an_error():
    rep = inspect_cloud(defective(scales=lambda a: np.log(a)), file_bytes=1, fmt="ply")
    assert rep["verdict"]["level"] == "unusable" and "scale-range" in codes(rep)


def test_all_invisible_is_an_error_and_mostly_transparent_a_warning():
    assert "all-invisible" in codes(inspect_cloud(defective(opacities=lambda a: a * 0), file_bytes=1, fmt="ply"))
    rep = inspect_cloud(defective(opacities=lambda a: np.where(np.arange(len(a)) % 4 == 0, a, 0.01).astype(np.float32)), file_bytes=1, fmt="ply")
    assert "mostly-transparent" in codes(rep) and rep["verdict"]["level"] == "suspect"


def test_nan_is_an_error_and_is_excluded_from_the_statistics():
    c = make_cloud(1000)
    c.positions[3, 1] = np.nan
    rep = inspect_cloud(c, file_bytes=1, fmt="ply")
    assert rep["non_finite_splats"] == 1 and rep["verdict"]["level"] == "unusable" and np.isfinite(rep["bbox"]["size"]).all()


def test_zero_and_non_unit_quaternions():
    c = make_cloud(1000)
    c.orientations[0] = 0
    c.orientations[1] *= 2
    rep = inspect_cloud(c, file_bytes=1, fmt="ply")
    assert rep["orientation"]["zero"] == 1 and rep["orientation"]["not_unit"] == 2 and {"zero-quaternion", "quaternion-not-unit"} <= codes(rep)


def test_wxyz_order_is_noticed():
    c = make_cloud(1000)
    q = np.tile(np.float32([0.98, 0.1, 0.1, 0.1]), (1000, 1))
    c.orientations = (q / np.linalg.norm(q, axis=1, keepdims=True)).astype(np.float32)
    assert "quaternion-order" in codes(inspect_cloud(c, file_bytes=1, fmt="ply"))
    c.orientations = c.orientations[:, [1, 2, 3, 0]]
    assert "quaternion-order" not in codes(inspect_cloud(c, file_bytes=1, fmt="ply"))


def test_far_floaters_are_flagged():
    c = make_cloud(2000)
    c.positions[:5] = 5000
    rep = inspect_cloud(c, file_bytes=1, fmt="ply")
    assert "position-outliers" in codes(rep) and max(rep["bbox"]["outlier_ratio"]) > LIMITS["outlier_extent_ratio"]


def test_huge_splats_and_needles_are_flagged():
    c = make_cloud(2000)
    c.scales[:10] = 100
    assert "huge-splats" in codes(inspect_cloud(c, file_bytes=1, fmt="ply"))
    c = make_cloud(2000)
    c.scales[:, 0] = 1e-5; c.scales[:, 1:] = 0.1
    assert "needle-splats" in codes(inspect_cloud(c, file_bytes=1, fmt="ply"))


def test_one_flat_axis_is_not_a_defect_but_dead_splats_are():
    c = make_cloud(2000)
    c.scales[:400, 0] = 1e-8                                   # surface-like splats: one axis flat
    rep = inspect_cloud(c, file_bytes=1, fmt="ply")
    assert rep["scale"]["flat_axis_splats"] == 400 and "degenerate-scale" not in codes(rep)
    c.scales[:50] = 1e-8                                       # every axis tiny: draws nothing
    assert "degenerate-scale" in codes(inspect_cloud(c, file_bytes=1, fmt="ply"))


def test_heavy_scene_is_flagged_by_count(monkeypatch):
    c = make_cloud(1000)
    assert "heavy-scene" not in codes(inspect_cloud(c, file_bytes=1, fmt="ply"))
    monkeypatch.setitem(LIMITS, "count_heavy", 999)            # the rule is a plain count threshold
    rep = inspect_cloud(c, file_bytes=1, fmt="ply")
    assert "heavy-scene" in codes(rep) and rep["verdict"]["level"] == "suspect" and rep["limits"]["count_heavy"] == 999


def test_memory_estimate_grows_linearly_and_orders_low_below_high():
    from transfer.inspect_splats import memory_estimates
    a, b = memory_estimates(1_000_000, 15, 1, 100), memory_estimates(2_000_000, 15, 1, 100)
    assert b["gpu_bytes_low"] == 2 * a["gpu_bytes_low"] and b["gpu_bytes_high"] == 2 * a["gpu_bytes_high"]
    assert a["gpu_bytes_low"] < a["gpu_bytes_high"] and a["float32_arrays_bytes"] == 4 * 1_000_000 * (14 + 45)


def test_empty_cloud():
    c = make_cloud(1)
    c = c.take(np.zeros(0, int))
    assert inspect_cloud(c, file_bytes=1, fmt="ply")["verdict"]["level"] == "unusable"


def test_memory_estimates_follow_the_stream_layout():
    rep = inspect_bytes(spz_bytes(make_cloud(1000, sh_degree=3)))
    m = rep["memory"]
    assert m["packed_stream_bytes"] == 1000 * (9 + 1 + 3 + 3 + 4 + 45) and m["float32_arrays_bytes"] == 4 * 1000 * (14 + 45)
    assert m["gpu_bytes_low"] == m["packed_stream_bytes"] + 8000 and m["source_file_bytes"] == len(spz_bytes(make_cloud(1000, sh_degree=3)))


def test_coordinate_frame_advice_differs_by_format():
    spz_msg = next(r["message"] for r in inspect_bytes(spz_bytes(make_cloud(300)))["verdict"]["reasons"] if r["code"] == "coordinate-frame")
    ply_msg = next(r["message"] for r in inspect_bytes(plyfix.write_ply(make_cloud(300)))["verdict"]["reasons"] if r["code"] == "coordinate-frame")
    assert "right-up-back" in spz_msg and "right-down-forward" in ply_msg and "rdf" in spz_msg and "rdf" in ply_msg


def test_no_xml_is_built():
    # inspect must not construct the X3D text of the scene: it estimates sizes from a sample
    import tracemalloc
    data = spz_bytes(make_cloud(20000))
    tracemalloc.start()
    rep = inspect_bytes(data)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert rep["x3d_text"]["estimated_bytes"] > 1_000_000 and peak < 40_000_000


# ---- real files

@needs_real
@pytest.mark.parametrize("name,count,per_node_min", [("pantheon.spz", 500000, 70000), ("athens.spz", 497556, 70000)])
def test_real_spz(name, count, per_node_min):
    rep = inspect_bytes(real(name))
    assert rep["count"] == count and rep["sh_degree"] == 0 and rep["spz"]["version"] == 3
    assert rep["verdict"]["level"] == "ok" and "chunk-before-x3d" in codes(rep)
    assert rep["x3d_text"]["max_splats_per_node"] > per_node_min and rep["x3d_text"]["chunks_needed"] == 7


@needs_real
def test_real_text_scene_reports_its_oversize_start_tag():
    import os
    from .splat_fixtures import SPLAT_DIR
    path = os.path.join(SPLAT_DIR, "txt", "pantheon_lattice.x3d")
    if not os.path.exists(path):
        pytest.skip("text scene not available")
    rep = inspect_bytes(open(path, "rb").read())
    assert rep["count"] == 500000 and rep["x3d_text"]["single_node_checkable"] is False and "start-tag-over-limit" in codes(rep)


@pytest.mark.skipif(not (SYNTH and __import__("os").path.exists(SYNTH)), reason="real PLY fixture not available")
def test_real_ply_with_sh3():
    rep = inspect_bytes(open(SYNTH, "rb").read())
    assert rep["count"] == 132923 and rep["sh_degree"] == 3 and rep["verdict"]["level"] == "ok"
    assert rep["scale"]["flat_axis_splats"] == 369 and "degenerate-scale" not in codes(rep)
    assert rep["x3d_text"]["max_splats_per_node"] < 20000          # SH3 is 59 numbers per splat


# ---- regressions found by the hostile-input pass

def test_malformed_splats_start_tag_is_reported_with_its_line():
    cloud = make_cloud(50)
    node = splat_node(cloud, digit_specs("bridge", cloud))
    text = x3d_document(node).replace("<GaussianSplats ", "\n\n<GaussianSplats broken=1 ", 1)
    with pytest.raises(SplatError, match=r"line \d+ is not well-formed"):
        inspect_bytes(text.encode())
    with pytest.raises(SplatError, match="not well-formed"):
        inspect_bytes(b"<X3D><Scene><GaussianSplats " + b'a="1" ' * 100_000)           # never closed
    with pytest.raises(SplatError, match="not well-formed"):
        inspect_bytes(b"<X3D><Scene><GaussianSplats " + b'a="1" ' * 100_000 + b"/></Scene></X3D>")      # past the attribute cap


def test_pathological_start_tags_cost_flat_memory():
    import time, tracemalloc
    from validation import oversize
    text = "<GaussianSplats " + 'a="1" ' * 1_000_000          # 6 MB, one million attributes, never closed
    tracemalloc.start(); t = time.perf_counter()
    assert oversize.find_big_start_tags(text) == []
    with pytest.raises(SplatError):
        inspect_bytes(text.encode())
    dt = time.perf_counter() - t; peak = tracemalloc.get_traced_memory()[1]; tracemalloc.stop()
    assert peak < 30_000_000 and dt < 3          # the regex version held about 470 bytes per attribute: 460 MB here


def test_odd_values_do_not_raise_numpy_warnings():
    import warnings
    c = make_cloud(400)
    c.scales[:5] = 1e30; c.orientations[:5] = 1e30; c.opacities[5] = 1e30
    data = plyfix.write_ply(c)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        rep = inspect_bytes(data)
    assert rep["verdict"]["level"] in ("suspect", "unusable")
