"""Data-level checks: mistakes that sit between values, across nodes, or in the files a scene names.

The XSD judges one attribute at a time. These checks compare values with each other (array lengths,
indices, bounds, weights), follow references (USE type, USE cycles, texture mappings, ROUTE targets)
and, when the caller says where the scene lives, open the files its url fields name.

They were written to match what the Khronos glTF-Validator checks for glTF, concept for concept; the
mapping is in docs/GLTF-VALIDATOR-PARITY.md and tests/test_gltf_parity.py runs one failing and one
passing scene per mapped check.

Every check takes the Scene element and returns Diagnostics. File checks also take base_dir and
are skipped when it is None, so validating a bare string never touches the disk.
"""
from __future__ import annotations

import math
import re
import struct
from pathlib import Path

from lxml import etree

_SPLIT = re.compile(r"[\s,]+")


def _sem():
    from validation import semantic
    return semantic


def _tag(el) -> str:
    t = el.tag
    if not isinstance(t, str):
        return ""
    return t.split("}", 1)[1] if t.startswith("{") else t


def _nums(value: str | None) -> list[float] | None:
    """Floats of an attribute, or None when a token is not a number (the XSD reports those)."""
    if value is None:
        return None
    try:
        return [float(x) for x in _SPLIT.split(value.strip()) if x]
    except ValueError:
        return None


def _ints(value: str | None) -> list[int] | None:
    if value is None:
        return None
    try:
        return [int(x) for x in _SPLIT.split(value.strip()) if x]
    except ValueError:
        return None


def _defs(scene) -> dict:
    out = {}
    for el in scene.iter():
        if isinstance(el.tag, str) and el.get("DEF") and el.get("DEF") not in out:
            out[el.get("DEF")] = el
    return out


def _resolve(el, defs):
    return defs.get(el.get("USE")) if el is not None and el.get("USE") else el


def _kid(el, tags, defs):
    for c in el:
        if _tag(c) in tags:
            return _resolve(c, defs)
    return None


def _diag(level, check, el, msg):
    D = _sem().Diagnostic
    tag = _tag(el)
    label = f" (DEF={el.get('DEF')!r})" if el.get("DEF") else ""
    return D(level=level, check=check, message=f"{tag}{label} {msg}", node_tag=tag, def_name=el.get("DEF", ""))


def _faces(idx: list[int]) -> list[list[int]]:
    faces, cur = [], []
    for i in idx:
        if i == -1:
            if cur:
                faces.append(cur)
            cur = []
        else:
            cur.append(i)
    if cur:
        faces.append(cur)
    return faces


# --------------------------------------------------------------------------- references

def check_use_type(scene) -> list:
    """A USE must name a DEF of the same node type (19775-1 4.4.3). glTF: UNRESOLVED_REFERENCE, TYPE_MISMATCH."""
    defs, out = _defs(scene), []
    for el in scene.iter():
        use = el.get("USE") if isinstance(el.tag, str) else None
        if not use or use not in defs:
            continue
        target = defs[use]
        if _tag(target) != _tag(el):
            out.append(_diag("error", "use-type-mismatch", el,
                             f"USE={use!r} names a {_tag(target)}, not a {_tag(el)}. A USE is another "
                             f"appearance of the same node, so the element name must match the DEF's."))
    return out


def check_use_cycle(scene) -> list:
    """A node must not contain a USE of itself: the scene graph is acyclic (19775-1 4.3.2). glTF: NODE_LOOP."""
    out = []
    for el in scene.iter():
        use = el.get("USE") if isinstance(el.tag, str) else None
        if not use:
            continue
        p = el.getparent()
        while p is not None:
            if p.get("DEF") == use:
                out.append(_diag("error", "use-cycle", el,
                                 f"USE={use!r} sits inside the node it names, so the node would contain "
                                 f"itself. The scene graph must have no cycles."))
                break
            p = p.getparent()
    return out


def check_duplicate_routes(scene) -> list:
    """The same ROUTE written twice sends the event once and hides an edit error. glTF: ANIMATION_DUPLICATE_TARGETS."""
    seen, out = {}, []
    for el in scene.iter():
        if _tag(el) != "ROUTE":
            continue
        key = (el.getparent(), el.get("fromNode"), el.get("fromField"), el.get("toNode"), el.get("toField"))
        if key in seen:
            out.append(_diag("warning", "route-duplicate", el,
                             f"{key[1]}.{key[2]} to {key[3]}.{key[4]} is already routed in this scope; "
                             f"the second ROUTE has no effect."))
        seen[key] = True
    return out


# --------------------------------------------------------------------------- geometry arrays

_PER_VERTEX = (("Normal", "vector", 3, "normalIndex", "normalPerVertex"),
               ("Color", "color", 3, "colorIndex", "colorPerVertex"),
               ("ColorRGBA", "color", 4, "colorIndex", "colorPerVertex"),
               ("TextureCoordinate", "point", 2, "texCoordIndex", None))
_NON_INDEX_SETS = ("IndexedTriangleSet", "IndexedTriangleStripSet", "IndexedTriangleFanSet", "IndexedQuadSet",
                   "TriangleSet", "TriangleStripSet", "TriangleFanSet", "QuadSet", "LineSet", "PointSet")


def check_attribute_counts(scene) -> list:
    """Normals, colours and texture coordinates must cover every vertex or face they are applied to
    (19775-1 13.3.6, 11.2.3). glTF: MESH_PRIMITIVE_UNEQUAL_ACCESSOR_COUNT."""
    defs, out = _defs(scene), []
    for g in scene.iter():
        tag = _tag(g)
        if g.get("USE") or tag not in ("IndexedFaceSet", "IndexedLineSet") + _NON_INDEX_SETS:
            continue
        coord = _kid(g, ("Coordinate", "CoordinateDouble"), defs)
        pts = _nums(coord.get("point")) if coord is not None else None
        if pts is None:
            continue
        n = len(pts) // 3
        idx = _ints(g.get("coordIndex" if tag in ("IndexedFaceSet", "IndexedLineSet") else "index")) or []
        faces = _faces(idx) if tag in ("IndexedFaceSet", "IndexedLineSet") else []
        used = [i for i in idx if i >= 0]
        for kid_tag, attr, width, index_field, pv_field in _PER_VERTEX:
            node = _kid(g, (kid_tag,), defs)
            vals = _nums(node.get(attr)) if node is not None else None
            if vals is None:
                continue
            have = len(vals) // width
            if tag in ("IndexedFaceSet", "IndexedLineSet"):
                if (g.get(index_field) or "").strip():
                    continue                      # its own index list is range-checked by index-out-of-range
                per_vertex = pv_field is None or g.get(pv_field, "true").strip().lower() != "false"
                if kid_tag.startswith("Color") and tag == "IndexedLineSet":
                    per_vertex = g.get("colorPerVertex", "true").strip().lower() != "false"
                need = (max(used) + 1 if used else 0) if per_vertex else len(faces)
                what = "the coordIndex reaches vertex " + str(need - 1) if per_vertex else f"there are {len(faces)} faces"
            else:
                if pv_field and g.get(pv_field, "true").strip().lower() == "false":
                    continue
                need, what = n, f"the coordinate node has {n} points"
            if have < need:
                out.append(_diag("error", "attribute-count-mismatch", g,
                                 f"{kid_tag} has {have} value(s) but {what}, so {need} are needed. "
                                 f"The missing ones are undefined and browsers draw them differently."))
    return out


def check_face_sizes(scene) -> list:
    """Every face needs 3 or more vertices, every polyline 2 or more, and counted primitives must fit the
    coordinates (19775-1 13.3.6, 11.3). glTF: MESH_PRIMITIVE_INCOMPATIBLE_MODE."""
    defs, out = _defs(scene), []
    for g in scene.iter():
        tag = _tag(g)
        if g.get("USE"):
            continue
        coord = _kid(g, ("Coordinate", "CoordinateDouble"), defs) if tag in _NON_INDEX_SETS + ("IndexedFaceSet", "IndexedLineSet") else None
        pts = _nums(coord.get("point")) if coord is not None else None
        n = len(pts) // 3 if pts is not None else None
        if tag in ("IndexedFaceSet", "IndexedLineSet"):
            idx = _ints(g.get("coordIndex"))
            if not idx:
                continue
            least, word = (3, "face") if tag == "IndexedFaceSet" else (2, "polyline")
            small = [f for f in _faces(idx) if len(f) < least]
            if small:
                out.append(_diag("error", "face-too-few-vertices", g,
                                 f"coordIndex has {len(small)} {word}(s) with fewer than {least} vertices "
                                 f"(first: {small[0]}). Such a {word} draws nothing."))
        elif tag in ("TriangleSet", "QuadSet") and n is not None:
            k = 3 if tag == "TriangleSet" else 4
            if n % k:
                out.append(_diag("error", "face-too-few-vertices", g,
                                 f"has {n} coordinates, not a multiple of {k}; the last {n % k} make no "
                                 f"{'triangle' if k == 3 else 'quad'}."))
        elif tag == "IndexedQuadSet":
            idx = _ints(g.get("index")) or []
            if len(idx) % 4:
                out.append(_diag("error", "face-too-few-vertices", g,
                                 f"index has {len(idx)} entries, not a multiple of 4."))
        elif tag in ("LineSet", "TriangleStripSet", "TriangleFanSet"):
            field, least = {"LineSet": ("vertexCount", 2), "TriangleStripSet": ("stripCount", 3),
                            "TriangleFanSet": ("fanCount", 3)}[tag]
            counts = _ints(g.get(field))
            if not counts:
                continue
            if min(counts) < least:
                out.append(_diag("error", "face-too-few-vertices", g,
                                 f"{field} has an entry of {min(counts)}; each needs {least} or more."))
            if n is not None and sum(counts) > n:
                out.append(_diag("error", "attribute-count-mismatch", g,
                                 f"{field} adds up to {sum(counts)} vertices but the coordinate node has {n}."))
    return out


def check_degenerate_faces(scene) -> list:
    """A face that names the same vertex twice has no area. glTF: ACCESSOR_INDEX_TRIANGLE_DEGENERATE (Information)."""
    out = []
    for g in scene.iter():
        tag = _tag(g)
        if g.get("USE"):
            continue
        if tag == "IndexedFaceSet":
            faces = _faces(_ints(g.get("coordIndex")) or [])
        elif tag == "IndexedTriangleSet":
            idx = _ints(g.get("index")) or []
            faces = [idx[i:i + 3] for i in range(0, len(idx) - len(idx) % 3, 3)]
        else:
            continue
        bad = [f for f in faces if len(f) >= 3 and len(set(f)) < 3]
        if bad:
            out.append(_diag("warning", "degenerate-face", g,
                             f"{len(bad)} of {len(faces)} face(s) repeat a vertex and have no area "
                             f"(first: {bad[0]})."))
    return out


def check_missing_coordinates(scene) -> list:
    """An indexed set with indices but no coordinate node draws nothing. glTF: MESH_PRIMITIVE_NO_POSITION."""
    defs, out = _defs(scene), []
    for g in scene.iter():
        tag = _tag(g)
        if g.get("USE") or tag not in ("IndexedFaceSet", "IndexedLineSet", "IndexedTriangleSet", "IndexedQuadSet",
                                         "IndexedTriangleStripSet", "IndexedTriangleFanSet"):
            continue
        field = "coordIndex" if tag in ("IndexedFaceSet", "IndexedLineSet") else "index"
        if (g.get(field) or "").strip() and _kid(g, ("Coordinate", "CoordinateDouble", "GeoCoordinate", "ProtoInstance"), defs) is None:
            out.append(_diag("warning", "geometry-no-coordinate", g,
                             f"has {field} values but no Coordinate node, so there is nothing to index."))
    return out


def check_normals_unit(scene) -> list:
    """Normal vectors are unit length (19775-1 12.4.5 Normal). glTF: ACCESSOR_VECTOR3_NON_UNIT."""
    out = []
    for el in scene.iter():
        if _tag(el) != "Normal" or el.get("USE"):
            continue
        v = _nums(el.get("vector"))
        if not v:
            continue
        bad = [i for i in range(0, len(v) - 2, 3)
               if abs(math.sqrt(v[i] ** 2 + v[i + 1] ** 2 + v[i + 2] ** 2) - 1.0) > 0.01]
        if bad:
            i = bad[0]
            out.append(_diag("warning", "normal-not-unit", el,
                             f"{len(bad)} of {len(v) // 3} normal(s) are not unit length "
                             f"(first: {v[i]:g} {v[i + 1]:g} {v[i + 2]:g}, length "
                             f"{math.sqrt(v[i] ** 2 + v[i + 1] ** 2 + v[i + 2] ** 2):.3g}). Lighting scales with the length."))
    return out


# --------------------------------------------------------------------------- values that depend on each other

def check_rotation_axes(scene) -> list:
    """An SFRotation axis is a normalized vector (19775-1 5.3.14); also OrientationInterpolator keyValue.
    glTF: ROTATION_NON_UNIT, ACCESSOR_ANIMATION_SAMPLER_OUTPUT_NON_NORMALIZED_QUATERNION."""
    sem = _sem()
    nodes = sem.get_x3duom().get_concrete_nodes()
    out = []

    def bad_axes(v):
        res = []
        for i in range(0, len(v) - 3, 4):
            ln = math.sqrt(v[i] ** 2 + v[i + 1] ** 2 + v[i + 2] ** 2)
            if ln > 1e-6 and abs(ln - 1.0) > 0.01:
                res.append((v[i:i + 4], ln))
        return res

    for el in scene.iter():
        tag = _tag(el)
        if tag not in nodes or el.get("USE"):
            continue
        fields = sem._node_field_map(tag, nodes)
        for name, value in el.attrib.items():
            ftype = fields.get(name, {}).get("type")
            if ftype not in ("SFRotation", "MFRotation"):
                continue
            v = _nums(value)
            if not v:
                continue
            bad = bad_axes(v)
            if bad:
                r, ln = bad[0]
                out.append(_diag("warning", "rotation-axis-not-unit", el,
                                 f"{name} has {len(bad)} rotation(s) whose axis is not unit length "
                                 f"(first: {' '.join(f'{x:g}' for x in r)}, axis length {ln:.3g}). The axis "
                                 f"must be normalized; tools disagree on what a longer axis means."))
    return out


def check_scale_zero(scene) -> list:
    """A zero scale component flattens the subtree to nothing and makes the transform non-invertible."""
    out = []
    for el in scene.iter():
        if _tag(el) not in ("Transform", "HAnimJoint", "HAnimHumanoid", "HAnimSite", "CADPart", "GeoTransform") or el.get("USE"):
            continue
        v = _nums(el.get("scale"))
        if v and any(x == 0 for x in v):
            out.append(_diag("warning", "scale-zero", el,
                             f"scale={el.get('scale')!r} has a zero component: everything under it "
                             f"collapses and picking and normals break."))
    return out


def check_view_volumes(scene) -> list:
    """Viewpoint clip distances and OrthoViewpoint extents (19775-1 23.4). -1 means automatic.
    glTF: CAMERA_ZFAR_LEQUAL_ZNEAR, CAMERA_XMAG_YMAG_ZERO."""
    out = []
    for el in scene.iter():
        tag = _tag(el)
        if el.get("USE") or tag not in ("Viewpoint", "OrthoViewpoint", "GeoViewpoint"):
            continue
        n, f = _nums(el.get("nearDistance", "-1")), _nums(el.get("farDistance", "-1"))
        if n and f:
            n, f = n[0], f[0]
            if n != -1 and n <= 0:
                out.append(_diag("error", "clip-distance", el, f"nearDistance={n:g} must be greater than 0, or -1 for automatic."))
            if f != -1 and f <= 0:
                out.append(_diag("error", "clip-distance", el, f"farDistance={f:g} must be greater than 0, or -1 for automatic."))
            elif f != -1 and n != -1 and f <= n:
                out.append(_diag("error", "clip-distance", el,
                                 f"farDistance={f:g} is not beyond nearDistance={n:g}, so nothing can be drawn."))
        if tag == "OrthoViewpoint":
            v = _nums(el.get("fieldOfView"))
            if v and len(v) == 4 and (v[2] <= v[0] or v[3] <= v[1]):
                out.append(_diag("error", "ortho-extent", el,
                                 f"fieldOfView={el.get('fieldOfView')!r} is (minX minY maxX maxY) and each "
                                 f"maximum must be greater than its minimum."))
    return out


def check_light_cones(scene) -> list:
    """SpotLight beamWidth wider than cutOffAngle is clamped (19775-1 17.4.3). glTF: KHR_LIGHTS_PUNCTUAL_LIGHT_SPOT_ANGLES."""
    out = []
    for el in scene.iter():
        if _tag(el) != "SpotLight" or el.get("USE"):
            continue
        b, c = _nums(el.get("beamWidth", "0.589049")), _nums(el.get("cutOffAngle", "1.570796"))
        if b and c and b[0] > c[0] + 1e-9:
            out.append(_diag("warning", "spot-beam-wider-than-cutoff", el,
                             f"beamWidth={b[0]:g} is wider than cutOffAngle={c[0]:g}; the beam is treated "
                             f"as equal to the cut-off, so the soft edge you asked for is lost."))
    return out


def check_alpha_cutoff(scene) -> list:
    """alphaCutoff only acts in MASK mode (19775-1 12.4.2). glTF: MATERIAL_ALPHA_CUTOFF_INVALID_MODE."""
    out = []
    for el in scene.iter():
        if _tag(el) != "Appearance" or el.get("USE") or el.get("alphaCutoff") is None:
            continue
        if el.get("alphaMode", "AUTO").strip() != "MASK":
            out.append(_diag("warning", "alpha-cutoff-unused", el,
                             f"alphaCutoff={el.get('alphaCutoff')!r} has no effect with "
                             f"alphaMode={el.get('alphaMode', 'AUTO')!r}; it is used only in MASK mode."))
    return out


def check_texture_mappings(scene) -> list:
    """A material's xxxTextureMapping must name a texture coordinate mapping that the geometry has
    (19775-1 12.2.4). glTF: MESH_PRIMITIVE_TOO_FEW_TEXCOORDS."""
    defs, out = _defs(scene), []
    for shape in scene.iter():
        if _tag(shape) != "Shape" or shape.get("USE"):
            continue
        app = _kid(shape, ("Appearance",), defs)
        if app is None:
            continue
        mat = _kid(app, ("Material", "PhysicalMaterial", "UnlitMaterial"), defs)
        geo = next((_resolve(c, defs) for c in shape if _tag(c) not in ("Appearance", "") and not _tag(c).startswith("Metadata")), None)
        if mat is None or geo is None:
            continue
        have = set()
        coords = [c for c in geo.iter() if _tag(c) in ("TextureCoordinate", "TextureCoordinate3D", "TextureCoordinate4D",
                                                      "TextureCoordinateGenerator")]
        if not coords:
            continue                               # the browser generates default coordinates
        for c in coords:
            c = _resolve(c, defs)
            if c is not None:
                have.add(c.get("mapping", ""))
        for name, value in mat.attrib.items():
            if name.endswith("TextureMapping") and value and value not in have:
                out.append(_diag("warning", "texture-mapping-unresolved", mat,
                                 f"{name}={value!r} names no texture coordinate of the geometry "
                                 f"(mappings there: {', '.join(repr(h) for h in sorted(have)) or 'none'}), so the "
                                 f"texture falls back to whichever coordinates the browser picks."))
    return out


def check_pixel_textures(scene) -> list:
    """PixelTexture image = width height components then width x height pixels (19775-1 18.4.5). glTF: IMAGE_DATA_INVALID."""
    out = []
    for el in scene.iter():
        if _tag(el) != "PixelTexture" or el.get("USE") or not (el.get("image") or "").strip():
            continue
        toks = [t for t in _SPLIT.split(el.get("image").strip()) if t]
        try:
            w, h, comp = int(toks[0], 0), int(toks[1], 0), int(toks[2], 0)
        except (ValueError, IndexError):
            out.append(_diag("error", "pixel-texture-count", el, "image must start with width, height and component count."))
            continue
        if not 0 <= comp <= 4:
            out.append(_diag("error", "pixel-texture-count", el, f"image has {comp} components; 0 to 4 are allowed."))
        elif len(toks) - 3 != w * h:
            out.append(_diag("error", "pixel-texture-count", el,
                             f"image declares {w} x {h} = {w * h} pixel(s) but lists {len(toks) - 3}."))
    return out


def check_mfstring_quotes(scene) -> list:
    """MFString values in the XML encoding are quoted strings (19776-1 5.3.14). An unquoted url is read
    as one string by some browsers and rejected with a warning by others. glTF: INVALID_URI."""
    sem = _sem()
    nodes = sem.get_x3duom().get_concrete_nodes()
    out = []
    for el in scene.iter():
        tag = _tag(el)
        if tag not in nodes or el.get("USE"):
            continue
        fields = sem._node_field_map(tag, nodes)
        for name, value in el.attrib.items():
            if fields.get(name, {}).get("type") == "MFString" and value.strip() and '"' not in value:
                out.append(_diag("warning", "mfstring-unquoted", el,
                                 f"{name}={value!r} is an MFString and needs each string in double quotes, "
                                 f"for example {name}='\"{value.strip()}\"'."))
    return out


def check_interpolator_targets(scene) -> list:
    """A CoordinateInterpolator or NormalInterpolator routed to a node must send as many values per key as
    that node holds (19775-1 19.4.2). glTF: MESH_PRIMITIVE_MORPH_TARGET_INVALID_ATTRIBUTE_COUNT."""
    defs, out = _defs(scene), []
    spec = {"CoordinateInterpolator": 3, "NormalInterpolator": 3, "CoordinateInterpolator2D": 2}
    for r in scene.iter():
        if _tag(r) != "ROUTE":
            continue
        src, dst = defs.get(r.get("fromNode")), defs.get(r.get("toNode"))
        if src is None or dst is None or _tag(src) not in spec or r.get("fromField") != "value_changed":
            continue
        keys, vals = _nums(src.get("key")), _nums(src.get("keyValue"))
        attr = {"Coordinate": "point", "CoordinateDouble": "point", "Normal": "vector", "TextureCoordinate": "point"}.get(_tag(dst))
        cur = _nums(dst.get(attr)) if attr else None
        if not keys or vals is None or not cur:
            continue
        w = spec[_tag(src)]
        per_key, have = len(vals) / w / len(keys), len(cur) // (2 if _tag(dst) == "TextureCoordinate" else 3)
        if per_key != have:
            out.append(_diag("error", "interpolator-target-count", src,
                             f"sends {per_key:g} value(s) per key to {_tag(dst)} {r.get('toNode')!r}, which holds "
                             f"{have}. The indices of the geometry would point at missing or stale values."))
    return out


# --------------------------------------------------------------------------- declared bounds

def _mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def _ident():
    return [[1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]


def _trans(t):
    m = _ident()
    m[0][3], m[1][3], m[2][3] = t
    return m


def _scale(s):
    m = _ident()
    m[0][0], m[1][1], m[2][2] = s
    return m


def _rot(r):
    x, y, z, a = r
    ln = math.sqrt(x * x + y * y + z * z)
    if ln < 1e-12:
        return _ident()
    x, y, z = x / ln, y / ln, z / ln
    c, s, t = math.cos(a), math.sin(a), 1 - math.cos(a)
    return [[t * x * x + c, t * x * y - s * z, t * x * z + s * y, 0],
            [t * x * y + s * z, t * y * y + c, t * y * z - s * x, 0],
            [t * x * z - s * y, t * y * z + s * x, t * z * z + c, 0], [0, 0, 0, 1]]


def _transform_matrix(el):
    def get(name, default, n):
        v = _nums(el.get(name))
        return v if v and len(v) == n and all(math.isfinite(x) for x in v) else default
    t, c = get("translation", [0, 0, 0], 3), get("center", [0, 0, 0], 3)
    r, sr = get("rotation", [0, 0, 1, 0], 4), get("scaleOrientation", [0, 0, 1, 0], 4)
    s = get("scale", [1, 1, 1], 3)
    m = _trans(t)
    for part in (_trans(c), _rot(r), _rot(sr), _scale(s), _rot(sr[:3] + [-sr[3]]), _trans([-x for x in c])):
        m = _mat_mul(m, part)
    return m, abs(r[3]) > 1e-9 or abs(sr[3]) > 1e-9


_GROUPS = ("Group", "Transform", "Shape", "Anchor", "Collision", "StaticGroup", "CADAssembly", "CADPart", "CADFace", "CADLayer")
_OPAQUE = ("Inline", "ProtoInstance", "Switch", "LOD", "Billboard", "Text", "Extrusion", "ElevationGrid", "GeoElevationGrid",
           "NurbsPatchSurface", "ParticleSystem", "HAnimHumanoid", "GaussianSplats", "ScreenGroup", "GeoLocation", "GeoTransform")


def _points_under(el, m, rotated, defs, moving, depth=0):
    """World-space points of the static geometry under el, or None when something cannot be bounded here."""
    if depth > 64:
        return None
    el = _resolve(el, defs)
    if el is None:
        return None
    tag = _tag(el)
    if tag in _OPAQUE or (el.get("DEF") in moving):
        return None
    if tag == "Shape" and any(_tag(x).endswith("Shader") for x in el.iter()):
        return None                                # a vertex shader may move the points
    pts = []
    if tag == "Transform":
        tm, rot = _transform_matrix(el)
        m, rotated = _mat_mul(m, tm), rotated or rot
    if tag in ("Coordinate", "CoordinateDouble"):
        v = _nums(el.get("point"))
        if v is None:
            return None
        local = [v[i:i + 3] for i in range(0, len(v) - 2, 3)]
    elif tag in ("Box", "Sphere", "Cylinder", "Cone"):
        if tag == "Box":
            v = _nums(el.get("size", "2 2 2"))
            if not v or len(v) != 3:
                return None
            hx, hy, hz = v[0] / 2, v[1] / 2, v[2] / 2
        elif tag == "Sphere":
            if rotated:
                return None
            r = (_nums(el.get("radius", "1")) or [1])[0]
            hx = hy = hz = r
        else:
            if rotated:
                return None
            r = (_nums(el.get("radius" if tag == "Cylinder" else "bottomRadius", "1")) or [1])[0]
            hx = hz = r
            hy = (_nums(el.get("height", "2")) or [2])[0] / 2
        local = [[sx * hx, sy * hy, sz * hz] for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
    else:
        local = []
    for p in local:
        if not all(math.isfinite(x) for x in p):
            return None
        pts.append([m[i][0] * p[0] + m[i][1] * p[1] + m[i][2] * p[2] + m[i][3] for i in range(3)])
    for c in el:
        ct = _tag(c)
        if not ct or ct in ("ROUTE", "Appearance", "IS", "connect") or ct.startswith("Metadata") or c.get("containerField") == "proxy":
            continue
        if ct in _GROUPS or ct in _OPAQUE or ct in ("Coordinate", "CoordinateDouble", "Box", "Sphere", "Cylinder", "Cone") \
                or ct.startswith("Indexed") or ct in ("TriangleSet", "TriangleStripSet", "TriangleFanSet", "QuadSet", "LineSet", "PointSet"):
            sub = _points_under(c, m, rotated, defs, moving, depth + 1)
            if sub is None:
                return None
            pts.extend(sub)
    return pts


def check_declared_bounds(scene) -> list:
    """A declared bboxSize must enclose the node's children (19775-1 10.2.2). Only static geometry made of
    coordinates and simple primitives is measured; anything animated or generated is left alone.
    glTF: ACCESSOR_MIN_MISMATCH, ACCESSOR_MAX_MISMATCH, ACCESSOR_ELEMENT_OUT_OF_MIN/MAX_BOUND."""
    defs, out = _defs(scene), []
    moving = {r.get("toNode") for r in scene.iter() if _tag(r) == "ROUTE"}
    for el in scene.iter():
        tag = _tag(el)
        if el.get("USE") or tag not in _GROUPS:
            continue
        size = _nums(el.get("bboxSize"))
        if not size or len(size) != 3 or all(x == -1 for x in size):
            continue
        centre = _nums(el.get("bboxCenter", "0 0 0")) or [0, 0, 0]
        pts = []
        ok = True
        if tag == "Shape" and any(_tag(x).endswith("Shader") for x in el.iter()):
            continue
        for c in el:
            ct = _tag(c)
            if not ct or ct in ("ROUTE", "Appearance") or ct.startswith("Metadata") or c.get("containerField") == "proxy":
                continue
            sub = _points_under(c, _ident(), False, defs, moving)
            if sub is None:
                ok = False
                break
            pts.extend(sub)
        if not ok or not pts:
            continue
        lo = [min(p[i] for p in pts) for i in range(3)]
        hi = [max(p[i] for p in pts) for i in range(3)]
        over = []
        for i, axis in enumerate("xyz"):
            blo, bhi = centre[i] - size[i] / 2, centre[i] + size[i] / 2
            tol = 1e-4 * max(1.0, abs(hi[i] - lo[i]))
            if lo[i] < blo - tol or hi[i] > bhi + tol:
                over.append(f"{axis}: geometry {lo[i]:g} to {hi[i]:g}, box {blo:g} to {bhi:g}")
        if over:
            out.append(_diag("error", "bbox-too-small", el,
                             f"bboxSize={el.get('bboxSize')!r} does not enclose its children ({'; '.join(over)}). "
                             f"Browsers cull by this box, so parts can vanish."))
    return out


# --------------------------------------------------------------------------- skinned humanoids (HAnim)

def check_hanim_skin(scene) -> list:
    """Skin binding data of HAnimHumanoid (19774-1 6.2, 6.4, 6.6). glTF: ACCESSOR_JOINTS_INDEX_OOB,
    MESH_PRIMITIVE_JOINTS_WEIGHTS_MISMATCH, ACCESSOR_WEIGHTS_NEGATIVE, ACCESSOR_WEIGHTS_NON_NORMALIZED,
    ACCESSOR_JOINTS_INDEX_DUPLICATE, SKIN_NO_COMMON_ROOT, NODE_SKINNED_MESH_WITHOUT_SKIN,
    MESH_PRIMITIVE_MORPH_TARGET_INVALID_ATTRIBUTE_COUNT (HAnimDisplacer)."""
    defs, out = _defs(scene), []
    for h in scene.iter():
        if _tag(h) != "HAnimHumanoid" or h.get("USE"):
            continue
        skin_coord = next((_resolve(c, defs) for c in h if _tag(c) in ("Coordinate", "CoordinateDouble")
                           and c.get("containerField", "coord") == "skinCoord"), None)
        pts = _nums(skin_coord.get("point")) if skin_coord is not None else None
        n = len(pts) // 3 if pts is not None else None
        skeleton_joints = {id(j) for c in h if c.get("containerField", "") == "skeleton" or (_tag(c) == "HAnimJoint" and c.get("containerField") in (None, "skeleton"))
                           for j in c.iter() if _tag(j) == "HAnimJoint" and not j.get("USE")}
        totals = {}
        any_binding = False
        for j in h.iter():
            if _tag(j) != "HAnimJoint":
                continue
            if j.get("USE") and j.get("containerField") == "joints":
                target = defs.get(j.get("USE"))
                if target is not None and id(target) not in skeleton_joints:
                    out.append(_diag("error", "hanim-joint-outside-skeleton", j,
                                     f"USE={j.get('USE')!r} is listed in joints but is not part of this humanoid's "
                                     f"skeleton, so the skin would follow a joint that is not in the hierarchy."))
                continue
            if j.get("USE"):
                continue
            idx, w = _ints(j.get("skinCoordIndex")), _nums(j.get("skinCoordWeight"))
            if idx:
                any_binding = True
                if n is not None:
                    bad = [i for i in idx if i < 0 or i >= n]
                    if bad:
                        out.append(_diag("error", "hanim-skin-index-out-of-range", j,
                                         f"skinCoordIndex uses {bad[0]} but skinCoord has {n} point(s); {len(bad)} bad entr"
                                         f"{'y' if len(bad) == 1 else 'ies'}."))
                if len(set(idx)) < len(idx):
                    out.append(_diag("warning", "hanim-skin-index-duplicate", j,
                                     f"skinCoordIndex lists a vertex more than once, so its weight is applied twice."))
                if w is not None and len(w) not in (0, len(idx)):
                    out.append(_diag("error", "hanim-skin-weight-count", j,
                                     f"skinCoordWeight has {len(w)} value(s) for {len(idx)} skinCoordIndex entr"
                                     f"{'y' if len(idx) == 1 else 'ies'}; they pair up one to one."))
                if w and any(x < 0 for x in w):
                    out.append(_diag("error", "hanim-skin-weight-negative", j,
                                     f"skinCoordWeight has a negative weight ({min(w):g})."))
                if w and len(w) == len(idx):
                    for i, x in zip(idx, w):
                        totals[i] = totals.get(i, 0.0) + x
            for d in j:
                if _tag(d) != "HAnimDisplacer" or d.get("USE"):
                    continue
                di, dv = _ints(d.get("coordIndex")), _nums(d.get("displacements"))
                if di and dv is not None and len(dv) // 3 != len(di):
                    out.append(_diag("error", "hanim-displacer-count", d,
                                     f"displacements has {len(dv) // 3} vector(s) for {len(di)} coordIndex entr"
                                     f"{'y' if len(di) == 1 else 'ies'}."))
                if di and n is not None and any(i < 0 or i >= n for i in di):
                    out.append(_diag("error", "hanim-skin-index-out-of-range", d,
                                     f"coordIndex points past the {n} skinCoord point(s)."))
        off = [(i, t) for i, t in sorted(totals.items()) if abs(t - 1.0) > 0.01]
        if off:
            out.append(_diag("warning", "hanim-skin-weight-sum", h,
                             f"{len(off)} skin vertex(es) have joint weights that do not add up to 1 "
                             f"(first: vertex {off[0][0]} sums to {off[0][1]:.3g}); the skin shrinks or swells there."))
        has_skin = any(c.get("containerField") == "skin" for c in h)
        if has_skin and skin_coord is not None and not any_binding:
            out.append(_diag("warning", "hanim-skin-unbound", h,
                             "has a skin and skinCoord but no joint has skinCoordIndex, so the skin will not move with the skeleton."))
    return out


# --------------------------------------------------------------------------- files a scene names

_URL_NODES = {"ImageTexture": "image", "ImageCubeMapTexture": "image", "ImageTexture3D": "image", "MovieTexture": "media",
              "AudioClip": "media", "Inline": "scene"}
_MAGIC = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpeg"), (b"GIF87a", "gif"), (b"GIF89a", "gif"),
          (b"\xabKTX 20\xbb\r\n\x1a\n", "ktx2"), (b"DDS ", "dds"), (b"BM", "bmp"))
_EXT = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".gif": "gif", ".ktx2": "ktx2", ".webp": "webp", ".dds": "dds", ".bmp": "bmp"}


def _sniff(data: bytes) -> str | None:
    for magic, kind in _MAGIC:
        if data.startswith(magic):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _urls(value: str) -> list[str]:
    q = re.findall(r'"((?:[^"\\]|\\.)*)"', value)
    return q if q else ([value.strip()] if value.strip() else [])


def _inside(base: Path, url: str):
    """The file a relative url names, or None when it would leave the scene's folder."""
    try:
        p = (base / url.split("#")[0]).resolve()
        return p if p.is_relative_to(base.resolve()) else None
    except (OSError, ValueError):
        return None


def check_url_form(scene) -> list:
    """A url that is an absolute local path only works on the author's machine. glTF: NON_RELATIVE_URI."""
    out = []
    for el in scene.iter():
        if _tag(el) not in _URL_NODES or el.get("USE"):
            continue
        for u in _urls(el.get("url", "")):
            if u.startswith("file:") or re.match(r"^(/|[A-Za-z]:[\\/])", u):
                out.append(_diag("warning", "url-absolute-path", el,
                                 f"url {u!r} is an absolute local path; it will not resolve on another machine. "
                                 f"Use a path relative to the scene file."))
                break
    return out


def check_files(scene, base_dir) -> list:
    """Open what the url fields name: it must exist and, for a texture, be an image of the kind its name says,
    complete. Only files inside base_dir are opened. glTF: IO_ERROR, IMAGE_UNRECOGNIZED_FORMAT, IMAGE_DATA_INVALID, IMAGE_MIME_TYPE_INVALID,
    IMAGE_UNEXPECTED_EOS, IMAGE_NPOT_DIMENSIONS. Remote urls are not fetched."""
    out = []
    base = Path(base_dir)
    for el in scene.iter():
        tag = _tag(el)
        if tag not in _URL_NODES or el.get("USE"):
            continue
        urls = _urls(el.get("url", ""))
        if not urls:
            continue
        local = [u for u in urls if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", u) and not u.startswith(("/", "\\"))]
        inside = [u for u in local if _inside(base, u)]
        if len(inside) < len(urls) and not any(_inside(base, u).is_file() for u in inside):
            continue                               # a remote or outside alternative exists; it is not opened here
        found = [u for u in inside if _inside(base, u).is_file()]
        if not found:
            out.append(_diag("error", "url-unresolved", el,
                             f"none of its url value(s) {urls} exists next to the scene, so the "
                             f"{'texture' if _URL_NODES[tag] == 'image' else 'file'} will be missing."))
            continue
        if _URL_NODES[tag] != "image":
            continue
        u = found[0]
        path = _inside(base, u)
        with open(path, "rb") as fh:               # head and tail are enough; never load a large texture
            data = fh.read(4096)
            fh.seek(0, 2)
            if fh.tell() > 4096:
                fh.seek(max(4096, fh.tell() - 64))
                data += fh.read()
        kind = _sniff(data)
        if kind is None:
            out.append(_diag("error", "image-unrecognized", el,
                             f"url {u!r} is not an image this check recognizes (PNG, JPEG, GIF, WebP, KTX2, DDS, BMP)."))
            continue
        want = _EXT.get(path.suffix.lower())
        if want and want != kind:
            out.append(_diag("warning", "image-extension-mismatch", el,
                             f"url {u!r} is named as {want} but its content is {kind}."))
        size = None
        if kind == "png":
            if len(data) < 33 or data[12:16] != b"IHDR":
                out.append(_diag("error", "image-truncated", el, f"url {u!r} is a PNG with no header chunk."))
                continue
            size = struct.unpack(">II", data[16:24])
            if b"IEND" not in data[-16:]:
                out.append(_diag("error", "image-truncated", el,
                                 f"url {u!r} is a PNG that ends before its IEND chunk; the file is cut short."))
        elif kind == "jpeg" and not data.rstrip(b"\x00").endswith(b"\xff\xd9"):
            out.append(_diag("error", "image-truncated", el,
                             f"url {u!r} is a JPEG that ends before its end marker; the file is cut short."))
        if kind in ("webp", "ktx2", "dds", "bmp"):
            out.append(_diag("info", "image-format-optional", el,
                             f"url {u!r} is {kind}; X3D browsers must read PNG and JPEG, other formats are optional, "
                             f"so add a PNG or JPEG alternative to the url list."))
        if kind == "png" and len(data) > 25 and (data[24] == 16 or any(c in data[:4096] for c in (b"iCCP", b"cHRM"))):
            out.append(_diag("info", "image-colour-info", el,
                             f"url {u!r} has 16-bit samples or an embedded colour profile; browsers differ in whether they honour it."))
        if kind == "jpeg" and b"\xff\xc2" in data[:4096]:
            out.append(_diag("info", "image-colour-info", el, f"url {u!r} is a progressive JPEG; some loaders do not read those."))
        if size and (0 in size):
            out.append(_diag("error", "image-truncated", el, f"url {u!r} declares a size of {size[0]} x {size[1]}."))
        elif size and any(s & (s - 1) for s in size):
            out.append(_diag("info", "image-npot", el,
                             f"url {u!r} is {size[0]} x {size[1]}, not powers of two; mipmapping and REPEAT "
                             f"can behave differently across GPUs."))
    return out


SCENE_CHECKS = [
    check_use_type, check_use_cycle, check_duplicate_routes, check_attribute_counts, check_face_sizes,
    check_degenerate_faces, check_missing_coordinates, check_normals_unit, check_rotation_axes, check_scale_zero,
    check_view_volumes, check_light_cones, check_alpha_cutoff, check_texture_mappings, check_pixel_textures,
    check_mfstring_quotes, check_interpolator_targets, check_declared_bounds, check_hanim_skin, check_url_form,
]


# --------------------------------------------------------------------------- second pass: the rest of the glTF list

def check_unused_indices(scene) -> list:
    """An index list for a node that is not there is ignored. glTF: EXTRA_PROPERTY (Information)."""
    defs, out = _defs(scene), []
    pairs = (("colorIndex", ("Color", "ColorRGBA")), ("normalIndex", ("Normal",)),
             ("texCoordIndex", ("TextureCoordinate", "MultiTextureCoordinate", "TextureCoordinateGenerator")))
    for g in scene.iter():
        if _tag(g) not in ("IndexedFaceSet", "IndexedLineSet") or g.get("USE"):
            continue
        for field, kids in pairs:
            if (g.get(field) or "").strip() and _kid(g, kids + ("ProtoInstance",), defs) is None:
                out.append(_diag("info", "index-without-node", g,
                                 f"has {field} but no {kids[0]} node, so the index list is ignored."))
    return out


def check_route_fan_in(scene) -> list:
    """Two interpolators driving the same field fight each other: the result for events with the same
    timestamp is undefined (19775-1 4.4.8.5). glTF: ANIMATION_DUPLICATE_TARGETS."""
    defs, seen, out = _defs(scene), {}, []
    for r in scene.iter():
        if _tag(r) != "ROUTE":
            continue
        src = defs.get(r.get("fromNode"))
        if src is None or not _tag(src).endswith(("Interpolator", "Interpolator2D", "Follower", "Damper", "Chaser")):
            continue
        key = (r.get("toNode"), (r.get("toField") or "").removeprefix("set_"))
        first = seen.setdefault(key, r.get("fromNode"))
        if first != r.get("fromNode"):
            out.append(_diag("warning", "route-fan-in", r,
                             f"{key[0]}.{key[1]} is driven by both {first!r} and {r.get('fromNode')!r}; when "
                             f"both fire in the same frame the winner is undefined."))
    return out


def check_unreachable_keys(scene) -> list:
    """A TimeSensor fraction runs 0 to 1, so interpolator keys outside that span are never reached.
    glTF: ACCESSOR_ANIMATION_INPUT_NEGATIVE."""
    defs, out, done = _defs(scene), [], set()
    for r in scene.iter():
        if _tag(r) != "ROUTE" or r.get("fromField") != "fraction_changed":
            continue
        src, dst = defs.get(r.get("fromNode")), defs.get(r.get("toNode"))
        if src is None or dst is None or _tag(src) != "TimeSensor" or id(dst) in done:
            continue
        done.add(id(dst))
        keys = _nums(dst.get("key"))
        if keys and (min(keys) < 0 or max(keys) > 1):
            out.append(_diag("warning", "interpolator-key-unreachable", dst,
                             f"has keys from {min(keys):g} to {max(keys):g} but is driven by TimeSensor "
                             f"{r.get('fromNode')!r}, whose fraction only runs 0 to 1; the keys outside are never reached."))
    return out


def check_tangents(scene) -> list:
    """Tangent nodes: one per vertex, with a normal, handedness w of 1 or -1, and a normal texture to use them.
    glTF: MESH_PRIMITIVE_TANGENT_WITHOUT_NORMAL, ACCESSOR_INVALID_SIGN, UNUSED_MESH_TANGENT,
    MESH_PRIMITIVE_GENERATED_TANGENT_SPACE, MESH_PRIMITIVE_NO_TANGENT_SPACE."""
    defs, out = _defs(scene), []
    for shape in scene.iter():
        if _tag(shape) != "Shape" or shape.get("USE"):
            continue
        app = _kid(shape, ("Appearance",), defs)
        mat = _kid(app, ("Material", "PhysicalMaterial"), defs) if app is not None else None
        wants = mat is not None and any(c.get("containerField") == "normalTexture" for c in mat)
        geo = next((_resolve(c, defs) for c in shape if _tag(c) not in ("Appearance", "") and not _tag(c).startswith("Metadata")), None)
        if geo is None:
            continue
        tan, nor = _kid(geo, ("Tangent",), defs), _kid(geo, ("Normal",), defs)
        coord = _kid(geo, ("Coordinate", "CoordinateDouble"), defs)
        if tan is None:
            if wants and coord is not None and nor is None:
                out.append(_diag("info", "normal-texture-generated-tangents", geo,
                                 "is drawn with a normalTexture but gives no Normal or Tangent node, so each "
                                 "browser generates its own tangent space and the bumps can differ."))
            continue
        v = _nums(tan.get("vector")) or []
        if nor is None:
            out.append(_diag("warning", "tangent-without-normal", geo, "has a Tangent node but no Normal node."))
        bad = [v[i + 3] for i in range(0, len(v) - 3, 4) if abs(abs(v[i + 3]) - 1.0) > 1e-6]
        if bad:
            out.append(_diag("error", "tangent-sign", tan,
                             f"{len(bad)} tangent(s) have a fourth value that is not 1 or -1 (first: {bad[0]:g}); "
                             f"it is the handedness sign."))
        pts = _nums(coord.get("point")) if coord is not None else None
        if pts is not None and len(v) // 4 != len(pts) // 3:
            out.append(_diag("error", "attribute-count-mismatch", geo,
                             f"Tangent has {len(v) // 4} value(s) but the coordinate node has {len(pts) // 3} points."))
        if mat is not None and not wants:
            out.append(_diag("info", "tangent-unused", geo,
                             "has a Tangent node but its material has no normalTexture, so the tangents are not used."))
    return out


def check_unused_components(scene) -> list:
    """A component statement no node needs asks a browser for support the scene does not use.
    glTF: UNUSED_EXTENSION_REQUIRED."""
    sem = _sem()
    nodes = sem.get_x3duom().get_concrete_nodes()
    root = scene.getroottree().getroot()
    used = {nodes[t].get("component") for t in {_tag(e) for e in scene.iter()} if t in nodes}
    if any(_tag(e) in ("ProtoInstance", "ExternProtoDeclare", "Inline", "Script") for e in scene.iter()):
        return []                                  # what those pull in cannot be seen from here
    out = []
    for el in root.iter():
        if _tag(el) == "component" and el.get("name") and el.get("name") not in used:
            out.append(_diag("info", "component-unused", el,
                             f"name={el.get('name')!r} is declared but no node of that component is in the scene."))
    return out


def check_hanim_bindings(scene) -> list:
    """jointBindingPositions, Rotations and Scales pair with joints one to one (19774-1 6.2).
    glTF: INVALID_IBM_ACCESSOR_COUNT. Also a joint that binds vertices while the humanoid has no skinCoord
    (NODE_SKINNED_MESH_WITHOUT_SKIN) and a binding with weight 0 (ACCESSOR_JOINTS_USED_ZERO_WEIGHT)."""
    out = []
    for h in scene.iter():
        if _tag(h) != "HAnimHumanoid" or h.get("USE"):
            continue
        joints = [c for c in h if _tag(c) == "HAnimJoint" and c.get("containerField") == "joints"]
        for field, width in (("jointBindingPositions", 3), ("jointBindingRotations", 4), ("jointBindingScales", 3)):
            v = _nums(h.get(field))
            if v and joints and len(v) // width != len(joints):
                out.append(_diag("error", "hanim-binding-count", h,
                                 f"{field} has {len(v) // width} value(s) for {len(joints)} joint(s) in joints; "
                                 f"they pair up one to one."))
        has_coord = any(_tag(c) in ("Coordinate", "CoordinateDouble") and c.get("containerField") == "skinCoord" for c in h)
        for j in h.iter():
            if _tag(j) != "HAnimJoint" or j.get("USE"):
                continue
            idx, w = _ints(j.get("skinCoordIndex")), _nums(j.get("skinCoordWeight"))
            if idx and not has_coord:
                out.append(_diag("warning", "hanim-skin-coord-missing", j,
                                 "has skinCoordIndex but the humanoid has no skinCoord node to bind to."))
                has_coord = True                   # say it once per humanoid
            if idx and w and len(w) == len(idx) and any(x == 0 for x in w):
                out.append(_diag("warning", "hanim-skin-weight-zero", j,
                                 f"binds {sum(1 for x in w if x == 0)} vertex(es) with weight 0, which does nothing."))
    return out


SCENE_CHECKS += [check_unused_indices, check_route_fan_in, check_unreachable_keys, check_tangents,
                 check_unused_components, check_hanim_bindings]


def data_diagnostics(scene, base_dir=None) -> list:
    out = []
    for fn in SCENE_CHECKS:
        out.extend(fn(scene))
    if base_dir is not None:
        out.extend(check_files(scene, base_dir))
    return out
