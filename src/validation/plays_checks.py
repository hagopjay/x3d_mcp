"""Checks for scenes that are legal X3D and play or draw wrong.

Every scene these checks report passes the XSD, has every node in a legal place and every ROUTE joining fields of
the right type. What is wrong shows only when the scene runs: a clock whose one cycle ended decades ago, a drag
sensor whose limits are swapped, a damper that does not damp, a Script function nobody calls, a LoadSensor watching
something that never loads, a link to a viewpoint that is not there, a level of detail that can never be picked, a
closed mesh wound inside out, a prototype field that the prototype does not have, an outline that crosses itself, a
knot vector the browser throws away, and a lit scene with no light.

Where they came from: the 15 planted faults that both kernels missed in blind court 3 (x3d.loom flywheel/court-3,
27 November 2026). Thirteen of the fifteen can be read from the file alone and are checked here; each check is
written for the family of the mistake, not for the one file. Two are left out on purpose because only the author's
intent tells right from wrong: which StringSensor output feeds a label, and which Viewpoint comes first. That set is
a fix confirmation for these checks, not a score.

Seven more checks at the end of the file come from no test set: mistakes of the same kind that the kernel editor
expected, written before blind court 4 was opened. A Switch with no whichChoice, a dented face declared convex, a
flat patch facing away from every viewpoint (a note), a scoped light with nothing in its scope, a clock stopped or
paused in 1970, a fully transparent shape, and a first viewpoint with everything behind it.

Each check takes the Scene element (or any ancestor of it) and returns Diagnostics.
"""
from __future__ import annotations

import difflib
import math
import re

from validation.data_checks import _defs, _diag, _ints, _nums, _sem, _tag, _urls
from validation.inert_checks import _field, _has_is, _into, _level, _outside, _route_els, _routes

_BOOL = {"true": True, "false": False}
_EPOCH_RECENT = 1.5e9          # 14 July 2017 as X3D time (seconds since 1970); a literal startTime below it is long past


def _is_meta(el) -> bool:
    return _tag(el).startswith("Metadata")


def _kids(el) -> list:
    return [c for c in el if isinstance(c.tag, str) and not _is_meta(c) and _tag(c) not in ("IS", "field", "fieldValue")]


def _near(name: str, pool) -> str:
    pool = [p for p in pool if p]
    low = {p.lower(): p for p in pool}
    if name.lower() in low and low[name.lower()] != name:
        return f" Did you mean {low[name.lower()]!r}? Names are case sensitive."
    hit = difflib.get_close_matches(name, pool, n=1, cutoff=0.6)
    return f" Did you mean {hit[0]!r}?" if hit else ""


# ------------------------------------------------------------------ (1) a clock whose only cycle is long over
def check_clock_ran_in_the_past(scene) -> list:
    """startTime is an absolute time in seconds since 1970 (19775-1 8.2.4). A TimeSensor with loop false and a
    small literal startTime played its one cycle decades ago: when the file loads it is already over, and what the
    clock drives stays still. (startTime 0 is the neighbouring case, reported as timesensor-never-starts.)"""
    out, routes = [], _routes(scene)
    lvl = _level(scene)
    for el in scene.iter():
        if _tag(el) != "TimeSensor" or el.get("USE") or _has_is(el):
            continue
        if (el.get("loop") or "false").strip().lower() == "true" or (el.get("enabled") or "true").strip().lower() == "false":
            continue
        st, ci = _nums(el.get("startTime")), _nums(el.get("cycleInterval"))
        st, ci = (st[0] if st else 0.0), (ci[0] if ci else 1.0)
        if st <= 0 or st + ci >= _EPOCH_RECENT:
            continue
        name = el.get("DEF")
        if _into(routes, name, {"startTime", "enabled", "loop", "stopTime", "cycleInterval"}):
            continue
        if not any(f == name and ff in {"fraction", "time", "cycleTime", "isActive", "elapsedTime"} for f, ff, _, _ in routes):
            continue
        out.append(_diag(lvl, "timesensor-start-in-past", el,
                         f"has loop false and startTime {st:g}, which is {st:g} seconds after 1 January 1970, so its one "
                         f"cycle of {ci:g} s ended long before this file loads and what it drives never moves. Set loop='true' "
                         f"to repeat, or ROUTE a time (for example a TouchSensor touchTime) to set_startTime."))
    return out


# ------------------------------------------------------------------ (2) drag limits swapped
def check_drag_limits(scene) -> list:
    """PlaneSensor clamps a drag between minPosition and maxPosition, and CylinderSensor between minAngle and
    maxAngle, but only where the minimum is not above the maximum: with min above max that axis is not clamped at
    all (19775-1 20.4.2, 20.4.1). The default maximum of -1 uses that rule on purpose, so a maximum of -1 is left
    alone; any other maximum below its minimum is read as a swap."""
    out = []
    for el in scene.iter():
        t = _tag(el)
        if el.get("USE") or _has_is(el):
            continue
        if t == "PlaneSensor" and el.get("maxPosition") is not None:
            lo, hi = _nums(el.get("minPosition") or "0 0"), _nums(el.get("maxPosition"))
            if not lo or not hi or len(lo) != 2 or len(hi) != 2:
                continue
            for i, axis in enumerate("xy"):
                if lo[i] > hi[i] and hi[i] != -1:
                    out.append(_diag("warning", "drag-limits-swapped", el,
                                     f"has minPosition {axis}={lo[i]:g} above maxPosition {axis}={hi[i]:g}. A minimum above the "
                                     f"maximum switches the limit off, so the drag is not clamped along {axis}. Swap the two "
                                     f"values to clamp between {hi[i]:g} and {lo[i]:g}; write a maximum of -1 if no limit is the intent."))
        elif t == "CylinderSensor" and el.get("maxAngle") is not None:
            lo, hi = _nums(el.get("minAngle") or "0"), _nums(el.get("maxAngle"))
            if not lo or not hi:
                continue
            if lo[0] > hi[0] and hi[0] != -1:
                out.append(_diag("warning", "drag-limits-swapped", el,
                                 f"has minAngle {lo[0]:g} above maxAngle {hi[0]:g}. A minimum above the maximum switches the "
                                 f"limit off, so the turn is not clamped. Swap the two values; write a maximum of -1 if no limit is the intent."))
    return out


# ------------------------------------------------------------------ (3) a follower that does not follow smoothly
def check_follower_passes_through(scene) -> list:
    """A damper with tau 0 or order 0 copies its input to its output at once, and a chaser with duration 0 arrives
    in no time (19775-1 39.3.2, 39.3.3): the node is there to smooth a change and smooths nothing."""
    out = []
    for el in scene.iter():
        t = _tag(el)
        if el.get("USE") or _has_is(el):
            continue
        if t.endswith("Damper"):
            tau, order = _nums(el.get("tau")), _ints(el.get("order"))
            if tau and tau[0] == 0:
                out.append(_diag("warning", "follower-no-smoothing", el,
                                 "has tau 0, so its output jumps to each new destination at once and nothing is smoothed. "
                                 "Give tau the time in seconds the change should take to settle, for example 0.3."))
            elif order and order[0] == 0:
                out.append(_diag("warning", "follower-no-smoothing", el,
                                 "has order 0, which switches the filter off, so its output jumps to each new destination. "
                                 "Use an order from 1 to 5 (the default is 3)."))
        elif t.endswith("Chaser"):
            d = _nums(el.get("duration"))
            if d and d[0] == 0:
                out.append(_diag("warning", "follower-no-smoothing", el,
                                 "has duration 0, so its output jumps to each new destination at once. Give duration the "
                                 "time in seconds the chase should take, for example 1."))
    return out


# ------------------------------------------------------------------ (4) a Script input nobody handles
_FUNC = (r"\bfunction\s*\*?\s+{n}\s*\(", r"\b{n}\s*=\s*(?:async\s+)?(?:function\b|\(|[A-Za-z_$][\w$]*\s*=>)",
         r"\b{n}\s*:\s*(?:async\s+)?function\b", r"^\s*(?:async\s+)?{n}\s*\([^)]*\)\s*\{{")
_ANY_FUNC = re.compile(r"\bfunction\s*\*?\s+([A-Za-z_$][\w$]*)\s*\(")


def _script_source(el) -> str:
    parts = [el.text or ""] + [(c.tail or "") for c in el]
    url = el.get("url") or ""
    if re.search(r"(?:ecmascript|javascript)\s*:", url):
        parts.append(url)
    return "\n".join(parts)


def check_script_handlers(scene) -> list:
    """An event sent to an inputOnly field of a Script runs the function of the same name (19777-1 7.5). When the
    script text has no function of that name the event arrives and nothing runs, so the outputs never change."""
    out, routes = [], _routes(scene)
    for el in scene.iter():
        if _tag(el) != "Script" or el.get("USE"):
            continue
        src = _script_source(el)
        if not re.search(r"(?:ecmascript|javascript)\s*:", src) or not re.search(r"\bfunction\b|=>", src):
            continue                                         # code in a file we cannot read
        name = el.get("DEF")
        catch_all = bool(re.search(r"\bfunction\s+(?:eventsProcessed|prepareEvents)\s*\(", src))
        defined = _ANY_FUNC.findall(src)
        for f in el:
            if _tag(f) != "field" or f.get("accessType") != "inputOnly" or not f.get("name"):
                continue
            fname = f.get("name")
            if not any(t == name and tf == _field(fname) for _, _, t, tf in routes if name):
                continue                                     # nothing routed in: a page may still call it
            if any(re.search(p.format(n=re.escape(fname)), src, re.M) for p in _FUNC):
                continue
            out.append(_diag("info" if catch_all else "warning", "script-handler-missing", el,
                             f"receives events on its inputOnly field {fname!r}, but its script has no function named "
                             f"{fname!r}, so nothing runs when the event arrives and its outputs never change. "
                             f"Name the function after the field."
                             + _near(fname, [d for d in defined if d not in ("initialize", "shutdown")])
                             + (f" The script defines: {', '.join(sorted(set(defined))[:6])}." if defined else "")))
    return out


# ------------------------------------------------------------------ (5) loading that never happens
def check_never_loads(scene) -> list:
    """A node with load='false' is not fetched until something sets load to true (19775-1 9.3.2). A LoadSensor
    watching such a node never reports isLoaded, and an Inline left that way never shows its content. A LoadSensor
    with nothing to watch never reports anything either."""
    out, defs, routes = [], _defs(scene), _routes(scene)
    lvl = _level(scene)
    watched = set()
    for el in scene.iter():
        if _tag(el) != "LoadSensor" or el.get("USE") or _has_is(el):
            continue
        name = el.get("DEF")
        routed_on = bool(name) and any(f == name for f, _, _, _ in routes)
        kids = _kids(el)
        if not kids and routed_on:
            out.append(_diag(lvl, "loadsensor-watches-nothing", el,
                             "has no child to watch, so isLoaded, isActive, progress and loadTime never fire and what they "
                             "are routed to never happens. Put the Inline, texture or sound it should watch inside it."))
        for c in kids:
            target = defs.get(c.get("USE")) if c.get("USE") else c
            if target is None:
                continue
            watched.add(target)
            if (target.get("load") or "true").strip().lower() == "false" and not _into(routes, target.get("DEF"), {"load"}):
                out.append(_diag(lvl, "loadsensor-watches-unloaded", el,
                                 f"watches {_tag(target)}{' ' + repr(target.get('DEF')) if target.get('DEF') else ''}, which has "
                                 f"load='false' and nothing in this file ROUTEs a true to its load field, so it is never fetched "
                                 f"and isLoaded never fires. Remove load='false' or ROUTE a true to set_load."))
    for el in scene.iter():
        if _tag(el) != "Inline" or el.get("USE") or el in watched or _has_is(el):
            continue
        if (el.get("load") or "true").strip().lower() == "false" and not _into(routes, el.get("DEF"), {"load"}):
            out.append(_diag("info", "inline-never-loads", el,
                             "has load='false' and nothing in this file ROUTEs a true to its load field, so its content is "
                             "never fetched or shown unless a page script loads it."))
    return out


# ------------------------------------------------------------------ (6) a link to a viewpoint that is not there
_VIEWPOINTS = {"Viewpoint", "OrthoViewpoint", "GeoViewpoint", "ViewpointGroup"}


def check_anchor_viewpoint(scene) -> list:
    """An Anchor url of the form "#Name" binds the Viewpoint with DEF Name in this scene (19775-1 9.4.1). When no
    node has that name the click does nothing. DEF names are case sensitive."""
    out, defs = [], _defs(scene)
    lvl = _level(scene)
    views = [n for n, e in defs.items() if _tag(e) in _VIEWPOINTS]
    for el in scene.iter():
        if _tag(el) != "Anchor" or el.get("USE") or _has_is(el) or not el.get("url"):
            continue
        urls = _urls(el.get("url"))
        frags = [u for u in urls if u.startswith("#") and len(u) > 1]
        if not frags or len(frags) != len(urls):
            continue                                         # another url may still work
        missing = [u[1:] for u in frags if u[1:] not in defs]
        wrong = [u[1:] for u in frags if u[1:] in defs and _tag(defs[u[1:]]) not in _VIEWPOINTS | {"ProtoInstance"}]
        if len(missing) == len(frags):
            m = missing[0]
            out.append(_diag(lvl, "anchor-viewpoint-missing", el,
                             f"links to \"#{m}\", but no node in this file has DEF {m!r}, so clicking it does nothing."
                             + _near(m, views or list(defs))))
        elif wrong and len(wrong) + len(missing) == len(frags):
            w = wrong[0]
            out.append(_diag(lvl, "anchor-target-not-viewpoint", el,
                             f"links to \"#{w}\", but DEF {w!r} is a {_tag(defs[w])}, not a Viewpoint, so there is no view to "
                             f"bind and clicking it does nothing." + (f" Viewpoints here: {', '.join(views[:5])}." if views else "")))
    return out


# ------------------------------------------------------------------ (7) LOD levels that can never be picked
def check_lod_levels(scene) -> list:
    """LOD shows child 0 nearer than range[0], child i between range[i-1] and range[i], and the last child beyond
    the last range: n ranges serve n+1 children (19775-1 23.4.3). A first range of 0 means the first child is never
    shown; more ranges than that repeat the last child; fewer leave children that are never shown."""
    out = []
    for el in scene.iter():
        if _tag(el) != "LOD" or el.get("USE") or _has_is(el):
            continue
        r = _nums(el.get("range"))
        n = len(_kids(el))
        if not r or n == 0:
            continue                                         # no range: the browser chooses the level
        if r[0] <= 0 and n > 1:
            out.append(_diag("warning", "lod-level-never-shown", el,
                             f"has range starting at {r[0]:g}. The first child is shown only nearer than the first range, and "
                             f"nothing is nearer than {r[0]:g}, so the most detailed child is never shown. Ranges are the "
                             f"distances where one child hands over to the next: {n} children need {n - 1}, none of them 0."))
        if len(r) > n - 1:
            out.append(_diag("warning", "lod-range-count", el,
                             f"has {len(r)} range value(s) for {n} child(ren). {n} children need {n - 1} range value(s); beyond "
                             f"the last child the browser repeats it, so the extra distances change nothing."))
        elif len(r) < n - 1:
            out.append(_diag("warning", "lod-level-never-shown", el,
                             f"has {len(r)} range value(s) for {n} children, so only the first {len(r) + 1} can ever be chosen and "
                             f"the last {n - len(r) - 1} are never shown. Give {n - 1} range values or remove the unused children."))
    return out


# ------------------------------------------------------------------ (8) a closed mesh wound inside out
def _welded(faces, pts):
    key, canon = {}, []
    for p in pts:
        k = (round(p[0], 9), round(p[1], 9), round(p[2], 9))
        canon.append(key.setdefault(k, len(key)))
    return [[canon[i] for i in f] for f in faces]


def _world(el):
    """World matrix of an element from its static Transform ancestors, and whether the chain mirrors (negative scale)."""
    from validation.data_checks import _ident, _mat_mul, _transform_matrix
    chain, e = [], el.getparent()
    while e is not None:
        if _tag(e) == "Transform":
            chain.append(e)
        e = e.getparent()
    m, mirrored = _ident(), False
    for t in reversed(chain):
        m = _mat_mul(m, _transform_matrix(t)[0])
        sc = _nums(t.get("scale")) or [1, 1, 1]
        if len(sc) == 3 and sc[0] * sc[1] * sc[2] < 0:
            mirrored = not mirrored
    return m, mirrored


def _apply(m, p):
    return tuple(m[i][0] * p[0] + m[i][1] * p[1] + m[i][2] * p[2] + m[i][3] for i in range(3))


def check_inside_out(scene) -> list:
    """With solid true (the default) a browser draws only the faces whose winding faces the viewer, as ccw says
    (19775-1 11.2.3). A closed mesh whose faces all wind towards its inside is drawn as its far inner walls: from
    outside the near faces are missing. Judged only where the mesh is closed and wound consistently, so the sign of
    its volume is certain. Left alone: a mesh under a mirroring Transform, and a room, that is a mesh with a
    Viewpoint inside it (inward faces are then what the viewer should see)."""
    from validation.usd_checks import _SURFACES, _mesh
    out, defs = [], _defs(scene)
    for g in scene.iter():
        if _tag(g) not in _SURFACES or g.get("USE") or _has_is(g):
            continue
        if (g.get("solid") or "true").strip().lower() == "false":
            continue
        m = _mesh(g, defs)
        if not m:
            continue
        faces, pts = m
        wf = [f for f in _welded(faces, pts) if len(set(f)) >= 3]
        if len(wf) < 4:
            continue
        directed = {}
        for f in wf:
            for a, b in zip(f, f[1:] + f[:1]):
                directed[(a, b)] = directed.get((a, b), 0) + 1
        if any(n != 1 for n in directed.values()) or any((b, a) not in directed for a, b in directed):
            continue                                         # open, non-manifold or mixed winding: no certain inside
        vol = 0.0
        for f in faces:
            if len(f) < 3:
                continue
            nx = ny = nz = 0.0
            for a, b in zip(f, f[1:] + f[:1]):
                (x1, y1, z1), (x2, y2, z2) = pts[a], pts[b]
                nx += (y1 - y2) * (z1 + z2); ny += (z1 - z2) * (x1 + x2); nz += (x1 - x2) * (y1 + y2)
            p = pts[f[0]]
            vol += (p[0] * nx + p[1] * ny + p[2] * nz) / 6.0
        ccw = (g.get("ccw") or "true").strip().lower() != "false"
        span = max(max(p[k] for p in pts) - min(p[k] for p in pts) for k in range(3))
        if span <= 0 or abs(vol) < 1e-9 * span ** 3:
            continue
        if (vol < 0) == ccw:
            m, mirrored = _world(g)
            if mirrored:
                continue
            corners = [_apply(m, p) for p in pts]
            lo = [min(c[k] for c in corners) for k in range(3)]
            hi = [max(c[k] for c in corners) for k in range(3)]
            eyes = []
            for vp in scene.iter():
                if _tag(vp) in ("Viewpoint", "OrthoViewpoint") and not vp.get("USE"):
                    pos = _nums(vp.get("position")) or [0.0, 0.0, 10.0]
                    if len(pos) == 3:
                        eyes.append(_apply(_world(vp)[0], pos))
            if any(all(lo[k] <= e[k] <= hi[k] for k in range(3)) for e in eyes or [(0.0, 0.0, 10.0)]):
                continue                                     # a room seen from inside
            out.append(_diag("warning", "mesh-inside-out", g,
                             f"is a closed mesh whose faces wind {'clockwise' if ccw else 'counter-clockwise'} seen from outside while "
                             f"ccw is {'true' if ccw else 'false'}, and solid is true, so the browser culls the faces towards the viewer "
                             f"and draws the far inside walls. Set ccw='{'false' if ccw else 'true'}' (or reverse the index order of every face)."))
    return out


# ------------------------------------------------------------------ (9) a prototype field the prototype does not have
def check_proto_field_values(scene) -> list:
    """A ProtoInstance may set only the fields its ProtoDeclare or ExternProtoDeclare declares (19775-1 4.4.4.2).
    A fieldValue with any other name is dropped, and the field it meant keeps its default."""
    out = []
    protos = _sem()._proto_interfaces(scene)
    for el in scene.iter():
        if _tag(el) != "ProtoInstance" or el.get("USE"):
            continue
        iface = protos.get(el.get("name"))
        if not iface:
            continue                                         # undeclared prototype (reported elsewhere) or no interface listed
        for fv in el:
            if _tag(fv) != "fieldValue" or not fv.get("name") or fv.get("name") in iface:
                continue
            out.append(_diag("error", "proto-field-unknown", el,
                             f"sets fieldValue {fv.get('name')!r}, but prototype {el.get('name')!r} declares no such field "
                             f"(it declares: {', '.join(sorted(iface)[:8])}). The value is dropped and the default stays."
                             + _near(fv.get("name"), list(iface))))
    return out


# ------------------------------------------------------------------ (10) an outline that crosses itself
def _crosses(p, q, r, s) -> bool:
    def side(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return (v > 1e-12) - (v < -1e-12)
    return side(p, q, r) * side(p, q, s) < 0 and side(r, s, p) * side(r, s, q) < 0


def check_cross_section(scene) -> list:
    """An Extrusion sweeps its crossSection outline along the spine (19775-1 13.3.5). An outline whose edges cross
    (points listed out of order: a bow-tie for a square) makes walls that pass through each other and caps that
    fill wrongly."""
    out = []
    for el in scene.iter():
        if _tag(el) != "Extrusion" or el.get("USE") or _has_is(el):
            continue
        v = _nums(el.get("crossSection"))
        if not v or len(v) % 2 or not 8 <= len(v) <= 4000:
            continue
        p = [(v[i], v[i + 1]) for i in range(0, len(v), 2)]
        segs = [(p[i], p[i + 1]) for i in range(len(p) - 1) if p[i] != p[i + 1]]
        hit = next(((i, j) for i in range(len(segs)) for j in range(i + 2, len(segs))
                    if _crosses(*segs[i], *segs[j])), None)
        if hit:
            i, j = hit
            out.append(_diag("warning", "extrusion-cross-section-crosses", el,
                             f"has a crossSection whose edge {i} ({segs[i][0][0]:g} {segs[i][0][1]:g} to {segs[i][1][0]:g} {segs[i][1][1]:g}) "
                             f"crosses edge {j} ({segs[j][0][0]:g} {segs[j][0][1]:g} to {segs[j][1][0]:g} {segs[j][1][1]:g}), so the "
                             f"outline is a bow-tie, the walls pass through each other and the caps fill wrongly. List the points in "
                             f"order around the outline."))
    return out


# ------------------------------------------------------------------ (11) NURBS knots the browser throws away
_NURBS_1D = {"NurbsCurve": 3, "NurbsPositionInterpolator": 3, "NurbsOrientationInterpolator": 3, "NurbsCurve2D": 2}
_NURBS_2D = ("NurbsPatchSurface", "NurbsTrimmedSurface", "NurbsSurfaceInterpolator", "NurbsTextureCoordinate")


def _control_count(el, defs, width):
    if _tag(el) in ("NurbsCurve2D", "NurbsTextureCoordinate"):
        v = _nums(el.get("controlPoint"))
        return len(v) // 2 if v else None
    for c in el:
        if _tag(c) in ("Coordinate", "CoordinateDouble"):
            c = defs.get(c.get("USE")) if c.get("USE") else c
            v = _nums(c.get("point")) if c is not None else None
            return len(v) // width if v else None
    return None


def _knot_findings(el, label, knot, n, order):
    out = []
    if order < 2:
        out.append(_diag("error", "nurbs-order", el, f"has {label}order {order}; the order must be 2 or more."))
        return out
    if n is not None and n < order:
        out.append(_diag("error", "nurbs-too-few-control-points", el,
                         f"has {n} control point(s) along {label or 'the curve'} for order {order}; it needs at least {order}, "
                         f"so nothing is drawn."))
        return out
    if knot and n is not None:
        if len(knot) != n + order:
            out.append(_diag("warning", "nurbs-knot-count", el,
                             f"has {len(knot)} {label}knot value(s), but {n} control point(s) of order {order} need {n + order} "
                             f"(dimension plus order). A knot vector of the wrong length is ignored and a uniform one is used, so the "
                             f"shape is not the one these knots describe."))
        elif any(b < a for a, b in zip(knot, knot[1:])):
            out.append(_diag("warning", "nurbs-knot-order", el,
                             f"has a {label}knot vector that goes down ({' '.join(f'{k:g}' for k in knot[:8])}...). Knots must not "
                             f"decrease; a bad knot vector is ignored and a uniform one is used."))
    return out


def check_nurbs_knots(scene) -> list:
    """A NURBS knot vector has exactly dimension plus order values, never decreasing; otherwise the browser
    discards it and uses a uniform one (19775-1 27.4.2, 27.4.4). A curve or surface needs at least `order` control
    points each way, and a surface exactly uDimension times vDimension of them."""
    out, defs = [], _defs(scene)
    for el in scene.iter():
        t = _tag(el)
        if el.get("USE") or _has_is(el):
            continue
        if t in _NURBS_1D:
            order = (_ints(el.get("order")) or [3])[0]
            out += _knot_findings(el, "", _nums(el.get("knot")), _control_count(el, defs, _NURBS_1D[t]), order)
        elif t in _NURBS_2D:
            dims = {}
            for a in "uv":
                d = _ints(el.get(a + "Dimension"))
                dims[a] = d[0] if d else 0
                order = (_ints(el.get(a + "Order")) or [3])[0]
                if dims[a] > 0:
                    out += _knot_findings(el, a, _nums(el.get(a + "Knot")), dims[a], order)
            n = _control_count(el, defs, 3)
            if n is not None and dims["u"] > 0 and dims["v"] > 0 and n != dims["u"] * dims["v"]:
                out.append(_diag("error", "nurbs-control-count", el,
                                 f"has {n} control point(s), but uDimension {dims['u']} times vDimension {dims['v']} needs "
                                 f"{dims['u'] * dims['v']}."))
    return out


# ------------------------------------------------------------------ (12) a lit scene with no light
_LIGHTS = {"DirectionalLight", "PointLight", "SpotLight", "EnvironmentLight"}
_UNKNOWN = {"Inline", "ProtoInstance", "ExternProtoDeclare", "Script", "IMPORT"}


def _lit(shape, defs) -> bool:
    app = next((c for c in shape if _tag(c) == "Appearance"), None)
    app = defs.get(app.get("USE")) if app is not None and app.get("USE") else app
    if app is None or any(_tag(c).endswith("Shader") or (c.get("containerField") or "") == "shaders" for c in app):
        return False                                         # no appearance: drawn unlit; a shader: drawn by its own code
    mat = next((c for c in app if _tag(c) in ("Material", "PhysicalMaterial", "TwoSidedMaterial")), None)
    mat = defs.get(mat.get("USE")) if mat is not None and mat.get("USE") else mat
    if mat is None or _has_is(mat):
        return False                                         # no material or UnlitMaterial: drawn unlit
    e = _nums(mat.get("emissiveColor"))
    if e and any(x > 0 for x in e):
        return False
    return not any((c.get("containerField") or "") == "emissiveTexture" for c in mat)


def check_unlit_scene(scene) -> list:
    """NavigationInfo headlight='false' switches off the light that travels with the viewer (19775-1 23.4.4). With
    no other light in the scene, every Shape with a Material is drawn black. Judged only when the whole scene is in
    this file and nothing ROUTEs to the headlight or to a light."""
    if any(_tag(e) in _UNKNOWN for e in scene.iter()):
        return []
    nav = next((e for e in scene.iter() if _tag(e) == "NavigationInfo" and not e.get("USE")), None)
    if nav is None or (nav.get("headlight") or "true").strip().lower() != "false" or _has_is(nav):
        return []
    defs, routes = _defs(scene), _routes(scene)
    lights = [e for e in scene.iter() if _tag(e) in _LIGHTS]
    if _into(routes, nav.get("DEF"), {"headlight", "bind"}) or any(_into(routes, e.get("DEF"), {"on", "intensity"}) for e in lights):
        return []
    if any((e.get("on") or "true").strip().lower() != "false" for e in lights):
        return []
    if sum(1 for e in scene.iter() if _tag(e) == "NavigationInfo") > 1:
        return []                                            # another NavigationInfo may be bound later
    lit = [s for s in scene.iter() if _tag(s) == "Shape" and not s.get("USE") and _lit(s, defs)]
    if not lit:
        return []
    why = "every light has on='false'" if lights else "there is no light node"
    return [_diag("warning", "scene-unlit", nav,
                  f"has headlight='false' and {why}, so the {len(lit)} Shape(s) with a Material are drawn black. Add a "
                  f"DirectionalLight (or PointLight), set headlight='true', or give the Material an emissiveColor.")]


# ------------------------------------------------------------------ (13) a toggle that starts out of step
def check_toggle_out_of_step(scene) -> list:
    """A BooleanToggle flips its own stored value and sends it on (19775-1 30.4.2). When the field it drives starts
    in the other state (the toggle starts false, the clock it switches starts enabled), the first click sends the
    value the field already has: nothing visible happens, and the thing was running before anyone clicked."""
    out, defs = [], _defs(scene)
    lvl = _level(scene)
    nodes = _sem().get_x3duom().get_concrete_nodes()
    for r in _route_els(scene):
        src, dst = defs.get(r.get("fromNode")), defs.get(r.get("toNode"))
        if src is None or dst is None or _tag(src) != "BooleanToggle" or _field(r.get("fromField")) != "toggle":
            continue
        if _has_is(src) or _has_is(dst):
            continue
        f = _field(r.get("toField"))
        spec = next((x for x in nodes.get(_tag(dst), {}).get("fields", []) if x["name"] == f and x["type"] == "SFBool"), None)
        if spec is None or spec.get("accessType") != "inputOutput":
            continue
        cur = _BOOL.get((dst.get(f) or spec.get("default") or "").strip().lower())
        tog = _BOOL.get((src.get("toggle") or "false").strip().lower())
        if cur is None or tog is None or cur == tog:
            continue
        if sum(1 for x in _route_els(scene) if x.get("toNode") == r.get("toNode") and _field(x.get("toField")) == f) > 1:
            continue                                         # something else also sets it
        out.append(_diag(lvl, "toggle-out-of-step", r,
                         f"sends BooleanToggle {r.get('fromNode')!r} (which starts {str(tog).lower()}) to {_tag(dst)} "
                         f"{r.get('toNode')!r} {f}, which starts {str(cur).lower()}. The first toggle sends {str(not tog).lower()}, the "
                         f"state {f} is already in, so {r.get('toNode')!r} is {'on' if cur else 'off'} before anyone clicks and the "
                         f"first click appears to do nothing. Start them in step: {f}='{str(tog).lower()}' on {r.get('toNode')!r}, "
                         f"or toggle='{str(cur).lower()}' on {r.get('fromNode')!r}."))
    return out


# ====================================================================== from the editor's own list
# The checks below were not written from any test set. They are mistakes of the same kind (legal, plays or draws
# wrong) that the kernel editor expected a hurried author to make, added before blind court 4 was opened.

_SHAPE_SOURCES = ("IndexedFaceSet", "IndexedTriangleSet", "IndexedQuadSet")


def _moving(scene) -> set:
    return {r.get("toNode") for r in _route_els(scene)}


def _static_chain(el, moving) -> bool:
    e = el
    while e is not None:
        if e.get("DEF") in moving or _tag(e) in ("Switch", "LOD", "Billboard", "ProtoBody", "ProtoDeclare", "Collision", "HAnimHumanoid"):
            return False
        e = e.getparent()
    return True


def _eyes(scene, moving):
    """World positions of the static Viewpoints (the default one at 0 0 10 when the scene has none), or None when
    a viewpoint moves or sits where its place cannot be worked out."""
    eyes = []
    for vp in scene.iter():
        if _tag(vp) in ("Viewpoint", "OrthoViewpoint") and not vp.get("USE"):
            if not _static_chain(vp, moving) or _has_is(vp):
                return None
            pos = _nums(vp.get("position")) or [0.0, 0.0, 10.0]
            if len(pos) != 3:
                return None
            eyes.append(_apply(_world(vp)[0], pos))
        elif _tag(vp) in ("GeoViewpoint", "ViewpointGroup"):
            return None
    return eyes or [(0.0, 0.0, 10.0)]


# ------------------------------------------------------------------ (14) a Switch that shows nothing, for good
def check_switch_shows_nothing(scene) -> list:
    """Switch.whichChoice defaults to -1, which shows no child (19775-1 10.4.2). A Switch with children, no
    whichChoice written and nothing routed to whichChoice never shows anything. Left alone: a written
    whichChoice='-1' (a statement, often flipped by a page script) and a Switch used as a shelf, whose children
    are DEF'd there and USEd elsewhere."""
    out, routes = [], _routes(scene)
    lvl = _level(scene)
    uses = {e.get("USE") for e in scene.iter() if isinstance(e.tag, str) and e.get("USE")}
    for sw in scene.iter():
        if _tag(sw) != "Switch" or sw.get("USE") or _has_is(sw) or not _kids(sw):
            continue
        if sw.get("whichChoice") is not None or _into(routes, sw.get("DEF"), {"whichChoice"}):
            continue                                         # a written -1 is a statement (a page script may flip it)
        if any(d.get("DEF") in uses for d in sw.iter() if d is not sw and isinstance(d.tag, str) and d.get("DEF")):
            continue
        if any("placeholder" in (c.get("DEF") or "").lower() for c in sw):
            continue
        out.append(_diag(lvl, "switch-shows-nothing", sw,
                         f"has no whichChoice, and the default is -1, which shows none of its {len(_kids(sw))} child(ren), and nothing in this file ROUTEs to "
                         f"whichChoice or USEs a child elsewhere, so they are never seen. Set whichChoice='0' to show the first."))
    return out


# ------------------------------------------------------------------ (15) a dented face declared convex
def check_concave_faces(scene) -> list:
    """convex defaults to true, a promise that every face is convex that lets the browser fill each face as a fan
    (19775-1 13.3.6). A face with a dent (an L, an arrow, a star) then fills across the dent."""
    from validation.usd_checks import _mesh
    out, defs = [], _defs(scene)
    for g in scene.iter():
        if _tag(g) != "IndexedFaceSet" or g.get("USE") or _has_is(g):
            continue
        if (g.get("convex") or "true").strip().lower() == "false":
            continue
        m = _mesh(g, defs)
        if not m:
            continue
        faces, pts = m
        bad = []
        for f in faces:
            if len(f) < 4 or len(set(f)) != len(f):
                continue
            n = [0.0, 0.0, 0.0]
            for a, b in zip(f, f[1:] + f[:1]):
                (x1, y1, z1), (x2, y2, z2) = pts[a], pts[b]
                n[0] += (y1 - y2) * (z1 + z2); n[1] += (z1 - z2) * (x1 + x2); n[2] += (x1 - x2) * (y1 + y2)
            ln = math.sqrt(sum(x * x for x in n))
            if ln < 1e-12:
                continue
            c0 = [sum(pts[i][k] for i in f) / len(f) for k in range(3)]
            size = max(math.sqrt(sum((pts[i][k] - c0[k]) ** 2 for k in range(3))) for i in f)
            if size <= 0 or any(abs(sum((pts[i][k] - c0[k]) * n[k] for k in range(3))) / ln > 1e-3 * size for i in f):
                continue                                     # a face that is not flat has no outline to judge
            turns = []
            for i in range(len(f)):
                a, b, c = pts[f[i - 1]], pts[f[i]], pts[f[(i + 1) % len(f)]]
                u, v = [b[k] - a[k] for k in range(3)], [c[k] - b[k] for k in range(3)]
                cr = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
                turns.append(sum(cr[k] * n[k] for k in range(3)) / ln)
            scale = max(abs(t) for t in turns)
            if scale > 0 and min(turns) < -1e-3 * scale and max(turns) > 1e-3 * scale:
                bad.append(f)
        if bad:
            out.append(_diag("warning", "face-concave", g,
                             f"has {len(bad)} face(s) with a dent (first: corners {' '.join(map(str, bad[0]))}) while convex is true "
                             f"(the default). The browser may fill a dented face straight across the dent. Set convex='false', or "
                             f"split the face into convex pieces."))
    return out


# ------------------------------------------------------------------ (16) a one-sided mesh seen only from behind
def check_faces_away(scene) -> list:
    """With solid true only the front of each face is drawn (19775-1 11.2.3). A flat patch (a floor, a wall, a
    sign) whose face has its back to every Viewpoint in the file is invisible from all of them: wound the wrong way
    round, or ccw set wrongly. Judged only for flat, still geometry and still viewpoints; the far panel of a body
    built from several meshes faces away by nature and is not flat-patch work, so curved or folded meshes are left."""
    from validation.usd_checks import _mesh
    out, defs, moving = [], _defs(scene), _moving(scene)
    if _outside(scene):
        return out
    eyes = _eyes(scene, moving)
    if not eyes:
        return out
    for g in scene.iter():
        if _tag(g) not in _SHAPE_SOURCES or g.get("USE") or _has_is(g):
            continue
        if (g.get("solid") or "true").strip().lower() == "false" or not _static_chain(g, moving):
            continue
        shape = g.getparent()
        if shape is None or shape.get("DEF") and _used_elsewhere_any(scene, shape.get("DEF")) or g.get("DEF") and _used_elsewhere_any(scene, g.get("DEF")):
            continue
        m = _mesh(g, defs)
        if not m:
            continue
        faces, pts = m
        w, mirrored = _world(g)
        wp = [_apply(w, p) for p in pts]
        sign = (-1.0 if mirrored else 1.0) * (1.0 if (g.get("ccw") or "true").strip().lower() != "false" else -1.0)
        seen, judged, first = False, 0, None
        for f in faces:
            n = [0.0, 0.0, 0.0]
            for a, b in zip(f, f[1:] + f[:1]):
                (x1, y1, z1), (x2, y2, z2) = wp[a], wp[b]
                n[0] += (y1 - y2) * (z1 + z2); n[1] += (z1 - z2) * (x1 + x2); n[2] += (x1 - x2) * (y1 + y2)
            if sum(x * x for x in n) < 1e-24:
                continue
            ln = math.sqrt(sum(x * x for x in n))
            unit = [x / ln for x in n]
            first = first or unit
            if sum(unit[k] * first[k] for k in range(3)) < 0.999:
                seen = True                                  # not one flat patch: the far side of a body faces away by nature
                break
            c = [sum(wp[i][k] for i in f) / len(f) for k in range(3)]
            judged += 1
            if any(sign * sum(n[k] * (e[k] - c[k]) for k in range(3)) > 0 for e in eyes):
                seen = True
                break
        if judged and not seen:
            out.append(_diag("info", "mesh-faces-away", g,      # a note: the far panel of a body built from flat pieces is legal
                             f"is a flat patch with all {judged} face(s) turned away from {'every Viewpoint' if len(eyes) > 1 else 'the Viewpoint'} in this "
                             f"file (ccw={g.get('ccw') or 'true'}, solid true), so from there it is not drawn at all. Reverse the index "
                             f"order of the faces, flip ccw, or set solid='false' to draw both sides."))
    return out


def _used_elsewhere_any(scene, name) -> bool:
    return any(e.get("USE") == name for e in scene.iter() if isinstance(e.tag, str))


# ------------------------------------------------------------------ (17) a scoped light with nothing in its scope
_SCOPES = {"Group", "Transform", "Anchor", "Billboard", "Collision", "LOD", "Switch", "StaticGroup", "CADAssembly", "CADLayer", "CADPart"}
_GLOBAL_DEFAULT = {"DirectionalLight": "false", "PointLight": "true", "SpotLight": "true"}


def check_light_scope(scene) -> list:
    """A light with global false (the default for DirectionalLight) lights only the nodes under its parent group
    (19775-1 17.2.1.2). Put in a group of its own, away from the shapes, it lights nothing."""
    out, defs = [], _defs(scene)
    routes = _routes(scene)
    for el in scene.iter():
        t = _tag(el)
        if t not in _GLOBAL_DEFAULT or el.get("USE") or _has_is(el):
            continue
        if (el.get("global") or _GLOBAL_DEFAULT[t]).strip().lower() != "false" or (el.get("on") or "true").strip().lower() == "false":
            continue
        if _into(routes, el.get("DEF"), {"global"}) or _used_elsewhere_any(scene, el.get("DEF") or "\0"):
            continue
        parent = el.getparent()
        if parent is None or _tag(parent) not in _SCOPES:
            continue                                         # in Scene: lights everything
        def has_shape(e, depth=0):
            e = defs.get(e.get("USE")) if e.get("USE") else e
            if e is None or depth > 40:
                return True
            if _tag(e) in ("Shape", "Inline", "ProtoInstance", "HAnimHumanoid", "ParticleSystem"):
                return True
            return any(has_shape(c, depth + 1) for c in e if isinstance(c.tag, str))
        if not any(has_shape(c) for c in parent if c is not el and isinstance(c.tag, str)):
            out.append(_diag("warning", "light-scope-empty", el,
                             f"has global false{'' if el.get('global') else ' (the default for ' + t + ')'}, so it lights only what "
                             f"sits under its parent {_tag(parent)}, and no Shape sits there: it lights nothing. Set global='true', "
                             f"or move the light into the group that holds the shapes."))
    return out


# ------------------------------------------------------------------ (18) a clock stopped or paused before it began
def check_clock_stopped(scene) -> list:
    """stopTime and pauseTime are absolute times too. A literal stopTime later than startTime, both long past,
    means the clock was stopped decades ago and never runs, loop or not (19775-1 8.2.4.3). A literal pauseTime
    later than resumeTime means it is paused as soon as it starts."""
    out, routes = [], _routes(scene)
    lvl = _level(scene)
    for el in scene.iter():
        if _tag(el) != "TimeSensor" or el.get("USE") or _has_is(el):
            continue
        if (el.get("enabled") or "true").strip().lower() == "false":
            continue
        name = el.get("DEF")
        if not any(f == name and ff in {"fraction", "time", "cycleTime", "isActive", "elapsedTime"} for f, ff, _, _ in routes):
            continue
        get = lambda k: (_nums(el.get(k)) or [0.0])[0]
        st, sp, pt, rt = get("startTime"), get("stopTime"), get("pauseTime"), get("resumeTime")
        if 0 < sp < _EPOCH_RECENT and sp > st and not _into(routes, name, {"startTime", "stopTime", "enabled"}):
            out.append(_diag(lvl, "timesensor-stopped-in-past", el,
                             f"has stopTime {sp:g} later than startTime {st:g}. Both are seconds after 1 January 1970, so the clock "
                             f"was stopped long before this file loads and never runs, whatever loop says. Leave stopTime at 0 to "
                             f"run, and ROUTE a time to set_stopTime when it should stop."))
        elif 0 < pt < _EPOCH_RECENT and pt > rt and not _into(routes, name, {"pauseTime", "resumeTime", "enabled"}):
            out.append(_diag(lvl, "timesensor-paused-forever", el,
                             f"has pauseTime {pt:g} later than resumeTime {rt:g}. Both are seconds after 1 January 1970, so the clock "
                             f"is paused as soon as it starts and nothing resumes it. Leave pauseTime at 0 and ROUTE a time to "
                             f"set_pauseTime when it should pause."))
    return out


# ------------------------------------------------------------------ (19) a shape nobody can see
def check_fully_transparent(scene) -> list:
    """A Material with transparency 1 draws nothing. That is a known trick for an invisible thing to click on or
    collide with, so a Shape beside a pointing sensor or in a Collision proxy is left alone; anywhere else the
    Shape is simply never seen."""
    out, defs, routes = [], _defs(scene), _routes(scene)
    lvl = _level(scene)
    for shape in scene.iter():
        if _tag(shape) != "Shape" or shape.get("USE") or (shape.get("containerField") or "") == "proxy":
            continue
        app = next((c for c in shape if _tag(c) == "Appearance"), None)
        app = defs.get(app.get("USE")) if app is not None and app.get("USE") else app
        if app is None or any(_tag(c).endswith("Shader") for c in app):
            continue
        mat = next((c for c in app if _tag(c) in ("Material", "PhysicalMaterial", "UnlitMaterial")), None)
        mat = defs.get(mat.get("USE")) if mat is not None and mat.get("USE") else mat
        if mat is None or _has_is(mat):
            continue
        t = _nums(mat.get("transparency"))
        if not t or t[0] < 1 or _into(routes, mat.get("DEF"), {"transparency"}):
            continue
        e, hit_area = shape, False
        while e is not None and not hit_area:
            parent = e.getparent()
            if parent is not None and any(_tag(c).endswith("Sensor") for c in parent):
                hit_area = True
            if _tag(e) == "Collision" or (e.get("containerField") or "") == "proxy":
                hit_area = True
            e = parent
        if hit_area or shape.get("DEF") and _into(routes, shape.get("DEF"), {"visible"}):
            continue
        out.append(_diag(lvl, "shape-fully-transparent", shape,
                         "has a Material with transparency 1 and nothing in this file ROUTEs to that transparency, so the Shape "
                         "is never seen. Lower the transparency (0 is opaque), or remove the Shape."))
    return out


# ------------------------------------------------------------------ (20) a first view of nothing
def check_view_of_nothing(scene) -> list:
    """The first Viewpoint in the file is the one bound at load (19775-1 23.3.1). When every piece of geometry lies
    behind it, the scene opens on an empty screen. Judged only when all the geometry is still and can be measured."""
    from validation.data_checks import _ident, _mat_mul, _points_under, _rot
    if _outside(scene):
        return []
    defs, moving = _defs(scene), _moving(scene)
    root = scene if _tag(scene) == "Scene" else next((e for e in scene.iter() if _tag(e) == "Scene"), None)
    vp = next((e for e in scene.iter() if _tag(e) == "Viewpoint" and not e.get("USE")), None)
    if root is None or vp is None or _has_is(vp) or not _static_chain(vp, moving) or vp.get("DEF") in moving:
        return []
    pts = _points_under(root, _ident(), False, defs, moving)
    if not pts:
        return []
    pos = _nums(vp.get("position")) or [0.0, 0.0, 10.0]
    ori = _nums(vp.get("orientation")) or [0.0, 0.0, 1.0, 0.0]
    if len(pos) != 3 or len(ori) != 4:
        return []
    w = _world(vp)[0]
    eye = _apply(w, pos)
    r = _mat_mul(w, _rot(ori))
    look = tuple(-r[i][2] for i in range(3))
    ln = math.sqrt(sum(x * x for x in look))
    if ln < 1e-9:
        return []
    if all(sum((p[k] - eye[k]) * look[k] for k in range(3)) <= 0 for p in pts):
        return [_diag("warning", "viewpoint-sees-nothing", vp,
                      f"is the first Viewpoint, so the scene opens on it, and all the geometry in this file lies behind it "
                      f"(position {' '.join(f'{x:g}' for x in pos)}, looking along {' '.join(f'{x / ln:.2g}' for x in look)}): the "
                      f"screen opens empty. Turn the orientation towards the scene or move the position.")]
    return []


PLAYS_CHECKS = [check_clock_ran_in_the_past, check_drag_limits, check_follower_passes_through, check_script_handlers,
                check_never_loads, check_anchor_viewpoint, check_lod_levels, check_inside_out, check_proto_field_values,
                check_cross_section, check_nurbs_knots, check_unlit_scene, check_toggle_out_of_step,
                check_switch_shows_nothing, check_concave_faces, check_faces_away, check_light_scope, check_clock_stopped,
                check_fully_transparent, check_view_of_nothing]
