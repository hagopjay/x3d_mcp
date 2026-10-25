"""PLY header reader and a reader for 3D Gaussian Splatting PLY files (INRIA layout), numpy only.

A 3DGS PLY has one `vertex` element of little-endian float32 properties:
  x y z, nx ny nz (ignored), f_dc_0..2, f_rest_0..(3*dim-1) when SH degree > 0, opacity (logit), scale_0..2 (ln),
  rot_0..3 (w x y z).  f_rest is channel-major: all R coefficients, then all G, then all B.
The same layout written as ASCII text (one splat per line) is read too, when the vertex element is the only one.

`read_header` looks at the first 64 KiB only and never touches the body, so a count and a property list cost nothing
even for a multi-gigabyte file.
"""

from dataclasses import dataclass, field

import re
import warnings

import numpy as np

from .cloud import SH_DIMS, SplatCloud, SplatError, sh_degree_for_dim

HEADER_LIMIT = 65536
DEFAULT_MAX_POINTS = 10_000_000
DEFAULT_MAX_ASCII_BYTES = 256 * 1024 * 1024    # an ASCII body parses to float64, 1.5 to 4 times its size
_ASCII_BLOCK = 8 * 1024 * 1024
_WHITESPACE = re.compile(rb"\s")
_NUMPY_TYPES = {"char": "i1", "int8": "i1", "uchar": "u1", "uint8": "u1", "short": "i2", "int16": "i2",
                "ushort": "u2", "uint16": "u2", "int": "i4", "int32": "i4", "uint": "u4", "uint32": "u4",
                "float": "f4", "float32": "f4", "double": "f8", "float64": "f8"}
_REQUIRED = ["x", "y", "z", "f_dc_0", "f_dc_1", "f_dc_2", "opacity",
             "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"]


class PlyError(SplatError):
    """Not a readable PLY."""


@dataclass
class PlyElement:
    name: str
    count: int
    properties: list[tuple[str, str]]       # (name, type); list properties appear as ("name", "list ...")

    @property
    def has_lists(self) -> bool:
        return any(t.startswith("list") for _, t in self.properties)


@dataclass
class PlyHeader:
    format: str                              # ascii, binary_little_endian, binary_big_endian
    elements: list[PlyElement]
    header_size: int
    comments: list[str] = field(default_factory=list)

    @property
    def vertex(self) -> PlyElement | None:
        return next((e for e in self.elements if e.name == "vertex"), None)

    @property
    def count(self) -> int:
        v = self.vertex
        return v.count if v else 0

    @property
    def is_gaussian_splat(self) -> bool:
        v = self.vertex
        return bool(v) and set(_REQUIRED) <= {n for n, _ in v.properties}

    @property
    def sh_degree(self) -> int:
        """SH degree implied by the f_rest_* property count (0 when there are none)."""
        v = self.vertex
        rest = sum(1 for n, _ in v.properties if n.startswith("f_rest_")) if v else 0
        if rest % 3:
            raise PlyError(f"{rest} f_rest properties is not a multiple of 3")
        return sh_degree_for_dim(rest // 3) if rest else 0

    @property
    def is_compressed_variant(self) -> str:
        """Name of a packed PLY dialect (a `chunk` element with packed_* properties), or ""."""
        if any(e.name == "chunk" for e in self.elements):
            return "chunked/compressed PLY (chunk element); decompress it to a plain 3DGS PLY first"
        return ""

    @property
    def row_bytes(self) -> int:
        v = self.vertex
        return sum(np.dtype(_NUMPY_TYPES[t]).itemsize for _, t in v.properties)


def read_header(data: bytes) -> PlyHeader:
    """Parse the PLY header at the start of `data`. Raises PlyError on anything that is not a PLY header."""
    head = bytes(data[:HEADER_LIMIT])
    if not head.startswith(b"ply"):
        raise PlyError("not a PLY file (no 'ply' magic)")
    end = head.find(b"end_header")
    if end < 0:
        raise PlyError(f"no end_header within the first {HEADER_LIMIT:,} bytes")
    nl = head.find(b"\n", end)
    if nl < 0:
        raise PlyError("end_header is not followed by a newline")
    lines = head[:end].decode("ascii", "replace").replace("\r", "").split("\n")
    if lines[0].strip() != "ply":
        raise PlyError("first line is not 'ply'")
    fmt, elements, comments = "", [], []
    for line in lines[1:]:
        tok = line.split()
        if not tok:
            continue
        if tok[0] == "format":
            fmt = tok[1] if len(tok) > 1 else ""
        elif tok[0] in ("comment", "obj_info"):
            comments.append(line.strip())
        elif tok[0] == "element":
            if len(tok) != 3 or not tok[2].isdigit():
                raise PlyError(f"bad element line: {line!r}")
            elements.append(PlyElement(tok[1], int(tok[2]), []))
        elif tok[0] == "property":
            if not elements:
                raise PlyError("property before any element")
            if tok[1] == "list":
                elements[-1].properties.append((tok[-1], "list " + " ".join(tok[2:-1])))
            elif len(tok) == 3 and tok[1] in _NUMPY_TYPES:
                elements[-1].properties.append((tok[2], tok[1]))
            else:
                raise PlyError(f"unsupported property: {line!r}")
    if fmt not in ("ascii", "binary_little_endian", "binary_big_endian"):
        raise PlyError(f"unknown PLY format {fmt!r}")
    return PlyHeader(fmt, elements, nl + 1, comments)


def _ascii_rows(data: bytes, hdr: PlyHeader, max_ascii_bytes: int) -> dict[str, np.ndarray]:
    """Columns of an ASCII vertex element, by property name (float64).

    The body is read in blocks cut at whitespace into an array sized by the header, so a body with more numbers than the
    header promises is refused after one block instead of being parsed whole. (numpy's `count` argument is not used: it
    returns uninitialised memory when the text holds fewer values.)
    """
    v = hdr.vertex
    names = [n for n, _ in v.properties]
    if len(hdr.elements) != 1:
        raise PlyError("an ASCII PLY with elements besides vertex is not supported")
    size = len(data) - hdr.header_size
    need = v.count * len(names)
    if need == 0:
        return {n: np.empty(0) for n in names}
    if size > max_ascii_bytes:
        raise PlyError(f"ASCII body of {size:,} bytes is above the limit of {max_ascii_bytes:,}")
    if size < 2 * need - 1:
        raise PlyError(f"truncated: {size:,} bytes of text cannot hold the {need:,} values the header promises")
    flat = np.empty(need, dtype=np.float64)
    got, pos, end = 0, hdr.header_size, len(data)
    while pos < end:
        stop = end
        if pos + _ASCII_BLOCK < end:
            gap = _WHITESPACE.search(data, pos + _ASCII_BLOCK)
            stop = gap.start() if gap else end
        piece = data[pos:stop]
        pos = stop
        if not piece.strip():
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error")            # numpy before 2 only warns about unreadable text
                values = np.fromstring(piece, dtype=np.float64, sep=" ")
        except (ValueError, DeprecationWarning) as exc:
            raise PlyError(f"ASCII body is not numbers: {str(exc)[:80]}") from exc
        if got + len(values) > need:
            raise PlyError(f"ASCII body holds more than the {need:,} values the header promises")
        flat[got:got + len(values)] = values
        got += len(values)
    if got != need:
        raise PlyError(f"ASCII body holds {got:,} values, the header promises {need:,}")
    return {n: flat[i::len(names)] for i, n in enumerate(names)}


def read_ply(data: bytes, *, max_points: int = DEFAULT_MAX_POINTS, max_ascii_bytes: int = DEFAULT_MAX_ASCII_BYTES) -> SplatCloud:
    """Decode a 3DGS PLY (binary little-endian, or ASCII) into a SplatCloud (scales exp'd, opacity sigmoid'd,
    quaternions x y z w)."""
    hdr = read_header(data)
    if hdr.format not in ("binary_little_endian", "ascii"):
        raise PlyError(f"{hdr.format} PLY is not supported (binary_little_endian and ascii are)")
    if hdr.is_compressed_variant:
        raise PlyError(hdr.is_compressed_variant)
    v = hdr.vertex
    if v is None or v.has_lists or not hdr.is_gaussian_splat:
        raise PlyError("not a 3D Gaussian Splatting PLY (needs x y z f_dc_* opacity scale_* rot_* properties)")
    if hdr.elements[0] is not v:
        raise PlyError("the vertex element must come first")
    if v.count > max_points:
        raise PlyError(f"{v.count:,} splats is above the limit of {max_points:,}")
    if hdr.format == "ascii":
        rows = _ascii_rows(data, hdr, max_ascii_bytes)
    else:
        dtype = np.dtype([(name, "<" + _NUMPY_TYPES[t]) for name, t in v.properties])
        need = hdr.header_size + v.count * dtype.itemsize
        if len(data) < need:
            raise PlyError(f"truncated: {len(data):,} bytes, header promises {need:,}")
        rows = np.frombuffer(data, dtype=dtype, count=v.count, offset=hdr.header_size)
    f32 = lambda *names: np.stack([rows[n] for n in names], axis=1).astype(np.float32)
    with np.errstate(all="ignore"):                    # odd values become inf or NaN and are reported by splat_inspect
        quat = f32("rot_1", "rot_2", "rot_3", "rot_0")      # file order w x y z -> x y z w
        norm = np.linalg.norm(quat, axis=1, keepdims=True)
        quat = np.where(norm > 0, quat / np.where(norm > 0, norm, 1), np.float32([0, 0, 0, 1]))
        cloud = SplatCloud(
            positions=f32("x", "y", "z"),
            scales=np.exp(f32("scale_0", "scale_1", "scale_2")),
            orientations=quat.astype(np.float32),
            opacities=(1 / (1 + np.exp(-rows["opacity"].astype(np.float64)))).astype(np.float32),
            dc=f32("f_dc_0", "f_dc_1", "f_dc_2"),
            source_format="ply",
            info={"format": hdr.format, "comments": hdr.comments[:5]},
        )
    degree = hdr.sh_degree
    if degree:
        dim = SH_DIMS[degree]
        rest = np.stack([rows[f"f_rest_{i}"] for i in range(3 * dim)], axis=1).astype(np.float32)
        cloud.sh = rest.reshape(v.count, 3, dim).transpose(0, 2, 1).copy()   # channel-major -> (n, coef, rgb)
        if degree > 3:
            cloud.sh = cloud.sh[:, :SH_DIMS[3], :]
            cloud.info["sh_degree_dropped"] = "degree 4 has no X3D field; degrees 0 to 3 kept"
    return cloud
