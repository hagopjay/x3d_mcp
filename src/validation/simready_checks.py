"""The X3D SimReady profile (draft 2): what NVIDIA's SimReady Foundation asks of an asset, stated in an X3D file.

Plain X3D leaves these open: a file may be in centimetres, built of Spheres, carry no licence and no labels, and
still be valid X3D. SimReady closes them. A file opts in here, and only then is it judged:

    opt in      any <meta name="simready:..."> in <head> (simready:category, simready:qcode, simready:asset_type,
                simready:features). A file with none is not bound for SimReady and these checks stay silent,
                except the label checks, which judge a label wherever one is written.
    features    <meta name="simready:features" content="rigid_bodies multibody semantic_labels material_labels"/>
                names the SimReady capabilities the asset claims; a capability it does not claim is not asked of it.
    provenance  <meta name="creator|author|dc.creator">, <meta name="license|rights|dc.rights">,
                <meta name="simready:category|category">, <meta name="simready:qcode|wikidata|qcode">
    thumbnail   <meta name="Image" content="picture.png"/> (the Web3D authoring-hints name for a scene's picture)
    labels      a MetadataSet named "semantics:labels" in a node's metadata field; each MetadataString in it is one
                taxonomy: its name is the SemanticsLabelsAPI instance (wikidata_qcode, coco, material, ...), its
                value the labels. A label on a Shape, any ancestor, or its Appearance or material covers the Shape.

Each check names the SimReady requirement it answers. docs/USD-VALIDATION-PARITY.md lists them rule by rule and
tests/test_usd_parity.py runs one failing and one passing scene for each. The same proof scenes are converted to
USD and judged by NVIDIA's own validator in x3d.nvidia (q12/profile_crossing.py), one receipt per rule.
Requirements: NVIDIA/simready-foundation c3bd1d8. Vocabularies: simready_vocab.py (generated from that commit).
"""
from __future__ import annotations

import re

from validation.data_checks import _defs, _diag, _inside, _nums, _resolve, _tag, _urls

_QCODE = re.compile(r"^Q[0-9]+$")
_WS = re.compile(r"[\s_\-]+")
_LABEL_SET = "semantics:labels"
_PROVENANCE = (("author", ("creator", "author", "dc.creator")),
               ("asset_license", ("license", "rights", "dc.rights")),
               ("category", ("simready:category", "category")),
               ("qcode", ("simready:qcode", "wikidata", "qcode")))
_MESHES = {"IndexedFaceSet", "IndexedTriangleSet", "IndexedTriangleFanSet", "IndexedTriangleStripSet", "IndexedQuadSet",
           "TriangleSet", "TriangleFanSet", "TriangleStripSet", "QuadSet", "ElevationGrid", "Extrusion"}
_PRIMITIVES = {"Box", "Sphere", "Cone", "Cylinder", "Text", "Arc2D", "ArcClose2D", "Circle2D", "Disk2D", "Polyline2D",
               "Polypoint2D", "Rectangle2D", "TriangleSet2D", "NurbsPatchSurface", "NurbsTrimmedSurface", "PointSet",
               "LineSet", "IndexedLineSet", "GeoElevationGrid"}
_MATERIALS = {"Material", "PhysicalMaterial", "UnlitMaterial", "TwoSidedMaterial"}


def _head(scene) -> dict:
    """<head> meta as {lower-case name: content}; the first of a repeated name wins."""
    out = {}
    for el in scene.getroottree().getroot().iter():
        if _tag(el) == "meta" and el.get("name"):
            out.setdefault(el.get("name").strip().lower(), (el.get("content") or "").strip())
    return out


def _bound(head: dict) -> bool:
    return any(k.startswith("simready:") for k in head)


def _features(head: dict) -> set:
    return set(re.split(r"[\s,]+", head.get("simready:features", "").lower())) - {""}


def _anchor(scene):
    """The element a file-level finding hangs on: <head> when there is one."""
    for el in scene.getroottree().getroot():
        if _tag(el) == "head":
            return el
    return scene


def _strings(value: str | None) -> list:
    """MFString text as a list: quoted items, or one bare word."""
    if value is None:
        return []
    return _urls(value)


def _label_sets(el):
    return [c for c in el if _tag(c) == "MetadataSet" and (c.get("name") or "").strip() == _LABEL_SET]


def _labels(el) -> dict:
    """{taxonomy: [(MetadataString element, [values])]} written directly on one node."""
    out = {}
    for ms in _label_sets(el):
        for c in ms:
            if _tag(c) == "MetadataString" and (c.get("name") or "").strip():
                out.setdefault(c.get("name").strip(), []).append((c, [v for v in _strings(c.get("value")) if v.strip()]))
    return out


def _all_labels(scene):
    """(owner node, taxonomy, MetadataString element, values) for every label in the scene."""
    for el in scene.iter():
        if not isinstance(el.tag, str) or el.get("USE"):
            continue
        for tax, entries in _labels(el).items():
            for ms, values in entries:
                yield el, tax, ms, values


# --------------------------------------------------------------------------- provenance (SR.003, SR.004)

def check_simready_provenance(scene) -> list:
    """SimReady SR.003 / SR.004: author, asset_license, category and qcode must be present and non-empty. The other
    required fields (source_file, date, rigid_body_count, mass, extents) are facts the X3D-to-USD writer measures.
    A qcode must be a Wikidata Q-code; the writer drops a malformed one, so the field is then missing in the USD."""
    head = _head(scene)
    if not _bound(head):
        return []
    out, missing = [], []
    for field, names in _PROVENANCE:
        if not any(head.get(n) for n in names):
            missing.append(f"{field} (<meta name=\"{names[0]}\">)")
    if missing:
        out.append(_diag("warning", "simready-metadata-missing", _anchor(scene),
                         f"is bound for SimReady (it carries a simready: meta) but does not state {', '.join(missing)}. SimReady "
                         f"SR.003 and SR.004 require each as a non-empty value; add the meta elements to <head>."))
    q = next((head[n] for n in _PROVENANCE[3][1] if head.get(n)), None)
    if q and not _QCODE.match(q):
        out.append(_diag("warning", "simready-qcode-malformed", _anchor(scene),
                         f"gives qcode {q!r}; SimReady wants a Wikidata Q-code, a capital Q followed by digits (for example Q42177)."))
    return out


# --------------------------------------------------------------------------- thumbnail (SR.002)

def check_simready_thumbnail(scene, base_dir=None) -> list:
    """SimReady SR.002: the asset has a thumbnail. In X3D the scene's picture is named by <meta name="Image">; the
    writer copies it to .thumbs/256x256/<asset>.usd.png. With a folder, the named file must exist inside it; without
    one (a scene checked as text) only a missing name is reported."""
    head = _head(scene)
    if not _bound(head):
        return []
    url = head.get("image")
    if not url:
        return [_diag("warning", "simready-thumbnail-missing", _anchor(scene),
                      "is bound for SimReady but names no thumbnail. Add <meta name=\"Image\" content=\"picture.png\"/> to <head> "
                      "(SimReady SR.002).")]
    if base_dir is not None and not re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):
        from pathlib import Path
        p = _inside(Path(base_dir), url)
        if p is None or not p.is_file():
            return [_diag("warning", "simready-thumbnail-missing", _anchor(scene),
                          f"names thumbnail {url!r} in <meta name=\"Image\">, which is not a file beside the scene (SimReady SR.002).")]
    return []


# --------------------------------------------------------------------------- units and geometry form

def check_simready_units(scene) -> list:
    """SimReady UN.007: one unit is one metre. X3D's default is metres; a UNIT statement may change it."""
    head = _head(scene)
    if not _bound(head):
        return []
    for el in scene.getroottree().getroot().iter():
        if _tag(el) == "unit" and el.get("category") == "length":
            f = _nums(el.get("conversionFactor"))
            if f and f[0] > 0 and abs(f[0] - 1.0) > 1e-12:
                return [_diag("warning", "simready-unit-not-metres", el,
                              f"sets the length unit to {f[0]:g} m. That is legal X3D, but SimReady UN.007 wants metres "
                              f"(metersPerUnit 1): remove the UNIT statement and scale the coordinates.")]
    return []


def _geometry(scene):
    for el in scene.iter():
        t = _tag(el)
        if (t in _MESHES or t in _PRIMITIVES) and not el.get("USE") and _tag(el.getparent()) not in ("ProtoBody", "field", "fieldValue"):
            yield el, t


def check_simready_mesh_form(scene) -> list:
    """SimReady VG.MESH.001 (geometry shall be meshes): a stage with no mesh fails, and a mesh beside other geometry
    is warned about; both are a warning here, once. VG.021: meshes are triangulated (information: no SimReady rule
    enforces it at c3bd1d8)."""
    head = _head(scene)
    if not _bound(head):
        return []
    out = []
    geo = list(_geometry(scene))
    meshes = [el for el, t in geo if t in _MESHES]
    prims = [el for el, t in geo if t in _PRIMITIVES]
    if prims and not meshes:
        names = sorted({_tag(p) for p in prims})
        out.append(_diag("warning", "simready-geometry-not-mesh", prims[0],
                         f"and every other geometry here is a primitive ({', '.join(names)}); the scene has no mesh. SimReady "
                         f"VG.MESH.001 wants the asset's geometry as meshes: use IndexedFaceSet or IndexedTriangleSet."))
    elif prims:
        names = sorted({_tag(p) for p in prims})
        out.append(_diag("warning", "simready-geometry-not-mesh", prims[0],
                         f"is not a mesh ({len(prims)} such geometry node(s) here: {', '.join(names)}). The scene has a mesh as "
                         f"SimReady VG.MESH.001 requires, and the same rule warns about any other kind of geometry beside it."))
    for el in meshes:
        t = _tag(el)
        if t == "IndexedFaceSet":
            n, big = 0, 0
            for tok in re.split(r"[\s,]+", (el.get("coordIndex") or "").strip()) + ["-1"]:
                if tok == "-1":
                    big += n > 3
                    n = 0
                elif tok:
                    n += 1
            if big:
                out.append(_diag("info", "simready-mesh-not-triangulated", el,
                                 f"has {big} face(s) with more than three corners. SimReady VG.021 wants triangles, so that "
                                 f"colliders can be made from the mesh: triangulate it, or use IndexedTriangleSet."))
        elif t in ("IndexedQuadSet", "QuadSet", "ElevationGrid", "Extrusion"):
            out.append(_diag("info", "simready-mesh-not-triangulated", el,
                             "is not stored as triangles. SimReady VG.021 wants triangles: use IndexedTriangleSet."))
    return out


# --------------------------------------------------------------------------- capabilities (RB.001, RB.MB.001)

def check_simready_bodies(scene) -> list:
    """SimReady RB.001 (at least one rigid body) and RB.MB.001 (at least two), asked only of an asset that claims the
    capability. A RigidBody with fixed TRUE is a static collider, not a body that moves."""
    head = _head(scene)
    feats = _features(head)
    if not _bound(head) or not feats & {"rigid_bodies", "multibody"}:
        return []
    moving = [el for el in scene.iter()
              if _tag(el) == "RigidBody" and not el.get("USE") and (el.get("fixed") or "false").strip().lower() != "true"]
    out = []
    if "rigid_bodies" in feats and not moving:
        out.append(_diag("warning", "simready-no-rigid-body", _anchor(scene),
                         "claims the SimReady feature rigid_bodies but the scene has no RigidBody that can move (fixed FALSE). "
                         "SimReady RB.001 wants at least one."))
    if "multibody" in feats and len(moving) < 2:
        out.append(_diag("warning", "simready-single-body", _anchor(scene),
                         f"claims the SimReady feature multibody but the scene has {len(moving)} moving RigidBody. "
                         f"SimReady RB.MB.001 wants at least two."))
    return out


# --------------------------------------------------------------------------- semantic labels

def check_simready_labels(scene) -> list:
    """Judged wherever a label is written. SL.003: a label that is applied and empty is unusable. SL.QCODE.001: a
    wikidata_qcode label is a Q-code. Closed vocabularies (COCO.001, CITY.001, ADE.001, VOC.001, SUN.001, IN1K.001,
    AFF.001, AFF.002): a label of that taxonomy must be one of its names."""
    from validation.simready_vocab import VOCAB
    out = []
    for owner, tax, ms, values in _all_labels(scene):
        if not values:
            out.append(_diag("warning", "simready-label-empty", ms,
                             f"is a {tax!r} label on {_tag(owner)} with no value, so the label is applied and unusable "
                             f"(SimReady SL.003). Give it a value or remove it."))
            continue
        if tax == "wikidata_qcode":
            for v in values:
                if not _QCODE.match(v):
                    out.append(_diag("warning", "simready-qcode-malformed", ms,
                                     f"holds {v!r}; a wikidata_qcode label is a capital Q followed by digits, for example "
                                     f"Q15026 (SimReady SL.QCODE.001)."))
        voc = VOCAB.get(tax)
        if voc:
            names = set(voc["names"])
            for v in values:
                if v in names:
                    continue
                n = _WS.sub(" ", v.strip().lower())
                canon = n if n in names else voc["alias"].get(n)
                if canon:                                    # SimReady accepts it with a warning; so does this check
                    out.append(_diag("warning", "simready-label-not-in-vocabulary", ms,
                                     f"holds {v!r}, which is not the canonical spelling in {voc['title']}; use {canon!r}."))
                else:
                    out.append(_diag("warning", "simready-label-not-in-vocabulary", ms,
                                     f"holds {v!r}, which is not one of the {len(names)} names of {voc['title']}. A {tax!r} "
                                     f"label must be a member of that vocabulary."))
    return out


def _has_label(el, tax=None) -> bool:
    lab = _labels(el)
    if tax is not None:
        return any(values for _, values in lab.get(tax, []))
    return any(values for entries in lab.values() for _, values in entries)


def _appearance_nodes(shape, defs):
    """The Shape's Appearance and its material nodes, USE followed."""
    out = []
    for c in shape:
        if _tag(c) == "Appearance":
            app = _resolve(c, defs)
            if app is None:
                continue
            out.append(app)
            for m in app:
                if _tag(m) in _MATERIALS:
                    mm = _resolve(m, defs)
                    if mm is not None:
                        out.append(mm)
    return out


def check_simready_labelled(scene) -> list:
    """SimReady SL.001: every renderable geometry is labelled, on itself, an ancestor or its material. SL.MAT.001:
    every material a Shape draws with carries a 'material' label. Asked only of an asset that claims the feature."""
    head = _head(scene)
    feats = _features(head)
    if not _bound(head) or not feats & {"semantic_labels", "material_labels"}:
        return []
    defs, out = _defs(scene), []
    for sh in scene.iter():
        if _tag(sh) != "Shape" or sh.get("USE"):
            continue
        if not any(_tag(c) in _MESHES or _tag(c) in _PRIMITIVES for c in sh):
            continue
        looks = _appearance_nodes(sh, defs)
        if "semantic_labels" in feats:
            chain, cur = [sh], sh.getparent()
            while cur is not None and _tag(cur) not in ("Scene", "X3D"):
                chain.append(cur)
                cur = cur.getparent()
            if not any(_has_label(n) for n in chain + looks):
                out.append(_diag("warning", "simready-shape-unlabelled", sh,
                                 "has no semantic label on itself, on a node above it, or on its Appearance or material. The file "
                                 "claims the SimReady feature semantic_labels, and SL.001 wants every drawn geometry labelled: add a "
                                 "MetadataSet named \"semantics:labels\" holding a MetadataString such as wikidata_qcode."))
        if "material_labels" in feats and looks and not any(_has_label(n, "material") for n in looks):
            out.append(_diag("warning", "simready-material-unlabelled", looks[0],
                             "is the look a Shape draws with and carries no 'material' label. The file claims the SimReady feature "
                             "material_labels, and SL.MAT.001 wants one: add a MetadataSet named \"semantics:labels\" holding "
                             "<MetadataString name=\"material\" value='\"steel\"'/> to the Appearance or its material."))
    return out


SIMREADY_SCENE_CHECKS = [check_simready_provenance, check_simready_units, check_simready_mesh_form, check_simready_bodies,
                         check_simready_labels, check_simready_labelled]
SIMREADY_FILE_CHECKS = [check_simready_thumbnail]
