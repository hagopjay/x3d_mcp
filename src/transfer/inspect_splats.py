"""splat_inspect: facts, sanity statistics, memory estimates and a verdict for a splat file, without building any XML.

Input is the bytes of an SPZ (v2, v3, v4), a 3DGS PLY, or an X3D text scene holding GaussianSplats nodes. Output is a
plain dict (the tool serialises it). Every threshold below is a policy default, named here and echoed in the result.
"""

import re

import numpy as np

from . import ply as plymod
from . import spz as spzmod
from .cloud import SH_DIMS, SplatCloud, SplatError, X3D_SH_MAX_DEGREE, sh_degree_for_dim
from validation.oversize import MAX_ATTRS, parse_start_tag
from .x3dtext import bytes_per_splat, digit_specs, max_splats_per_node, START_TAG_BUDGET

START_TAG_LIMIT = 10_000_000          # libxml2's limit on one start tag (see validation.oversize)

# policy thresholds (changeable in one place; echoed in the report so a reader can judge the verdict)
LIMITS = {
    "count_heavy": 3_000_000,            # above this a desktop browser is likely to struggle (Suspected, rule of thumb)
    "invisible_opacity": 1 / 255,        # below one 8-bit step a splat contributes nothing
    "mostly_invisible_share": 0.5,       # share of splats under 0.05 opacity that makes a scene suspect
    "all_invisible_share": 0.99,
    "outlier_extent_ratio": 8.0,         # full bbox span / central (p1-p99) span on any axis
    "big_scale_vs_extent": 0.25,         # a splat whose largest axis exceeds this share of the central extent
    "big_scale_share": 0.001,
    "needle_ratio": 1000.0,              # largest / smallest axis
    "needle_share": 0.05,
    "tiny_scale": 1e-6,
    "degenerate_share": 0.001,           # share of splats with every axis tiny that makes a scene suspect
    "quat_norm_tol": 1e-3,
}


def detect_format(data: bytes) -> str:
    head = bytes(data[:64])
    if head[:2] == b"\x1f\x8b" or head[:4] == b"NGSP":
        return "spz"
    if head[:3] == b"ply":
        return "ply"
    if re.match(rb"(?:\xef\xbb\xbf)?\s*(<\?xml|<X3D|<!--)", head):     # a UTF-8 byte order mark may lead
        return "x3d-xml"
    raise SplatError("unrecognised input: expected an SPZ, a PLY or an X3D XML scene with GaussianSplats nodes")


# One pass over the text: a comment or CDATA section matches without a group (and is skipped), a GaussianSplats start tag
# matches with group 1.  An unterminated comment or CDATA section runs to the end, as an XML parser would refuse it.
_SCAN = re.compile(r"<!--.*?(?:-->|\Z)|<!\[CDATA\[.*?(?:\]\]>|\Z)|(<GaussianSplats\b)", re.DOTALL)
_REF = re.compile(r"&(?:#x([0-9a-fA-F]+)|#([0-9]+)|(amp|lt|gt|quot|apos));")
_PREDEFINED = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}
MAX_NODES = 100_000                    # GaussianSplats nodes in one text scene (each one is kept as arrays until the end)
_DATA_FIELDS = ("positions", "scales", "orientations", "opacities", "sphericalHarmonicsDegree0Coef0")
_SH_NAMES = [f"sphericalHarmonicsDegree{d}Coef{c}" for d in range(1, 4) for c in range(2 * d + 1)]


def _unescape(value: str, field: str, where: str) -> str:
    """Resolve character references and the five predefined entities in an attribute value; any other entity is refused."""
    if "&" not in value:
        return value

    def sub(m):
        if m.group(3):
            return _PREDEFINED[m.group(3)]
        try:
            return chr(int(m.group(1), 16) if m.group(1) else int(m.group(2)))
        except (ValueError, OverflowError):
            raise SplatError(f"{where}: {field} holds a character reference that is not a character") from None
    out = _REF.sub(sub, value)
    if "&" in out:
        raise SplatError(f"{where}: {field} holds an entity reference other than &amp; &lt; &gt; &quot; &apos; and character "
                         "references, which are not resolved here; write the numbers out (validate_x3d reports the exact error)")
    return out


def cloud_from_x3d(text: str, *, max_points: int) -> tuple[SplatCloud, dict]:
    """Concatenate every GaussianSplats node of an X3D XML text. No XML parser runs, so no size limit applies.

    Comments and CDATA sections are skipped, character references in the values are resolved, and a node with only a USE
    is counted in the facts but adds no splats. Each node's arrays are parsed to float32 as it is found, and the splat
    count is checked against `max_points` as it grows, so a large file is not held twice."""
    from validation.splat_arrays import ArrayError, parse_numbers
    per_key: dict[str, list] = {k: [] for k in _DATA_FIELDS}
    sh_nodes: dict[str, dict[int, object]] = {k: {} for k in _SH_NAMES}
    counts: list[int] = []            # splats per node
    biggest = used = total = 0
    for m in _SCAN.finditer(text):
        if not m.group(1):
            continue
        tag = parse_start_tag(text, m.start())
        if tag is None:
            raise SplatError(f"the GaussianSplats start tag at line {text.count(chr(10), 0, m.start()) + 1} is not well-formed "
                             f"(unterminated, an unquoted value, or more than {MAX_ATTRS} attributes); run validate_x3d for the exact error")
        _, spans, end = tag
        biggest = max(biggest, end - m.start())
        if "USE" in spans:
            used += 1
            continue
        i = len(counts)
        if i >= MAX_NODES:
            raise SplatError(f"more than {MAX_NODES:,} GaussianSplats nodes; read the SPZ or PLY the scene came from")
        where = f"GaussianSplats node {i + 1}"
        vec = {}
        for k in (*_DATA_FIELDS, *_SH_NAMES):
            if k in spans:
                a, b = spans[k]
                try:
                    vec[k] = parse_numbers(_unescape(text[a:b], k, where), strict=True).astype(np.float32)
                except ArrayError as exc:
                    raise SplatError(f"{where}: {k}: {exc}") from exc
        n = vec["positions"].size // 3 if "positions" in vec else 0
        total += n
        if total > max_points:
            raise SplatError(f"{total:,} splats is above the limit of {max_points:,}")
        problems = [k for k, per in (("positions", 3), ("scales", 3), ("orientations", 4), ("opacities", 1), ("sphericalHarmonicsDegree0Coef0", 3))
                    if (vec[k].size if k in vec else 0) != per * n]
        if problems:
            raise SplatError(f"arrays disagree on the splat count in node {i + 1} (" + ", ".join(problems) +
                             "); run validate_semantic for the exact diagnosis")
        counts.append(n)
        for k in _DATA_FIELDS:
            if k in vec:
                per_key[k].append(vec[k])
        for k in _SH_NAMES:
            if k in vec:
                sh_nodes[k][i] = vec[k]
    if not counts:
        raise SplatError("no GaussianSplats node found" + (f" with data (only {used} USE nodes)" if used else ""))
    n = total
    f32 = lambda k, w: (np.concatenate(per_key[k]) if per_key[k] else np.empty(0, np.float32)).reshape(n, w)
    degree_names = []
    for d in range(1, 4):
        names = [f"sphericalHarmonicsDegree{d}Coef{c}" for c in range(2 * d + 1)]
        full = [i for i in range(len(counts)) if all(i in sh_nodes[k] for k in names)]
        if not full:
            break                                   # absent or a partial degree in every node: dropped, like the renderers do
        if len(full) != len(counts):
            partial = next(i for i in range(len(counts)) if i not in full)
            raise SplatError(f"spherical harmonics degree {d} is complete in node {full[0] + 1} but not in node {partial + 1}; "
                             "write the same degrees on every GaussianSplats node, or none")
        for k in names:
            for i, c in enumerate(counts):
                if sh_nodes[k][i].size != 3 * c:
                    raise SplatError(f"GaussianSplats node {i + 1}: {k} has {sh_nodes[k][i].size} numbers for {c} splats "
                                     f"(expected {3 * c}); run validate_semantic for the exact diagnosis")
        degree_names += names
    sh = np.stack([np.concatenate([sh_nodes[k][i] for i in range(len(counts))]).reshape(n, 3) for k in degree_names], axis=1) if degree_names else None
    facts = {"nodes": len(counts), "max_start_tag_bytes": biggest, **({"use_only_nodes": used} if used else {})}
    return SplatCloud(f32("positions", 3), f32("scales", 3), f32("orientations", 4), f32("opacities", 1).reshape(n),
                      f32("sphericalHarmonicsDegree0Coef0", 3), sh, "x3d-xml", {"nodes": len(counts)}), facts


def _q(a: np.ndarray, qs=(0.01, 0.5, 0.99)) -> list[float]:
    return [float(x) for x in np.quantile(a, qs)]


def _r(v, d=4):
    return [round(float(x), d) for x in v]


def memory_estimates(n: int, sh_dim: int, file_bytes: int, text_per_splat: float) -> dict:
    floats = 3 + 3 + 4 + 1 + 3 + 3 * sh_dim
    packed = 9 + 1 + 3 + 3 + 4 + 3 * sh_dim              # the SPZ stream layout
    return {
        "assumptions": "estimates, renderer-specific; floats per splat = 14 + 3 x SH coefficients per channel",
        "float32_arrays_bytes": 4 * n * floats,          # numpy here, or typed arrays in a viewer that keeps X3D fields as float32
        "packed_stream_bytes": packed * n,               # what a packed (SPZ-like) renderer needs at least
        "sort_buffers_bytes": 8 * n,                     # depth key + index per splat, a typical splat sorter
        "gpu_bytes_low": packed * n + 8 * n,
        "gpu_bytes_high": 4 * n * floats + 8 * n,
        "x3d_text_bytes": int(text_per_splat * n),
        "source_file_bytes": file_bytes,
    }


def inspect_cloud(cloud: SplatCloud, *, file_bytes: int, fmt: str, extra: dict | None = None, max_points: int = 10_000_000) -> dict:
    with np.errstate(all="ignore"):              # huge or odd values are reported, not warned about
        return _inspect(cloud, file_bytes=file_bytes, fmt=fmt, extra=extra, max_points=max_points)


def _inspect(cloud: SplatCloud, *, file_bytes: int, fmt: str, extra: dict | None, max_points: int) -> dict:
    n = len(cloud)
    rep: dict = {"format": fmt, "source_format": cloud.source_format, "bytes": file_bytes, "count": n,
                 "sh_degree": cloud.sh_degree}
    rep.update(extra or {})
    reasons: list[dict] = []

    def note(level: str, code: str, message: str):
        reasons.append({"level": level, "code": code, "message": message})

    if n == 0:
        note("error", "empty", "the file holds no splats")
        rep.update(verdict={"level": "unusable", "reasons": reasons})
        return rep
    pos, sc, q, op = cloud.positions, cloud.scales, cloud.orientations, cloud.opacities
    finite = cloud.finite_mask()
    bad = int((~finite).sum())
    rep["non_finite_splats"] = bad
    if bad:
        note("error", "non-finite", f"{bad:,} splats hold NaN or infinity; viewers draw garbage or nothing for them")
    keep = finite if bad else slice(None)
    pos, sc, q, op, dc = pos[keep], sc[keep], q[keep], op[keep], cloud.dc[keep]
    m = len(pos)
    if m == 0:
        rep.update(verdict={"level": "unusable", "reasons": reasons})
        return rep

    lo, hi = pos.min(0), pos.max(0)
    p01, p99 = (np.quantile(pos, 0.01, axis=0), np.quantile(pos, 0.99, axis=0))
    central = np.maximum(p99 - p01, 1e-9)
    rep["bbox"] = {"min": _r(lo), "max": _r(hi), "size": _r(hi - lo), "center": _r((lo + hi) / 2),
                   "central_p01": _r(p01), "central_p99": _r(p99), "central_size": _r(central)}
    ratio = (hi - lo) / central
    rep["bbox"]["outlier_ratio"] = _r(ratio, 2)
    if (ratio > LIMITS["outlier_extent_ratio"]).any():
        note("warn", "position-outliers", "the bounding box is more than %gx the central 98%% of the splats on axis %s: "
             "far-away floaters inflate it (and every chunk box built on it)" % (LIMITS["outlier_extent_ratio"], "xyz"[int(np.argmax(ratio))]))

    lo_q, med_q, hi_q = _q(op)
    rep["opacity"] = {"min": round(float(op.min()), 4), "p01": round(lo_q, 4), "median": round(med_q, 4), "p99": round(hi_q, 4),
                      "max": round(float(op.max()), 4), "mean": round(float(op.mean()), 4),
                      "share_invisible": round(float((op < LIMITS["invisible_opacity"]).mean()), 4),
                      "share_under_0.05": round(float((op < 0.05).mean()), 4), "share_opaque": round(float((op > 0.99).mean()), 4)}
    if op.min() < 0 or op.max() > 1:
        note("error", "opacity-range", "opacities outside 0 to 1: a logit was not converted (apply the sigmoid)")
    if rep["opacity"]["share_invisible"] >= LIMITS["all_invisible_share"] or op.max() < 0.05:
        note("error", "all-invisible", "nearly every splat has opacity under 1/255: nothing will be drawn")
    elif rep["opacity"]["share_under_0.05"] > LIMITS["mostly_invisible_share"]:
        note("warn", "mostly-transparent", "more than half the splats are under 0.05 opacity")

    smax, smin = sc.max(1), sc.min(1)
    aniso = smax / np.maximum(smin, 1e-12)
    rep["scale"] = {"min": float("%.4g" % sc.min()), "median_per_axis": [float("%.4g" % v) for v in np.median(sc, axis=0)],
                    "p99_largest_axis": float("%.4g" % np.quantile(smax, 0.99)), "max": float("%.4g" % sc.max()),
                    "anisotropy_median": round(float(np.median(aniso)), 2), "anisotropy_p99": round(float(np.quantile(aniso, 0.99)), 2)}
    if sc.min() < 0:
        note("error", "scale-range", "negative scales: ln(scale) was not converted (apply exp)")
    # one flat axis is legitimate (surface-like splats); a splat whose every axis is tiny draws nothing
    dead = int((smax < LIMITS["tiny_scale"]).sum())
    rep["scale"]["flat_axis_splats"] = int((smin < LIMITS["tiny_scale"]).sum())
    if dead / m > LIMITS["degenerate_share"]:
        note("warn", "degenerate-scale", f"{dead:,} splats have every axis under {LIMITS['tiny_scale']:g}: they draw nothing")
    big = float((smax > LIMITS["big_scale_vs_extent"] * float(central.max())).mean())
    rep["scale"]["share_larger_than_quarter_of_central_extent"] = round(big, 5)
    if big > LIMITS["big_scale_share"]:
        note("warn", "huge-splats", f"{big:.2%} of splats are larger than a quarter of the central extent: expect sky or haze blobs")
    needles = float((aniso > LIMITS["needle_ratio"]).mean())
    if needles > LIMITS["needle_share"]:
        note("warn", "needle-splats", f"{needles:.1%} of splats are over {LIMITS['needle_ratio']:g}:1 needles")

    norm = np.linalg.norm(q.astype(np.float64), axis=1)
    off = int((np.abs(norm - 1) > LIMITS["quat_norm_tol"]).sum()); zero = int((norm == 0).sum())
    lead = np.bincount(np.abs(q).argmax(1), minlength=4)
    rep["orientation"] = {"max_norm_error": float("%.3g" % np.abs(norm - 1).max()), "not_unit": off, "zero": zero,
                          "largest_component_share_xyzw": [round(float(v) / m, 3) for v in lead]}
    if zero:
        note("error", "zero-quaternion", f"{zero:,} orientations are all zero")
    if off - zero > 0:
        note("warn", "quaternion-not-unit", f"{off - zero:,} orientations are not unit length (tolerance {LIMITS['quat_norm_tol']:g})")
    if m >= 100 and lead[0] > 0.6 * m and lead[3] < 0.2 * m:
        note("warn", "quaternion-order", "the largest component is mostly first: this looks written w x y z; X3D reads x y z w")

    dcl = np.quantile(dc, [0.01, 0.99])
    rep["color_dc"] = {"p01": round(float(dcl[0]), 3), "p99": round(float(dcl[1]), 3),
                       "approx_rgb_mean": _r(np.clip(0.5 + 0.28209479 * dc.mean(0), 0, 1), 3)}

    # sizes of the X3D text form and what the validators can do with it (nothing is built; spz and ply are sampled)
    sh_dim = 0 if cloud.sh is None else cloud.sh.shape[1]
    if fmt == "x3d-xml":                      # the file as it is: its own digits and its largest start tag
        per = file_bytes / n
        per_node = max(1, int(START_TAG_BUDGET // max(per, 1.0)))
        biggest = int((extra or {}).get("max_start_tag_bytes", 0))
        rep["x3d_text"] = {"digits": "as-file", "bytes_per_splat": round(per, 1), "estimated_bytes": file_bytes,
                           "max_start_tag_bytes": biggest, "start_tag_limit_bytes": START_TAG_LIMIT,
                           "max_splats_per_node": per_node, "start_tag_budget_bytes": START_TAG_BUDGET,
                           "single_node_checkable": biggest < START_TAG_LIMIT,
                           "chunks_needed": int(-(-n // per_node))}
    else:
        precision = "lattice" if cloud.source_format.startswith("spz") else "bridge"
        specs = digit_specs(precision, cloud)
        per = bytes_per_splat(cloud, specs)
        per_node = max_splats_per_node(cloud, specs)
        rep["x3d_text"] = {"digits": precision, "bytes_per_splat": round(per, 1), "estimated_bytes": int(per * n),
                           "max_splats_per_node": per_node, "start_tag_budget_bytes": START_TAG_BUDGET,
                           "single_node_checkable": n <= per_node, "chunks_needed": int(-(-n // per_node))}
    rep["memory"] = memory_estimates(n, sh_dim, file_bytes, per)
    if fmt == "x3d-xml":
        if not rep["x3d_text"]["single_node_checkable"]:
            note("info", "start-tag-over-limit", f"the largest GaussianSplats start tag is {biggest:,} bytes, over libxml2's "
                 f"{START_TAG_LIMIT:,}-byte limit, so validate_x3d cannot run the XSD on this file: use splat_chunk "
                 f"({rep['x3d_text']['chunks_needed']} chunks at minimum at these digits)")
    elif n > per_node:
        note("info", "chunk-before-x3d", f"{n:,} splats do not fit one validatable GaussianSplats node ({per_node:,} at these digits): "
             f"use splat_chunk ({rep['x3d_text']['chunks_needed']} chunks at minimum)")
    if n > LIMITS["count_heavy"]:
        note("warn", "heavy-scene", f"{n:,} splats is heavy for a browser (over {LIMITS['count_heavy']:,})")
    if cloud.sh_degree:
        note("info", "spherical-harmonics", f"SH degree {cloud.sh_degree}: {3 * sh_dim} extra numbers per splat; node capacity is {per_node:,} splats")
    if cloud.info.get("sh_degree_dropped"):
        note("info", "sh-degree-dropped", cloud.info["sh_degree_dropped"])
    if fmt == "spz":
        note("info", "coordinate-frame", "an SPZ file does not record its axes (the reference library defaults to right-up-back, y up); "
             "some SPZ files are y-down: if the scene renders upside down, pass coordinate_system 'rdf' to splat_chunk to rotate 1 0 0 3.14159265; "
             "check with a render before trusting up")
    elif fmt == "ply":
        note("info", "coordinate-frame", "a 3DGS training PLY is right-down-forward (y down, z forward); X3D is y up: "
             "pass coordinate_system 'rdf' to splat_chunk to rotate 1 0 0 3.14159265")
    rep["limits"] = {k: v for k, v in LIMITS.items()}
    levels = {r["level"] for r in reasons}
    level = "unusable" if "error" in levels else "suspect" if "warn" in levels else "ok"
    rep["verdict"] = {"level": level, "reasons": reasons}
    return rep


MAX_TEXT_BYTES = 256 * 1024 * 1024      # X3D text parses to float64 (8 bytes per number, 1.5 to 4 times the text), so it is capped by size


def _ply_refusal(hdr, max_points: int) -> str:
    """Why this PLY is not decoded here ("" when it is)."""
    if not hdr.is_gaussian_splat:
        return "not a 3DGS layout (no f_dc/opacity/scale/rot properties)"
    if hdr.is_compressed_variant:
        return hdr.is_compressed_variant
    if hdr.format not in ("binary_little_endian", "ascii"):
        return f"{hdr.format} PLY is not decoded here"
    if hdr.format == "ascii" and len(hdr.elements) != 1:
        return "an ASCII PLY with elements besides vertex is not decoded here"
    if hdr.count > max_points:
        return f"{hdr.count:,} splats is above the inspect limit of {max_points:,}"
    return ""


def load_cloud(data: bytes, *, max_points: int = 10_000_000, max_ratio: float | None = spzmod.DEFAULT_MAX_RATIO,
               max_text_bytes: int = MAX_TEXT_BYTES, max_payload_bytes: int | None = spzmod.DEFAULT_MAX_PAYLOAD) -> tuple[SplatCloud, str, dict]:
    """Decode the bytes of an SPZ, PLY or X3D text file into (cloud, format, facts). Raises SplatError for unreadable
    input, a count above `max_points`, or X3D text above `max_text_bytes`. X3D text is read from its GaussianSplats
    nodes only: any other node of the scene is not carried over."""
    fmt = detect_format(data)
    if fmt == "spz":
        return spzmod.read_spz(data, max_points=max_points, max_ratio=max_ratio, max_payload_bytes=max_payload_bytes), fmt, {}
    if fmt == "ply":
        return plymod.read_ply(data, max_points=max_points, max_ascii_bytes=max_text_bytes), fmt, {}
    if len(data) > max_text_bytes:
        raise SplatError(f"X3D text of {len(data):,} bytes is above the limit of {max_text_bytes:,}; use the SPZ or PLY it came from")
    cloud, facts = cloud_from_x3d(data.decode("utf-8-sig", "replace"), max_points=max_points)
    return cloud, fmt, facts


def inspect_bytes(data: bytes, *, max_points: int = 10_000_000, max_text_bytes: int = MAX_TEXT_BYTES,
                  max_ratio: float | None = spzmod.DEFAULT_MAX_RATIO, max_payload_bytes: int | None = spzmod.DEFAULT_MAX_PAYLOAD) -> dict:
    """Entry point: bytes of an SPZ, PLY or X3D text -> report dict. Raises SplatError for unreadable input."""
    fmt = detect_format(data)
    size = len(data)
    if fmt == "spz":
        hdr = spzmod.read_header(data)
        facts = {"spz": {"version": hdr.version, "antialiased": hdr.antialiased, "fractional_bits": hdr.fractional_bits,
                         "wrapped": hdr.wrapped, "decoded_payload_bytes": hdr.payload_size}}
        over = (f"{hdr.count:,} splats is above the inspect limit of {max_points:,}" if hdr.count > max_points else
                f"{hdr.payload_size:,} bytes of streams is above the inspect limit of {max_payload_bytes:,}"
                if max_payload_bytes is not None and hdr.payload_size > max_payload_bytes else "")
        if over:
            return {"format": "spz", "bytes": size, "count": hdr.count, "sh_degree": hdr.sh_degree, **facts,
                    "verdict": {"level": "unusable", "reasons": [{"level": "error", "code": "too-large",
                                "message": f"{over}; header facts only"}]}}
        return inspect_cloud(spzmod.read_spz(data, max_points=max_points, max_ratio=max_ratio, max_payload_bytes=max_payload_bytes), file_bytes=size, fmt="spz", extra=facts, max_points=max_points)
    if fmt == "ply":
        hdr = plymod.read_header(data)
        facts = {"ply": {"format": hdr.format, "properties": len(hdr.vertex.properties) if hdr.vertex else 0,
                         "elements": [e.name for e in hdr.elements], "gaussian_layout": hdr.is_gaussian_splat}}
        why = _ply_refusal(hdr, max_points)
        if why:
            return {"format": "ply", "bytes": size, "count": hdr.count, **facts,
                    "verdict": {"level": "unusable", "reasons": [{"level": "error", "code": "ply-not-decoded", "message": why + "; header facts only"}]}}
        return inspect_cloud(plymod.read_ply(data, max_points=max_points, max_ascii_bytes=max_text_bytes), file_bytes=size, fmt="ply",
                             extra=facts, max_points=max_points)
    if size > max_text_bytes:
        raise SplatError(f"X3D text of {size:,} bytes is above the limit of {max_text_bytes:,}; inspect the SPZ or PLY it came from")
    cloud, facts = cloud_from_x3d(data.decode("utf-8-sig", "replace"), max_points=max_points)
    return inspect_cloud(cloud, file_bytes=size, fmt="x3d-xml", extra=facts, max_points=max_points)
