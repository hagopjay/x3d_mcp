"""3DGS PLY reader."""

import os

import numpy as np
import pytest

from . import splat_fixtures as plyfix
from .splat_fixtures import SPLAT_PLY as SYNTH, make_cloud
from transfer import ply
from transfer.ply import PlyError


@pytest.mark.parametrize("degree", [0, 1, 2, 3])
def test_roundtrip(degree):
    src = make_cloud(1500, sh_degree=degree)
    c = ply.read_ply(plyfix.write_ply(src))
    assert len(c) == 1500 and c.sh_degree == degree
    assert np.abs(c.positions - src.positions).max() == 0
    assert np.abs(c.scales / src.scales - 1).max() < 1e-6
    assert np.abs(c.opacities - src.opacities).max() < 1e-6
    assert np.abs(c.orientations - src.orientations).max() < 1e-6        # w x y z in the file, x y z w here
    assert np.abs(c.dc - src.dc).max() == 0
    if degree:
        assert np.abs(c.sh - src.sh).max() == 0                          # f_rest is channel-major


def test_f_rest_is_channel_major():
    src = make_cloud(4, sh_degree=1)
    data = plyfix.write_ply(src)
    hdr = ply.read_header(data)
    rows = np.frombuffer(data, dtype=np.dtype([(n, "<f4") for n, _ in hdr.vertex.properties]), offset=hdr.header_size, count=4)
    # f_rest_0..2 are the three R coefficients of splat 0, f_rest_3..5 the three G coefficients
    assert [rows[f"f_rest_{i}"][0] for i in range(3)] == pytest.approx(src.sh[0, :, 0].tolist())
    assert [rows[f"f_rest_{3 + i}"][0] for i in range(3)] == pytest.approx(src.sh[0, :, 1].tolist())


def test_header_reader_is_header_only():
    data = plyfix.write_ply(make_cloud(10, sh_degree=3), extra_header="obj_info x\n")
    h = ply.read_header(data[:2000])                                      # no body at all
    assert h.format == "binary_little_endian" and h.count == 10 and h.sh_degree == 3 and h.is_gaussian_splat
    assert h.row_bytes == 4 * (3 + 3 + 3 + 45 + 1 + 3 + 4) and len(h.vertex.properties) == 62
    assert not h.is_compressed_variant and h.comments[0].startswith("comment")


def test_non_splat_ply_is_recognised_not_decoded():
    head = b"ply\nformat binary_little_endian 1.0\nelement vertex 2\nproperty float x\nproperty float y\nproperty float z\nend_header\n"
    h = ply.read_header(head + bytes(24))
    assert not h.is_gaussian_splat and h.count == 2
    with pytest.raises(PlyError, match="not a 3D Gaussian"):
        ply.read_ply(head + bytes(24))


def test_compressed_variant_is_named():
    head = (b"ply\nformat binary_little_endian 1.0\nelement chunk 1\nproperty float min_x\nelement vertex 2\n"
            b"property uint packed_position\nend_header\n")
    h = ply.read_header(head)
    assert "chunk" in h.is_compressed_variant
    with pytest.raises(PlyError, match="chunked/compressed"):
        ply.read_ply(head + bytes(100))


def test_rejections():
    good = plyfix.write_ply(make_cloud(20))
    with pytest.raises(PlyError, match="no 'ply' magic"):
        ply.read_header(b"PK\x03\x04")
    with pytest.raises(PlyError, match="no end_header"):
        ply.read_header(b"ply\nformat ascii 1.0\n" + b"comment x\n" * 10)
    with pytest.raises(PlyError, match="no end_header within"):
        ply.read_header(b"ply\n" + b"comment " + b"a" * 70000 + b"\nend_header\n")
    with pytest.raises(PlyError, match="not supported"):
        ply.read_ply(good.replace(b"binary_little_endian", b"binary_big_endian"))
    with pytest.raises(PlyError, match="not numbers|values"):
        ply.read_ply(good.replace(b"binary_little_endian", b"ascii"))      # a binary body under an ascii header
    with pytest.raises(PlyError, match="truncated"):
        ply.read_ply(good[:-10])
    with pytest.raises(PlyError, match="above the limit"):
        ply.read_ply(good, max_points=10)
    with pytest.raises(PlyError, match="bad element"):
        ply.read_header(b"ply\nformat ascii 1.0\nelement vertex lots\nend_header\n")
    with pytest.raises(PlyError, match="unsupported property"):
        ply.read_header(b"ply\nformat ascii 1.0\nelement vertex 1\nproperty complex x\nend_header\n")
    with pytest.raises(PlyError, match="multiple of 3"):
        h = ply.read_header(b"ply\nformat ascii 1.0\nelement vertex 1\nproperty float f_rest_0\nend_header\n")
        h.sh_degree


def test_count_larger_than_data_is_refused_before_allocating():
    head = plyfix.write_ply(make_cloud(1)).split(b"end_header\n")[0].replace(b"element vertex 1", b"element vertex 900000000")
    with pytest.raises(PlyError, match=r"above the limit|truncated"):
        ply.read_ply(head + b"end_header\n" + bytes(100))
    with pytest.raises(PlyError, match="truncated"):
        ply.read_ply(head + b"end_header\n" + bytes(100), max_points=10 ** 10)


def test_zero_quaternion_becomes_identity():
    src = make_cloud(3)
    src.orientations[1] = 0
    c = ply.read_ply(plyfix.write_ply(src))
    assert c.orientations[1].tolist() == [0, 0, 0, 1]


@pytest.mark.skipif(not (SYNTH and os.path.exists(SYNTH)), reason="real PLY fixture not available")
def test_real_ply_header_and_decode():
    data = open(SYNTH, "rb").read()
    h = ply.read_header(data)
    assert (h.count, h.sh_degree, h.is_gaussian_splat) == (132923, 3, True)
    c = ply.read_ply(data)
    assert len(c) == 132923 and c.sh.shape == (132923, 15, 3)
    assert np.abs(np.linalg.norm(c.orientations, axis=1) - 1).max() < 1e-6
    assert 0 <= c.opacities.min() and c.opacities.max() <= 1 and (c.scales > 0).all()


# ---------------------------------------------------------------- ASCII bodies

def ascii_ply(cloud, *, fmt="ascii", digits=9, tail=b"", extra_element=""):
    """The binary fixture re-written as text: one splat per line, values printed with `digits` significant digits."""
    data = plyfix.write_ply(cloud)
    hdr = ply.read_header(data)
    rows = np.frombuffer(data, dtype="<f4", offset=hdr.header_size).reshape(len(cloud), -1)
    body = "".join(" ".join(f"{x:.{digits}g}" for x in r) + "\n" for r in rows).encode()
    head = data[:hdr.header_size].replace(b"binary_little_endian", fmt.encode())
    return head.replace(b"end_header\n", extra_element.encode() + b"end_header\n") + body + tail


@pytest.mark.parametrize("degree", [0, 3])
def test_ascii_matches_binary(degree):
    src = make_cloud(800, sh_degree=degree)
    a = ply.read_ply(ascii_ply(src))
    b = ply.read_ply(plyfix.write_ply(src))
    assert len(a) == 800 and a.sh_degree == degree
    assert np.abs(a.positions - b.positions).max() == 0 and np.abs(a.dc - b.dc).max() == 0      # nine digits reproduce a float32 exactly
    for field in ("scales", "opacities", "orientations"):                    # exp, sigmoid, normalise: float64 here, float32 there
        assert np.abs(getattr(a, field) / getattr(b, field) - 1).max() < 1e-6
    if degree:
        assert np.abs(a.sh - b.sh).max() == 0


def test_ascii_printed_short_stays_close():
    src = make_cloud(300)
    a = ply.read_ply(ascii_ply(src, digits=6))
    assert np.abs(a.positions - src.positions).max() < 1e-4 * max(1.0, np.abs(src.positions).max())


def test_ascii_parse_is_exact_against_python_float():
    src = make_cloud(50, sh_degree=1)
    text = ascii_ply(src, digits=17)
    hdr = ply.read_header(text)
    want = np.array([float(t) for t in text[hdr.header_size:].split()])
    cols = ply._ascii_rows(text, hdr, 1 << 30)
    got = np.stack([cols[n] for n, _ in hdr.vertex.properties], axis=1).reshape(-1)
    assert np.array_equal(got, want)


def test_ascii_is_read_across_block_boundaries(monkeypatch):
    monkeypatch.setattr(ply, "_ASCII_BLOCK", 64)                           # many cuts, some of them mid-line
    src = make_cloud(120, sh_degree=1)
    a = ply.read_ply(ascii_ply(src).replace(b"\n", b"\r\n", 5))
    b = ply.read_ply(plyfix.write_ply(src))
    assert np.abs(a.positions - b.positions).max() == 0 and np.abs(a.sh - b.sh).max() == 0


def test_ascii_tolerates_trailing_blank_lines_and_crlf():
    src = make_cloud(5)
    text = ascii_ply(src, tail=b"\n\n  \n")
    assert len(ply.read_ply(text)) == 5
    assert len(ply.read_ply(text.replace(b"\n", b"\r\n"))) == 5


def test_ascii_refusals():
    src = make_cloud(10)
    good = ascii_ply(src)
    with pytest.raises(PlyError, match="values"):                          # fewer numbers than promised
        ply.read_ply(good[:-40])
    with pytest.raises(PlyError, match="more than"):                       # more numbers than promised
        ply.read_ply(good + b"1 2 3\n")
    with pytest.raises(PlyError, match="not numbers"):
        ply.read_ply(good[:-3] + b"abc\n")
    with pytest.raises(PlyError, match="not numbers"):
        ply.read_ply(good + b"1 2 x\n")
    with pytest.raises(PlyError, match="truncated|values"):                # whitespace only body
        ply.read_ply(good[:ply.read_header(good).header_size] + b"  \n  \n")
    with pytest.raises(PlyError, match="truncated|values"):                # no body at all
        ply.read_ply(good[:ply.read_header(good).header_size])
    with pytest.raises(PlyError, match="besides vertex"):
        ply.read_ply(ascii_ply(src, extra_element="element face 0\nproperty uchar x\n"))
    with pytest.raises(PlyError, match="above the limit"):
        ply.read_ply(good, max_points=5)


def test_ascii_byte_cap_is_checked_before_parsing():
    good = ascii_ply(make_cloud(100))
    with pytest.raises(PlyError, match="ASCII body of .* above the limit"):
        ply.read_ply(good, max_ascii_bytes=1000)


def test_ascii_never_returns_uninitialised_values():
    # numpy.fromstring(count=...) hands back unset memory when the text is short; the reader must say so instead
    src = make_cloud(200)
    good = ascii_ply(src)
    for cut in (len(good) - 500, len(good) // 2, len(good) // 10):
        with pytest.raises(PlyError):
            ply.read_ply(good[:cut])
