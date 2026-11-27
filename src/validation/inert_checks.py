"""Checks for scenes that are wired legally and do nothing.

The XSD and the wiring checks accept all of these: every node is in a legal place and every ROUTE joins fields of
the right type and access. What is wrong is that no event can ever travel the wiring, or that it travels and ends
where nobody sees it. Eight checks: a pointing sensor with nothing to sense or switched off for good, a clock that
can never start, an interpolator, sequencer or trigger with nothing in or nothing out, a Switch index past its
children, plain ROUTEs that close a circle, an environment sensor of zero size, a Script with no code whose outputs
are routed on, and a looping spin that does not end where it began.

Where they came from: a blind test of 20 behaviour faults written by an author who had not read this code (x3d.loom
flywheel/sprint-20261126/fault-court-2). Nine of the twenty passed every check then in the kernel, and all nine were
of this kind. The checks were written from those misses, so that set is a fix confirmation for them, not a score.

A scene with a Script or a Proto may be driven from code these checks cannot read, and a page may drive a scene from
outside the file, so the wiring checks drop to info when the scene holds a Script, ProtoInstance, ProtoDeclare,
ExternProtoDeclare, IMPORT or EXPORT. Each check takes the Scene element (or any ancestor of it) and returns
Diagnostics.
"""
from __future__ import annotations

import math
import re

from validation.data_checks import _defs, _diag, _nums, _quat, _tag

SENSORS = {"TouchSensor", "PlaneSensor", "CylinderSensor", "SphereSensor"}
OUTSIDE = {"Script", "ProtoInstance", "ProtoDeclare", "ExternProtoDeclare", "IMPORT", "EXPORT"}
GEOMETRY = {"Shape", "Inline", "InlineLoadControl", "CADFace", "GeoLocation", "ProtoInstance",
            "Text", "Box", "Sphere", "Cone", "Cylinder", "IndexedFaceSet", "ElevationGrid", "Extrusion"}
GROUPING_SKIP = {"Scene", "X3D"}
TRIGGERS = {"BooleanToggle", "BooleanFilter", "BooleanTrigger", "IntegerTrigger", "TimeTrigger"}


def _field(f: str | None) -> str:
    f = f or ""
    if f.startswith("set_"):
        f = f[4:]
    if f.endswith("_changed"):
        f = f[:-8]
    return f


def _routes(scene):
    """(from DEF, from field, to DEF, to field) for every ROUTE, fields normalised so set_x, x and x_changed match."""
    return [(r.get("fromNode"), _field(r.get("fromField")), r.get("toNode"), _field(r.get("toField")))
            for r in scene.iter() if _tag(r) == "ROUTE"]


def _outside(scene) -> bool:
    return any(_tag(e) in OUTSIDE for e in scene.iter())


def _has_is(el) -> bool:
    return any(_tag(c) == "IS" for c in el)


def _level(scene, level="warning"):
    return "info" if _outside(scene) else level


def _into(routes, name, fields):
    return bool(name) and any(t == name and tf in fields for _, _, t, tf in routes)


def _used_elsewhere(scene, name):
    return bool(name) and any(e.get("USE") == name for e in scene.iter() if isinstance(e.tag, str))


def _holds_geometry(el, defs, seen=None) -> bool:
    seen = seen if seen is not None else set()
    if el in seen:
        return False
    seen.add(el)
    if el.get("USE"):
        el = defs.get(el.get("USE"))
        if el is None:
            return True                       # unresolved USE: do not guess
    t = _tag(el)
    if t in GEOMETRY:
        return True
    return any(_holds_geometry(c, defs, seen) for c in el if isinstance(c.tag, str))


def check_sensor_senses_nothing(scene) -> list:
    """A pointing-device sensor senses the geometry of its parent's other children and their descendants
    (19777-1 / 19775-1 20.2.1). With no geometry there, nobody can click or drag it. An enabled='false' sensor
    that nothing in the file ROUTEs an enabled value to never reacts either."""
    out, defs, routes = [], _defs(scene), _routes(scene)
    lvl = _level(scene)
    for el in scene.iter():
        if _tag(el) not in SENSORS or el.get("USE") or _has_is(el):
            continue
        parent = el.getparent()
        if parent is None or _tag(parent) in {"IS", "field", "fieldValue", "ProtoBody", "ProtoInterface"}:
            continue
        name = el.get("DEF")
        if not _used_elsewhere(scene, name):
            sibs = [c for c in parent if c is not el and isinstance(c.tag, str)]
            if not any(_holds_geometry(c, defs) for c in sibs):
                where = "directly in Scene" if _tag(parent) == "Scene" else f"in its own {_tag(parent)}"
                out.append(_diag(lvl, "sensor-senses-nothing", el,
                                 f"sits {where} with no Shape beside it or under its siblings, so there is nothing to click or drag "
                                 f"and it never fires. Put the sensor in the same group as the Shape it should react to."))
                continue
        if (el.get("enabled") or "").strip().lower() == "false" and not _into(routes, name, {"enabled"}):
            out.append(_diag(lvl, "sensor-disabled-never-enabled", el,
                             "is set enabled='false' and nothing in this file ROUTEs a value to its enabled field, so it ignores "
                             "the pointer and any ROUTEs it starts stay silent. Remove enabled='false' or ROUTE a true to set_enabled."))
    return out


def check_clock_never_starts(scene) -> list:
    """A TimeSensor with loop false and startTime 0 never runs unless something sets startTime, enabled or loop.
    Its outputs then never fire, so the animation it feeds stays still."""
    out, routes = [], _routes(scene)
    lvl = _level(scene)
    for el in scene.iter():
        if _tag(el) != "TimeSensor" or el.get("USE") or _has_is(el):
            continue
        name = el.get("DEF")
        loop = (el.get("loop") or "false").strip().lower() == "true"
        enabled = (el.get("enabled") or "true").strip().lower() != "false"
        st = _nums(el.get("startTime"))
        sp = _nums(el.get("stopTime"))
        st = st[0] if st else 0.0
        sp = sp[0] if sp else 0.0
        fed = _into(routes, name, {"startTime", "enabled", "loop"})
        used = any(f == name and ff in {"fraction", "time"} for f, ff, _, _ in routes)
        if used and not fed:
            if not enabled:
                out.append(_diag("info", "timesensor-never-starts", el,       # a deliberate switch-off, usually flipped by page script
                                 "is set enabled='false' and nothing in this file ROUTEs to its enabled field, so it never runs "
                                 "and what it drives stays still. Remove enabled='false' or ROUTE a true to set_enabled."))
            elif not loop and st <= 0:
                out.append(_diag(lvl, "timesensor-never-starts", el,
                                 "has loop false and startTime 0, and nothing in this file ROUTEs to its startTime, enabled or loop, "
                                 "so it never starts and what it drives stays still. Set loop='true', or ROUTE a time such as a "
                                 "TouchSensor touchTime to set_startTime."))
        if sp > 0 and sp <= st and not _into(routes, name, {"stopTime", "startTime"}):
            out.append(_diag("info", "timesensor-stop-ignored", el,
                             f"has stopTime {sp:g} not later than startTime {st:g}; a stopTime at or before startTime is ignored, "
                             f"so the clock is not stopped when you may expect. Set stopTime later than startTime, or 0."))
    return out


def _is_flow(tag: str) -> bool:
    return tag.endswith("Interpolator") or tag.endswith("Sequencer") or tag in TRIGGERS


def check_output_goes_nowhere(scene) -> list:
    """An interpolator, sequencer or trigger node that is fed by a ROUTE but whose output is routed nowhere does
    its sum and the viewer sees nothing; one that sends output but is fed by nothing never changes."""
    out, defs, routes = [], _defs(scene), _routes(scene)
    lvl = _level(scene)
    for el in scene.iter():
        t = _tag(el)
        if not _is_flow(t) or el.get("USE") or _has_is(el) or not el.get("DEF"):
            continue
        name = el.get("DEF")
        ins = [r for r in routes if r[2] == name]
        outs = [r for r in routes if r[0] == name]
        if ins and not outs:
            out.append(_diag(lvl, "output-goes-nowhere", el,
                             "receives a ROUTE but nothing in this file ROUTEs its output anywhere, so it changes nothing the "
                             "viewer can see. ROUTE its output (value_changed, or the trigger output) to a node field."))
        elif outs and not ins:
            out.append(_diag(lvl, "input-from-nowhere", el,
                             "sends events but nothing in this file ROUTEs anything into it, so it never produces a value. "
                             "ROUTE a TimeSensor fraction_changed (or another event) to its input."))
    return out


def _route_els(scene):
    return [r for r in scene.iter() if _tag(r) == "ROUTE"]


def _is_meta(el):
    return _tag(el).startswith("Metadata")


# ------------------------------------------------------------------ (1) switch index
def check_switch_index(scene) -> list:
    """Switch.whichChoice picks one child by index; an index past the last child shows nothing (19775-1 10.4.2).
    -1 is legal and shows nothing on purpose. An IntegerSequencer or IntegerTrigger that counts beyond the children
    leaves the viewer with an empty scene for part of every cycle."""
    out, defs = [], _defs(scene)
    for sw in scene.iter():
        if _tag(sw) != "Switch" or sw.get("USE"):
            continue
        n = sum(1 for c in sw if not _is_meta(c) and _tag(c) not in ("IS", "field"))
        if any("placeholder" in (c.get("DEF") or "").lower() for c in sw):
            continue                                         # a stand-in child: the page script fills the Switch at run time
        if n == 0 or any(_tag(c) == "IS" for c in sw):
            continue                                         # empty switch: empty-group's business; IS: set by a Proto
        w = sw.get("whichChoice")
        if w is not None:
            try:
                wi = int(float(w))
            except ValueError:
                wi = None
            if wi is not None and wi >= n:
                out.append(_diag("warning", "switch-index-past-children", sw,
                                 f"has whichChoice={wi} but only {n} child(ren) (indices 0 to {n - 1}), so it shows nothing. "
                                 f"Use an index below {n}, or -1 if showing nothing is the intent."))
    for r in _route_els(scene):
        if (r.get("toField") or "") not in ("whichChoice", "set_whichChoice"):
            continue
        sw, src = defs.get(r.get("toNode")), defs.get(r.get("fromNode"))
        if sw is None or src is None or _tag(sw) != "Switch":
            continue
        n = sum(1 for c in sw if not _is_meta(c) and _tag(c) not in ("IS", "field"))
        if n == 0 or any("placeholder" in (c.get("DEF") or "").lower() for c in sw):
            continue                                         # a stand-in child: the page script fills the Switch at run time
        t = _tag(src)
        if t == "IntegerSequencer":
            raw = src.get("keyValue")
            try:
                vals = [int(float(x)) for x in re.split(r"[\s,]+", raw.strip()) if x] if raw else None
            except ValueError:
                vals = None
        elif t == "IntegerTrigger":
            raw = src.get("integerKey")
            try:
                vals = [int(float(x)) for x in re.split(r"[\s,]+", raw.strip()) if x] if raw else None
            except ValueError:
                vals = None
            if raw is None:
                continue
        else:
            continue
        if not vals:
            continue
        top = max(vals)
        if top >= n:
            what = "key value" if t == "IntegerSequencer" else "integer key"
            out.append(_diag("warning", "switch-index-past-children", r,
                             f"routes {r.get('fromNode')!r} to Switch {r.get('toNode')!r}, whose {what}s reach {top} but the Switch has "
                             f"only {n} child(ren) (indices 0 to {n - 1}). While the value is {top} the viewer sees nothing. "
                             f"Add children or lower the values (-1 shows nothing on purpose)."))
    return out


# ------------------------------------------------------------------ (2) route cycle
_NOT_PASSTHROUGH = re.compile(r"Interpolator|Sequencer|Trigger|Filter|Toggle|Sensor|Script|Proto|Follower|Damper|Chaser|Inline")


def check_route_cycle(scene) -> list:
    """ROUTEs that pass a value straight round in a circle (A.translation to B.translation and back). X3D stops
    a loop within one timestamp, so the scene does not hang, but the two fields just copy each other and nothing
    drives them. Probably a mistake. A deliberate two-way binding between two fields looks the same: if that is the
    intent, ignore this."""
    defs, edges, route_of = _defs(scene), {}, {}
    for r in _route_els(scene):
        a, b, ff, tf = r.get("fromNode"), r.get("toNode"), r.get("fromField") or "", r.get("toField") or ""
        if a not in defs or b not in defs:
            continue
        if _NOT_PASSTHROUGH.search(_tag(defs[a])) or _NOT_PASSTHROUGH.search(_tag(defs[b])):
            continue
        if _field(ff) != _field(tf) or not _field(ff):
            continue
        if ff.startswith("set_") or tf.endswith("_changed"):
            continue                                         # an input as source or an output as target is another check's
        u, v = (a, _field(ff)), (b, _field(tf))
        edges.setdefault(u, []).append(v)
        route_of[(u, v)] = r
    out, done = [], set()
    for start in list(edges):
        if start in done:
            continue
        stack, path, onpath = [(start, iter(edges.get(start, [])))], [start], {start}
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                stack.pop(); path.pop(); onpath.discard(node); done.add(node)
                continue
            if nxt in onpath:
                cyc = path[path.index(nxt):]
                key = frozenset(cyc)
                if key not in done:
                    done.add(key)
                    r = route_of[(cyc[-1], nxt)]
                    names = " -> ".join(f"{n}.{f}" for n, f in cyc + [nxt])
                    out.append(_diag("warning", "route-cycle", r,
                                     f"closes a loop of plain ROUTEs ({names}). Each field just copies the other, nothing drives "
                                     f"them, and the browser cuts the loop after one pass. If this is a deliberate two-way "
                                     f"binding, ignore it; otherwise remove one ROUTE."))
            elif nxt not in done:
                stack.append((nxt, iter(edges.get(nxt, [])))); path.append(nxt); onpath.add(nxt)
    return out


# ------------------------------------------------------------------ (3) sensor zero size
def check_sensor_zero_size(scene) -> list:
    """A ProximitySensor, VisibilitySensor or TransformSensor senses a box of the given size, and the default size
    0 0 0 is no box at all (19775-1 22.4.1), so it never fires. Flagged only when its outputs are routed on and
    nothing ROUTEs a size into it."""
    out, defs = [], _defs(scene)
    routed = {}
    for r in _route_els(scene):
        routed.setdefault(r.get("fromNode"), []).append(r)
    into = {(r.get("toNode"), _field(r.get("toField"))) for r in _route_els(scene)}
    for el in scene.iter():
        if _tag(el) not in ("ProximitySensor", "VisibilitySensor", "TransformSensor") or el.get("USE"):
            continue
        if el.get("enabled", "true").lower() == "false":
            continue
        if any(_tag(c) == "IS" for c in el):
            continue
        s = _nums(el.get("size")) if el.get("size") is not None else [0.0, 0.0, 0.0]
        if s is None or len(s) != 3 or any(x != 0 for x in s):
            continue
        name = el.get("DEF")
        if not name or not routed.get(name) or (name, "size") in into:
            continue
        outs = sorted({r.get("fromField") for r in routed[name]})
        out.append(_diag("warning", "sensor-zero-size", el,
                         f"has size 0 0 0 (the default), so it never senses anything and never sends {', '.join(outs)}. "
                         f"Set size to the region to watch, for example size=\"10 10 10\"."))
    return out


# ------------------------------------------------------------------ (4) empty script
def check_script_empty(scene) -> list:
    """A Script with no url and no text between its tags runs nothing, so every output routed from it stays at
    its default value."""
    out, defs = [], _defs(scene)
    routed = {}
    for r in _route_els(scene):
        routed.setdefault(r.get("fromNode"), []).append(r)
    for el in scene.iter():
        if _tag(el) != "Script" or el.get("USE"):
            continue
        if any(_tag(c) == "IS" for c in el):
            continue
        url = (el.get("url") or "").replace('"', "").replace("'", "").strip()
        body = (el.text or "") + "".join((c.tail or "") for c in el)
        if url or body.strip() or any(_tag(c) == "url" for c in el):
            continue
        name = el.get("DEF")
        rs = routed.get(name) if name else None
        if not rs:
            continue
        outs = sorted({r.get("fromField") for r in rs})
        out.append(_diag("warning", "script-empty", el,
                         f"has no url and no script text, so it never produces anything, yet {', '.join(outs)} "
                         f"{'is' if len(outs) == 1 else 'are'} routed onward and stay at their default value. "
                         f"Add the script as text between the tags or point url at a file."))
    return out


# ------------------------------------------------------------------ (5) loop does not close
TIMELINE_TRACKS = 3        # a clock that feeds this many interpolators or sequencers is read as a shared timeline


def check_loop_does_not_close(scene) -> list:
    """A looping clock driving a spin about one axis whose last orientation is not its first: the object snaps
    back at every cycle. Left alone: more than 12 keys (a sampled recording, where the snap is the replay), several
    axes, joints of a humanoid. A clock that feeds three or more interpolators is a shared timeline (a film or a
    replay that restarts on purpose), so there the finding is a note, not a warning."""
    out, defs = [], _defs(scene)
    has_dyn = any(_tag(e) in ("Script", "ProtoInstance", "ProtoDeclare") for e in scene.iter())
    tracks = {}
    for r in _route_els(scene):
        dst = defs.get(r.get("toNode"))
        if r.get("fromField") == "fraction_changed" and dst is not None and _is_flow(_tag(dst)):
            tracks.setdefault(r.get("fromNode"), set()).add(r.get("toNode"))
    for r in _route_els(scene):
        if r.get("fromField") != "fraction_changed" or r.get("toField") not in ("set_fraction", "fraction"):
            continue
        ts, oi = defs.get(r.get("fromNode")), defs.get(r.get("toNode"))
        if ts is None or oi is None or _tag(ts) != "TimeSensor" or _tag(oi) != "OrientationInterpolator":
            continue
        if (ts.get("loop") or "false").lower() != "true" or (ts.get("enabled") or "true").lower() == "false":
            continue
        v = _nums(oi.get("keyValue"))
        if not v or len(v) < 8 or len(v) % 4:
            continue
        rots = [v[i:i + 4] for i in range(0, len(v), 4)]
        if len(rots) > 12:
            continue                                          # a sampled timeline (baked physics, Blender, GSAP): a snap back is the replay
        axes = []
        for q in rots:
            ln = math.sqrt(sum(x * x for x in q[:3]))
            if ln < 1e-9 or abs(q[3]) < 1e-9:
                continue                                      # identity or zero axis: no axis of its own
            axes.append(tuple(x / ln for x in q[:3]))
        if not axes or any(abs(sum(a * b for a, b in zip(axes[0], ax))) < 1 - 1e-4 for ax in axes[1:]):
            continue                                          # several axes: not a simple spin
        qa, qb = _quat(rots[0]), _quat(rots[-1])
        if qa is None or qb is None:
            continue
        d = max(-1.0, min(1.0, abs(sum(x * y for x, y in zip(qa, qb)))))
        gap = 2.0 * math.acos(d)
        if gap <= 0.5:
            continue
        if any(_tag(rr_dst) == "HAnimJoint" for rr_dst in
               (defs.get(x.get("toNode")) for x in _route_els(scene) if x.get("fromNode") == oi.get("DEF") and oi.get("DEF")) if rr_dst is not None):
            continue
        timeline = len(tracks.get(r.get("fromNode"), ())) >= TIMELINE_TRACKS
        out.append(_diag("info" if has_dyn or timeline else "warning", "loop-does-not-close", oi,
                         f"is driven by looping TimeSensor {r.get('fromNode')!r} but its last orientation is {gap:.3g} rad away from "
                         f"its first, so the object snaps back every cycle. End on the first orientation (for a full spin, "
                         f"angles 0 to 6.28319 in steps below pi), or set loop to false."
                         + (f" The clock drives {len(tracks[r.get('fromNode')])} tracks, so this may be a timeline that "
                            f"restarts on purpose." if timeline else "")))
    return out


INERT_CHECKS = [check_sensor_senses_nothing, check_clock_never_starts, check_output_goes_nowhere, check_switch_index,
                check_route_cycle, check_sensor_zero_size, check_script_empty, check_loop_does_not_close]
