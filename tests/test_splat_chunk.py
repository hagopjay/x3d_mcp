"""Chunking: partitions, the envelope, the manifest, digits, and the hostile-input regressions."""

import hashlib
import json
import tarfile
import io

import numpy as np
import pytest
from lxml import etree

from . import splat_fixtures as spzfix
from .splat_fixtures import make_cloud
from transfer import chunk, spz
from transfer.cloud import SplatError
from transfer.inspect_splats import cloud_from_x3d
from validation.splat_arrays import parse_numbers

X3D = "{http://www.web3d.org/specifications/x3d-4.1.xsd}"


def spz_cloud(n=6000, sh_degree=0, fractional_bits=10, seed=3):
    return spz.read_spz(spzfix.write_spz(make_cloud(n, sh_degree=sh_degree, seed=seed), fractional_bits=fractional_bits, antialiased=True))


def pts(n=5000, seed=1):
    return np.random.default_rng(seed).normal(size=(n, 3)) * [10, 4, 7]


# ---- partitioning

@pytest.mark.parametrize("method", chunk.METHODS)
def test_partition_is_a_disjoint_cover_under_target(method):
    p = pts()
    parts = chunk.partition(p, target=700, method=method)
    allidx = np.concatenate(parts)
    assert np.array_equal(np.sort(allidx), np.arange(len(p)))
    assert max(len(i) for i in parts) <= 700 and all(len(i) for i in parts)


def test_kd_counts_are_balanced():
    parts = chunk.partition(pts(5000), target=700, method="kd")
    assert len(parts) == 8 and max(map(len, parts)) - min(map(len, parts)) <= 1


@pytest.mark.parametrize("method", chunk.METHODS)
def test_chunks_are_spatially_compact(method):
    # a compact cut puts each chunk in a small share of the scene volume; a file-order cut would span all of it
    p = pts(8000)
    parts = chunk.partition(p, target=500, method=method)
    vol = lambda ids: np.prod(p[ids].max(0) - p[ids].min(0))
    whole = np.prod(p.max(0) - p.min(0))
    file_order = np.array_split(np.arange(len(p)), len(parts))          # same number of chunks, cut in file order
    spatial, naive = np.mean([vol(i) for i in parts]) / whole, np.mean([vol(i) for i in file_order]) / whole
    assert spatial < naive / 5          # measured on this data: kd 0.039 vs 0.504, octree 0.006 vs 0.289, grid 0.015 vs 0.408


@pytest.mark.parametrize("method", chunk.METHODS)
def test_partition_keeps_the_target_when_points_coincide(method):
    # 3000 identical points cannot be told apart by position; the cut still must not exceed the target
    p = np.zeros((3000, 3))
    parts = chunk.partition(p, target=400, method=method)
    assert max(map(len, parts)) <= 400 and sum(map(len, parts)) == 3000


@pytest.mark.parametrize("method", chunk.METHODS)
def test_partition_small_and_empty_inputs(method):
    assert chunk.partition(np.zeros((0, 3)), target=10, method=method) == []
    one = chunk.partition(np.array([[1.0, 2, 3]]), target=10, method=method)
    assert len(one) == 1 and one[0].tolist() == [0]
    flat = chunk.partition(np.c_[np.arange(100.0), np.zeros(100), np.zeros(100)], target=30, method=method)   # a line
    assert max(map(len, flat)) <= 30 and sum(map(len, flat)) == 100


def test_partition_errors():
    with pytest.raises(SplatError, match="unknown method"):
        chunk.partition(pts(10), target=5, method="bsp")
    with pytest.raises(SplatError, match="at least 1"):
        chunk.partition(pts(10), target=0)
    with pytest.raises(SplatError, match="above the limit"):
        chunk.partition(pts(chunk.MAX_CHUNKS + 5), target=1)


def test_min_fraction_folds_tiny_leaves():
    p = np.vstack([pts(4000), pts(7, seed=9) + 200])            # a handful of far-away splats make tiny leaves
    raw = chunk.partition(p, target=600, method="octree")
    merged = chunk.partition(p, target=600, method="octree", min_fraction=0.2)
    assert min(map(len, raw)) < 120 <= 600
    assert min(map(len, merged)) >= 120 or len(merged) < len(raw)
    assert max(map(len, merged)) <= 600 and sum(map(len, merged)) == len(p)


# ---- envelope

@pytest.fixture(scope="module")
def env():
    cloud = spz_cloud(6000, sh_degree=1)
    files, man = chunk.build_envelope(cloud, name="demo", target=900, url_prefix="")
    return cloud, files, man


def test_envelope_files_and_manifest(env):
    cloud, files, man = env
    assert sorted(files) == sorted(["scene.x3d", "manifest.json"] + [c["file"] for c in man["chunks"]])
    assert man["schema"] == chunk.SCHEMA and man["source"]["splats"] == 6000 and man["source"]["sh_degree"] == 1
    assert sum(c["splats"] for c in man["chunks"]) == 6000 and len(man["chunks"]) == 7
    assert man["partition"]["max_splats"] <= 900 and man["partition"]["chunks"] == 7
    assert json.loads(files["manifest.json"]) == man
    assert chunk.verify_manifest(files, man) == []
    for c in man["chunks"]:
        assert hashlib.sha256(files[c["file"]]).hexdigest() == c["sha256"] and len(files[c["file"]]) == c["bytes"]


def test_manifest_detects_tampering(env):
    _, files, man = env
    bad = dict(files); bad["chunks/c001.x3d"] = bad["chunks/c001.x3d"].replace(b"0", b"1", 1)
    assert chunk.verify_manifest(bad, man) == ["chunks/c001.x3d: sha256 differs"]
    short = dict(files); short["chunks/c002.x3d"] = short["chunks/c002.x3d"][:-1]
    assert "bytes, manifest says" in chunk.verify_manifest(short, man)[0]
    gone = {k: v for k, v in files.items() if k != "chunks/c003.x3d"}
    assert chunk.verify_manifest(gone, man) == ["chunks/c003.x3d: missing"]


def test_master_scene_is_wellformed_and_points_at_every_chunk(env):
    _, files, man = env
    root = etree.fromstring(files["scene.x3d"])
    assert root.get("version") == "4.1"
    inl = root.findall(".//Inline")
    assert [i.get("url") for i in inl] == ['"%s"' % c["file"] for c in man["chunks"]]
    for i, c in zip(inl, man["chunks"]):
        assert i.get("bboxCenter") and i.get("bboxSize") and i.get("DEF") == "IN_" + c["id"]
    assert root.find(".//Viewpoint") is not None


def test_chunk_files_hold_one_node_each_with_boxes(env):
    cloud, files, man = env
    lo, hi = cloud.positions.min(0), cloud.positions.max(0)
    for c in man["chunks"]:
        root = etree.fromstring(files[c["file"]])
        nodes = root.findall(".//GaussianSplats")
        assert len(nodes) == 1
        node = nodes[0]
        centre = parse_numbers(node.get("bboxCenter")); size = parse_numbers(node.get("bboxSize"))
        assert centre.tolist() == pytest.approx(c["written_bbox"]["center"], abs=1e-3)
        n = c["splats"]
        assert parse_numbers(node.get("positions")).size == 3 * n and parse_numbers(node.get("orientations")).size == 4 * n
        # cull_safe: the written box covers the whole scene from the chunk centre; the tight box is in the manifest
        assert (centre - size / 2 <= lo + 1e-3).all() and (centre + size / 2 >= hi - 1e-3).all()
        tc, ts = np.array(c["bbox"]["center"]), np.array(c["bbox"]["size"])
        assert tc.tolist() == pytest.approx(centre.tolist(), abs=1e-3) and (ts <= size + 1e-3).all()


def test_tight_box_mode():
    cloud = spz_cloud(3000)
    files, man = chunk.build_envelope(cloud, target=800, bbox_mode="tight")
    for c in man["chunks"]:
        assert c["bbox"] == c["written_bbox"]
        sub = parse_numbers(etree.fromstring(files[c["file"]]).find(".//GaussianSplats").get("positions")).reshape(-1, 3)
        half = np.array(c["bbox"]["size"]) / 2
        assert (np.abs(sub - c["bbox"]["center"]) <= half + 1e-3).all()


def test_lattice_digits_round_trip_the_spz_values_exactly():
    cloud = spz_cloud(4000, sh_degree=2, fractional_bits=11)
    files, man = chunk.build_envelope(cloud, target=1000)
    got = []
    for c in man["chunks"]:
        cl, _ = cloud_from_x3d(files[c["file"]].decode(), max_points=10 ** 7)
        got.append(cl)
    pos = np.concatenate([g.positions for g in got]); op = np.concatenate([g.opacities for g in got])
    sc = np.concatenate([g.scales for g in got]); sh = np.concatenate([g.sh for g in got]); dc = np.concatenate([g.dc for g in got])
    q = np.concatenate([g.orientations for g in got])
    # order differs (spatial cut); sort both sides by a key that is unique per splat
    def key(x): return np.lexsort((x[:, 2], x[:, 1], x[:, 0]))
    a, b = key(pos), key(cloud.positions)
    assert np.array_equal(np.round(pos[a] * 2 ** 11), np.round(cloud.positions[b] * 2 ** 11))     # rounding recovers the SPZ integer
    assert np.abs(pos[a] * 2 ** 11 - np.round(pos[a] * 2 ** 11)).max() < 0.1                      # within a tenth of a step (5 decimals)
    assert np.abs(pos[a] - cloud.positions[b]).max() < 2 ** -11 / 10
    assert np.abs(op[a] - cloud.opacities[b]).max() < 1 / 255 / 10
    assert np.abs(np.log(sc[a] / cloud.scales[b])).max() < 0.005                      # 3 significant digits
    assert np.abs(dc[a] - cloud.dc[b]).max() < 1 / (255 * 0.15) / 10
    assert np.abs(sh[a] - cloud.sh[b]).max() < 1 / 128 / 10
    assert np.abs(q[a] - cloud.orientations[b]).max() < 0.7071067811865476 / 511 / 10 + 1e-6


def test_effective_target_respects_the_start_tag_budget():
    cloud = spz_cloud(5000, sh_degree=3)
    _, man = chunk.build_envelope(cloud, target=10 ** 9)
    assert man["partition"]["effective_target"] < 10 ** 9
    assert man["partition"]["max_splats"] <= man["partition"]["effective_target"]
    assert all(c["start_tag_bytes"] < chunk.START_TAG_BUDGET for c in man["chunks"])


def test_budget_is_enforced_in_bytes_not_just_counts():
    cloud = spz_cloud(3000, sh_degree=3)
    # a budget so small that a few hundred splats exceed it forces more chunks than the requested target implies
    from transfer import x3dtext
    specs = x3dtext.digit_specs("auto", cloud)
    per = x3dtext.max_splats_per_node(cloud, specs, budget=60_000)
    assert 0 < per < 3000
    assert x3dtext.max_splats_per_node(cloud, specs, budget=8_000_000) > per


def test_rdf_adds_the_flip_transform_and_as_is_does_not():
    cloud = spz_cloud(800)
    f_rdf, m_rdf = chunk.build_envelope(cloud, target=300, coordinate_system="rdf")
    f_as, m_as = chunk.build_envelope(cloud, target=300)
    r = etree.fromstring(f_rdf["scene.x3d"])
    assert r.find(".//Transform").get("rotation") == "1 0 0 3.14159265" and r.find(".//Group") is None
    assert etree.fromstring(f_as["scene.x3d"]).find(".//Transform") is None
    assert m_rdf["coordinate_system"] == "rdf" and m_as["coordinate_system"] == "as-is"
    # the chunk contents do not change with the frame
    assert f_rdf["chunks/c000.x3d"] == f_as["chunks/c000.x3d"]


def test_arguments_are_checked():
    cloud = spz_cloud(200)
    with pytest.raises(SplatError, match="coordinate_system"):
        chunk.build_envelope(cloud, coordinate_system="ruf")
    with pytest.raises(SplatError, match="bbox_mode"):
        chunk.build_envelope(cloud, bbox_mode="loose")
    for bad in ("", "a b", "../x", 'a"b', "a/b"):
        with pytest.raises(SplatError, match="name"):
            chunk.build_envelope(cloud, name=bad)
    with pytest.raises(SplatError, match="precision"):
        chunk.build_envelope(cloud, precision="fine")
    with pytest.raises(SplatError, match="lattice"):
        chunk.build_envelope(make_cloud(100), precision="lattice")


def test_url_prefix_is_escaped_into_the_attribute():
    cloud = spz_cloud(300)
    files, _ = chunk.build_envelope(cloud, target=200, url_prefix="https://h/x?a=1&b=2/")
    root = etree.fromstring(files["scene.x3d"])             # a raw '&' would make this fail to parse
    assert root.find(".//Inline").get("url") == '"https://h/x?a=1&b=2/chunks/c000.x3d"'


@pytest.mark.parametrize("bad", ['a"b', "a b", "a'b", "a\\b", "a<b", "a\nb", "a>b"])
def test_url_prefix_that_would_break_the_mfstring_is_refused(bad):
    with pytest.raises(SplatError, match="url_prefix"):
        chunk.build_envelope(spz_cloud(100), url_prefix=bad)


def test_envelope_is_deterministic_and_tar_is_reproducible(env):
    cloud, files, _ = env
    again, _ = chunk.build_envelope(cloud, name="demo", target=900, url_prefix="")
    assert again == files
    t1, t2 = chunk.to_tar(files), chunk.to_tar(dict(reversed(list(files.items()))))
    assert t1 == t2
    with tarfile.open(fileobj=io.BytesIO(t1)) as tar:
        assert tar.getnames() == sorted(files) and all(m.mtime == 0 and m.uname == "" for m in tar.getmembers())
        assert tar.extractfile("scene.x3d").read() == files["scene.x3d"]


def test_single_chunk_case():
    cloud = spz_cloud(100)
    files, man = chunk.build_envelope(cloud, target=50000)
    assert len(man["chunks"]) == 1 and man["partition"]["min_splats"] == man["partition"]["max_splats"] == 100


def test_chunks_validate_against_the_x3d_xsd(env):
    pytest.importorskip("validation.validate")
    from validation.validate import validate_xml
    _, files, man = env
    for path in ["scene.x3d"] + [c["file"] for c in man["chunks"]]:
        res = validate_xml(files[path].decode())
        assert res["valid"], (path, res["errors"][:3])


# ---- regressions found by the hostile-input pass

def test_non_finite_splats_are_refused_or_dropped():
    cloud = spz_cloud(1200)
    cloud.positions[5, 0] = np.nan
    cloud.scales[9, 2] = np.inf
    with pytest.raises(SplatError, match="2 splats hold NaN or infinity"):
        chunk.build_envelope(cloud, target=500)
    files, man = chunk.build_envelope(cloud, target=500, drop_invalid=True)
    assert man["source"]["splats"] == 1198 and man["source"]["dropped_non_finite"] == 2
    assert sum(c["splats"] for c in man["chunks"]) == 1198
    text = b"".join(files.values()).lower()
    assert b"nan" not in text and b"inf" not in text
    assert "dropped_non_finite" not in chunk.build_envelope(spz_cloud(300), target=500)[1]["source"]


def test_empty_cloud_is_an_error():
    with pytest.raises(SplatError, match="no splats"):
        chunk.build_envelope(spz_cloud(100).take(np.zeros(0, int)))
    bad = spz_cloud(50)
    bad.positions[:] = np.nan
    with pytest.raises(SplatError, match="no splats once the non-finite"):
        chunk.build_envelope(bad, drop_invalid=True)


def test_a_low_size_estimate_is_corrected_by_cutting_finer(monkeypatch):
    cloud = spz_cloud(3000)
    monkeypatch.setattr(chunk, "max_splats_per_node", lambda *a, **k: 3000)       # an estimate that is far too high
    monkeypatch.setattr(chunk, "START_TAG_BUDGET", 100_000)
    files, man = chunk.build_envelope(cloud, target=3000)
    assert man["partition"]["estimate_corrected"] is True and man["partition"]["effective_target"] < 3000
    assert all(c["start_tag_bytes"] <= 100_000 for c in man["chunks"])
    assert sum(c["splats"] for c in man["chunks"]) == 3000 and chunk.verify_manifest(files, man) == []
    monkeypatch.undo()                                  # the same cloud with the real estimate needs no correction
    _, ok = chunk.build_envelope(cloud, target=3000)
    assert ok["partition"]["estimate_corrected"] is False


def test_a_budget_no_splat_can_meet_is_an_error(monkeypatch):
    monkeypatch.setattr(chunk, "START_TAG_BUDGET", 100)
    with pytest.raises(SplatError, match="single splat"):
        chunk.build_envelope(spz_cloud(200), target=50)


def test_a_long_url_prefix_is_refused():
    with pytest.raises(SplatError, match="url_prefix is"):
        chunk.build_envelope(spz_cloud(100), url_prefix="h" * (chunk.MAX_URL_PREFIX + 1))
    chunk.build_envelope(spz_cloud(100), url_prefix="h" * chunk.MAX_URL_PREFIX)


def test_url_prefix_counts_toward_the_output_limit():
    cloud = spz_cloud(2000)
    _, man = chunk.build_envelope(cloud, target=10)
    limit = man["totals"]["bytes"]
    chunk.build_envelope(cloud, target=10, max_total_bytes=limit * 2)
    with pytest.raises(SplatError, match="above the limit"):
        chunk.build_envelope(cloud, target=10, url_prefix="h" * 1000, max_total_bytes=limit + 100_000)


def test_too_many_chunks_is_refused_before_partitioning(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("partitioned")
    monkeypatch.setattr(chunk, "_kd", boom)
    with pytest.raises(SplatError, match="chunks is above the limit"):
        chunk.partition(pts(chunk.MAX_CHUNKS + 5), target=1)
    with pytest.raises(SplatError, match="chunks is above the limit"):
        chunk.build_envelope(spz_cloud(chunk.MAX_CHUNKS + 5), target=1)
