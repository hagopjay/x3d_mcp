"""Shared helpers of the splat tests: synthetic clouds, an SPZ writer, a PLY writer, and the real-file switches.

Nothing here ships. The server only reads SPZ and PLY; the writers let the tests build files.
The SPZ writer follows the reference packer (positions rounded, alphas already sigmoided, colours as the SH
degree 0 coefficient, scales linear), not the reader under test.

Real-file tests run when X3D_SPLAT_FIXTURES names a folder holding pantheon.spz and athens.spz (and txt/ with the
text scenes) and X3D_SPLAT_PLY names a 3DGS PLY with SH degree 3; without them those tests are skipped, so a public
checkout needs no private data.
"""

import gzip
import os
import struct
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from transfer.cloud import SH_DIMS, SplatCloud
from transfer.spz import COLOR_SCALE, MAGIC, SQRT1_2

SPLAT_DIR = os.environ.get("X3D_SPLAT_FIXTURES", "")
SPLAT_PLY = os.environ.get("X3D_SPLAT_PLY", "")
F32 = np.float32


def make_cloud(n=2000, sh_degree=0, seed=3, spread=10.0) -> SplatCloud:
    """Random splats whose values already sit on the SPZ lattice, so a write/read round trip is exact."""
    rng = np.random.default_rng(seed)
    q = rng.normal(size=(n, 4)).astype(np.float32)
    q /= np.linalg.norm(q, axis=1, keepdims=True)
    dim = SH_DIMS[sh_degree]
    return SplatCloud(
        positions=(rng.uniform(-spread, spread, (n, 3))).astype(np.float32),
        scales=np.exp(rng.uniform(-6, 0, (n, 3))).astype(np.float32),
        orientations=q,
        opacities=rng.uniform(0.05, 1, n).astype(np.float32),
        dc=rng.uniform(-1.5, 1.5, (n, 3)).astype(np.float32),
        sh=rng.uniform(-0.9, 0.9, (n, dim, 3)).astype(np.float32) if dim else None,
    )


def real(name):
    path = os.path.join(SPLAT_DIR, name)
    return open(path, "rb").read() if SPLAT_DIR and os.path.exists(path) else None


needs_real = pytest.mark.skipif(real("pantheon.spz") is None, reason="real SPZ fixtures not available")
needs_real_ply = pytest.mark.skipif(not (SPLAT_PLY and os.path.exists(SPLAT_PLY)), reason="real PLY fixture not available")


# ---------------------------------------------------------------- SPZ writer

def _u8(x: np.ndarray) -> np.ndarray:
    """Round half away from zero, clamp to 0..255 (the reference toUint8)."""
    return np.clip(np.floor(np.abs(x) + F32(0.5)) * np.sign(x), 0, 255).astype(np.uint8)


def pack_rotations(q: np.ndarray) -> np.ndarray:
    """(n, 4) x y z w -> (n,) uint32 smallest-three words, the reference packQuaternionSmallestThree."""
    q = q.astype(F32)
    q = q / np.linalg.norm(q, axis=1, keepdims=True).astype(F32)
    n = q.shape[0]
    largest = np.abs(q).argmax(1)                  # first maximum, like the reference's strict >
    negate = q[np.arange(n), largest] < 0
    comp = largest.astype(np.uint32)
    for i in range(4):
        use = largest != i
        negbit = ((q[:, i] < 0) ^ negate).astype(np.uint32)
        mag = (F32(511) * (np.abs(q[:, i]) / SQRT1_2) + F32(0.5)).astype(np.uint32)
        nxt = (comp << np.uint32(10)) | (negbit << np.uint32(9)) | mag
        comp = np.where(use, nxt, comp).astype(np.uint32)
    return comp


def pack_streams(cloud: SplatCloud, fractional_bits: int = 12, version: int = 3, sh_degree: int | None = None):
    """Return the stream bytes in file order (positions, alphas, colors, scales, rotations, sh) as a list."""
    n = len(cloud)
    fx = np.round(cloud.positions.astype(np.float64) * (1 << fractional_bits)).astype(np.int64)
    fx = np.clip(fx, -(1 << 23), (1 << 23) - 1) & 0xFFFFFF
    pos = np.stack([fx & 255, (fx >> 8) & 255, (fx >> 16) & 255], axis=-1).astype(np.uint8)
    alphas = _u8(cloud.opacities * F32(255))
    colors = _u8(cloud.dc * (COLOR_SCALE * F32(255)) + F32(0.5 * 255.0))
    scales = _u8((np.log(cloud.scales) + F32(10)) * F32(16))
    if version >= 3:
        rot = pack_rotations(cloud.orientations).astype("<u4").view(np.uint8).reshape(n, 4)
    else:
        q = cloud.orientations / np.linalg.norm(cloud.orientations, axis=1, keepdims=True)
        q = q * np.where(q[:, 3:4] < 0, F32(-127.5), F32(127.5)) + F32(127.5)
        rot = _u8(q[:, :3])
    degree = cloud.sh_degree if sh_degree is None else sh_degree
    dim = SH_DIMS[degree]
    if dim:
        sh = np.clip(np.round(cloud.sh[:, :dim, :] * 128 + 128), 0, 255).astype(np.uint8)
    else:
        sh = np.zeros((n, 0, 3), np.uint8)
    return [pos.reshape(-1), alphas, colors.reshape(-1), scales.reshape(-1), rot.reshape(-1), sh.reshape(-1)], degree


def write_spz(cloud: SplatCloud, *, fractional_bits: int = 12, version: int = 3, antialiased: bool = False,
              sh_degree: int | None = None, wrap: str = "gzip") -> bytes:
    """Build a file. wrap: "gzip" (v2, v3), "none" (unwrapped stream), "zstd" (v4)."""
    streams, degree = pack_streams(cloud, fractional_bits, version, sh_degree)
    flags = 1 if antialiased else 0
    n = len(cloud)
    if version >= 4:
        import zstandard
        kept = [s for s in streams if s.size]
        frames = [zstandard.ZstdCompressor(level=3).compress(s.tobytes()) for s in kept]
        header = struct.pack("<IIIBBBBI12s", MAGIC, version, n, degree, fractional_bits, flags, len(frames), 32, b"")
        toc = b"".join(struct.pack("<QQ", len(f), s.size) for f, s in zip(frames, kept))
        return header + toc + b"".join(frames)
    raw = struct.pack("<IIIBBBB", MAGIC, version, n, degree, fractional_bits, flags, 0) + b"".join(s.tobytes() for s in streams)
    return gzip.compress(raw, 6, mtime=0) if wrap == "gzip" else raw


# ---------------------------------------------------------------- PLY writer

def write_ply(cloud, *, fmt="binary_little_endian", extra_header="", comment="made by the test fixture") -> bytes:
    """A 3DGS INRIA-layout PLY (the inverse of transfer.ply.read_ply)."""
    n = len(cloud)
    dim = 0 if cloud.sh is None else cloud.sh.shape[1]
    names = ["x", "y", "z", "nx", "ny", "nz", "f_dc_0", "f_dc_1", "f_dc_2"] + [f"f_rest_{i}" for i in range(3 * dim)] + \
            ["opacity", "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"]
    cols = {k: np.zeros(n, np.float32) for k in names}
    for i, k in enumerate("xyz"):
        cols[k] = cloud.positions[:, i]
    for i in range(3):
        cols[f"f_dc_{i}"] = cloud.dc[:, i]
    if dim:
        for ch in range(3):
            for k in range(dim):
                cols[f"f_rest_{ch * dim + k}"] = cloud.sh[:, k, ch]             # channel-major
    o = cloud.opacities.astype(np.float64).clip(1e-6, 1 - 1e-6)
    cols["opacity"] = np.log(o / (1 - o)).astype(np.float32)
    for i in range(3):
        cols[f"scale_{i}"] = np.log(cloud.scales[:, i])
    q = cloud.orientations
    cols["rot_0"], cols["rot_1"], cols["rot_2"], cols["rot_3"] = q[:, 3], q[:, 0], q[:, 1], q[:, 2]       # w x y z
    head = f"ply\nformat {fmt} 1.0\ncomment {comment}\n{extra_header}element vertex {n}\n" + \
           "".join(f"property float {k}\n" for k in names) + "end_header\n"
    body = np.stack([cols[k] for k in names], axis=1).astype("<f4").tobytes()
    return head.encode("ascii") + body
