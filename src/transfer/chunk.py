"""Cut a splat set into spatial chunks and write the envelope: a master scene of Inline nodes, one X3D file per
chunk, and a manifest with counts, sizes and SHA-256 digests.

Why the cut matters: X_ITE sorts the splats inside one GaussianSplats node, but draws the nodes themselves like
transparent shapes, far to near by the view depth of each node's bboxCenter.  Chunks cut in file order each span the
whole scene, so their centres sit at about the same depth and the nodes layer in an arbitrary order.  A chunk must
be a compact, disjoint block of space.
"""

import hashlib
import io
import json
import re
import tarfile

import numpy as np

from .cloud import SplatCloud, SplatError
from .x3dtext import (START_TAG_BUDGET, Box, bytes_per_splat, digit_specs, max_splats_per_node, splat_node, x3d_document, _esc)

SCHEMA = "x3d-mcp-splat-envelope/1"
METHODS = ("kd", "octree", "grid")
FRAMES = {"as-is": None, "rub": None, "rdf": "1 0 0 3.14159265"}    # coordinate_system -> rotation of the wrapping Transform
MAX_CHUNKS = 4096
_BAD_URL_CHARS = re.compile(r"[\s\"'\\<>\x00-\x1f\x7f]")


# ---------------------------------------------------------------- partitioning

def _kd(points: np.ndarray, idx: np.ndarray, parts: int, out: list) -> None:
    """Split into `parts` blocks of near-equal count with median cuts across the longest axis (iterative)."""
    stack = [(idx, parts)]
    while stack:
        ids, k = stack.pop()
        if k <= 1 or len(ids) <= 1:
            out.append(ids)
            continue
        p = points[ids]
        axis = int(np.argmax(p.max(0) - p.min(0)))
        left_parts = k // 2
        cut = int(round(len(ids) * left_parts / k))
        order = np.argpartition(p[:, axis], cut)
        # push right first so blocks come out in a stable left-to-right order
        stack.append((ids[order[cut:]], k - left_parts))
        stack.append((ids[order[:cut]], left_parts))


def _octree(points: np.ndarray, target: int, max_depth: int = 14) -> list[np.ndarray]:
    """Adaptive midpoint subdivision of the bounding cube; sibling leaves are merged back while they fit in `target`."""
    lo = points.min(0).astype(np.float64)
    size = float((points.max(0) - lo).max()) or 1.0

    def rec(ids: np.ndarray, corner: np.ndarray, edge: float, depth: int) -> list[np.ndarray]:
        if len(ids) <= target or depth >= max_depth:
            return [ids] if len(ids) else []
        half = edge / 2
        mid = corner + half
        p = points[ids]
        code = (p[:, 0] >= mid[0]).astype(np.int8) | ((p[:, 1] >= mid[1]).astype(np.int8) << 1) | ((p[:, 2] >= mid[2]).astype(np.int8) << 2)
        leaves = []
        for c in range(8):
            sel = ids[code == c]
            if len(sel):
                off = np.array([(c >> 0) & 1, (c >> 1) & 1, (c >> 2) & 1], dtype=np.float64) * half
                leaves.append(rec(sel, corner + off, half, depth + 1))
        merged = [i for sub in leaves for i in sub]
        if sum(len(i) for i in merged) <= target:      # children of this cell fit together again
            return [np.concatenate(merged)]
        return merged

    return rec(np.arange(len(points)), lo, size, 0)


def _merge_small(points: np.ndarray, leaves: list[np.ndarray], target: int, floor: int) -> list[np.ndarray]:
    """Fold every leaf under `floor` splats into the leaf with the nearest centre that can take it (count <= target)."""
    leaves = [l for l in leaves if len(l)]
    centres = [(points[l].min(0) + points[l].max(0)) / 2 for l in leaves]
    while True:
        small = [i for i, l in enumerate(leaves) if len(l) < floor]
        if not small or len(leaves) < 2:
            return leaves
        i = min(small, key=lambda k: len(leaves[k]))
        best, best_d = None, None
        for j, l in enumerate(leaves):
            if j == i or len(l) + len(leaves[i]) > target:
                continue
            d = float(np.sum((centres[i] - centres[j]) ** 2))
            if best_d is None or d < best_d:
                best, best_d = j, d
        if best is None:                         # nothing can take it: leave it (it is as large as it can get)
            floor = len(leaves[i])
            continue
        leaves[best] = np.concatenate([leaves[best], leaves[i]])
        centres[best] = (points[leaves[best]].min(0) + points[leaves[best]].max(0)) / 2
        del leaves[i], centres[i]


def _grid(points: np.ndarray, target: int) -> list[np.ndarray]:
    """Uniform cells sized for about `target` splats on average; a cell over `target` is median-cut."""
    lo, hi = points.min(0).astype(np.float64), points.max(0).astype(np.float64)
    ext = np.maximum(hi - lo, 1e-9)
    cells = max(1.0, len(points) / target)
    cell = float((np.prod(ext) / cells) ** (1 / 3)) if np.all(ext > 1e-6) else float(ext.max() / cells)
    cell = max(cell, float(ext.max()) / 256)
    ijk = np.minimum(((points - lo) / cell).astype(np.int64), 1023)
    key = ijk[:, 0] + 1024 * (ijk[:, 1] + 1024 * ijk[:, 2])
    order = np.argsort(key, kind="stable")
    keys = key[order]
    cuts = np.flatnonzero(np.diff(keys)) + 1
    out: list[np.ndarray] = []
    for ids in np.split(order, cuts):
        if len(ids) > target:
            _kd(points, ids, int(np.ceil(len(ids) / target)), out)
        else:
            out.append(ids)
    return out


def partition(points: np.ndarray, *, target: int, method: str = "kd", min_fraction: float = 0.0) -> list[np.ndarray]:
    """Index arrays, one per chunk, none above `target` splats.

    kd      median cuts across the longest axis; ceil(n/target) blocks of near-equal count, compact, never tiny.
    octree  midpoint cuts of the bounding cube until a cell holds <= target; equal-size cells, uneven counts.
    grid    uniform cells; uneven counts, some tiny.
    `min_fraction` (octree, grid) folds leaves under that share of `target` into their nearest neighbour.
    """
    if method not in METHODS:
        raise SplatError(f"unknown method {method!r} ({', '.join(METHODS)})")
    n = len(points)
    if target < 1:
        raise SplatError("target must be at least 1")
    if n == 0:
        return []
    pts = np.asarray(points, dtype=np.float64)
    if method == "kd":
        out: list[np.ndarray] = []
        _kd(pts, np.arange(n), int(np.ceil(n / target)), out)
    elif method == "octree":
        out = _octree(pts, target)
    else:
        out = _grid(pts, target)
    if min_fraction and method != "kd":
        out = _merge_small(pts, out, target, int(target * min_fraction))
    if any(len(ids) > target for ids in out):        # coincident points can stop an octree at its depth limit
        split: list[np.ndarray] = []
        for ids in out:
            if len(ids) > target:
                _kd(pts, ids, int(np.ceil(len(ids) / target)), split)
            else:
                split.append(ids)
        out = split
    if len(out) > MAX_CHUNKS:
        raise SplatError(f"{len(out)} chunks is above the limit of {MAX_CHUNKS}; raise target")
    return out


# ---------------------------------------------------------------- envelope

def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _vec(v, digits=4) -> list[float]:
    return [round(float(x), digits) for x in v]


def build_envelope(cloud: SplatCloud, *, name: str = "scene", target: int = 50000, method: str = "kd",
                   precision: str = "auto", coordinate_system: str = "as-is", bbox_mode: str = "cull_safe",
                   url_prefix: str = "", drop_invalid: bool = False, source: dict | None = None,
                   max_total_bytes: int | None = None) -> tuple[dict[str, bytes], dict]:
    """Return ({path: bytes}, manifest).  Paths: scene.x3d, chunks/cNNN.x3d, manifest.json (the manifest is also
    returned as a dict).  Nothing is written anywhere; the caller stores the files.  `max_total_bytes` refuses, before
    anything is cut, a cloud whose X3D text would be estimated (from a sample) to exceed it."""
    if coordinate_system not in FRAMES:
        raise SplatError(f"unknown coordinate_system {coordinate_system!r} ({', '.join(FRAMES)})")
    if bbox_mode not in ("tight", "cull_safe"):
        raise SplatError("bbox_mode must be 'tight' or 'cull_safe'")
    if not name or not name.replace("-", "").replace("_", "").isalnum():
        raise SplatError("name must be letters, digits, '-' or '_'")
    if _BAD_URL_CHARS.search(url_prefix):
        raise SplatError("url_prefix must not contain spaces, quotes, backslashes, angle brackets or control characters "
                         "(it is written inside an MFString)")
    finite = cloud.finite_mask()
    dropped = int((~finite).sum())
    if dropped:
        if not drop_invalid:
            raise SplatError(f"{dropped:,} splats hold NaN or infinity; splat_inspect counts them, drop_invalid=true leaves them out")
        cloud = cloud.take(finite)
    if len(cloud) == 0:
        raise SplatError("the cloud holds no splats" + (" once the non-finite ones are dropped" if dropped else ""))
    specs = digit_specs(precision, cloud)
    if max_total_bytes is not None:
        estimate = int(bytes_per_splat(cloud, specs) * len(cloud))
        if estimate > max_total_bytes:
            raise SplatError(f"{len(cloud):,} splats would be about {estimate:,} bytes of X3D text, above the limit of "
                             f"{max_total_bytes:,} (X3D_MCP_SPLAT_MAX_OUTPUT_BYTES); chunk a smaller file or drop spherical harmonics")
    eff_target = min(target, max_splats_per_node(cloud, specs))
    scene_lo, scene_hi = cloud.positions.min(0).astype(np.float64), cloud.positions.max(0).astype(np.float64)

    def cut(eff: int):
        parts = partition(cloud.positions, target=eff, method=method)
        files: dict[str, bytes] = {}
        chunks, inlines = [], []
        for i, ids in enumerate(parts):
            sub = cloud.take(ids)
            cid = f"c{i:03d}"
            pad = max(0.01, 3.0 * float(np.percentile(sub.scales.max(1), 99)))
            tight = Box.of(sub.positions, pad)
            if bbox_mode == "cull_safe":
                # centre stays the chunk's own (it is the sort key); the size covers the whole scene from that centre
                half = np.maximum(np.abs(scene_hi - tight.center), np.abs(scene_lo - tight.center)) + pad
                written = Box(tight.center, 2 * half)
            else:
                written = tight
            node = splat_node(sub, specs, def_name=f"GS_{cid}", box=written, indent="    ")
            text = x3d_document(node).encode("utf-8")
            path = f"chunks/{cid}.x3d"
            files[path] = text
            chunks.append({"id": cid, "file": path, "splats": int(len(ids)), "bytes": len(text), "sha256": _sha(text),
                           "start_tag_bytes": len(node.strip()),
                           "bbox": {"center": _vec(tight.center), "size": _vec(tight.size)},
                           "written_bbox": {"center": _vec(written.center), "size": _vec(written.size)}})
            inlines.append(f'      <Inline DEF="IN_{cid}" url=\'"{_esc(url_prefix + path)}"\' {written.attrs()}/>')
        return parts, files, chunks, inlines

    # The per-splat size is a sample estimate; check the real start tags and cut finer if it was low (odd values print long).
    corrected = False
    for _ in range(7):
        parts, files, chunks, inlines = cut(eff_target)
        if max(c["start_tag_bytes"] for c in chunks) <= START_TAG_BUDGET or eff_target == 1:
            break
        eff_target, corrected = max(1, eff_target // 2), True
    if max(c["start_tag_bytes"] for c in chunks) > START_TAG_BUDGET:
        raise SplatError("a single splat's text is too large to fit a start tag; use precision 'bridge' or check the values with splat_inspect")
    rot = FRAMES[coordinate_system]
    ext = float(np.max(np.percentile(cloud.positions, 98, axis=0) - np.percentile(cloud.positions, 2, axis=0))) or 1.0
    centre = (np.percentile(cloud.positions, 98, axis=0) + np.percentile(cloud.positions, 2, axis=0)) / 2
    if rot:                                    # the Transform turns the scene half a turn about X: y -> -y, z -> -z
        centre = centre * np.array([1.0, -1.0, -1.0])
    view = f'    <Viewpoint DEF="Overview" description="Whole scene" position="{centre[0]:.3f} {centre[1]:.3f} {centre[2] + 1.5 * ext:.3f}"/>'
    group_open = f'    <Transform DEF="Frame" rotation="{rot}">' if rot else '    <Group DEF="Frame">'
    group_close = "    </Transform>" if rot else "    </Group>"
    master_body = "\n".join([view, group_open, *inlines, group_close])
    master = x3d_document(master_body, title=f"{name}: {len(cloud):,} Gaussian splats in {len(parts)} chunks",
                          metas={"description": f"Master scene: one Inline per chunk of at most {eff_target:,} splats; see manifest.json."}).encode("utf-8")
    files["scene.x3d"] = master
    manifest = {
        "schema": SCHEMA, "name": name,
        "source": dict(source or {}, splats=int(len(cloud)), sh_degree=cloud.sh_degree, **({"dropped_non_finite": dropped} if dropped else {})),
        "digits": specs, "coordinate_system": coordinate_system,
        "partition": {"method": method, "requested_target": target, "effective_target": eff_target,
                      "start_tag_budget_bytes": START_TAG_BUDGET, "estimate_corrected": corrected, "chunks": len(parts),
                      "min_splats": min(c["splats"] for c in chunks), "max_splats": max(c["splats"] for c in chunks)},
        "bbox_mode": bbox_mode,
        "ordering_note": ("Nodes are drawn far to near by the view depth of bboxCenter; splats are sorted inside a node. "
                          "Chunks are disjoint blocks cut by planes and bboxCenter is the centre of each block's splats, so the "
                          "node order follows the view from any pose. With bbox_mode cull_safe bboxSize covers the whole scene so "
                          "X_ITE 16.5 never culls a node (a culled node is never sorted and holds loadURL); the tight boxes are "
                          "in chunks[].bbox for clients that stream."),
        "master": {"file": "scene.x3d", "bytes": len(master), "sha256": _sha(master)},
        "chunks": chunks,
        "totals": {"chunk_bytes": sum(c["bytes"] for c in chunks), "bytes": sum(c["bytes"] for c in chunks) + len(master)},
    }
    files["manifest.json"] = (json.dumps(manifest, indent=1) + "\n").encode("utf-8")
    return files, manifest


def to_tar(files: dict[str, bytes]) -> bytes:
    """Deterministic ustar archive (sorted names, mtime 0, no owner) so the bundle has a reproducible digest."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for path in sorted(files):
            info = tarfile.TarInfo(path)
            info.size, info.mtime, info.mode, info.uname, info.gname = len(files[path]), 0, 0o644, "", ""
            tar.addfile(info, io.BytesIO(files[path]))
    return buf.getvalue()


def verify_manifest(files: dict[str, bytes], manifest: dict) -> list[str]:
    """Problems found when checking `files` against `manifest` (empty list = every digest, size and count matches)."""
    bad = []
    for entry in [manifest["master"]] + manifest["chunks"]:
        data = files.get(entry["file"])
        if data is None:
            bad.append(f"{entry['file']}: missing")
        elif len(data) != entry["bytes"]:
            bad.append(f"{entry['file']}: {len(data)} bytes, manifest says {entry['bytes']}")
        elif _sha(data) != entry["sha256"]:
            bad.append(f"{entry['file']}: sha256 differs")
    if sum(c["splats"] for c in manifest["chunks"]) != manifest["source"]["splats"]:
        bad.append("chunk splat counts do not add up to the source count")
    return bad
