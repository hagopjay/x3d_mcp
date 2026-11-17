"""A small, budgeted, trust-tiered description of an X3D scene, for a model that must not be handed the whole file.

`scene_brief(text)` answers in one compact JSON document of a size the caller sets (`budget_bytes`, a hard cap on
the UTF-8 length). Everything in it is one of:

* a number, parsed and checked finite,
* a name from a closed set (X3DUOM node and field names, check ids, fixed words) or `"?"`,
* a handle `n<k>`, the 1-based document-order index of an element of the Scene (stable for one `sha256`),
* a count, a length or an 8-hex digest of author text.

Author text (DEF names, `meta`, titles, descriptions, urls, Script source, comments) never appears in a facet.
It is counted and digested under `untrusted`, and quoted there (capped, JSON-escaped) only when `text="quoted"`.
Validator messages are never forwarded: XSD errors are classified into a closed `kind`, semantic findings keep
their check id. Default counts mode removes direct author-text channels; this is not a general proof of prompt
injection resistance. Opt-in quoted text remains untrusted and must never acquire tool or acceptance authority.

The brief is a slice, not a proof. `validators` facets are oracle outputs of this server's own checkers; do not
hand them to a judge you are trying to score against those same checkers (answer leakage): ask for the other
facets only. Bounds are the axis-aligned box of the authored pose, conservative under rotation, and say `static`.

Pure functions; uses lxml and the repository's validators; no mcp import.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext

from lxml import etree

SCHEMA = "x3d-scene-brief/1"
FACETS = ("census", "bounds", "camera", "lights", "materials", "routes", "validators", "text")
MIN_BUDGET = 1024
DEFAULT_BUDGET = 4096
DEFAULT_MAX_BUDGET = 65536
TEXT_MODES = ("off", "counts", "quoted")
ID_MODES = ("handles", "names")
VERSIONS = ("3.0", "3.1", "3.2", "3.3", "4.0", "4.1")
PROFILES = ("Core", "Interchange", "CADInterchange", "Interactive", "Immersive", "MedicalInterchange", "MPEG-4", "Full")
MAX_DEPTH = 64                    # bounds walk: deeper subtrees are counted, not entered
TIMING_RESERVE = 22               # bytes kept free for `,"timing_ms":NNNNN.N` so the hard cap holds with it
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,47}$")
CHECK_ID = re.compile(r"^[a-z0-9-]{1,40}$")
STRUCTURAL = {"X3D", "head", "Scene", "meta", "component", "unit", "ROUTE", "IS", "connect", "field", "fieldValue",
              "ProtoDeclare", "ProtoInterface", "ProtoBody", "ProtoInstance", "ExternProtoDeclare",
              "ExternProtoInstance", "ImportedNodes", "IMPORT", "EXPORT", "Scene"}
IDENT_ATTRS = ("DEF", "USE", "class", "id", "style", "containerField")
_NUMERIC_SLOT = re.compile(r"^(?:\s|[+\-.,0-9eE]|0[xX][0-9a-fA-F]+|true|false|TRUE|FALSE)*$")

_PARSER = etree.XMLParser(huge_tree=False, resolve_entities=False, no_network=True, load_dtd=False)


class BriefError(ValueError):
    """The input cannot be briefed. `code` is a fixed word, `line` an int or None; never document text."""

    def __init__(self, code: str, line: int | None = None, **extra):
        super().__init__(code)
        self.code = code
        self.line = line
        self.extra = extra


# --- X3DUOM-derived vocabulary (built once) -----------------------------------------------------------------
_VOCAB: dict | None = None


def _vocab() -> dict:
    global _VOCAB
    if _VOCAB is not None:
        return _VOCAB
    from x3d_utils.x3duom import get_x3duom
    uom = get_x3duom()
    nodes, abstract = uom.get_concrete_nodes(), uom.get_abstract_types()
    parents: dict[str, list[str]] = {}
    for name, info in list(nodes.items()) + list(abstract.items()):
        ps = [info.get("baseType")] + list(info.get("additionalBaseTypes") or [])
        parents[name] = [p for p in ps if p]

    def inherits(name: str, target: str) -> bool:
        seen, todo = set(), [name]
        while todo:
            cur = todo.pop()
            if cur == target:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            todo.extend(parents.get(cur, ()))
        return False

    fields = {n: {f["name"] for f in info["fields"]} for n, info in nodes.items()}
    kinds = {n: {f["name"]: f["type"] for f in info["fields"]} for n, info in nodes.items()}
    _VOCAB = {
        "nodes": set(nodes),
        "fields": fields,
        "ftype": kinds,
        "lights": {n for n in nodes if inherits(n, "X3DLightNode")},
        "viewpoints": {n for n in nodes if inherits(n, "X3DViewpointNode")},
        "textures": {n for n in nodes if inherits(n, "X3DTextureNode")},
        "materials": {n for n in nodes if inherits(n, "X3DMaterialNode")},
        "geometry": {n for n in nodes if inherits(n, "X3DGeometryNode")},
        "backgrounds": {n for n in nodes if inherits(n, "X3DBackgroundNode")},
    }
    return _VOCAB


def _safe_node(name: str) -> str:
    v = _vocab()
    return name if name in v["nodes"] or name in STRUCTURAL else "?"


def _safe_field(node: str, field: str) -> str:
    """A field or event name from the node's own X3DUOM interface (or its set_/_changed form), else '?'."""
    v = _vocab()
    known = v["fields"].get(node)
    if known is None:
        return "?"
    if field in known:
        return field
    if field.startswith("set_") and field[4:] in known:
        return field
    if field.endswith("_changed") and field[:-8] in known:
        return field
    return "?"


def _tag(el) -> str:
    t = el.tag
    if not isinstance(t, str):
        return ""
    return t.rsplit("}", 1)[-1]


def floats(value, n: int | None = None) -> list[float] | None:
    """Parse X3D numbers; None unless every token is finite (and, when n is given, there are exactly n)."""
    try:
        out = [float(x) for x in (value or "").replace(",", " ").split()]
    except ValueError:
        return None
    if n is not None and len(out) != n:
        return None
    return out if all(math.isfinite(x) for x in out) else None


def _r3(vals) -> list[float]:
    return [round(float(x) + 0.0, 3) for x in vals]


# --- bounds -----------------------------------------------------------------------------------------------
_I3 = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]


def _rot(axis_angle):
    x, y, z, a = axis_angle
    scale = max(abs(x), abs(y), abs(z))
    if scale == 0:
        return _I3 if a == 0 else None
    x, y, z = x / scale, y / scale, z / scale
    n = math.hypot(x, y, z)
    x, y, z = x / n, y / n, z / n
    c, s = math.cos(a), math.sin(a)
    t = 1 - c
    return [[t * x * x + c, t * x * y - s * z, t * x * z + s * y],
            [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
            [t * x * z - s * y, t * y * z + s * x, t * z * z + c]]


def _mm(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _mv(m, v):
    return [sum(m[i][k] * v[k] for k in range(3)) for i in range(3)]


def _local(el):
    """(linear, offset) of a Transform: T * C * R * SR * S * -SR * -C, or None when a field is not a number."""
    t = floats(el.get("translation", "0 0 0"), 3)
    c = floats(el.get("center", "0 0 0"), 3)
    r = floats(el.get("rotation", "0 0 1 0"), 4)
    so = floats(el.get("scaleOrientation", "0 0 1 0"), 4)
    s = floats(el.get("scale", "1 1 1"), 3)
    if None in (t, c, r, so, s):
        return None
    R, SR = _rot(r), _rot(so)
    if R is None or SR is None:
        return None
    S = [[s[0], 0, 0], [0, s[1], 0], [0, 0, s[2]]]
    lin = _mm(_mm(_mm(R, SR), S), [list(col) for col in zip(*SR)])
    off = [t[i] + c[i] - sum(lin[i][k] * c[k] for k in range(3)) for i in range(3)]
    return lin, off


def _compose(parent, child):
    (pl, po), (cl, co) = parent, child
    return _mm(pl, cl), [po[i] + sum(pl[i][k] * co[k] for k in range(3)) for i in range(3)]


def _finite_transform(transform):
    linear, offset = transform
    return all(math.isfinite(x) for row in linear for x in row) and all(math.isfinite(x) for x in offset)


def _outward_box(lo, hi):
    """Quantize the displayed endpoints outwards; nearest rounding can shrink a conservative box."""
    quantum = Decimal("0.001")
    # 400 decimal digits cover all finite binary64 values, including their fractional part at this precision.
    with localcontext() as ctx:
        ctx.prec = 400
        # Quantize the shortest decimal that names the double (0.1, not its 55-digit binary expansion), so a value that sits a hair above a grid
        # point is not widened by a whole quantum; the min/max against the double itself keeps the box conservative.
        low = [min(x, float(Decimal(repr(x)).quantize(quantum, rounding=ROUND_FLOOR))) for x in lo]
        high = [max(x, float(Decimal(repr(x)).quantize(quantum, rounding=ROUND_CEILING))) for x in hi]
    size = [high[i] - low[i] for i in range(3)]
    center = [low[i] / 2 + high[i] / 2 for i in range(3)]
    if not all(math.isfinite(x) for x in low + high + size + center):
        return None
    return {"min": low, "max": high, "size": size, "center": center}


def _shape_box(shape, defs):
    """((min, max), None) in the shape's own frame, or (None, reason) with a reason from a closed set."""
    v = _vocab()
    geom = None
    for c in shape:
        t = _tag(c)
        if c.get("USE"):
            c = defs.get(c.get("USE"), c)
            t = _tag(c)
        if t in v["geometry"]:
            geom = c
            break
    if geom is None:
        return None, "no-geometry"
    g = _tag(geom)
    if g == "Box":
        s = floats(geom.get("size", "2 2 2"), 3)
        return (([-x / 2 for x in s], [x / 2 for x in s]), None) if s and min(s) >= 0 else (None, "invalid-value")
    if g == "Sphere":
        r = floats(geom.get("radius", "1"), 1)
        return (([-r[0]] * 3, [r[0]] * 3), None) if r and r[0] >= 0 else (None, "invalid-value")
    if g in ("Cylinder", "Cone"):
        r = floats(geom.get("radius" if g == "Cylinder" else "bottomRadius", "1"), 1)
        h = floats(geom.get("height", "2"), 1)
        if r and h and r[0] >= 0 and h[0] >= 0:
            return (([-r[0], -h[0] / 2, -r[0]], [r[0], h[0] / 2, r[0]]), None)
        return None, "invalid-value"
    for c in geom:
        t = _tag(c)
        if c.get("USE"):
            c = defs.get(c.get("USE"), c)
            t = _tag(c)
        if t == "Coordinate" and c.get("point") is not None:
            p = floats(c.get("point"))
            if p is None or len(p) % 3 != 0:
                return None, "invalid-value"
            if not p:
                return None, "no-geometry"
            xs, ys, zs = p[0::3], p[1::3], p[2::3]
            return (([min(xs), min(ys), min(zs)], [max(xs), max(ys), max(zs)]), None)
    return None, "unsupported:" + _safe_node(g)


def _bounds(scene, defs, total_shapes: int) -> dict:
    lo, hi = [math.inf] * 3, [-math.inf] * 3
    got = 0
    unbounded: Counter = Counter()
    stack = [(scene, (_I3, [0.0, 0.0, 0.0]), 0)]
    while stack:
        el, m, depth = stack.pop()
        if depth > MAX_DEPTH:
            unbounded["too-deep"] += 1
            continue
        kids = [c for c in el if _tag(c)]
        t = _tag(el)
        if t == "Switch":
            w = floats(el.get("whichChoice", "-1"), 1)
            if not w or not w[0].is_integer():
                unbounded["invalid-value"] += 1
                kids = []
                continue
            w = int(w[0])
            kids = [kids[w]] if 0 <= w < len(kids) else []
        elif t == "LOD":
            kids = kids[:1]
        for c in reversed(kids):
            ct = _tag(c)
            if ct in ("ProtoDeclare", "ExternProtoDeclare", "head"):
                continue
            if t == "Collision" and c.get("containerField") == "proxy":
                continue                  # a collision proxy is never drawn
            node = defs.get(c.get("USE"), c) if c.get("USE") else c
            nt = _tag(node)
            if nt == "Shape":
                box, why = _shape_box(node, defs)
                if box is None:
                    unbounded[why] += 1
                    continue
                lin, off = m
                points = []
                for cx in (box[0][0], box[1][0]):
                    for cy in (box[0][1], box[1][1]):
                        for cz in (box[0][2], box[1][2]):
                            p = _mv(lin, [cx, cy, cz])
                            points.append([p[i] + off[i] for i in range(3)])
                if not all(math.isfinite(q) for p in points for q in p):
                    unbounded["numeric-overflow"] += 1
                    continue
                for p in points:
                    for i, q in enumerate(p):
                        lo[i] = min(lo[i], q)
                        hi[i] = max(hi[i], q)
                got += 1
            elif nt in ("Transform", "CADPart", "HAnimJoint", "HAnimSite", "HAnimSegment", "HAnimHumanoid"):
                loc = _local(node)
                if loc is None:
                    unbounded["invalid-value"] += 1
                    continue
                combined = _compose(m, loc)
                if not _finite_transform(loc) or not _finite_transform(combined):
                    unbounded["numeric-overflow"] += 1
                    continue
                stack.append((node, combined, depth + 1))
            elif nt in ("Inline", "ProtoInstance", "Text", "GeoTransform", "GeoLocation", "GeoLOD", "EspduTransform"):
                unbounded["unsupported:" + _safe_node(nt)] += 1
            elif nt in ("Group", "Anchor", "Collision", "Billboard", "StaticGroup", "Switch", "LOD",
                        "CADLayer", "CADAssembly", "CADFace"):
                stack.append((node, m, depth + 1))
    out = {"tier": "derived", "static": True, "method": "aabb-of-transformed-primitive-aabbs", "conservative": True,
           "shapes": total_shapes, "bounded_shapes": got, "unbounded": dict(sorted(unbounded.items()))}
    if got:
        summary = _outward_box(lo, hi)
        if summary is None:
            out["unbounded"]["aggregate-numeric-overflow"] = 1
        else:
            out.update(summary)
    return out


# --- validators, as structure -----------------------------------------------------------------------------
_XSD = [(re.compile(r"Element '([^']+)', attribute '([^']+)': \[facet '(\w+)'\]"), "facet"),
        (re.compile(r"Element '([^']+)': This element is not expected"), "not-expected"),
        (re.compile(r"Element '([^']+)': Missing child"), "missing-child"),
        (re.compile(r"Element '([^']+)', attribute '([^']+)': '"), "bad-value"),
        (re.compile(r"Element '([^']+)', attribute '([^']+)': The attribute '[^']*' is not allowed"), "attribute-not-allowed")]
_FACETS = ("pattern", "enumeration", "minInclusive", "maxInclusive", "minExclusive", "maxExclusive", "length", "minLength", "maxLength")


def _xsd_class(msg: str) -> dict:
    """One XSD error as {line, kind, element, attribute?}: names are checked against X3DUOM, text is dropped."""
    m = re.match(r"(?:Line )?(\d+): (.*)", msg, re.S) or re.match(r"(?:Line )?(\d+)[:,] (.*)", msg, re.S)
    line, body = (int(m.group(1)), m.group(2)) if m else (None, msg)
    for rx, kind in _XSD:
        g = rx.match(body)
        if not g:
            continue
        names = g.groups()
        el = _safe_node(names[0])
        out = {"kind": kind, "element": el}
        if kind in ("facet", "bad-value", "attribute-not-allowed"):
            out["attribute"] = _safe_field(el, names[1]) if el != "?" else "?"
        if kind == "facet":
            out["kind"] = names[2] if names[2] in _FACETS else "facet"
        if line is not None:
            out["line"] = line
        return out
    return {"kind": "other", **({"line": line} if line is not None else {})}


def _validators(text: str, els, defs, handle, max_items: int) -> dict:
    out: dict = {}
    from validation.validate import validate_xml
    from validation.semantic import NoSceneError, semantic_diagnostics
    try:
        x = validate_xml(text)
        if x.get("checked") is False:
            out["xsd"] = {"tier": "not_run", "checked": False, "reason": "not-checkable-at-this-size"}
        else:
            errs = list(x.get("errors") or [])
            out["xsd"] = {"tier": "oracle", "oracle": "xsd-" + (str(x.get("schema_version")) if str(x.get("schema_version")) in VERSIONS else "?"),
                          "valid": bool(x.get("valid")), "errors": len(errs), "first": [_xsd_class(e) for e in errs[:max_items]]}
    except Exception:       # a checker that raises is a missing verdict, never a pass
        out["xsd"] = {"tier": "not_run", "checked": False, "reason": "checker-raised"}
    try:
        diags = semantic_diagnostics(text)
        lvl = Counter(d.level for d in diags)
        chk = Counter((d.level, d.check if CHECK_ID.match(d.check or "") else "?") for d in diags if d.level != "info")
        first = []
        for d in diags:
            if d.level != "error" or len(first) >= max_items:
                continue
            item = {"check": d.check if CHECK_ID.match(d.check or "") else "?", "node": _safe_node(d.node_tag or "")}
            if d.def_name and d.def_name in defs:
                item["at"] = handle(defs[d.def_name])
            first.append(item)
        out["semantic"] = {"tier": "oracle", "errors": lvl["error"], "warnings": lvl["warning"], "infos": lvl["info"],
                           "by_check": {f"{a}:{b}": n for (a, b), n in sorted(chk.items())}, "first_errors": first}
    except NoSceneError:
        out["semantic"] = {"tier": "not_run", "checked": False, "reason": "no-scene"}
    except Exception:
        out["semantic"] = {"tier": "not_run", "checked": False, "reason": "checker-raised"}
    out["render"] = {"tier": "not_run", "checked": False, "reason": "use render_image"}
    return out


# --- the text surface ---------------------------------------------------------------------------------------
def _text_surface(root, handle, defs, text_mode: str, text_cap: int, ids: str, max_items: int) -> dict:
    v = _vocab()
    kinds: dict[str, dict] = {}
    quoted: list = []

    def add(kind: str, s: str, el=None, quote: bool = False):
        k = kinds.setdefault(kind, {"items": 0, "chars": 0, "_h": hashlib.sha256()})
        k["items"] += 1
        k["chars"] += len(s)
        k["_h"].update(s.encode("utf-8", "surrogatepass"))
        if quote and text_mode == "quoted" and len(quoted) < 4 * max_items:
            quoted.append({"kind": kind, "at": handle(el) if el is not None else None, "quoted": s[:text_cap], "cut": len(s) > text_cap})

    for sib in list(root.itersiblings(preceding=True)) + list(root.itersiblings()):
        add("comment", sib.text or "")
    for e in root.iter():
        if not isinstance(e.tag, str):
            add("comment", e.text or "")
            continue
        t = _tag(e)
        known = v["fields"].get(t)
        for a, val in e.attrib.items():
            a = a.rsplit("}", 1)[-1]
            if t == "meta" and a in ("name", "content"):
                add("meta", val, e, True)
            elif a == "DEF":
                add("def_name", val, e)
            elif a == "USE":
                add("use_name", val, e)
            elif a in IDENT_ATTRS:
                add("ident_attr", val, e)
            elif known is not None and a in known:
                if v["ftype"][t][a] in ("SFString", "MFString"):
                    kind = f"{t}.{a}"
                    add(kind, val, e, kind not in ("Script.url",))
                elif not _NUMERIC_SLOT.match(val):
                    add("slot_text", val, e)       # words where a number or boolean belongs: still author text
            elif t in ("ProtoInstance", "field", "fieldValue", "connect", "ROUTE", "meta", "component", "unit", "X3D", "head", "Scene", "IS"):
                if t == "ROUTE" or t == "meta":
                    continue
                add("proto_attr", val, e)
            else:
                add("other_attr", val, e)
        if t in ("Script", "ShaderPart", "ShaderProgram", "ComposedShader", "PackagedShader"):
            src = ((e.text or "") + "".join(c.tail or "" for c in e)).strip()
            if src:
                add("script_source", src, e)
    untrusted: dict = {"tier": "untrusted",
                       "text_surface": {k: {"items": s["items"], "chars": s["chars"], "sha256_8": s["_h"].hexdigest()[:8]}
                                        for k, s in sorted(kinds.items())}}
    if text_mode == "quoted" and quoted:
        untrusted["quoted"] = quoted[:max_items]
    if ids == "names":
        names = {}
        for n, e in defs.items():
            if IDENT.match(n):
                names[handle(e)] = n
        untrusted["names"] = dict(list(sorted(names.items(), key=lambda kv: int(kv[0][1:])))[:4 * max_items])
        omitted = len(defs) - len(names)
        if omitted:
            untrusted["names_omitted"] = omitted
    return untrusted


# --- assembling and fitting ----------------------------------------------------------------------------------
def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _cut(d: dict, key: str, n: int):
    cur = d.get(key)
    if isinstance(cur, dict) and len(cur) > n:
        d[key] = dict(list(cur.items())[:n])
    elif isinstance(cur, list) and len(cur) > n:
        d[key] = cur[:n]


def _trim_lists(body: dict, n: int):
    f = body["facets"]
    if "census" in f:
        _cut(f["census"], "top", n)
    if "routes" in f:
        _cut(f["routes"], "top_edges", n)
    if "materials" in f:
        _cut(f["materials"], "material_types", n)
    if "lights" in f:
        _cut(f["lights"], "by_type", n)
    if "validators" in f:
        v = f["validators"]
        if "xsd" in v:
            _cut(v["xsd"], "first", n)
        if "semantic" in v:
            _cut(v["semantic"], "first_errors", n)
            _cut(v["semantic"], "by_check", max(n, 1) * 2)
    u = body.get("untrusted")
    if u:
        _cut(u, "quoted", n)
        _cut(u, "names", n)


def _collapse(body: dict):
    f = body["facets"]
    for fac, keys in (("census", ("top",)), ("routes", ("top_edges",)), ("materials", ("material_types",)), ("lights", ("by_type",))):
        for k in keys:
            if fac in f:
                f[fac].pop(k, None)
    v = f.get("validators")
    if v:
        if "xsd" in v:
            v["xsd"].pop("first", None)
        if "semantic" in v:
            v["semantic"].pop("first_errors", None)
            v["semantic"].pop("by_check", None)
    u = body.get("untrusted")
    if u and "text_surface" in u:
        ts = u["text_surface"]
        body["untrusted"] = {"tier": "untrusted", "kinds": len(ts), "items": sum(x["items"] for x in ts.values()),
                             "chars": sum(x["chars"] for x in ts.values())}


def _used(body: dict) -> int:
    """Upper bound on the final answer's UTF-8 length: this body with a hash placeholder plus the timing reserve."""
    return len((_canon({**body, "brief_sha256": "0" * 64})).encode("utf-8")) + TIMING_RESERVE


def _fit(body: dict, budget: int, max_items: int, clamped: bool = False) -> dict:
    dropped: list[str] = []
    body["budget"] = {"bytes": budget, "used": 0, "est_tokens": 0, "est_method": "bytes/4", "max_items": max_items, "dropped": dropped}
    if clamped:
        body["budget"]["clamped"] = True

    def over() -> bool:
        return _used(body) > budget

    n = max_items
    while over() and n > 1:
        n = max(1, n // 2)
        _trim_lists(body, n)
        dropped.append(f"max_items:{n}")
    body["budget"]["max_items"] = n
    u = body.get("untrusted")
    for key in ("names", "quoted"):
        if over() and u and key in u:
            u.pop(key)
            if key == "names":
                u.pop("names_omitted", None)
            dropped.append(f"untrusted.{key}")
    if over():
        _collapse(body)
        dropped.append("lists-to-counts")  # also folds the text-surface digests into one total
    for fac in ("materials", "lights", "camera", "bounds", "routes", "census"):
        if over() and fac in body["facets"]:
            body["facets"].pop(fac)
            dropped.append(f"facet:{fac}")
    if over() and "untrusted" in body:
        body.pop("untrusted")
        dropped.append("untrusted")
    if over() and "validators" in body["facets"]:
        body["facets"]["validators"].pop("render", None)
        dropped.append("validators.render")
    for _ in range(6):                       # `used` and `est_tokens` count their own digits: iterate to a fixed point
        used = _used(body)
        body["budget"]["used"] = used
        body["budget"]["est_tokens"] = -(-used // 4)
        if _used(body) == used:
            break
    if _used(body) > budget and "sha256_full" in body.get("input", {}):
        # Last resort, so the documented 1,024-byte floor stays satisfiable: the 64-hex digest is the only fixed-size field added after the
        # floor was set. The 16-hex `sha256` stays, and the drop is named.
        body["input"].pop("sha256_full")
        dropped.append("input.sha256_full")
        for _ in range(6):
            used = _used(body)
            body["budget"]["used"] = used
            body["budget"]["est_tokens"] = -(-used // 4)
            if _used(body) == used:
                break
    if _used(body) > budget:
        raise BriefError("budget-too-small", min_bytes=_used(body))
    return body


def scene_brief(text: str, *, facets=None, budget_bytes: int = DEFAULT_BUDGET, max_items: int = 8, text_mode: str = "counts",
                text_cap: int = 80, ids: str = "handles", max_budget: int = DEFAULT_MAX_BUDGET) -> dict:
    """Build the brief for one X3D document (XML text). Raises BriefError(code, line) for an input it cannot read."""
    t0 = time.perf_counter()
    want = tuple(facets) if facets else FACETS
    bad = [f for f in want if f not in FACETS]
    if bad:
        raise BriefError("unknown-facet")
    if text_mode not in TEXT_MODES:
        raise BriefError("bad-text-mode")
    if ids not in ID_MODES:
        raise BriefError("bad-ids-mode")
    clamped = False
    if budget_bytes < MIN_BUDGET:
        budget_bytes, clamped = MIN_BUDGET, True
    if budget_bytes > max_budget:
        budget_bytes, clamped = max_budget, True
    max_items = min(50, max(1, int(max_items)))
    text_cap = min(200, max(16, int(text_cap)))
    data = text.encode("utf-8", "surrogatepass")
    try:
        root = etree.fromstring(data, _PARSER)
    except etree.XMLSyntaxError as e:
        raise BriefError("not-well-formed", int(e.lineno) if getattr(e, "lineno", None) else None)
    if _tag(root) != "X3D":
        raise BriefError("not-x3d")
    scene = next((c for c in root if _tag(c) == "Scene"), None)
    if scene is None:
        raise BriefError("no-scene")
    v = _vocab()
    els = [e for e in scene.iter() if isinstance(e.tag, str)]
    index = {id(e): i for i, e in enumerate(els, 1)}
    defs: dict = {}
    for e in els:
        d = e.get("DEF")
        if d and d not in defs:
            defs[d] = e

    def handle(e):
        return f"n{index[id(e)]}" if id(e) in index else None      # head, meta and the root are outside the Scene

    tags = [_tag(e) for e in els]
    census_all = Counter(_safe_node(t) for t in tags if t not in ("Scene", "ROUTE"))
    shapes = [e for e, t in zip(els, tags) if t == "Shape"]
    out_facets: dict = {}
    if "census" in want:
        depth_max = depth = 0
        for ev, el in etree.iterwalk(scene, events=("start", "end")):
            if not isinstance(el.tag, str):
                continue
            if ev == "start":
                depth += 1
                depth_max = max(depth_max, depth)
            else:
                depth -= 1
        out_facets["census"] = {"tier": "measured", "nodes": sum(census_all.values()),
                                "types": len(census_all), "defs": len(defs), "uses": sum(1 for e in els if e.get("USE")),
                                "depth_max": depth_max, "protos": sum(1 for t in tags if t == "ProtoDeclare"),
                                "top": dict(census_all.most_common(max_items))}
    if "bounds" in want:
        out_facets["bounds"] = _bounds(scene, defs, len(shapes))
    if "camera" in want:
        vps = [e for e, t in zip(els, tags) if t in v["viewpoints"]]
        navs = [e for e, t in zip(els, tags) if t == "NavigationInfo"]
        cam: dict = {"tier": "measured", "viewpoints": len(vps), "navigation_info": len(navs),
                     "headlight": (navs[0].get("headlight", "true") != "false") if navs else True,
                     "background": sum(1 for t in tags if t in v["backgrounds"])}
        if vps and _tag(vps[0]) == "Viewpoint":
            vp = vps[0]
            pos, ori, fov = floats(vp.get("position", "0 0 10"), 3), floats(vp.get("orientation", "0 0 1 0"), 4), floats(vp.get("fieldOfView", "0.7854"), 1)
            cam["first"] = {"at": handle(vp), "position": _r3(pos) if pos else None, "orientation": _r3(ori) if ori else None,
                            "fov": round(fov[0], 4) if fov else None}
        out_facets["camera"] = cam
    if "lights" in want:
        lt = Counter(_safe_node(t) for t in tags if t in v["lights"])
        navs = [e for e, t in zip(els, tags) if t == "NavigationInfo"]
        out_facets["lights"] = {"tier": "measured", "count": sum(lt.values()), "by_type": dict(sorted(lt.items())),
                                "headlight": (navs[0].get("headlight", "true") != "false") if navs else True}
    if "materials" in want:
        mats = [e for e, t in zip(els, tags) if t in v["materials"]]
        transparent = 0
        for e in mats:
            tr = floats(e.get("transparency", "0"), 1)
            if tr and tr[0] > 0:
                transparent += 1
        out_facets["materials"] = {"tier": "measured", "shapes": len(shapes),
                                   "without_appearance": sum(1 for s in shapes if not any(_tag(c) == "Appearance" for c in s)),
                                   "material_types": dict(sorted(Counter(_safe_node(_tag(e)) for e in mats).items())),
                                   "textures": sum(1 for t in tags if t in v["textures"]), "transparent": transparent}
    if "routes" in want:
        routes = [e for e, t in zip(els, tags) if t == "ROUTE"]
        fan_in: Counter = Counter()
        fan_out: Counter = Counter()
        pairs: Counter = Counter()
        dangling = script_ends = 0
        for r in routes:
            a, b = defs.get(r.get("fromNode")), defs.get(r.get("toNode"))
            if a is None or b is None:
                dangling += 1
                continue
            ta, tb = _tag(a), _tag(b)
            if ta == "Script" or tb == "Script":
                script_ends += 1
            fan_out[r.get("fromNode") + "." + (r.get("fromField") or "")] += 1
            fan_in[r.get("toNode") + "." + (r.get("toField") or "")] += 1
            pairs[f"{_safe_node(ta)}.{_safe_field(ta, r.get('fromField') or '')}>{_safe_node(tb)}.{_safe_field(tb, r.get('toField') or '')}"] += 1
        out_facets["routes"] = {"tier": "measured", "count": len(routes), "dangling": dangling, "script_endpoints": script_ends,
                                "max_fan_in": max(fan_in.values(), default=0), "max_fan_out": max(fan_out.values(), default=0),
                                "top_edges": dict(pairs.most_common(max_items))}
    if "validators" in want:
        out_facets["validators"] = _validators(text, els, defs, handle, max_items)
    input_digest = hashlib.sha256(data).hexdigest()
    body: dict = {
        "schema": SCHEMA, "ok": True,
        "input": {"sha256": input_digest[:16], "sha256_full": input_digest, "bytes": len(data),
                  "x3d_version": root.get("version") if root.get("version") in VERSIONS else None,
                  "profile": root.get("profile") if root.get("profile") in PROFILES else None,
                  "declared_invalid": [k for k, ok in (("version", root.get("version") in VERSIONS or root.get("version") is None),
                                                       ("profile", root.get("profile") in PROFILES or root.get("profile") is None)) if not ok]},
        "facets": out_facets,
    }
    if "text" in want and text_mode != "off":
        body["untrusted"] = _text_surface(root, handle, defs, text_mode, text_cap, ids, max_items)
    elif "text" in want:
        body["untrusted"] = {"tier": "untrusted", "text": "off"}
    body = _fit(body, budget_bytes, max_items, clamped)
    body["brief_sha256"] = hashlib.sha256(_canon(body).encode("utf-8")).hexdigest()
    body["timing_ms"] = round(min((time.perf_counter() - t0) * 1000, 99999.9), 1)
    return body


def render(brief: dict) -> str:
    """The canonical text of a brief: sorted keys, compact, ASCII only (bidi and zero-width characters show as escapes)."""
    return _canon(brief)


def card(brief: dict) -> dict:
    """The verdict card (about 350 bytes): digest, size and the two validator verdicts when they were run."""
    f = brief.get("facets", {})
    val = f.get("validators", {})
    return {"schema": SCHEMA, "ok": True, "input": brief["input"], "nodes": f.get("census", {}).get("nodes"),
            "xsd_valid": val.get("xsd", {}).get("valid"), "semantic_errors": val.get("semantic", {}).get("errors"),
            "brief_sha256": brief["brief_sha256"]}
