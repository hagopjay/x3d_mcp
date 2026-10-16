"""Break it on purpose: inject one known defect into a scene, then watch which
test catches it. Most defects are the validator's to catch; the last two are
"silent" faults that leave the scene valid and only the browser can see.

These operators are written for the playground. They resemble the kinds used
in the mutation study but are not that study's operator set.
"""

from __future__ import annotations

import random

from lxml import etree

_PARSER = etree.XMLParser(remove_blank_text=False, resolve_entities=False, no_network=True)


def _tag(el) -> str:
    return el.tag.split("}")[-1] if isinstance(el.tag, str) else ""


def _all(root, *tags):
    return [el for el in root.iter() if _tag(el) in tags]


def _scene(root):
    for el in root.iter():
        if _tag(el) == "Scene":
            return el
    return None


def _text(root) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + etree.tostring(root, encoding="unicode")


class NotApplicable(Exception):
    pass


def _route_typo(root, rng):
    routes = _all(root, "ROUTE")
    if not routes:
        raise NotApplicable("the scene has no ROUTE")
    r = rng.choice(routes)
    field = r.get("toField", "")
    r.set("toField", field[:-2] + field[-1:] + field[-2] if len(field) > 3 else field + "x")
    return f"misspelled a ROUTE's toField ({field} -> {r.get('toField')})"


def _route_type(root, rng):
    routes = [r for r in _all(root, "ROUTE") if r.get("toField") in ("set_fraction", "set_startTime")]
    times = [el for el in _all(root, "TimeSensor") if el.get("DEF")]
    trans = [el for el in _all(root, "Transform") if el.get("DEF")]
    if not times or not trans:
        raise NotApplicable("needs a DEF'd TimeSensor and a DEF'd Transform")
    scene = _scene(root)
    bad = etree.SubElement(scene, "ROUTE", fromNode=times[0].get("DEF"), fromField="fraction_changed",
                           toNode=trans[0].get("DEF"), toField="set_translation")
    del routes
    return f"added a ROUTE from SFFloat fraction_changed to SFVec3f set_translation ({bad.get('toNode')})"


def _drop_def(root, rng):
    used = {el.get("USE") for el in root.iter() if isinstance(el.tag, str) and el.get("USE")}
    used |= {el.get(a) for el in _all(root, "ROUTE") for a in ("fromNode", "toNode")}
    cands = [el for el in root.iter() if isinstance(el.tag, str) and el.get("DEF") in used]
    if not cands:
        raise NotApplicable("no DEF is referenced by a USE or ROUTE")
    el = rng.choice(cands)
    name = el.get("DEF")
    del el.attrib["DEF"]
    return f"removed DEF='{name}' that a USE or ROUTE still names"


def _duplicate_def(root, rng):
    defs = [el for el in root.iter() if isinstance(el.tag, str) and el.get("DEF")]
    others = [el for el in root.iter() if _tag(el) in ("Transform", "Shape", "Material", "Group")
              and not el.get("DEF") and not el.get("USE")]
    if not defs or not others:
        raise NotApplicable("needs a DEF and another node to copy it onto")
    name = rng.choice(defs).get("DEF")
    rng.choice(others).set("DEF", name)
    return f"gave a second node the DEF name '{name}'"


def _unknown_field(root, rng):
    mats = _all(root, "Material", "PhysicalMaterial")
    if not mats:
        raise NotApplicable("the scene has no Material")
    m = rng.choice(mats)
    m.set("colour", m.get("diffuseColor") or m.get("baseColor") or "1 0 0")
    return "added a field that does not exist (colour on a Material)"


def _bad_bool(root, rng):
    cands = _all(root, "TimeSensor", "NavigationInfo", "Viewpoint", "TouchSensor")
    if not cands:
        raise NotApplicable("needs a TimeSensor, TouchSensor, Viewpoint or NavigationInfo")
    el = rng.choice(cands)
    field = {"TimeSensor": "loop", "NavigationInfo": "headlight", "Viewpoint": "jump",
             "TouchSensor": "enabled"}[_tag(el)]
    el.set(field, "TRUE")
    return f"wrote an SFBool in upper case ({_tag(el)} {field}='TRUE')"


def _version_drift(root, rng):
    if root.get("version") != "4.0":
        raise NotApplicable("the scene does not declare version 4.0")
    etree.SubElement(_scene(root), "EnvironmentLight", intensity="0.3")
    return "added an X3D 4.1 node (EnvironmentLight) to a 4.0 document"


def _profile_drift(root, rng):
    """Lower the profile to Interchange and drop the component statements, as a
    model does when it copies a static scene's header. Applies only when some
    node then falls outside the profile; X_ITE refuses such a scene."""
    from validation.profile import check_profile
    if root.get("profile") == "Interchange" and not _all(root, "component"):
        raise NotApplicable("the scene already uses the Interchange profile")
    head = next((el for el in root if _tag(el) == "head"), None)
    if head is not None:
        for c in [c for c in head if _tag(c) == "component"]:
            head.remove(c)
    root.set("profile", "Interchange")
    report = check_profile(root)
    if report.ok:
        raise NotApplicable("every node in this scene is inside the Interchange profile")
    outside = sorted({m["node"] for m in report.missing})
    return f"lowered the profile to Interchange, which does not cover {', '.join(outside[:3])}"


def _missing_geometry(root, rng):
    shapes = [s for s in _all(root, "Shape") if not s.get("USE")]
    if not shapes:
        raise NotApplicable("the scene has no Shape")
    s = rng.choice(shapes)
    for child in list(s):
        if _tag(child) not in ("Appearance",) and isinstance(child.tag, str):
            s.remove(child)
    return "removed a Shape's geometry"


def _keyvalue_count(root, rng):
    interps = [el for el in root.iter() if _tag(el).endswith("Interpolator") and el.get("keyValue")]
    if not interps:
        raise NotApplicable("the scene has no interpolator")
    el = rng.choice(interps)
    vals = el.get("keyValue").split()
    el.set("keyValue", " ".join(vals[:-1]))
    return f"dropped the last keyValue number from a {_tag(el)}"


def _break_xml(root, rng):
    text = _text(root)
    cut = text.rfind("</Scene>")
    return text[:cut] + text[cut + len("</Scene>"):], "deleted the closing </Scene> tag"


def _invisible(root, rng):
    mats = _all(root, "Material", "PhysicalMaterial", "UnlitMaterial")
    if not mats:
        raise NotApplicable("the scene has no material")
    for m in mats:
        m.set("transparency", "1")
    return "made every material fully transparent (still valid)"


def _camera_away(root, rng):
    vps = _all(root, "Viewpoint", "OrthoViewpoint")
    if not vps:
        raise NotApplicable("the scene has no Viewpoint")
    for vp in vps:
        pos = (vp.get("position") or "0 0 10").split()
        if len(pos) == 3:
            vp.set("position", f"{pos[0]} {pos[1]} {float(pos[2]) + 500:g}")
        vp.set("orientation", "0 1 0 3.14159")
        vp.set("centerOfRotation", vp.get("centerOfRotation", "0 0 0"))
    return "moved the camera 500 m back and turned it around (still valid)"


OPERATORS = {
    "route-typo": ("validator", _route_typo),
    "route-type": ("validator", _route_type),
    "drop-def": ("validator", _drop_def),
    "duplicate-def": ("validator", _duplicate_def),
    "unknown-field": ("validator", _unknown_field),
    "bad-bool": ("validator", _bad_bool),
    "version-drift": ("validator", _version_drift),
    "profile-drift": ("validator", _profile_drift),
    # Legal X3D (geometry is optional) that draws nothing: the validator warns.
    "missing-geometry": ("warning", _missing_geometry),
    "keyvalue-count": ("validator", _keyvalue_count),
    "break-xml": ("validator", _break_xml),
    "invisible": ("browser", _invisible),
    "camera-away": ("browser", _camera_away),
}


def catalogue() -> list[dict]:
    return [{"op": k, "caught_by": v[0]} for k, v in OPERATORS.items()]


def mutate(text: str, op: str, seed: int = 0) -> dict:
    if op not in OPERATORS:
        raise ValueError(f"unknown operator {op}")
    caught_by, fn = OPERATORS[op]
    try:
        root = etree.fromstring(text.encode("utf-8"), _PARSER)
    except etree.XMLSyntaxError as exc:
        raise ValueError(f"the scene is not well-formed: {exc}")
    rng = random.Random(seed)
    try:
        out = fn(root, rng)
    except NotApplicable as exc:
        return {"applied": False, "op": op, "reason": str(exc)}
    if isinstance(out, tuple):
        mutated, what = out
    else:
        mutated, what = _text(root), out
    return {"applied": True, "op": op, "what": what, "caught_by": caught_by, "x3d": mutated}
