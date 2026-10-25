"""SPZ reader: layout, round trips, refusals, bounded decoding."""

import gzip
import struct
import tracemalloc
import zlib

import numpy as np
import pytest

from . import splat_fixtures as spzfix
from .splat_fixtures import make_cloud, needs_real, real
from transfer import spz
from transfer.cloud import SH_DIMS


def ang(a, b):
    a = a.astype(np.float64); b = b.astype(np.float64)
    a /= np.linalg.norm(a, axis=1, keepdims=True); b /= np.linalg.norm(b, axis=1, keepdims=True)
    return np.degrees(2 * np.arccos(np.abs((a * b).sum(1)).clip(0, 1)))


def header(count, version=3, sh=0, frac=12, flags=0):
    return struct.pack("<IIIBBBB", spz.MAGIC, version, count, sh, frac, flags, 0)


# ---- the format, from the layout documented in the module, not from the writer

def test_hand_built_stream_decodes():
    # two splats, no SH. positions: (1.5, -2, 0.25) and (-0.0625, 3, 100) at 4 fractional bits
    def fx(v): return int(round(v * 16)) & 0xFFFFFF
    pos = b"".join(struct.pack("<I", fx(v))[:3] for v in (1.5, -2, 0.25, -0.0625, 3, 100))
    alphas = bytes([255, 51])                       # 1.0 and 0.2
    colors = bytes([128, 255, 0, 128, 128, 128])    # dc = (b/255 - 0.5)/0.15
    scales = bytes([160, 160, 160, 128, 144, 176])  # ln(scale) = b/16 - 10 -> 0, -2, 1 for b=160, 128, 176...
    ident = (3 << 30)                               # largest = w, the other three zero
    rot = struct.pack("<II", ident, ident)
    blob = gzip.compress(header(2, frac=4) + pos + alphas + colors + scales + rot)
    c = spz.read_spz(blob)
    assert c.positions.tolist() == [[1.5, -2, 0.25], [-0.0625, 3, 100]]
    assert c.opacities.tolist() == pytest.approx([1.0, 0.2])
    assert c.dc[0, 0] == pytest.approx((128 / 255 - 0.5) / 0.15, abs=1e-6) and c.dc[0, 1] == pytest.approx(0.5 / 0.15, abs=1e-6)
    assert c.scales[0].tolist() == pytest.approx([np.exp(0.0)] * 3)
    assert c.scales[1].tolist() == pytest.approx(np.exp(np.array([128, 144, 176]) / 16 - 10).tolist())
    assert c.orientations.tolist() == [[0, 0, 0, 1], [0, 0, 0, 1]]


def test_smallest_three_known_words():
    # q = (0.5, 0.5, 0.5, 0.5): the first maximum wins (index 0); fields for indices 1, 2, 3, highest index in the low bits
    mag = int(0.5 / 0.7071067811865476 * 511 + 0.5)
    word = (0 << 30) | (mag << 20) | (mag << 10) | mag
    q = spz.decode_rotations(np.array([word], "<u4").view(np.uint8).reshape(1, 4))
    assert q[0].tolist() == pytest.approx([0.5, 0.5, 0.5, 0.5], abs=2e-3)
    # negative sign bit on the lowest field only (the w component of a quaternion whose largest is x)
    word = (0 << 30) | (0 << 20) | (0 << 10) | (1 << 9) | mag
    q = spz.decode_rotations(np.array([word], "<u4").view(np.uint8).reshape(1, 4))
    assert q[0, 3] < 0 and q[0, 1] == 0 and q[0, 2] == 0 and q[0, 0] > 0


def test_sh_layout_is_coefficient_major_rgb_interleaved():
    # degree 1: coefficient k, channel c lives at byte k * 3 + c of a splat's SH block
    n, dim = 2, 3
    block = bytearray()
    for s in range(n):
        for k in range(dim):
            for ch in range(3):
                block.append(128 + 10 * (s + 1) + 4 * k + ch)
    body = bytes(9 * n) + bytes(n) + bytes(3 * n) + bytes(3 * n) + struct.pack("<II", 3 << 30, 3 << 30) + bytes(block)
    c = spz.read_spz(gzip.compress(header(n, sh=1) + body))
    assert c.sh.shape == (2, 3, 3)
    for s in range(n):
        for k in range(dim):
            for ch in range(3):
                assert c.sh[s, k, ch] == pytest.approx((10 * (s + 1) + 4 * k + ch) / 128)


# ---- round trips through the reference-faithful writer

@pytest.mark.parametrize("version,wrap", [(3, "gzip"), (3, "none"), (2, "gzip"), (4, "zstd")])
def test_roundtrip_within_quantisation(version, wrap):
    src = make_cloud(3000, sh_degree=2)
    blob = spzfix.write_spz(src, fractional_bits=10, version=version, wrap=wrap, antialiased=True)
    c = spz.read_spz(blob)
    assert len(c) == 3000 and c.sh.shape == (3000, 8, 3) and c.info["antialiased"] and c.info["version"] == version
    assert np.abs(c.positions - src.positions).max() <= 0.5 / 1024 + 1e-6
    assert np.abs(c.opacities - src.opacities).max() <= 0.5 / 255 + 1e-6
    assert np.abs(c.dc - src.dc).max() <= 0.5 / (255 * 0.15) + 1e-5
    assert (np.abs(np.log(c.scales / src.scales)) <= 1 / 32 + 1e-5).all()
    assert np.abs(c.sh - src.sh).max() <= 0.5 / 128 + 1e-6
    if version >= 3:
        assert ang(c.orientations, src.orientations).max() < 0.2


def test_v2_rotation_follows_the_three_byte_rule():
    # v2 keeps x y z as bytes (b / 127.5 - 1) with the quaternion flipped to w >= 0, and rebuilds w. Derived here from the
    # rule itself, not from the writer.
    src = make_cloud(3000)
    c = spz.read_spz(spzfix.write_spz(src, version=2))
    q = src.orientations.astype(np.float64)
    q = q * np.where(q[:, 3:4] < 0, -1.0, 1.0)
    xyz = np.clip(np.round((q[:, :3] + 1) * 127.5), 0, 255) / 127.5 - 1
    w = np.sqrt(np.maximum(0, 1 - (xyz ** 2).sum(1)))
    assert np.abs(c.orientations[:, :3] - xyz).max() < 2e-7
    assert np.abs(c.orientations[:, 3].astype(np.float64) ** 2 - w ** 2).max() < 1e-6      # float32 sums near 1.0 (measured 5.2e-7); w itself differs by ~1e-7 / 2w
    # the format is lossy near w = 0 (w is rebuilt from a difference of nearly equal numbers); away from it the error is small
    ok = np.abs(q[:, 3]) > 0.5
    assert ok.sum() > 500 and ang(c.orientations[ok], src.orientations[ok]).max() < 1.5


def test_versions_agree_on_everything_but_rotation():
    src = make_cloud(1000, sh_degree=1)
    a = spz.read_spz(spzfix.write_spz(src, version=3))
    b = spz.read_spz(spzfix.write_spz(src, version=4, wrap="zstd"))
    for name in ("positions", "scales", "opacities", "dc", "sh", "orientations"):
        assert np.array_equal(getattr(a, name), getattr(b, name)), name


def test_degree_4_keeps_three_and_says_so():
    src = make_cloud(50, sh_degree=3)
    streams, _ = spzfix.pack_streams(src, 12)
    sh4 = np.zeros((50, 24, 3), np.uint8) + 128
    sh4[:, :15] = np.clip(np.round(src.sh * 128 + 128), 0, 255).astype(np.uint8)
    body = b"".join(s.tobytes() for s in streams[:5]) + sh4.tobytes()
    c = spz.read_spz(gzip.compress(header(50, sh=4) + body))
    assert c.sh.shape == (50, 15, 3) and "degree 4" in c.info["sh_degree_dropped"] and c.sh_degree == 3


def test_header_only_read():
    blob = spzfix.write_spz(make_cloud(100, sh_degree=3), fractional_bits=9, antialiased=True)
    h = spz.read_header(blob)
    assert (h.version, h.count, h.sh_degree, h.fractional_bits, h.antialiased, h.wrapped) == (3, 100, 3, 9, True, "gzip")
    assert h.payload_size == 100 * (20 + 45)


# ---- hostile and broken input

def test_rejections():
    good = spzfix.write_spz(make_cloud(100))
    with pytest.raises(spz.SpzError, match="not an SPZ"):
        spz.read_spz(gzip.compress(b"PK" + bytes(40)))
    with pytest.raises(spz.SpzError, match="version 1"):
        spz.read_spz(gzip.compress(header(1, version=1) + bytes(20)))
    with pytest.raises(spz.SpzError, match="version 9"):
        spz.read_spz(gzip.compress(header(1, version=9) + bytes(20)))
    with pytest.raises(spz.SpzError, match="SH degree"):
        spz.read_spz(gzip.compress(header(1, sh=5) + bytes(20)))
    with pytest.raises(spz.SpzError, match="fractionalBits"):
        spz.read_spz(gzip.compress(header(1, frac=31) + bytes(20)))
    with pytest.raises(spz.SpzError, match="truncated"):
        spz.read_spz(gzip.compress(gzip.decompress(good)[:-5]))
    with pytest.raises(spz.SpzError, match="corrupt|truncated"):
        spz.read_spz(good[:-12])
    with pytest.raises(spz.SpzError, match="corrupt"):
        spz.read_spz(good[:40] + bytes([good[40] ^ 255]) + good[41:])
    with pytest.raises(spz.SpzError, match="above the limit"):
        spz.read_spz(good, max_points=50)


def test_count_larger_than_stream_is_refused_before_allocating():
    # header claims 5,000,000 splats (100 MB) but the gzip body is 40 bytes of zeros
    blob = gzip.compress(header(5_000_000) + bytes(40))
    tracemalloc.start()
    with pytest.raises(spz.SpzError, match="cannot fit"):
        spz.read_spz(blob)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 5_000_000


def test_count_that_fits_the_ratio_but_not_the_stream_is_truncated():
    # 1000 splats claim 20 KB; a gzip of that header and 40 zero bytes is small, but 20 KB is within 1032 times its size
    blob = gzip.compress(header(1000) + bytes(40))
    with pytest.raises(spz.SpzError, match="truncated"):
        spz.read_spz(blob, max_ratio=None)                      # the default ratio limit (250) refuses it earlier
    with pytest.raises(spz.SpzError, match="ratio limit of 250"):
        spz.read_spz(blob)


def test_gzip_bomb_stops_at_the_size_the_header_allows():
    # header says 100 splats (2 KB); the stream goes on for 300 MB of zeros (a 300 KB file)
    body = header(100) + bytes(300_000_000)
    blob = gzip.compress(body, 9)
    assert len(blob) < 1_000_000
    tracemalloc.start()
    with pytest.raises(spz.SpzError, match="more than"):
        spz.read_spz(blob, max_ratio=None)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 10_000_000


def test_count_cap_on_unwrapped_stream_uses_file_size():
    with pytest.raises(spz.SpzError, match="cannot fit"):
        spz.read_spz(header(9_000_000) + bytes(100))


def test_v4_toc_lies():
    good = spzfix.write_spz(make_cloud(100), version=4, wrap="zstd")
    with pytest.raises(spz.SpzError, match="declares"):
        bad = bytearray(good); struct.pack_into("<Q", bad, 32 + 8, 7); spz.read_spz(bytes(bad))
    with pytest.raises(spz.SpzError, match="past the end"):
        bad = bytearray(good); struct.pack_into("<Q", bad, 32, 10 ** 9); spz.read_spz(bytes(bad))
    with pytest.raises(spz.SpzError, match="outside the file"):
        bad = bytearray(good); struct.pack_into("<I", bad, 16, 10 ** 9); spz.read_spz(bytes(bad))
    with pytest.raises(spz.SpzError, match="unexplained"):
        spz.read_spz(good + b"x")


# ---- real files

@needs_real
@pytest.mark.parametrize("name,count", [("pantheon.spz", 500000), ("athens.spz", 497556)])
def test_real_file_header_and_stats(name, count):
    data = real(name)
    h = spz.read_header(data)
    assert (h.version, h.count, h.sh_degree, h.fractional_bits, h.antialiased) == (3, count, 0, 10, True)
    c = spz.read_spz(data)
    assert len(c) == count
    assert np.abs(np.linalg.norm(c.orientations, axis=1) - 1).max() < 2e-7
    assert c.opacities.min() >= 0 and c.opacities.max() <= 1 and (c.scales > 0).all()


@needs_real
def test_real_file_repack_is_byte_exact_except_rotations():
    data = real("athens.spz")
    p = spz.unpack(data)
    c = spz.decode(p)
    streams, _ = spzfix.pack_streams(c, fractional_bits=10)
    assert np.array_equal(streams[0], p.positions.reshape(-1))
    assert np.array_equal(streams[1], p.alphas)
    assert np.array_equal(streams[2], p.colors.reshape(-1))
    assert np.array_equal(streams[3], p.scales.reshape(-1))
    same = (streams[4].view("<u4") == spz.rotation_words(p.rotations)).mean()
    assert same > 0.95            # measured 0.9540 (athens) and 0.9733 (pantheon); the rest are 1-LSB or sign-of-zero differences


@needs_real
def test_real_file_decode_is_fast():
    import time
    data = real("pantheon.spz")
    t = time.perf_counter(); spz.read_spz(data); dt = time.perf_counter() - t
    assert dt < 3.0               # measured 0.15 to 0.24 s; the bound is loose on purpose


# ---- regressions found by the mutation fuzz and the hostile-input pass

def test_corrupt_zstd_frame_header_is_an_spz_error_not_a_zstd_error():
    good = spzfix.write_spz(make_cloud(100), version=4, wrap="zstd")
    data_start = 32 + 16 * 5                          # header, then five TOC entries (the SH stream is empty at degree 0)
    for junk in (bytes(8), b"\xff" * 8, b"\x28\xb5\x2f\xfd" + b"\xff" * 8):     # no magic, all ones, magic then a bad descriptor
        bad = good[:data_start] + junk + good[data_start + len(junk):]
        with pytest.raises(spz.SpzError):
            spz.read_spz(bad)


def test_zstd_frame_without_a_content_size_cannot_outgrow_the_toc():
    zstandard = pytest.importorskip("zstandard")
    cloud = make_cloud(100)
    good = spzfix.write_spz(cloud, version=4, wrap="zstd")
    sizes = [s for s in spz.read_header(good).stream_sizes.values() if s]
    frames = [zstandard.ZstdCompressor(level=19, write_content_size=False).compress(bytes(50_000_000 if i == 0 else s)) for i, s in enumerate(sizes)]
    head = struct.pack("<IIIBBBBI12s", spz.MAGIC, 4, 100, 0, 12, 0, len(frames), 32, b"")
    blob = head + b"".join(struct.pack("<QQ", len(f), s) for f, s in zip(frames, sizes)) + b"".join(frames)
    assert len(blob) < 20_000
    tracemalloc.start()
    with pytest.raises(spz.SpzError):
        spz.read_spz(blob)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 5_000_000                           # measured 0.01 MB


def test_ratio_policy_is_off_by_default_and_refuses_when_set():
    good = spzfix.write_spz(make_cloud(2000))
    assert len(spz.read_spz(good)) == 2000
    assert len(spz.read_spz(good, max_ratio=100)) == 2000
    with pytest.raises(spz.SpzError, match="ratio limit"):
        spz.read_spz(good, max_ratio=1.0)
    with pytest.raises(spz.SpzError, match="ratio limit"):
        spz.read_spz(spzfix.write_spz(make_cloud(2000), version=4, wrap="zstd"), max_ratio=0.5)


def test_mutation_fuzz_only_ever_raises_spz_errors():
    import random
    rng = random.Random(11)
    cloud = make_cloud(120, sh_degree=1, seed=5)
    bases = [spzfix.write_spz(cloud, antialiased=True), spzfix.write_spz(cloud, wrap="none"), spzfix.write_spz(cloud, version=2),
             spzfix.write_spz(cloud, version=4, wrap="zstd")]
    decoded = rejected = 0
    for base in bases:
        for _ in range(250):
            data = bytearray(base)
            kind = rng.choice(["flip", "bits", "truncate", "insert"])
            if kind == "flip":
                data[rng.randrange(len(data))] = rng.randrange(256)
            elif kind == "bits":
                for _ in range(3):
                    data[rng.randrange(len(data))] ^= 1 << rng.randrange(8)
            elif kind == "truncate":
                data = data[:rng.randrange(len(data))]
            else:
                p = rng.randrange(len(data)); data[p:p] = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 9)))
            try:
                spz.read_spz(bytes(data), max_points=100_000)
                decoded += 1
            except spz.SpzError:
                rejected += 1
    assert decoded + rejected == 1000 and rejected > 300          # many mutations are caught; the rest decode to some splats


# ---- expansion bombs and short files

def test_a_header_claiming_a_huge_cloud_is_refused_before_anything_is_allocated():
    # 10M splats at SH degree 4 claim 920 MB of streams; a gzip of zeros reaches that in under 1 MB
    blob = gzip.compress(header(10_000_000, sh=4) + bytes(1_000_000), 9)
    tracemalloc.start()
    with pytest.raises(spz.SpzError, match="cannot fit"):
        spz.read_spz(blob, max_payload_bytes=None)
    with pytest.raises(spz.SpzError, match="limit of 536,870,912"):
        spz.read_spz(blob)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 20_000_000
    assert spz.unpack(spzfix.write_spz(make_cloud(100)), max_payload_bytes=2_000).header.count == 100
    # a header that deflate could carry (about 1000:1) but real files never reach: the ratio limit refuses it
    mid = gzip.compress(header(100_000, sh=4) + bytes(100_000 * 92), 9)
    assert 250 < (100_000 * 92 + 16) / len(mid) < 1032
    with pytest.raises(spz.SpzError, match="ratio limit of 250"):
        spz.read_spz(mid)


def test_the_ratio_limit_covers_v4_too():
    data = spzfix.write_spz(make_cloud(2000), version=4, wrap="zstd")
    spz.read_spz(data)
    with pytest.raises(spz.SpzError, match="ratio limit"):
        spz.read_spz(data, max_ratio=0.5)


def test_degree_4_sh_keeps_the_first_15_coefficients():
    n = 300
    raw = header(n, sh=4) + bytes(n * (9 + 1 + 3 + 3 + 4 + 72))
    cloud = spz.read_spz(raw)
    assert cloud.sh.shape == (n, 15, 3) and cloud.info["sh_degree_dropped"]


def test_a_cut_off_gzip_stream_says_so_not_that_the_header_is_missing():
    data = spzfix.write_spz(make_cloud(2000))
    with pytest.raises(spz.SpzError, match="truncated gzip stream"):
        spz.read_header(data[:50])
    with pytest.raises(spz.SpzError, match="truncated gzip stream"):
        spz.read_spz(data[:50])
