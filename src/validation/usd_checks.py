"""Checks written to answer the USD validators: mistakes OpenUSD, NVIDIA's Omniverse Asset Validator and the
SimReady Foundation requirements catch in a USD asset, which X3D can state too and nothing here reported.

The mapping, rule by rule, is in docs/USD-VALIDATION-PARITY.md; tests/test_usd_parity.py runs one failing and
one passing scene for every check claimed there. Sources: usd-core 26.11 (30 UsdValidation validators),
omniverse-asset-validator 1.18.0 (40 rules) and NVIDIA/simready-foundation c3bd1d8 (270 requirements).

Every check takes the Scene element and returns Diagnostics; the one file check also takes base_dir.
No numpy: the core validator does not depend on it.
"""
from __future__ import annotations

import math
import struct
import unicodedata

from validation.data_checks import (_URL_NODES, _defs, _diag, _faces, _inside, _ints, _kid, _nums, _resolve,
                                    _sniff, _tag, _urls)

_INDEXED = {"IndexedFaceSet": "coordIndex", "IndexedLineSet": "coordIndex", "IndexedTriangleSet": "index",
            "IndexedQuadSet": "index", "IndexedTriangleFanSet": "index", "IndexedTriangleStripSet": "index"}
_SURFACES = ("IndexedFaceSet", "IndexedTriangleSet", "IndexedQuadSet")
_COORDS = ("Coordinate", "CoordinateDouble")


# --------------------------------------------------------------------------- mesh helpers

def _points(coord) -> list[tuple[float, float, float]]:
    v = _nums(coord.get("point")) or [] if coord is not None else []
    return [(v[i], v[i + 1], v[i + 2]) for i in range(0, len(v) - 2, 3)]


def _surface_faces(g) -> list[list[int]]:
    tag = _tag(g)
    if tag == "IndexedFaceSet":
        return _faces(_ints(g.get("coordIndex")) or [])
    idx = _ints(g.get("index")) or []
    n = 3 if tag == "IndexedTriangleSet" else 4
    return [idx[i:i + n] for i in range(0, len(idx) - len(idx) % n, n)]


def _newell(face, pts):
    """Unnormalised face normal (twice the area vector), counter-clockwise order."""
    nx = ny = nz = 0.0
    for a, b in zip(face, face[1:] + face[:1]):
        (x1, y1, z1), (x2, y2, z2) = pts[a], pts[b]
        nx += (y1 - y2) * (z1 + z2)
        ny += (z1 - z2) * (x1 + x2)
        nz += (x1 - x2) * (y1 + y2)
    return nx, ny, nz


def _length(v):
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def _edges(faces):
    count = {}
    for f in faces:
        for a, b in zip(f, f[1:] + f[:1]):
            if a != b:
                e = (a, b) if a < b else (b, a)
                count[e] = count.get(e, 0) + 1
    return count


def _mesh(g, defs):
    """(faces, points) for an indexed surface with a Coordinate whose indices are all in range, else None."""
    coord = _kid(g, _COORDS, defs)
    pts = _points(coord)
    faces = [f for f in _surface_faces(g) if len(f) >= 3]
    if not pts or not faces or any(i < 0 or i >= len(pts) for f in faces for i in f):
        return None                                   # index-out-of-range / geometry-no-coordinate say so
    return faces, pts


# --------------------------------------------------------------------------- geometry (VG.*, OAV Geometry)

def check_manifold(scene) -> list:
    """An edge shared by more than two faces is non-manifold. OAV ManifoldChecker; SimReady VG.007 (must be
    manifold), VG.014. Indices are compared, as the USD checkers do, so a seam of duplicated points is not one."""
    defs, out = _defs(scene), []
    for g in scene.iter():
        if _tag(g) not in _SURFACES or g.get("USE"):
            continue
        m = _mesh(g, defs)
        if not m:
            continue
        bad = [e for e, n in _edges(m[0]).items() if n > 2]
        if bad:
            out.append(_diag("warning", "mesh-non-manifold-edge", g,
                             f"{len(bad)} edge(s) are shared by more than two faces (first: points {bad[0][0]} "
                             f"and {bad[0][1]}); smoothing, booleans and physics colliders assume two at most."))
    return out


def check_zero_area_faces(scene) -> list:
    """A face whose distinct corners lie on one line (or one point) has no area, so no normal. OAV
    ZeroAreaFaceChecker; SimReady VG.019. degenerate-face already reports a repeated index."""
    defs, out = _defs(scene), []
    for g in scene.iter():
        if _tag(g) not in _SURFACES or g.get("USE"):
            continue
        m = _mesh(g, defs)
        if not m:
            continue
        faces, pts = m
        xs = [p for p in pts]
        diag = _length([max(p[k] for p in xs) - min(p[k] for p in xs) for k in range(3)])
        if diag == 0:
            continue
        eps = 1e-12 * diag * diag
        bad = [f for f in faces if len(set(f)) >= 3 and _length(_newell(f, pts)) / 2 <= eps]
        if bad:
            out.append(_diag("warning", "mesh-zero-area-face", g,
                             f"{len(bad)} of {len(faces)} face(s) have distinct corners on one line and no area "
                             f"(first: {bad[0]}); their normal is undefined."))
    return out


def check_normal_winding(scene) -> list:
    """Explicit normals should point to the side the winding makes the front (ccw, 19775-1 13.3.6). OAV
    NormalsWindingsChecker; SimReady VG.029 (winding order) and VG.028."""
    defs, out = _defs(scene), []
    for g in scene.iter():
        if _tag(g) not in _SURFACES or g.get("USE"):
            continue
        normal = _kid(g, ("Normal",), defs)
        m = _mesh(g, defs)
        nv = _nums(normal.get("vector")) if normal is not None else None
        if not m or not nv:
            continue
        faces, pts = m
        vec = [(nv[i], nv[i + 1], nv[i + 2]) for i in range(0, len(nv) - 2, 3)]
        per_vertex = (g.get("normalPerVertex") or "true") != "false"
        tag = _tag(g)
        n_index = _ints(g.get("normalIndex")) if tag == "IndexedFaceSet" else None
        n_faces = _faces(n_index) if (n_index and per_vertex) else None
        sign = -1.0 if (g.get("ccw") or "true") == "false" else 1.0
        flipped = 0
        for fi, f in enumerate(faces):
            if per_vertex:
                ids = n_faces[fi] if n_faces and fi < len(n_faces) else f
            else:
                ids = [n_index[fi]] if n_index and fi < len(n_index) else [fi]
            if any(i < 0 or i >= len(vec) for i in ids):
                continue                                   # attribute-count-mismatch says so
            geo = _newell(f, pts)
            if _length(geo) == 0:
                continue
            dots = [sign * (geo[0] * vec[i][0] + geo[1] * vec[i][1] + geo[2] * vec[i][2]) for i in ids]
            if dots and all(d < 0 for d in dots):          # every normal of the face, not their average:
                flipped += 1                               # smoothing at a thin fold tilts one normal legitimately
        if flipped:
            out.append(_diag("warning", "mesh-normal-against-winding", g,
                             f"{flipped} of {len(faces)} face(s) have normals pointing to the back side their "
                             f"winding defines (ccw={g.get('ccw') or 'true'}); they light as if turned inside out."))
    return out


def check_unused_points(scene) -> list:
    """Coordinate points no index uses can be removed. OAV UnusedMeshTopologyChecker; SimReady VG.018.
    A Coordinate shared by several sets counts every set's indices; one used anywhere else is skipped."""
    defs = _defs(scene)
    used, skip, owner = {}, set(), {}
    for c in scene.iter():
        if _tag(c) not in _COORDS:
            continue
        node = _resolve(c, defs)
        if node is None:
            continue
        key = node                                     # the element itself: lxml reuses id() of freed proxies
        owner[key] = node
        parent = c.getparent()
        ptag = _tag(parent) if parent is not None else ""
        if ptag not in _INDEXED or (c.get("containerField") or "coord") != "coord":
            skip.add(key)
            continue
        used.setdefault(key, set()).update(i for i in (_ints(parent.get(_INDEXED[ptag])) or []) if i >= 0)
    out = []
    for key, node in owner.items():
        if key in skip or key not in used or not used[key]:
            continue
        n = len(_points(node))
        unused = n - len({i for i in used[key] if i < n})
        if unused > 0:
            out.append(_diag("info", "mesh-unused-point", node,
                             f"{unused} of {n} point(s) are used by no index; they cost memory and skew bounds."))
    return out


def check_unused_attribute_values(scene) -> list:
    """Values a separate index list never names are dead weight. OAV UnusedPrimvarChecker; SimReady VG.011."""
    defs, out = _defs(scene), []
    pairs = (("colorIndex", ("Color", "ColorRGBA"), "color", 3), ("normalIndex", ("Normal",), "vector", 3),
             ("texCoordIndex", ("TextureCoordinate",), "point", 2))
    for g in scene.iter():
        if _tag(g) != "IndexedFaceSet" or g.get("USE"):
            continue
        for field, kids, values, width in pairs:
            idx = _ints(g.get(field))
            node = _kid(g, kids, defs)
            if not idx or node is None:
                continue
            w = 4 if _tag(node) == "ColorRGBA" else width
            n = len(_nums(node.get(values)) or []) // w
            unused = n - len({i for i in idx if 0 <= i < n})
            if n and unused > 0:
                out.append(_diag("info", "mesh-unused-value", node,
                                 f"{unused} of {n} value(s) are named by no entry of {field}."))
    return out


def check_colocated_points(scene) -> list:
    """Points at exactly the same position could be welded. OAV WeldChecker; SimReady VG.016. Information:
    seams for texture coordinates and hard edges are legitimate reasons to keep them."""
    out = []
    for c in scene.iter():
        if _tag(c) not in _COORDS or c.get("USE"):
            continue
        pts = _points(c)
        if len(pts) < 2:
            continue
        dup = len(pts) - len(set(pts))
        if dup:
            out.append(_diag("info", "mesh-colocated-points", c,
                             f"{dup} of {len(pts)} point(s) repeat the exact position of another point."))
    return out


def _metres_per_unit(scene) -> float:
    root = scene.getroottree().getroot()
    for el in root.iter():
        if _tag(el) == "unit" and el.get("category") == "length":
            f = _nums(el.get("conversionFactor"))
            if f and f[0] > 0:
                return f[0]
    return 1.0


def check_coordinate_precision(scene) -> list:
    """Single-precision points so far from the origin that neighbouring values are more than 1 mm apart and more
    than 1/10,000 of the mesh's own size, or past 2^38 units. SimReady VG.020 (points precision limit) and the
    RTX bounds limit (2^40, checked from 2^38). CoordinateDouble and GeoCoordinate exist for that."""
    mpu, out = _metres_per_unit(scene), []
    for c in scene.iter():
        if _tag(c) != "Coordinate" or c.get("USE"):
            continue
        v = _nums(c.get("point")) or []
        if not v:
            continue
        far = max(abs(x) for x in v)
        step = far * 2.0 ** -23 * mpu                     # float32 spacing at that magnitude, in metres
        size = max(max(v[k::3]) - min(v[k::3]) for k in range(3)) * mpu
        if far >= 2.0 ** 38 or (step > 1e-3 and step > 1e-4 * size):   # RTX discards past 2^40; jitter shows when
            # the spacing is a visible share of the object's own size (a 40 km terrain at 5 mm is fine, a 1 m part is not)
            out.append(_diag("warning", "coordinate-precision", c,
                             f"has a value of {far:g} ({far * mpu:g} m from the origin); in 32-bit floats neighbouring "
                             f"values there are {step * 1000:.2g} mm apart, so the mesh will jitter. Move the origin "
                             f"near the geometry with a Transform, or use CoordinateDouble."))
    return out


_DRAWS = {"VolumeData", "IsoSurfaceVolumeData", "SegmentedVolumeData", "GaussianSplats", "ParticleSystem",
          "Inline", "ProtoInstance", "Script", "Text"}


def check_scene_has_geometry(scene) -> list:
    """A scene with nothing to draw. SimReady VG.001 (at least one imageable geometry)."""
    for el in scene.iter():
        tag = _tag(el)
        if tag in _DRAWS:
            return []
        if tag == "Shape" and any(_tag(c) not in ("Appearance", "") and not _tag(c).startswith("Metadata") for c in el):
            return []
    return [_diag("info", "scene-no-geometry", scene, "contains no geometry, so nothing is drawn.")]


def check_constant_interpolators(scene) -> list:
    """An interpolator whose every keyValue is the same changes nothing over time. SimReady VG.015 (time
    samples only when values change)."""
    out = []
    for el in scene.iter():
        tag = _tag(el)
        if not tag.endswith("Interpolator") or el.get("USE"):
            continue
        key, kv = _nums(el.get("key")), _nums(el.get("keyValue"))
        if not key or not kv or len(key) < 2 or len(kv) % len(key):
            continue
        w = len(kv) // len(key)
        chunks = {tuple(kv[i:i + w]) for i in range(0, len(kv), w)}
        if len(chunks) == 1 and w:
            out.append(_diag("info", "interpolator-constant", el,
                             f"has {len(key)} keys that all hold the same value, so it animates nothing; set the "
                             f"field directly."))
    return out


def check_transparent_open_mesh(scene) -> list:
    """A see-through surface with holes cannot be simulated as a volume. SimReady VG.022 (transparent physical
    bodies should be watertight)."""
    defs, out = _defs(scene), []
    for s in scene.iter():
        if _tag(s) != "Shape" or s.get("USE"):
            continue
        app = _kid(s, ("Appearance",), defs)
        mat = _kid(app, ("Material", "PhysicalMaterial"), defs) if app is not None else None
        t = _nums(mat.get("transparency")) if mat is not None else None
        if not t or t[0] <= 0:
            continue
        for g in s:
            g = _resolve(g, defs)
            if g is None or _tag(g) not in _SURFACES:
                continue
            m = _mesh(g, defs)
            if not m:
                continue
            open_edges = sum(1 for n in _edges(m[0]).values() if n == 1)
            if open_edges:
                out.append(_diag("info", "transparent-open-mesh", g,
                                 f"is transparent ({t[0]:g}) and has {open_edges} open edge(s); light transmission "
                                 f"and refraction need a closed (watertight) surface."))
    return out


_NOT_SURFACES = {"IndexedLineSet", "LineSet", "PointSet", "Polyline2D", "Polypoint2D", "Arc2D", "Circle2D", "Text"}


def check_coincident_shapes(scene) -> list:
    """Two Shapes under the same parent with the same geometry occupy exactly the same space: z-fighting in
    a renderer, doubled contacts in physics. SimReady VG.008 (meshes should not share the exact same space).
    Information: a hidden palette of tiny Shapes that only holds DEF'd Appearances, and a mesh drawn twice on
    purpose, are common X3D idioms (survey of 1,548 files, 26 Nov 2026)."""
    defs, out = _defs(scene), []

    def key(g):
        g = _resolve(g, defs)
        if g is None:
            return None
        coord = _kid(g, _COORDS, defs)
        attrs = tuple(sorted((k, v) for k, v in g.attrib.items() if k not in ("DEF", "USE", "containerField")))
        return (_tag(g), attrs, coord.get("point") if coord is not None else None)

    for parent in scene.iter():
        if _tag(parent) in ("LOD", "Switch", "GeoLOD", "CADLayer"):
            continue                                       # alternatives, never drawn together
        seen = {}
        for s in parent:
            if _tag(s) != "Shape":
                continue
            s = _resolve(s, defs)
            geo = next((c for c in s if isinstance(c.tag, str) and _tag(c) not in ("Appearance",) and not _tag(c).startswith("Metadata")), None) if s is not None else None
            k = key(geo) if geo is not None else None
            if k is None or k[0] in _NOT_SURFACES:
                continue
            if k in seen and seen[k] is not s:
                out.append(_diag("info", "mesh-coincident", s,
                                 f"has the same {k[0]} as another Shape under the same parent, in the same place; "
                                 f"the two surfaces z-fight and, as colliders, double every contact."))
            seen.setdefault(k, s)
    return out


def _rot_matrix(r):
    x, y, z, a = (r + [0, 0, 1, 0])[:4]
    n = math.sqrt(x * x + y * y + z * z) or 1.0
    x, y, z = x / n, y / n, z / n
    c, s, t = math.cos(a), math.sin(a), 1 - math.cos(a)
    return [[t * x * x + c, t * x * y - s * z, t * x * z + s * y],
            [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
            [t * x * z - s * y, t * y * z + s * x, t * z * z + c]]


def _apply(m, v):
    return [m[i][0] * v[0] + m[i][1] * v[1] + m[i][2] * v[2] for i in range(3)]


def _colliders(body, defs):
    """A body's primitive collidables in the collection's frame: ("sphere", centre, radius) or
    ("box", centre, axes, half extents); cylinders and cones as their boxes. None when anything else is there."""
    bp = (_nums(body.get("position")) or [0, 0, 0])[:3]
    br = _rot_matrix(_nums(body.get("orientation")) or [0, 0, 1, 0])
    out = []
    for c in body:
        c = _resolve(c, defs)
        if c is None or _tag(c) != "CollidableShape":
            continue
        shape = _kid(c, ("Shape",), defs)
        geo = next((g for g in shape if isinstance(g.tag, str) and _tag(g) in ("Box", "Sphere", "Cylinder", "Cone")), None) \
            if shape is not None else None
        if geo is None:
            return None
        geo = _resolve(geo, defs)
        t = _tag(geo)
        cp = (_nums(c.get("translation")) or [0, 0, 0])[:3]
        cr = _rot_matrix(_nums(c.get("rotation")) or [0, 0, 1, 0])
        centre = [a + b for a, b in zip(_apply(br, cp), bp)]
        if t == "Sphere":
            out.append(("sphere", centre, (_nums(geo.get("radius")) or [1])[0]))
            continue
        if t == "Box":
            half = [v / 2 for v in (_nums(geo.get("size")) or [2, 2, 2])[:3]]
        else:
            r = (_nums(geo.get("radius" if t == "Cylinder" else "bottomRadius")) or [1])[0]
            half = [r, (_nums(geo.get("height")) or [2])[0] / 2, r]
        m = [[sum(br[i][k] * cr[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
        axes = [[m[0][j], m[1][j], m[2][j]] for j in range(3)]
        out.append(("box", centre, axes, half))
    return out or None


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _overlap(p, q, tol=1e-6):
    """True when two primitives interpenetrate by more than tol (touching is not overlap)."""
    if p[0] == "box" and q[0] == "sphere":
        p, q = q, p
    if p[0] == "sphere" and q[0] == "sphere":
        return _length([a - b for a, b in zip(p[1], q[1])]) < p[2] + q[2] - tol
    if p[0] == "sphere":                                   # sphere against oriented box: closest point
        d = [a - b for a, b in zip(p[1], q[1])]
        closest = list(q[1])
        for ax, h in zip(q[2], q[3]):
            t = max(-h, min(h, _dot(d, ax)))
            closest = [c + t * a for c, a in zip(closest, ax)]
        return _length([a - b for a, b in zip(p[1], closest)]) < p[2] - tol
    d = [a - b for a, b in zip(q[1], p[1])]                # two oriented boxes: separating axis theorem
    axes = list(p[2]) + list(q[2]) + [_cross(a, b) for a in p[2] for b in q[2]]
    for ax in axes:
        n = _length(ax)
        if n < 1e-9:
            continue
        ax = [x / n for x in ax]
        ra = sum(h * abs(_dot(a, ax)) for a, h in zip(p[2], p[3]))
        rb = sum(h * abs(_dot(a, ax)) for a, h in zip(q[2], q[3]))
        if abs(_dot(d, ax)) >= ra + rb - tol:
            return False
    return True


def check_collider_overlap(scene) -> list:
    """Collidables of two bodies that are not joined to each other overlap at rest, so the solver pushes them
    apart on the first step. SimReady BA.002 (non-adjacent collision meshes must not overlap). Information:
    spheres exactly; boxes, cylinders and cones as oriented boxes (separating-axis test). Touching is not
    overlap, and two fixed bodies may overlap: static scenery often does."""
    defs, out = _defs(scene), []
    for coll in scene.iter():
        if _tag(coll) != "RigidBodyCollection" or coll.get("USE"):
            continue
        bodies = [_resolve(b, defs) for b in coll if _tag(b) == "RigidBody"]
        joined = set()
        for j in coll:
            if _tag(j) in _JOINTS:
                j = _resolve(j, defs)
                ends = [_resolve(c, defs) for c in j if isinstance(c.tag, str) and c.get("containerField") in ("body1", "body2")]
                if len(ends) == 2 and None not in ends:
                    joined.add(frozenset(ends))
        prims = [(b, _colliders(b, defs)) for b in bodies if b is not None]
        prims = [(b, x) for b, x in prims if x]
        for i in range(len(prims)):
            for k in range(i + 1, len(prims)):
                (a, pa), (b, pb) = prims[i], prims[k]
                if frozenset((a, b)) in joined or (a.get("fixed") == "true" and b.get("fixed") == "true"):
                    continue
                if any(_overlap(x, y) for x in pa for y in pb):
                    out.append(_diag("info", "physics-collider-overlap", a,
                                     f"has collision shapes that overlap those of RigidBody {b.get('DEF') or '(unnamed)'}, "
                                     f"which is not joined to it; the solver will push them apart on the first step."))
    return out


# --------------------------------------------------------------------------- names (OAV UnicodeNameChecker)

def check_def_name_unicode(scene) -> list:
    """Two DEF names that differ only in Unicode form (composed vs decomposed, compatibility forms) look the
    same to a person and different to a parser. OAV UnicodeNameChecker."""
    seen, out = {}, []
    for el in scene.getroottree().getroot().iter():
        name = el.get("DEF") if isinstance(el.tag, str) else None
        if not name:
            continue
        key = unicodedata.normalize("NFKC", name)
        if key in seen and seen[key] != name:
            out.append(_diag("warning", "def-name-unicode-ambiguous", el,
                             f"DEF {name!r} and DEF {seen[key]!r} are the same name after Unicode normalization; a USE "
                             f"typed by a person may bind to either."))
        seen.setdefault(key, name)
    return out


# --------------------------------------------------------------------------- physics (RB.*, JT.*, DJ.*, OAV Physics)

def _jacobi_eigenvalues(a):
    a = [row[:] for row in a]
    for _ in range(50):
        p, q = max(((0, 1), (0, 2), (1, 2)), key=lambda ij: abs(a[ij[0]][ij[1]]))
        if abs(a[p][q]) < 1e-15:
            break
        theta = (a[q][q] - a[p][p]) / (2 * a[p][q])
        t = (1 if theta >= 0 else -1) / (abs(theta) + math.sqrt(theta * theta + 1))
        c = 1 / math.sqrt(t * t + 1)
        s = t * c
        for k in range(3):
            akp, akq = a[k][p], a[k][q]
            a[k][p], a[k][q] = c * akp - s * akq, s * akp + c * akq
        for k in range(3):
            apk, aqk = a[p][k], a[q][k]
            a[p][k], a[q][k] = c * apk - s * aqk, s * apk + c * aqk
    return sorted(a[i][i] for i in range(3))


def check_inertia(scene) -> list:
    """RigidBody inertia must be a physical inertia tensor: symmetric, positive definite, and its principal
    moments must satisfy the triangle inequality. OAV MassChecker; SimReady RB.008 (detailed mass)."""
    out = []
    for b in scene.iter():
        if _tag(b) != "RigidBody" or b.get("USE") or b.get("inertia") is None:
            continue
        v = _nums(b.get("inertia"))
        if not v or len(v) != 9 or not all(math.isfinite(x) for x in v):
            continue                                       # tuple-count / physics-non-finite say so
        m = [v[0:3], v[3:6], v[6:9]]
        scale = max(abs(x) for x in v) or 1.0
        if any(abs(m[i][j] - m[j][i]) > 1e-6 * scale for i in range(3) for j in range(i + 1, 3)):
            out.append(_diag("error", "physics-inertia", b,
                             f"inertia='{b.get('inertia')}' is not symmetric; an inertia tensor always is."))
            continue
        ev = _jacobi_eigenvalues(m)
        if ev[0] <= 0:
            out.append(_diag("error", "physics-inertia", b,
                             f"inertia='{b.get('inertia')}' has a principal moment of {ev[0]:.3g}; every principal "
                             f"moment of a real body is above zero."))
        elif ev[0] + ev[1] < ev[2] * (1 - 1e-6):
            out.append(_diag("error", "physics-inertia", b,
                             f"inertia='{b.get('inertia')}' has principal moments {ev[0]:.3g}, {ev[1]:.3g}, {ev[2]:.3g}; "
                             f"no body has a largest moment above the sum of the other two."))
    return out


def check_shared_bodies(scene) -> list:
    """A RigidBody listed twice among bodies, or a collidable used by two bodies, is one object simulated
    twice. SimReady RB.005 (rigid bodies cannot be part of an instance); OAV RigidBodyChecker."""
    defs, out = _defs(scene), []
    listed, carried = {}, {}
    for el in scene.iter():
        tag = _tag(el)
        parent = el.getparent()
        ptag = _tag(parent) if parent is not None else ""
        if tag == "RigidBody" and ptag == "RigidBodyCollection" and (el.get("containerField") or "bodies") == "bodies":
            node = _resolve(el, defs)
            if node is not None:
                listed[node] = listed.get(node, 0) + 1
                if listed[node] == 2:
                    out.append(_diag("error", "physics-body-shared", node,
                                     "is listed as a body more than once; it would be simulated twice."))
        if tag in ("CollidableShape", "CollidableOffset") and ptag == "RigidBody" and \
                (el.get("containerField") or "geometry") == "geometry":
            node, body = _resolve(el, defs), _resolve(parent, defs)
            if node is not None and body is not None:
                carried.setdefault(node, set()).add(body)
                if len(carried[node]) == 2:
                    out.append(_diag("error", "physics-body-shared", node,
                                     "is the geometry of two different RigidBody nodes; each body needs its own collidable."))
    return out


_JOINTS = ("BallJoint", "DoubleAxisHingeJoint", "MotorJoint", "SingleAxisHingeJoint", "SliderJoint", "UniversalJoint")


def check_joint_graph(scene) -> list:
    """Two joints between the same pair of bodies, or a closed loop of joints, cannot become an articulation
    (reduced-coordinate solvers such as PhysX articulations, Isaac Sim, MuJoCo). SimReady DJ.011."""
    defs, out = _defs(scene), []
    for coll in scene.iter():
        if _tag(coll) != "RigidBodyCollection" or coll.get("USE"):
            continue
        pairs, parent = {}, {}

        def find(x):
            while parent.get(x, x) != x:
                x = parent[x]
            return x
        loop_reported = False
        for j in coll:
            if _tag(j) not in _JOINTS:
                continue
            j = _resolve(j, defs)
            ends = [_resolve(c, defs) for c in j if isinstance(c.tag, str) and c.get("containerField") in ("body1", "body2")]
            if len(ends) != 2 or None in ends or ends[0] is ends[1]:
                continue                                   # physics-joint-body says so
            key = frozenset(ends)                          # elements, not id(): lxml reuses id() of freed proxies
            if key in pairs:
                out.append(_diag("info", "physics-joint-loop", j,
                                 "joins the same two bodies as another joint; legal in X3D, but an articulation "
                                 "(PhysX, Isaac Sim, MuJoCo) allows one joint per pair."))
                continue
            pairs[key] = j
            a, b = find(ends[0]), find(ends[1])
            if a == b and not loop_reported:
                out.append(_diag("info", "physics-joint-loop", j,
                                 "closes a loop of joints; that is a closed kinematic chain, which reduced-coordinate "
                                 "solvers (articulations) cannot represent without cutting a joint."))
                loop_reported = True
            parent[a] = b
    return out


def check_body_collider(scene) -> list:
    """A moving body with no collidable, in a collection that has a CollisionCollection (so contacts are
    meant), passes through everything. SimReady RB.013 (enabled rigid bodies must have collision geometry).
    Without a CollisionCollection a body with no geometry is an ordinary X3D point mass (a pendulum bob)."""
    defs, out = _defs(scene), []
    for coll in scene.iter():
        if _tag(coll) != "RigidBodyCollection" or coll.get("USE"):
            continue
        if not any(_tag(c) == "CollisionCollection" for c in coll):
            continue
        for b in coll:
            if _tag(b) != "RigidBody":
                continue
            b = _resolve(b, defs)
            if b is None or b.get("enabled") == "false" or b.get("fixed") == "true":
                continue
            if not any(_tag(c) in ("CollidableShape", "CollidableOffset") for c in b):
                out.append(_diag("info", "physics-body-no-collider", b,
                                 "is a moving body with no CollidableShape in a collection that collides, so it "
                                 "passes through everything."))
    return out


# --------------------------------------------------------------------------- files (SimReady materials)

def _image_size(data: bytes, kind: str):
    if kind == "png" and len(data) >= 24 and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    if kind == "jpeg":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return None


def check_texture_size(scene, base_dir) -> list:
    """Textures above 16,384 pixels on a side exceed what RTX renderers and most GPUs load. SimReady
    material-texture-maxsize."""
    from pathlib import Path
    out, base = [], Path(base_dir)
    for el in scene.iter():
        tag = _tag(el)
        if _URL_NODES.get(tag) != "image" or el.get("USE"):
            continue
        for u in _urls(el.get("url", "")):
            p = _inside(base, u)
            if p is None or not p.is_file():
                continue
            with open(p, "rb") as fh:
                data = fh.read(65536)
            kind = _sniff(data)
            size = _image_size(data, kind) if kind else None
            if size and max(size) > 16384:
                out.append(_diag("warning", "texture-too-large", el,
                                 f"url {u!r} is {size[0]} x {size[1]}; above 16,384 pixels on a side many GPUs and RTX "
                                 f"renderers refuse the texture."))
            break
    return out


USD_SCENE_CHECKS = [check_manifold, check_zero_area_faces, check_normal_winding, check_unused_points,
                    check_unused_attribute_values, check_colocated_points, check_coordinate_precision,
                    check_scene_has_geometry, check_constant_interpolators, check_transparent_open_mesh,
                    check_coincident_shapes, check_def_name_unicode, check_inertia, check_shared_bodies,
                    check_joint_graph, check_body_collider, check_collider_overlap]
USD_FILE_CHECKS = [check_texture_size]
