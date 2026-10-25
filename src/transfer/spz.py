"""Reader for SPZ Gaussian-splat files (format versions 2, 3 and 4), numpy only.

Layout (the reference loader's load-spz.cc and splat-utils.h, MIT-licensed; checked on two real files):

  v2, v3  gzip( 16-byte header | streams )          an unwrapped stream (no gzip) is accepted too
  v4      32-byte header | extensions | TOC | one zstd frame per stream

  header (v2, v3)  magic 0x5053474E "NGSP", version u32, count u32, shDegree u8, fractionalBits u8, flags u8, reserved u8
  header (v4)      the same 16 bytes, then numStreams u8 (in the old reserved byte), tocByteOffset u32, 12 reserved bytes
                   -- the reference header is magic, version, count, shDegree, fractionalBits, flags, numStreams,
                      tocByteOffset, reserved[12]; flags bit 0 = antialiased, bit 1 = has extensions
  streams, in this order and each stored for all splats before the next (struct of arrays):
      positions  n x 9 B   three 24-bit little-endian two's-complement ints, value / 2**fractionalBits
      alphas     n x 1 B   opacity * 255 (already a sigmoid, so 0..1 after / 255)
      colors     n x 3 B   SH degree 0 coefficient: (byte / 255 - 0.5) / 0.15
      scales     n x 3 B   ln(scale): byte / 16 - 10
      rotations  n x 4 B   (v3, v4) smallest three: bits 30-31 index of the largest component, then for each of the
                           other three, in index order, a 10-bit field (sign bit 9, magnitude 0..511 scaled by
                           1/sqrt(2)); the field of the highest index sits in the lowest bits.  Components x y z w.
                 n x 3 B   (v2) x y z as byte / 127.5 - 1, w = sqrt(1 - |xyz|^2)
      sh         n x (3 * dim) B   per splat, per coefficient, R G B interleaved; (byte - 128) / 128
  dim (higher-order SH coefficients per channel): degree 1 -> 3, 2 -> 8, 3 -> 15, 4 -> 24

The decoder is bounded: the header fixes the exact decoded size, so a gzip stream or a zstd frame that tries to
produce more is refused before it is held in memory, and a count above `max_points` is refused before any allocation.
"""

import struct
import zlib
from dataclasses import dataclass

import numpy as np

from .cloud import SH_DIMS, SplatCloud, SplatError

MAGIC = 0x5053474E                # "NGSP" read as a little-endian u32
COLOR_SCALE = np.float32(0.15)
SQRT1_2 = np.float32(0.707106781186547524401)
DEFAULT_MAX_POINTS = 10_000_000   # 200 MB of streams and about 560 MB of float32 arrays at SH degree 0
DEFAULT_MAX_RATIO = 250.0         # streams : file; real files measure 1.2 to 3, the blob store refuses uploads above this ratio too
DEFAULT_MAX_PAYLOAD = 512 << 20   # decoded stream bytes (10M splats: 200 MB at SH degree 0, 650 MB at degree 3)
DEFLATE_MAX_RATIO = 1032             # the most one deflate byte can stand for; a header claiming more than this is lying
FLAG_ANTIALIASED = 0x1
FLAG_EXTENSIONS = 0x2

_LEGACY = struct.Struct("<IIIBBBB")          # 16 bytes
_NGSP = struct.Struct("<IIIBBBBI12s")        # 32 bytes (v4)


class SpzError(SplatError):
    """Not a readable SPZ file."""


@dataclass(frozen=True)
class SpzHeader:
    version: int
    count: int
    sh_degree: int
    fractional_bits: int
    flags: int
    num_streams: int = 0          # v4 only
    toc_offset: int = 0           # v4 only
    header_size: int = 16
    wrapped: str = "gzip"         # "gzip", "none" (unwrapped stream) or "zstd" (v4)

    @property
    def antialiased(self) -> bool:
        return bool(self.flags & FLAG_ANTIALIASED)

    @property
    def has_extensions(self) -> bool:
        return bool(self.flags & FLAG_EXTENSIONS)

    @property
    def sh_dim(self) -> int:
        return SH_DIMS[self.sh_degree]

    @property
    def rotation_bytes(self) -> int:
        return 4 if self.version >= 3 else 3

    @property
    def stream_sizes(self) -> dict[str, int]:
        n = self.count
        return {"positions": 9 * n, "alphas": n, "colors": 3 * n, "scales": 3 * n,
                "rotations": self.rotation_bytes * n, "sh": 3 * self.sh_dim * n}

    @property
    def payload_size(self) -> int:
        """Decoded bytes of all streams (what an unwrapped v2/v3 file holds after its 16-byte header)."""
        return sum(self.stream_sizes.values())


@dataclass
class Packed:
    """The raw byte streams of one file as uint8 arrays shaped (n, ...); no float has been computed yet."""
    header: SpzHeader
    positions: np.ndarray    # (n, 3, 3)
    alphas: np.ndarray       # (n,)
    colors: np.ndarray       # (n, 3)
    scales: np.ndarray       # (n, 3)
    rotations: np.ndarray    # (n, 4) or (n, 3)
    sh: np.ndarray           # (n, dim, 3)


def _parse_header(buf: bytes, wrapped: str) -> SpzHeader:
    if len(buf) < 16:
        raise SpzError(f"SPZ header needs 16 bytes, got {len(buf)}")
    magic, version, count, sh_degree, frac, flags, _ = _LEGACY.unpack_from(buf)
    if magic != MAGIC:
        raise SpzError(f"not an SPZ file (magic {magic:#010x}, expected {MAGIC:#010x})")
    if version not in (2, 3, 4):
        raise SpzError(f"SPZ version {version} is not supported (2, 3 and 4 are; 1 was never released)")
    if sh_degree > 4 or (sh_degree == 4 and version < 3):
        raise SpzError(f"SH degree {sh_degree} is not valid for SPZ version {version}")
    if frac > 24:
        raise SpzError(f"fractionalBits {frac} is implausible for 24-bit positions")
    if version < 4:
        return SpzHeader(version, count, sh_degree, frac, flags, wrapped=wrapped)
    if wrapped == "gzip":
        raise SpzError("a gzip-wrapped SPZ must be version 2 or 3")
    if len(buf) < 32:
        raise SpzError("SPZ v4 header needs 32 bytes")
    *_, num_streams, toc, _reserved = _NGSP.unpack_from(buf)
    return SpzHeader(version, count, sh_degree, frac, flags, num_streams, toc, 32, "zstd")


def read_header(data: bytes) -> SpzHeader:
    """Header facts from the first bytes of a file (gzip-wrapped, unwrapped or v4); nothing else is decoded."""
    if bytes(data[:2]) == b"\x1f\x8b":
        try:
            buf = zlib.decompressobj(31).decompress(bytes(data), 32)
        except zlib.error as exc:
            raise SpzError(f"corrupt gzip stream: {exc}") from exc
        if len(buf) < 16:
            raise SpzError(f"truncated gzip stream: {len(data):,} bytes hold {len(buf)} of the 16 header bytes")
        return _parse_header(buf, "gzip")
    return _parse_header(bytes(data[:32]), "none")


def _slice_streams(hdr: SpzHeader, body) -> Packed:
    n = hdr.count
    sizes = hdr.stream_sizes
    off = 0
    parts = {}
    for name, size in sizes.items():
        parts[name] = np.frombuffer(body, dtype=np.uint8, count=size, offset=off)
        off += size
    rot = parts["rotations"].reshape(n, hdr.rotation_bytes)
    return Packed(hdr, parts["positions"].reshape(n, 3, 3), parts["alphas"], parts["colors"].reshape(n, 3),
                  parts["scales"].reshape(n, 3), rot, parts["sh"].reshape(n, hdr.sh_dim, 3))


def _check_count(hdr: SpzHeader, max_points: int, size: int | None = None) -> None:
    if hdr.count > max_points:
        raise SpzError(f"{hdr.count:,} splats is above the limit of {max_points:,}")
    if size is not None and hdr.count > size * 1024 // 9:   # the reference loader's own sanity bound (ratio 1024)
        raise SpzError(f"{hdr.count:,} splats cannot fit in {size:,} bytes")


def unpack(data: bytes, *, max_points: int = DEFAULT_MAX_POINTS, max_ratio: float | None = DEFAULT_MAX_RATIO,
           max_payload_bytes: int | None = DEFAULT_MAX_PAYLOAD) -> Packed:
    """Split an SPZ file into its byte streams, without interpreting them.

    Raises SpzError for a bad header, a count over `max_points`, a stream that is truncated or longer than the
    header allows, and a v4 table of contents that does not match the header. `max_ratio` (default 250; None
    switches it off) refuses a file whose streams would expand to more than that many times its size, whatever the
    header says it holds; real files measure 1.2 to 3. `max_payload_bytes` (None: no limit) caps the decoded streams.
    """
    data = bytes(data) if not isinstance(data, (bytes, bytearray, memoryview)) else data
    hdr = read_header(data)
    _check_count(hdr, max_points, len(data) if hdr.wrapped != "gzip" else None)
    if max_payload_bytes is not None and hdr.payload_size > max_payload_bytes:
        raise SpzError(f"{hdr.count:,} splats at SH degree {hdr.sh_degree} hold {hdr.payload_size:,} bytes of streams, above the "
                       f"limit of {max_payload_bytes:,} (X3D_MCP_SPLAT_MAX_PAYLOAD_BYTES)")
    if hdr.wrapped == "gzip" and hdr.payload_size + 16 > len(data) * DEFLATE_MAX_RATIO:
        raise SpzError(f"{hdr.count:,} splats cannot fit in {len(data):,} bytes (deflate never exceeds {DEFLATE_MAX_RATIO}:1)")
    if max_ratio is not None and hdr.payload_size + 16 > len(data) * max_ratio:
        raise SpzError(f"the streams would expand to {hdr.payload_size + 16:,} bytes from {len(data):,}, above the ratio limit of {max_ratio:g}:1")
    if hdr.version >= 4:
        return _unpack_v4(data, hdr)
    if hdr.wrapped == "gzip":
        d = zlib.decompressobj(31)
        try:
            d.decompress(bytes(data), 16)
            body = d.decompress(d.unconsumed_tail, hdr.payload_size + 1)
            if len(body) == hdr.payload_size and not d.eof:
                d.decompress(d.unconsumed_tail, 1)     # let zlib read the gzip trailer and CRC
        except zlib.error as exc:
            raise SpzError(f"corrupt gzip stream: {exc}") from exc
        if len(body) > hdr.payload_size:
            raise SpzError(f"gzip stream holds more than the {hdr.payload_size:,} bytes the header allows")
        if len(body) < hdr.payload_size or not d.eof:
            raise SpzError(f"truncated: {len(body):,} of {hdr.payload_size:,} bytes after the header")
    else:
        body = memoryview(data)[16:]
        if len(body) != hdr.payload_size:
            raise SpzError(f"expected {hdr.payload_size:,} payload bytes after the header, found {len(body):,}")
    return _slice_streams(hdr, body)


def _unpack_v4(data, hdr: SpzHeader) -> Packed:
    try:
        import zstandard
    except ImportError as exc:
        raise SpzError("SPZ v4 needs the 'zstandard' package") from exc
    size = len(data)
    sizes = [s for s in hdr.stream_sizes.values() if s]
    if hdr.num_streams != len(sizes):
        raise SpzError(f"v4 header lists {hdr.num_streams} streams, the layout has {len(sizes)}")
    if hdr.toc_offset < 32 or hdr.toc_offset + 16 * hdr.num_streams > size:
        raise SpzError("v4 table of contents lies outside the file")
    pos = hdr.toc_offset + 16 * hdr.num_streams
    dctx = zstandard.ZstdDecompressor()
    out = bytearray(sum(sizes))                   # one buffer, filled in place: no growth copies, no final bytes() copy
    at = 0
    for i, want in enumerate(sizes):
        csize, usize = struct.unpack_from("<QQ", data, hdr.toc_offset + 16 * i)
        if usize != want:
            raise SpzError(f"v4 stream {i} declares {usize:,} bytes, the header implies {want:,}")
        if csize > size - pos:
            raise SpzError(f"v4 stream {i} extends past the end of the file")
        frame = bytes(data[pos:pos + csize])
        try:
            declared = zstandard.frame_content_size(frame)
        except zstandard.ZstdError as exc:
            raise SpzError(f"v4 stream {i}: {exc}") from exc
        if declared not in (-1, want):
            raise SpzError(f"v4 stream {i}: zstd frame declares {declared:,} bytes, expected {want:,}")
        try:
            piece = dctx.decompress(frame, max_output_size=want)
        except zstandard.ZstdError as exc:
            raise SpzError(f"v4 stream {i}: {exc}") from exc
        if len(piece) != want:
            raise SpzError(f"v4 stream {i} decoded to {len(piece):,} bytes, expected {want:,}")
        out[at:at + want] = piece
        del piece
        at += want
        pos += csize
    if pos != size:
        raise SpzError(f"{size - pos:,} unexplained bytes after the last v4 stream")
    return _slice_streams(hdr, out)


def _lut(fn) -> np.ndarray:
    return fn(np.arange(256, dtype=np.float32)).astype(np.float32)


_DC_LUT = _lut(lambda b: ((b / np.float32(255)) - np.float32(0.5)) / COLOR_SCALE)
_LOG_SCALE_LUT = _lut(lambda b: b / np.float32(16) - np.float32(10))
_SCALE_LUT = np.exp(_LOG_SCALE_LUT).astype(np.float32)
_SH_LUT = _lut(lambda b: (b - np.float32(128)) / np.float32(128))


def decode_positions(raw: np.ndarray, fractional_bits: int) -> np.ndarray:
    """(n, 3, 3) uint8 little-endian 24-bit two's complement -> (n, 3) float32."""
    v = raw[..., 0].astype(np.int32) | (raw[..., 1].astype(np.int32) << 8) | (raw[..., 2].astype(np.int32) << 16)
    v = (v << 8) >> 8                                    # sign-extend bit 23
    return v.astype(np.float32) * np.float32(1.0 / (1 << fractional_bits))


def rotation_words(raw: np.ndarray) -> np.ndarray:
    """(n, 4) uint8 -> (n,) uint32 little-endian smallest-three words."""
    return np.ascontiguousarray(raw).view("<u4").reshape(-1)


def decode_rotations(raw: np.ndarray) -> np.ndarray:
    """Smallest-three words (v3, v4) or first-three bytes (v2) -> (n, 4) float32, x y z w."""
    n = raw.shape[0]
    if raw.shape[1] == 3:
        xyz = raw.astype(np.float32) * np.float32(1.0 / 127.5) - np.float32(1)
        w = np.sqrt(np.maximum(np.float32(0), np.float32(1) - (xyz * xyz).sum(1, dtype=np.float32)))
        return np.concatenate([xyz, w[:, None]], axis=1).astype(np.float32)
    rest = rotation_words(raw).copy()
    largest = (rest >> 30).astype(np.int64)
    q = np.zeros((n, 4), dtype=np.float32)
    sum_sq = np.zeros(n, dtype=np.float32)
    for i in (3, 2, 1, 0):                               # the highest index sits in the lowest bits
        use = largest != i
        mag = (rest & 511).astype(np.float32)
        val = SQRT1_2 * mag / np.float32(511)
        val = np.where(((rest >> 9) & 1) == 1, -val, val)
        val = np.where(use, val, np.float32(0))
        q[:, i] = val
        sum_sq += val * val
        rest = np.where(use, rest >> 10, rest)
    q[np.arange(n), largest] = np.sqrt(np.maximum(np.float32(0), np.float32(1) - sum_sq))
    return q


def decode(packed: Packed, *, sh: bool = True) -> SplatCloud:
    """Packed streams -> SplatCloud in X3D units (linear scales, opacity 0..1, quaternions x y z w)."""
    hdr = packed.header
    cloud = SplatCloud(
        positions=decode_positions(packed.positions, hdr.fractional_bits),
        scales=_SCALE_LUT[packed.scales],
        orientations=decode_rotations(packed.rotations),
        opacities=packed.alphas.astype(np.float32) / np.float32(255),
        dc=_DC_LUT[packed.colors],
        source_format=f"spz-{hdr.version}",
        info={"version": hdr.version, "antialiased": hdr.antialiased, "fractional_bits": hdr.fractional_bits,
              "sh_degree_file": hdr.sh_degree, "has_extensions": hdr.has_extensions, "wrapped": hdr.wrapped},
    )
    if sh and hdr.sh_dim:
        raw = packed.sh[:, :SH_DIMS[3], :] if hdr.sh_degree == 4 else packed.sh     # slice first: no float for degree 4
        cloud.sh = _SH_LUT[raw]
        if hdr.sh_degree == 4:
            cloud.info["sh_degree_dropped"] = "degree 4 has no X3D field; degrees 0 to 3 kept"
    return cloud


def read_spz(data: bytes, *, max_points: int = DEFAULT_MAX_POINTS, max_ratio: float | None = DEFAULT_MAX_RATIO,
             max_payload_bytes: int | None = DEFAULT_MAX_PAYLOAD, sh: bool = True) -> SplatCloud:
    """Decode an SPZ file (bytes) into a SplatCloud."""
    return decode(unpack(data, max_points=max_points, max_ratio=max_ratio, max_payload_bytes=max_payload_bytes), sh=sh)
