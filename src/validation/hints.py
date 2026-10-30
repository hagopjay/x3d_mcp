"""Exact-fix hints: turn validator errors into structured, mechanically applicable fixes.

The XSD says "Element 'Box': This element is not expected." A model in a repair
loop then has to work out where a Box may go. This module answers that from the
specification data the server already ships:

* X3DUOM ``acceptableNodeTypes`` per parent field (which parent fields accept a
  node, through its abstract ancestors), field types, defaults and
  enumerations;
* the verified containment table (``schemas/verified-containment.json``, built
  by ``evaluation/build_verified_containment.py``): the XSD verdict for every
  X3DUOM-allowed (parent, field, child), so a suggested placement is one the
  XSD has been seen to accept;
* the XSD error itself (expected-element lists, enumeration sets, facets).

Each hint is a dict::

    {rule, source, error_index, node, parent, field, line, path, message,
     allowed_parents | allowed_children | allowed_values, suggested_fix, confidence}

``suggested_fix`` is a small edit (``op`` plus arguments, element paths as
lxml XPaths) that ``apply_fix`` performs; ``message`` says the same in words.
``confidence`` is ``exact`` when the data leaves one choice and ``choice`` when
the first of several was taken. A hint without ``suggested_fix`` still carries
the allowed lists. Keys with empty values are left out.

The hints are advisory: they come from tables, not from a model, and nothing
here runs the XSD on a candidate fix before suggesting it.
"""

from __future__ import annotations

import copy
import difflib
import json
import re
from functools import lru_cache

from lxml import etree

# ---------------------------------------------------------------------------
# data

_STATEMENTS_HEAD = {"meta", "component", "unit"}
_STATEMENTS_SCENE_END = {"ROUTE", "IMPORT", "EXPORT"}
_STATEMENTS_SCENE_START = {"ProtoDeclare", "ExternProtoDeclare"}
_COMMON_ATTRS = {"DEF", "USE", "class", "id", "style", "containerField"}
_COLOUR_TYPES = {"SFColor", "MFColor", "SFColorRGBA", "MFColorRGBA"}
_NODE_TYPES = {"SFNode", "MFNode"}
# Wrappers the hints may insert, most useful first. Kept short on purpose: a
# wrapper outside the common profiles would trade an XSD error for a profile one.
_WRAPPERS = [("Shape",), ("Appearance",), ("Shape", "Appearance"), ("Group",)]
_PREFERRED_FIELDS = [
    "children", "geometry", "appearance", "material", "texture", "coord", "color",
    "normal", "texCoord", "fogCoord", "skeleton", "baseTexture", "emissiveTexture",
]
_LIST_CAP = 20
# Names models reach for that are not X3D nodes, mapped to the X3D node meant.
_SYNONYMS = {"Cube": "Box", "Cuboid": "Box", "Ball": "Sphere", "Pyramid": "Cone", "Tube": "Cylinder",
             "Camera": "Viewpoint", "Light": "PointLight", "Mesh": "IndexedFaceSet", "Group3D": "Group",
             "Node": "Group", "Text3D": "Text", "Torus": "IndexedFaceSet"}
_PROFILE_ORDER = ["Interchange", "Interactive", "Immersive", "Full"]


def _local(el) -> str:
    tag = el.tag if isinstance(el.tag, str) else ""
    return tag.split("}", 1)[1] if "}" in tag else tag


def _strip_ns(name: str) -> str:
    return name.split("}", 1)[1] if "}" in name else name


class _Model:
    """X3DUOM plus verified containment for one X3D version."""

    def __init__(self, version: str):
        from validation.validate import SCHEMAS_DIR
        from x3d_utils.x3duom import X3DUOM

        uom = X3DUOM(SCHEMAS_DIR / f"X3dUnifiedObjectModel-{version}.xml")
        self.version = version
        self.nodes = uom.get_concrete_nodes()
        self.abstracts = uom.get_abstract_types()
        self.field_types = uom.get_field_types()
        self.fields = {n: {f["name"]: f for f in d["fields"]} for n, d in self.nodes.items()}
        self._anc: dict[str, set[str]] = {}
        path = SCHEMAS_DIR / "verified-containment.json"
        self.verified = path.exists()
        exc = {}
        if self.verified:
            exc = json.loads(path.read_text())["versions"].get(version, {}).get("exceptions", {})
        self.rejected = {k: set(v.get("R", [])) for k, v in exc.items()}
        self.inconclusive = {k: set(v.get("I", [])) for k, v in exc.items()}
        self._parents_of: dict[str, list[str]] = {}

    def ancestry(self, tag: str) -> set[str]:
        if tag not in self._anc:
            from validation.semantic import _type_ancestry
            self._anc[tag] = _type_ancestry(tag, self.nodes, self.abstracts)
        return self._anc[tag]

    def accepts(self, parent: str, field: str, child: str) -> str | None:
        """'A' (X3DUOM allows and the XSD accepted it), 'I' (allowed, not verified), or None."""
        if parent == "Scene":
            parent, field = "Group", "children" if field == "children" else field
        if child not in self.nodes:
            return None
        f = self.fields.get(parent, {}).get(field)
        if f is None or f.get("type") not in _NODE_TYPES:
            return None
        acc = f.get("acceptableNodeTypes")
        if acc and not (self.ancestry(child) & set(re.split(r"[|\s]+", acc.strip()))):
            return None
        if child in self.rejected.get(f"{parent}.{field}", ()):
            return None
        if not acc or child in self.inconclusive.get(f"{parent}.{field}", ()):
            return "I"
        return "A" if self.verified else "I"

    def node_fields(self, parent: str) -> list[str]:
        if parent == "Scene":
            return ["children"]
        return [n for n, f in self.fields.get(parent, {}).items() if f.get("type") in _NODE_TYPES]

    def fields_accepting(self, parent: str, child: str) -> list[str]:
        """Fields of `parent` that take `child`, verified ones first, conventional order."""
        a, i = [], []
        for name in self.node_fields(parent):
            v = self.accepts(parent, name, child)
            (a if v == "A" else i if v == "I" else []).append(name)
        key = lambda n: (_PREFERRED_FIELDS.index(n) if n in _PREFERRED_FIELDS else 99, n)
        return sorted(a, key=key) + sorted(i, key=key)

    def parents_of(self, child: str) -> list[str]:
        """Every 'Parent.field' that accepts `child` (verified ones only when the table exists)."""
        if child not in self._parents_of:
            out = []
            for p in sorted(self.nodes):
                for f in self.node_fields(p):
                    v = self.accepts(p, f, child)
                    if v == "A" or (v == "I" and not self.verified):
                        out.append(f"{p}.{f}")
            self._parents_of[child] = out
        return self._parents_of[child]

    def children_of(self, parent: str, field: str | None = None) -> list[str]:
        fields = [field] if field else self.node_fields(parent)
        return sorted({c for f in fields for c in self.nodes if self.accepts(parent, f, c) == "A"})

    def attribute_fields(self, tag: str) -> list[str]:
        """Fields that can be written as XML attributes of `tag`."""
        return [n for n, f in self.fields.get(tag, {}).items()
                if f.get("type") not in _NODE_TYPES and f.get("accessType") in ("initializeOnly", "inputOutput")]

    def field(self, tag: str, name: str) -> dict | None:
        return self.fields.get(tag, {}).get(name)


@lru_cache(maxsize=4)
def _model(version: str) -> _Model:
    return _Model(version)


# ---------------------------------------------------------------------------
# helpers on a parsed document


def _path(el) -> str:
    return el.getroottree().getpath(el)


def _find(doc, path: str | None, line: int | None, tag: str):
    """The element an error names: by its XPath, else by line and tag."""
    if path:
        try:
            hits = doc.getroottree().xpath(path)
            if hits and isinstance(hits[0], etree._Element):
                return hits[0]
        except etree.XPathError:
            pass
    for el in doc.iter():
        if _local(el) == tag and (line is None or el.sourceline == line):
            return el
    return None


def _effective_cf(el, m: _Model) -> str | None:
    return el.get("containerField") or (m.nodes.get(_local(el), {}).get("containerField"))


def _occupied(parent, field: str, m: _Model, skip=None) -> bool:
    """True when `field` of `parent` is SFNode and a child other than `skip` fills it."""
    ptag = _local(parent)
    f = m.field("Group" if ptag == "Scene" else ptag, field)
    if f is None or f.get("type") != "SFNode":
        return False
    return any(c is not skip and _local(c) in m.nodes and _effective_cf(c, m) == field
               for c in parent if isinstance(c.tag, str))


def _free_fields(parent, child_tag: str, m: _Model, skip=None) -> list[str]:
    return [f for f in m.fields_accepting(_local(parent), child_tag)
            if not _occupied(parent, f, m, skip)]


def _cf_arg(child_tag: str, field: str, m: _Model) -> dict:
    """{'containerField': field} when the child's default would not land there."""
    return {} if m.nodes.get(child_tag, {}).get("containerField") == field else {"containerField": field}


def _scene(doc):
    return next((e for e in doc.iter() if _local(e) == "Scene"), None)


def _close(word: str, choices, cutoff=0.6) -> str | None:
    choices = list(choices)
    low = {c.lower(): c for c in choices}
    if word.lower() in low:
        return low[word.lower()]
    hit = difflib.get_close_matches(word, choices, n=1, cutoff=cutoff)
    return hit[0] if hit else None


def _cap(items: list) -> list:
    return items[:_LIST_CAP] + ([f"... {len(items) - _LIST_CAP} more"] if len(items) > _LIST_CAP else [])


def _hint(**kw) -> dict:
    return {k: v for k, v in kw.items() if v not in (None, "", [], {})}


# ---------------------------------------------------------------------------
# XSD error patterns

_RE_NOT_EXPECTED = re.compile(
    r"Element '([^']+)': This element is not expected\.(?: Expected is (?:one of )?\( ?(.*?) ?\)\.)?")
_RE_MISSING_CHILD = re.compile(
    r"Element '([^']+)': Missing child element\(s\)\.(?: Expected is (?:one of )?\( ?(.*?) ?\)\.)?")
_RE_ENUM = re.compile(
    r"Element '([^']+)', attribute '([^']+)': \[facet 'enumeration'\] The value '(.*)' is not an element "
    r"of the set \{(.*)\}\.")
_RE_PATTERN = re.compile(
    r"Element '([^']+)', attribute '([^']+)': \[facet 'pattern'\] The value '(.*)' is not accepted by "
    r"the pattern '(.*)'\.")
_RE_ATOMIC = re.compile(
    r"Element '([^']+)', attribute '([^']+)': '(.*)' is not a valid value of the (?:local )?(?:atomic|union|list) "
    r"type(?: '([^']+)')?\.")
_RE_RANGE = re.compile(
    r"Element '([^']+)', attribute '([^']+)': \[facet '(min|max)(Inclusive|Exclusive)'\] The value '(.*)' is "
    r"(?:less|greater) than (?:or equal to )?the (?:minimum|maximum) value allowed \('(.*)'\)\.")
_RE_NOT_ALLOWED = re.compile(r"Element '([^']+)', attribute '([^']+)': The attribute '[^']+' is not allowed\.")
_RE_REQUIRED = re.compile(r"Element '([^']+)': The attribute '([^']+)' is required but missing\.")
# written by validate.py when libxml2's pattern engine gives up and the value is read number by number
_RE_VALUE = re.compile(r"Element '([^']+)', attribute '([^']+)': \[value\] (.*)")


def _fmt(x: float) -> str:
    s = f"{x:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _floats(text: str) -> list[float] | None:
    try:
        return [float(t) for t in re.split(r"[\s,]+", text.strip()) if t]
    except ValueError:
        return None


def _xsd_match(pattern: str, value: str) -> bool | None:
    """Does `value` match the XSD facet `pattern`? None when unknown.

    Python's backtracking engine can take exponential time on the X3D list
    patterns, so only short values are tried."""
    if len(value) > 120:
        return None
    try:
        return re.fullmatch(pattern, value) is not None
    except re.error:
        return None


# ---------------------------------------------------------------------------
# element placement


def _placement_hint(doc, el, m: _Model, base: dict) -> dict:
    """Where a node that its parent does not take can go, and the edit that puts it there."""
    tag, parent = _local(el), el.getparent()
    ptag = _local(parent) if parent is not None else ""
    path = _path(el)
    base = dict(base, node=tag, parent=ptag, path=path, line=el.sourceline)

    # statements
    if tag in _STATEMENTS_SCENE_END or tag in _STATEMENTS_SCENE_START or tag in _STATEMENTS_HEAD:
        if tag in _STATEMENTS_HEAD:
            head = next((e for e in doc.iter() if _local(e) == "head"), None)
            fix = ({"op": "move", "path": path, "to_path": _path(head), "position": "end"} if head is not None
                   else {"op": "move", "path": path, "to_path": "/X3D", "position": "start", "create": "head"})
            return _hint(**base, rule="misplaced-statement", field="head",
                         message=f"<{tag}> belongs in <head>, not in <{ptag}>. Move it into <head>.",
                         allowed_parents=["head"], suggested_fix=fix, confidence="exact")
        holder = next((a for a in el.iterancestors() if _local(a) in ("ProtoBody", "Scene")), None)
        if holder is None:
            holder = _scene(doc)
        if holder is None:
            return _hint(**base, rule="misplaced-statement", message=f"<{tag}> needs a <Scene> to sit in.")
        where = "end" if tag not in _STATEMENTS_SCENE_START else "start"
        rule = "misplaced-route" if tag == "ROUTE" else "misplaced-statement"
        return _hint(**base, rule=rule, field=_local(holder),
                     message=f"<{tag}> cannot be a child of <{ptag}>; {tag} statements go directly in "
                             f"<{_local(holder)}> (or a grouping node's children). Move it to the {where} of "
                             f"<{_local(holder)}>.",
                     allowed_parents=["Scene", "ProtoBody", "X3DGroupingNode.children"],
                     suggested_fix={"op": "move", "path": path, "to_path": _path(holder), "position": where},
                     confidence="exact")

    # unknown element name
    if tag not in m.nodes:
        accepted = m.children_of(ptag) if (ptag in m.nodes or ptag == "Scene") else []
        syn = _SYNONYMS.get(tag) or _SYNONYMS.get(tag[:1].upper() + tag[1:])
        guess = (_close(tag, accepted, 0.6) or (syn if syn and (not accepted or syn in accepted) else None)
                 or _close(tag, m.nodes, 0.75))
        allowed = base.pop("xsd_expected", None) or _cap(accepted)
        if guess:
            return _hint(**base, rule="unknown-node", allowed_children=allowed,
                         message=f"'{tag}' is not an X3D {m.version} node. Did you mean <{guess}>?",
                         suggested_fix={"op": "rename_element", "path": path, "to": guess},
                         confidence="choice")
        return _hint(**base, rule="unknown-node", allowed_children=allowed,
                     message=f"'{tag}' is not an X3D {m.version} node and no node name is close; "
                             f"remove it or replace it with one of the allowed children.",
                     suggested_fix={"op": "remove_element", "path": path}, confidence="choice")

    allowed_parents = _cap(m.parents_of(tag))
    base["allowed_parents"] = allowed_parents

    # a node sitting directly under X3D or head belongs in the Scene
    if ptag in ("X3D", "head"):
        scene = _scene(doc)
        fix = ({"op": "move", "path": path, "to_path": _path(scene), "position": "end"} if scene is not None
               else {"op": "wrap", "path": path, "wrappers": ["Scene"]})
        return _hint(**base, rule="wrong-parent", field="Scene",
                     message=f"<{tag}> cannot be a child of <{ptag}>; scene nodes go inside <Scene>.",
                     suggested_fix=fix, confidence="exact")

    if ptag not in m.nodes and ptag != "Scene":
        return _hint(**base, rule="wrong-parent",
                     message=f"<{tag}> is not expected inside <{ptag}>.")

    cf = _effective_cf(el, m)
    fields_here = m.fields_accepting(ptag, tag)
    if fields_here:
        free = _free_fields(parent, tag, m, skip=el)
        if cf not in fields_here:
            target = (free or fields_here)[0]
            return _hint(**base, rule="wrong-containerfield", field=cf,
                         allowed_values=fields_here,
                         message=f"{ptag} takes a {tag} in field(s) {', '.join(fields_here)}, not '{cf}'. "
                                 f"Set containerField='{target}'.",
                         suggested_fix={"op": "set_attribute", "path": path, "attribute": "containerField",
                                        "value": target},
                         confidence="exact" if len(fields_here) == 1 else "choice")
        if _occupied(parent, cf, m, skip=el):
            if free:
                return _hint(**base, rule="sfnode-occupied", field=cf, allowed_values=free,
                             message=f"{ptag}.{cf} holds one node and already has one; put this {tag} in "
                                     f"containerField='{free[0]}'.",
                             suggested_fix={"op": "set_attribute", "path": path, "attribute": "containerField",
                                            "value": free[0]}, confidence="choice")
            return _hint(**base, rule="sfnode-occupied", field=cf,
                         message=f"{ptag}.{cf} holds a single node and another child already fills it; "
                                 f"remove this extra {tag} (or the other one).",
                         suggested_fix={"op": "remove_element", "path": path}, confidence="choice")
        return _hint(**base, rule="content-order", field=cf,
                     message=f"X3DUOM allows a {tag} in {ptag}.{cf}, but the XSD rejects it here; "
                             f"the XSD content model may need a different order or companion node.")

    # the parent takes no such node: move it to a sibling, wrap it, or lift it
    for sib in parent:
        if sib is el or not isinstance(sib.tag, str) or _local(sib) not in m.nodes:
            continue
        free = _free_fields(sib, tag, m)
        if free and m.accepts(_local(sib), free[0], tag) == "A":
            fix = {"op": "move", "path": path, "to_path": _path(sib), "position": "end",
                   **_cf_arg(tag, free[0], m)}
            return _hint(**base, rule="wrong-parent", field=cf,
                         message=f"{ptag} has no field that takes a {tag}. Move it into the sibling "
                                 f"<{_local(sib)}> (field {free[0]}).",
                         suggested_fix=fix, confidence="choice")
    for chain in _WRAPPERS:
        outer = chain[0]
        outer_cf = m.nodes.get(outer, {}).get("containerField")
        pf = [f for f in _free_fields(parent, outer, m, skip=el) if m.accepts(ptag, f, outer) == "A"]
        if not pf:
            continue
        ok, holder = True, outer
        for inner in chain[1:]:
            if m.accepts(holder, m.nodes[inner]["containerField"], inner) != "A":
                ok = False
                break
            holder = inner
        inner_fields = [f for f in m.fields_accepting(holder, tag) if m.accepts(holder, f, tag) == "A"]
        if not ok or not inner_fields:
            continue
        fix = {"op": "wrap", "path": path, "wrappers": list(chain), **_cf_arg(tag, inner_fields[0], m)}
        if outer_cf != pf[0]:
            fix["wrapper_containerField"] = pf[0]
        chain_txt = "".join(f"<{w}>" for w in chain)
        return _hint(**base, rule="wrong-parent", field=cf,
                     message=f"{ptag} has no field that takes a {tag}. Wrap it: {chain_txt}<{tag}/>"
                             f"{''.join(f'</{w}>' for w in reversed(chain))} "
                             f"({holder}.{inner_fields[0]} takes a {tag}).",
                     suggested_fix=fix, confidence="choice")
    below = el
    for anc in el.iterancestors():
        if _local(anc) == "X3D":
            break
        if anc is not parent and (_local(anc) in m.nodes or _local(anc) == "Scene"):
            free = [f for f in _free_fields(anc, tag, m) if m.accepts(_local(anc), f, tag) == "A"]
            if free:
                fix = {"op": "move", "path": path, "to_path": _path(anc), "after_path": _path(below),
                       **_cf_arg(tag, free[0], m)}
                return _hint(**base, rule="wrong-parent", field=cf,
                             message=f"{ptag} has no field that takes a {tag}. Move it up into "
                                     f"<{_local(anc)}> (field {free[0]}), after <{_local(below)}>.",
                             suggested_fix=fix, confidence="choice")
        below = anc
    return _hint(**base, rule="wrong-parent", field=cf,
                 message=f"{ptag} has no field that takes a {tag}, and no nearby node does; "
                         f"remove it or move it under one of the allowed parents.",
                 suggested_fix={"op": "remove_element", "path": path}, confidence="choice")


# ---------------------------------------------------------------------------
# attribute values


def _pattern_candidates(value: str, ftype: str | None, m: _Model, default: str | None) -> list[tuple[str, str]]:
    """(candidate value, why) pairs for a value the XSD pattern rejects."""
    out = []
    nums = _floats(value)
    if ftype in _COLOUR_TYPES and nums is not None:
        # 0..255 when every component is a whole number in 0..255 and one exceeds 1
        # (or one exceeds 2); otherwise a slip past 1 or below 0, which is clamped
        if (any(x > 1 for x in nums) and all(0 <= x <= 255 for x in nums)
                and (all(x == int(x) for x in nums) or max(nums) > 2)):
            out.append((" ".join(_fmt(x / 255) for x in nums),
                        "X3D colours are 0..1; the values look like 0..255, so they were divided by 255"))
        out.append((" ".join(_fmt(min(1.0, max(0.0, x))) for x in nums),
                    "X3D colour components must be in 0..1; out-of-range components were clamped"))
    if "," in value:
        out.append((re.sub(r"\s*,\s*", " ", value).strip(), "use spaces, not commas, between numbers"))
    info = m.field_types.get(ftype or "", {})
    size = info.get("tupleSize", 1)
    if nums is not None and size > 1 and not info.get("isArray") and len(nums) != size:
        dflt = _floats(default or "") or [0.0] * size
        fixed = (nums + dflt[len(nums):])[:size]
        out.append((" ".join(_fmt(x) for x in fixed), f"{ftype} takes exactly {size} numbers"))
    if default is not None:
        out.append((default, f"the X3DUOM default for this field"))
    return out


def _attribute_hint(doc, el, attr: str, msg: str, m: _Model, base: dict) -> dict | None:
    tag = _local(el)
    path = _path(el)
    f = m.field(tag, attr)
    ftype = f.get("type") if f else None
    default = f.get("default") if f else None
    base = dict(base, node=tag, parent=_local(el.getparent()) if el.getparent() is not None else "",
                field=attr, path=path, line=el.sourceline)
    value = el.get(attr)

    if (mm := _RE_ENUM.search(msg)):
        allowed = re.findall(r"'([^']*)'", mm.group(4))
        pick = _close(value or "", allowed, 0.5) or (default if default in allowed else allowed[0])
        return _hint(**base, rule="bad-enum", allowed_values=allowed,
                     message=f"{tag} {attr}='{value}' is not one of {', '.join(allowed)}. Use '{pick}'.",
                     suggested_fix={"op": "set_attribute", "path": path, "attribute": attr, "value": pick},
                     confidence="exact" if (value or "").lower() == pick.lower() else "choice")

    if (mm := _RE_PATTERN.search(msg)):
        pattern = mm.group(4)
        for cand, why in _pattern_candidates(value or "", ftype, m, default):
            if cand != value and _xsd_match(pattern, cand) is not False:
                rule = "colour-out-of-range" if ftype in _COLOUR_TYPES else "bad-value"
                return _hint(**base, rule=rule, type=ftype,
                             message=f"{tag} {attr}='{value}' is not a valid {ftype or 'value'}: {why}. "
                                     f"Use {attr}='{cand}'.",
                             suggested_fix={"op": "set_attribute", "path": path, "attribute": attr,
                                            "value": cand},
                             confidence="choice")
        return _hint(**base, rule="bad-value", type=ftype,
                     message=f"{tag} {attr}='{value}' does not match the {ftype or 'XSD'} pattern.")

    if (mm := _RE_RANGE.search(msg)):
        side, kind, bound = mm.group(3), mm.group(4), mm.group(6)
        cand = bound if kind == "Inclusive" else None
        if cand is None and default is not None:
            d, b = _floats(default), _floats(bound)
            if d and b and ((side == "min" and d[0] > b[0]) or (side == "max" and d[0] < b[0])):
                cand = default
        bound_txt = ("at least " if side == "min" else "at most ") if kind == "Inclusive" else \
            ("greater than " if side == "min" else "less than ")
        return _hint(**base, rule="out-of-range", type=ftype,
                     message=f"{tag} {attr} must be {bound_txt}{bound}; got '{value}'."
                             + (f" Use {attr}='{cand}'." if cand is not None else ""),
                     suggested_fix=({"op": "set_attribute", "path": path, "attribute": attr, "value": cand}
                                    if cand is not None else None),
                     confidence="choice")

    if (mm := _RE_ATOMIC.search(msg)):
        atype = mm.group(4) or ftype
        cand = None
        v = (value or "").strip()
        if (atype or ftype) == "SFBool" or ftype == "SFBool":
            cand = {"true": "true", "false": "false", "1": "true", "0": "false", "yes": "true",
                    "no": "false", "on": "true", "off": "false"}.get(v.lower().strip('"'))
        if cand is None:
            nums = _floats(v.replace('"', ""))
            if nums is not None and len(nums) == 1 and ftype in ("SFInt32",):
                cand = str(int(round(nums[0])))
            elif nums is not None and len(nums) == 1 and ftype in ("SFFloat", "SFDouble", "SFTime"):
                cand = _fmt(nums[0])
        if cand is None and default is not None:
            cand = default
        return _hint(**base, rule="bad-value", type=atype,
                     message=f"{tag} {attr}='{value}' is not a valid {atype or 'value'}."
                             + (f" Use {attr}='{cand}'." if cand is not None else ""),
                     suggested_fix=({"op": "set_attribute", "path": path, "attribute": attr, "value": cand}
                                    if cand is not None and cand != value else None),
                     confidence="choice")

    if _RE_VALUE.search(msg):
        if ftype in _COLOUR_TYPES:
            for cand, why in _pattern_candidates(value or "", ftype, m, default):
                if cand != value:
                    return _hint(**base, rule="colour-out-of-range", type=ftype,
                                 message=f"{tag} {attr} has components outside 0..1: {why}.",
                                 suggested_fix={"op": "set_attribute", "path": path, "attribute": attr,
                                                "value": cand},
                                 confidence="choice")
        return _hint(**base, rule="bad-value", type=ftype,
                     message=f"{tag} {attr} is not a valid {ftype or 'value'}: {_RE_VALUE.search(msg).group(3)}")

    if _RE_NOT_ALLOWED.search(msg):
        names = m.attribute_fields(tag)
        guess = _close(attr, [n for n in names if n not in el.attrib], 0.75)
        has = sorted(n for n, fs in m.fields.items() if attr in fs and n != tag
                     and m.ancestry(n) & m.ancestry(tag) - {"X3DNode", "X3DChildNode"})[:5]
        also = f" ({', '.join(has)} has a field '{attr}')" if has else ""
        if guess:
            return _hint(**base, rule="unknown-attribute", allowed_values=_cap(sorted(names)),
                         message=f"{tag} has no field '{attr}'. Did you mean '{guess}'?{also}",
                         suggested_fix={"op": "rename_attribute", "path": path, "from": attr, "to": guess},
                         confidence="choice")
        return _hint(**base, rule="unknown-attribute", allowed_values=_cap(sorted(names)), alternatives=has,
                     message=f"{tag} has no field '{attr}'; remove it.{also}",
                     suggested_fix={"op": "remove_attribute", "path": path, "attribute": attr},
                     confidence="exact" if not has else "choice")
    return None


def _route_endpoint_fields(doc, def_name: str, direction: str, m: _Model) -> tuple[str, dict]:
    from validation.semantic import _endpoint_fields, _proto_interfaces
    el = next((e for e in doc.iter() if e.get("DEF") == def_name), None)
    if el is None:
        return "", {}
    tag = _local(el)
    fields = _endpoint_fields(el, tag, m.nodes, _proto_interfaces(doc))
    want = ("outputOnly", "inputOutput") if direction == "out" else ("inputOnly", "inputOutput")
    return tag, {n: f for n, f in fields.items() if f.get("accessType") in want and f.get("type") not in ()}


def _route_field_choice(doc, route, which: str, m: _Model) -> tuple[str | None, list[str]]:
    """Choose a fromField/toField for a ROUTE from the endpoint's fields and the other end's type."""
    if which == "toField":
        node, other_node, other_field, direction = route.get("toNode"), route.get("fromNode"), route.get("fromField"), "in"
    else:
        node, other_node, other_field, direction = route.get("fromNode"), route.get("toNode"), route.get("toField"), "out"
    _, fields = _route_endpoint_fields(doc, node or "", direction, m)
    _, ofields = _route_endpoint_fields(doc, other_node or "", "out" if direction == "in" else "in", m)
    otype = None
    if other_field:
        from validation.semantic import _resolve_route_field
        info = _resolve_route_field(ofields, other_field, "out" if direction == "in" else "in")
        otype = info.get("type") if info else None
    cands = sorted(n for n, f in fields.items() if otype is None or f.get("type") == otype)
    if not cands:
        return None, []
    stem = re.sub(r"^set_|_changed$", "", other_field or "")
    named = [c for c in cands if re.sub(r"^set_|_changed$", "", c) == stem]
    current = route.get(which) or ""
    pick = (named[0] if named else None) or (_close(current, cands, 0.5) if current else None) \
        or (cands[0] if len(cands) == 1 else None)
    if pick is None:
        pick = cands[0]
    if direction == "in" and pick in fields and fields[pick].get("accessType") == "inputOutput" and current.startswith("set_"):
        pick = pick if pick.startswith("set_") else f"set_{pick}" if f"set_{pick}" not in fields else pick
    return pick, cands


def _required_hint(doc, el, attr: str, m: _Model, base: dict) -> dict:
    tag = _local(el)
    path = _path(el)
    base = dict(base, node=tag, field=attr, path=path, line=el.sourceline,
                parent=_local(el.getparent()) if el.getparent() is not None else "")
    value, why, allowed, conf = None, "", [], "choice"
    if tag == "X3D" and attr == "version":
        from validation.repair import _lowest_version_with, _scene_tags
        value = _lowest_version_with(_scene_tags(doc), None) or "4.1"
        why = "the lowest bundled X3D version that has every node in the scene"
        allowed, conf = ["3.0", "3.1", "3.2", "3.3", "4.0", "4.1"], "exact"
    elif tag == "X3D" and attr == "profile":
        from validation.profile import check_profile
        trial = copy.deepcopy(doc)
        for p in _PROFILE_ORDER:
            trial.set("profile", p)
            if check_profile(trial).ok:
                value = p
                break
        why = "the smallest standard profile that covers every node with the declared components"
        allowed, conf = _PROFILE_ORDER, "exact"
    elif tag == "ROUTE" and attr in ("fromField", "toField"):
        value, allowed = _route_field_choice(doc, el, attr, m)
        why = "a field of that node with the right access type and the other end's field type"
        conf = "exact" if len(allowed) == 1 else "choice"
    elif tag == "ROUTE" and attr in ("fromNode", "toNode"):
        defs = [e.get("DEF") for e in doc.iter() if e.get("DEF")]
        fld = el.get("fromField" if attr == "fromNode" else "toField") or ""
        direction = "out" if attr == "fromNode" else "in"
        from validation.semantic import _resolve_route_field
        allowed = [d for d in defs if _resolve_route_field(_route_endpoint_fields(doc, d, direction, m)[1], fld, direction)]
        value = allowed[0] if allowed else None
        why = f"a DEF whose node has a field '{fld}'"
        conf = "exact" if len(allowed) == 1 else "choice"
    elif tag == "ProtoInstance" and attr == "name":
        allowed = [e.get("name") for e in doc.iter() if _local(e) in _STATEMENTS_SCENE_START and e.get("name")]
        value = allowed[0] if allowed else None
        why = "a declared prototype"
    else:
        f = m.field(tag, attr)
        if f is not None and f.get("default") is not None:
            value, why = f["default"], "the X3DUOM default"
    return _hint(**base, rule="missing-attribute", allowed_values=_cap(allowed),
                 message=f"<{tag}> needs {attr}." + (f" Use {attr}='{value}' ({why})." if value else ""),
                 suggested_fix=({"op": "set_attribute", "path": path, "attribute": attr, "value": value}
                                if value else None),
                 confidence=conf)


def _missing_child_hint(doc, el, expected: list[str], m: _Model, base: dict) -> dict:
    tag = _local(el)
    base = dict(base, node=tag, path=_path(el), line=el.sourceline, allowed_children=expected)
    if tag == "GeoLOD":
        return _hint(**base, rule="missing-child",
                     message="GeoLOD inline children need a GeoOrigin sibling under the bundled XSD (see notes); "
                             "adding one re-anchors the frame, so it is left to you.")
    if len(expected) == 1:
        return _hint(**base, rule="missing-child",
                     message=f"<{tag}> needs a <{expected[0]}> child.",
                     suggested_fix={"op": "insert_element", "to_path": _path(el), "tag": expected[0],
                                    "position": "end"},
                     confidence="exact")
    return _hint(**base, rule="missing-child", message=f"<{tag}> is missing a required child: one of {', '.join(expected)}.")


# ---------------------------------------------------------------------------
# public: hints for XSD errors


def xsd_hints(doc, entries, schema_version: str) -> list[dict]:
    """Hints for XSD error log entries (objects with .message, .line, .path) on parsed `doc`.

    Returns one hint per error it understands, each with `error_index` pointing
    into the validator's `errors` list.
    """
    m = _model(schema_version)
    out = []
    for i, entry in enumerate(entries):
        msg, line, path = entry.message, entry.line, getattr(entry, "path", None)
        base = {"source": "xsd", "error_index": i}
        try:
            h = _one_xsd_hint(doc, msg, line, path, m, base)
        except Exception as exc:          # a hint must never break validation
            h = {"source": "xsd", "error_index": i, "rule": "internal", "message": f"hint failed: {exc}"}
        if h:
            out.append(h)
    return out


def _one_xsd_hint(doc, msg, line, path, m, base):
    if (mm := _RE_NOT_EXPECTED.search(msg)):
        tag = _strip_ns(mm.group(1))
        el = _find(doc, path, line, tag)
        if el is None:
            return None
        if mm.group(2):
            base["xsd_expected"] = [s.strip() for s in mm.group(2).split(",")]
        h = _placement_hint(doc, el, m, base)
        return h
    if (mm := _RE_MISSING_CHILD.search(msg)):
        el = _find(doc, path, line, _strip_ns(mm.group(1)))
        if el is None:
            return None
        exp = [s.strip() for s in (mm.group(2) or "").split(",") if s.strip()]
        return _missing_child_hint(doc, el, exp, m, base)
    if (mm := _RE_REQUIRED.search(msg)):
        el = _find(doc, path, line, _strip_ns(mm.group(1)))
        return _required_hint(doc, el, mm.group(2), m, base) if el is not None else None
    for rx in (_RE_ENUM, _RE_PATTERN, _RE_RANGE, _RE_ATOMIC, _RE_NOT_ALLOWED, _RE_VALUE):
        if (mm := rx.search(msg)):
            el = _find(doc, path, line, _strip_ns(mm.group(1)))
            if el is None:
                return None
            return _attribute_hint(doc, el, mm.group(2), msg, m, base)
    return None


# ---------------------------------------------------------------------------
# public: hints for semantic errors


def semantic_hints(xml_string: str, version: str | None = None) -> list[dict]:
    """Hints for the error-level semantic diagnostics of a schema-valid document.

    Covers containerField, ROUTE endpoint, ROUTE order, USE-before-DEF, colour range
    and profile/component errors. Each hint carries `check`, the semantic check name
    it answers.
    """
    from validation.semantic import _PARSER, _find_scene
    from validation.validate import resolve_xsd_version
    root = etree.fromstring(xml_string.encode("utf-8"), _PARSER)
    m = _model(resolve_xsd_version(version or root.get("version"))[0])
    scene = _find_scene(root)
    if scene is None:
        return []
    out: list[dict] = []
    out.extend(_containerfield_hints(root, scene, m))
    out.extend(_route_hints(root, scene, m))
    out.extend(_route_order_hints(scene))
    out.extend(_use_def_hints(scene))
    out.extend(_colour_hints(scene, m))
    out.extend(_profile_hints(root))
    return out


def _containerfield_hints(root, scene, m: _Model) -> list[dict]:
    from validation.autofix import _pick
    out = []
    for parent in scene.iter():
        ptag = _local(parent)
        if ptag not in m.nodes:
            continue
        pfields = m.fields.get(ptag, {})
        for child in parent:
            ctag = _local(child)
            if ctag not in m.nodes or ctag == "ProtoInstance":
                continue
            cf = child.get("containerField") or m.nodes[ctag]["containerField"]
            if not cf:
                continue
            field = pfields.get(cf)
            from validation.semantic import _field_accepts
            anc = m.ancestry(ctag)
            if field is not None and _field_accepts(field, anc):
                continue
            check = ("containerfield-unknown" if field is None else
                     "containerfield-not-node" if field.get("type") not in _NODE_TYPES else
                     "containerfield-type-mismatch")
            cands = [n for n, f in pfields.items() if _field_accepts(f, anc)]
            base = {"source": "semantic", "check": check}
            if not cands:
                h = _placement_hint(root, child, m, base)
                out.append(h)
                continue
            chosen = _pick(cands, child, pfields)
            out.append(_hint(**base, rule="wrong-containerfield", node=ctag, parent=ptag, field=cf,
                             line=child.sourceline, path=_path(child), allowed_values=cands,
                             message=f"{ptag} takes a {ctag} in {', '.join(cands)}, not '{cf}'. "
                                     f"Set containerField='{chosen}'.",
                             suggested_fix={"op": "set_attribute", "path": _path(child),
                                            "attribute": "containerField", "value": chosen},
                             confidence="exact" if len(cands) == 1 else "choice"))
    return out


def _route_hints(root, scene, m: _Model) -> list[dict]:
    from validation.semantic import _resolve_route_field
    out = []
    defs = [e.get("DEF") for e in scene.iter() if e.get("DEF")]
    defs += [e.get("AS") or e.get("importedDEF") for e in scene.iter()
             if _local(e) == "IMPORT" and (e.get("AS") or e.get("importedDEF"))]
    for route in scene.iter("ROUTE"):
        path = _path(route)
        base = {"source": "semantic", "node": "ROUTE", "path": path, "line": route.sourceline,
                "parent": _local(route.getparent())}
        for attr, check in (("fromNode", "route-missing-from-node"), ("toNode", "route-missing-to-node")):
            if route.get(attr, "") not in defs:
                pick = _close(route.get(attr, ""), defs, 0.5)
                out.append(_hint(**base, check=check, rule="route-endpoint", field=attr, allowed_values=_cap(defs),
                                 message=f"ROUTE {attr}='{route.get(attr, '')}' names no DEF."
                                         + (f" Did you mean '{pick}'?" if pick else ""),
                                 suggested_fix=({"op": "set_attribute", "path": path, "attribute": attr,
                                                 "value": pick} if pick else None),
                                 confidence="choice"))
                break
        else:
            for attr, direction, check in (("fromField", "out", "route-invalid-from-field"),
                                           ("toField", "in", "route-invalid-to-field")):
                node = route.get("fromNode" if attr == "fromField" else "toNode")
                tag, fields = _route_endpoint_fields(scene, node, direction, m)
                if not tag:
                    break
                from validation.semantic import _endpoint_fields, _proto_interfaces
                el = next(e for e in scene.iter() if e.get("DEF") == node)
                all_fields = _endpoint_fields(el, tag, m.nodes, _proto_interfaces(scene))
                info = _resolve_route_field(all_fields, route.get(attr, ""), direction)
                if info is None or info.get("accessType") not in (
                        ("outputOnly", "inputOutput") if direction == "out" else ("inputOnly", "inputOutput")):
                    pick, cands = _route_field_choice(scene, route, attr, m)
                    out.append(_hint(**base, check=check if info is None else "route-wrong-access-type",
                                     rule="route-field", field=attr, allowed_values=_cap(cands),
                                     message=f"ROUTE {attr}='{route.get(attr, '')}' is not a usable "
                                             f"{'output' if direction == 'out' else 'input'} field of {tag} "
                                             f"(DEF='{node}')." + (f" Use '{pick}'." if pick else ""),
                                     suggested_fix=({"op": "set_attribute", "path": path, "attribute": attr,
                                                     "value": pick} if pick else None),
                                     confidence="exact" if len(cands) == 1 else "choice"))
                    break
            else:
                _, ff = _route_endpoint_fields(scene, route.get("fromNode"), "out", m)
                _, tf = _route_endpoint_fields(scene, route.get("toNode"), "in", m)
                fi = _resolve_route_field(ff, route.get("fromField"), "out")
                ti = _resolve_route_field(tf, route.get("toField"), "in")
                if fi and ti and fi.get("type") and ti.get("type") and fi["type"] != ti["type"]:
                    pick, cands = _route_field_choice(scene, route, "toField", m)
                    out.append(_hint(**base, check="route-type-mismatch", rule="route-field", field="toField",
                                     allowed_values=_cap(cands),
                                     message=f"ROUTE carries {fi['type']} but toField '{route.get('toField')}' is "
                                             f"{ti['type']}." + (f" Use toField='{pick}'." if pick else ""),
                                     suggested_fix=({"op": "set_attribute", "path": path, "attribute": "toField",
                                                     "value": pick} if pick else None),
                                     confidence="choice"))
    return out


def _route_order_hints(scene) -> list[dict]:
    """route-before-def: move the ROUTE to the end of its name scope, after every node it can name."""
    from validation.semantic import _check_route_order, _name_scope
    early = {d.def_name for d in _check_route_order(scene) if d.check == "route-before-def"}
    out, done = [], set()
    for route in scene.iter():
        if _local(route) != "ROUTE" or route in done:
            continue
        names = [n for n in (route.get("fromNode"), route.get("toNode")) if n in early]
        if not names:
            continue
        scope = _name_scope(route)
        holder = scope if scope is not None else scene
        done.add(route)
        out.append(_hint(source="semantic", check="route-before-def", rule="route-before-def", node="ROUTE",
                         path=_path(route), line=route.sourceline, field="fromNode",
                         message=f"ROUTE comes before DEF='{names[0]}'. Move it to the end of "
                                 f"{_local(holder)}, after the nodes it names.",
                         suggested_fix={"op": "move", "path": _path(route), "to_path": _path(holder)},
                         confidence="exact"))
    return out


def _colour_hints(scene, m: _Model) -> list[dict]:
    """colour-out-of-range: the same 0..255 or clamp candidates the XSD colour hint offers."""
    from validation.semantic import _COLOUR_WIDTH, _colour_problem, _proto_interfaces
    protos = _proto_interfaces(scene)
    out = []
    for el in scene.iter():
        tag = _local(el)
        if tag in ("field", "fieldValue"):
            ftype = el.get("type", "")
            if tag == "fieldValue" and el.getparent() is not None:
                ftype = protos.get(el.getparent().get("name"), {}).get(el.get("name", ""), {}).get("type", "")
            pairs = [("value", ftype)] if el.get("value") is not None else []
        elif tag in m.nodes and not el.get("USE"):
            pairs = [(a, (m.field(tag, a) or {}).get("type", "")) for a in el.attrib]
        else:
            continue
        for attr, ftype in pairs:
            if ftype not in _COLOUR_WIDTH or not _colour_problem(el.get(attr), ftype):
                continue
            value = el.get(attr)
            cand, why = next(((c, w) for c, w in _pattern_candidates(value, ftype, m, None) if c != value),
                             (None, None))
            out.append(_hint(source="semantic", check="colour-out-of-range", rule="colour-out-of-range",
                             node=tag, path=_path(el), line=el.sourceline, field=attr, type=ftype,
                             message=f"{tag} {attr} has components outside 0..1" + (f": {why}." if why else "."),
                             suggested_fix=({"op": "set_attribute", "path": _path(el), "attribute": attr,
                                             "value": cand} if cand else None),
                             confidence="choice"))
    return out


def _use_def_hints(scene) -> list[dict]:
    out = []
    first_def = {}
    for el in scene.iter():
        if el.get("DEF") and el.get("DEF") not in first_def:
            first_def[el.get("DEF")] = el
    seen = set()
    for el in scene.iter():
        u = el.get("USE")
        if u and u not in seen and u in first_def:
            d = first_def[u]
            if d in set(el.iterancestors()) or el in set(d.iter()):
                continue
            out.append(_hint(source="semantic", check="use-before-def", rule="use-before-def", node=_local(el),
                             path=_path(el), line=el.sourceline, field="USE",
                             message=f"USE='{u}' comes before DEF='{u}'. Swap them: put the DEF node where the "
                                     f"first USE is, and a USE where the DEF was.",
                             suggested_fix={"op": "swap_def_use", "use_path": _path(el), "def_path": _path(d)},
                             confidence="exact"))
            seen.add(u)
        if el.get("DEF"):
            seen.add(el.get("DEF"))
    return out


def _profile_hints(root) -> list[dict]:
    from validation.profile import check_profile
    report = check_profile(root)
    if report.ok:
        return []
    needed: dict[str, int] = {}
    for mm in report.missing:
        if mm["component"]:
            needed[mm["component"]] = max(needed.get(mm["component"], 0), mm["level"])
    comps = [{"name": c, "level": lvl} for c, lvl in sorted(needed.items())]
    nodes = sorted({mm["node"] for mm in report.missing})
    return [_hint(source="semantic", check="profile-component", rule="profile-component", node=", ".join(nodes),
                  message=f"Nodes {', '.join(nodes)} are outside profile '{report.profile}'. Add "
                          + " ".join(f"<component name='{c['name']}' level='{c['level']}'/>" for c in comps)
                          + " to <head>.",
                  suggested_fix={"op": "add_components", "components": comps}, confidence="exact")]


# ---------------------------------------------------------------------------
# applying a fix


class FixError(ValueError):
    """A suggested fix could not be applied to this document."""


def _parse(xml_string: str):
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)
    return etree.fromstring(xml_string.encode("utf-8"), parser)


def _get(root, path: str):
    hits = root.getroottree().xpath(path)
    if not hits or not isinstance(hits[0], etree._Element):
        raise FixError(f"no element at {path}")
    return hits[0]


def _detach(el):
    """Remove `el` from its parent; whitespace after it goes with it."""
    if el.tail and el.tail.strip() == "":
        el.tail = None
    el.getparent().remove(el)
    return el


def apply_fix(xml_string: str, fix: dict) -> str:
    """Apply one `suggested_fix` to an X3D XML document and return the new document."""
    root = _parse(xml_string)
    op = fix.get("op")
    if op in ("set_attribute", "remove_attribute", "rename_attribute", "rename_element", "remove_element",
              "move", "wrap"):
        el = _get(root, fix["path"])
    if op == "set_attribute":
        el.set(fix["attribute"], str(fix["value"]))
    elif op == "remove_attribute":
        el.attrib.pop(fix["attribute"], None)
    elif op == "rename_attribute":
        v = el.attrib.pop(fix["from"])
        el.set(fix["to"], v)
    elif op == "rename_element":
        el.tag = fix["to"]
    elif op == "remove_element":
        _detach(el)
    elif op == "move":
        target = None
        if fix.get("create"):
            target = etree.Element(fix["create"])
            parent = _get(root, fix["to_path"])
            parent.insert(0, target)
        else:
            target = _get(root, fix["to_path"])
        after = _get(root, fix["after_path"]) if fix.get("after_path") else None
        if target is el or el in set(target.iterancestors()):
            raise FixError("cannot move a node into itself")
        _detach(el)
        if fix.get("containerField"):
            el.set("containerField", fix["containerField"])
        if after is not None:
            after.addnext(el)
            el.tail = after.tail
        elif fix.get("position") == "start":
            target.insert(0, el)
        else:
            target.append(el)
    elif op == "wrap":
        parent = el.getparent()
        idx = parent.index(el)
        tail = el.tail
        parent.remove(el)
        outer = etree.Element(fix["wrappers"][0])
        if fix.get("wrapper_containerField"):
            outer.set("containerField", fix["wrapper_containerField"])
        holder = outer
        for w in fix["wrappers"][1:]:
            holder = etree.SubElement(holder, w)
        if fix.get("containerField"):
            el.set("containerField", fix["containerField"])
        el.tail = None
        holder.append(el)
        outer.tail = tail
        parent.insert(idx, outer)
    elif op == "insert_element":
        target = _get(root, fix["to_path"])
        new = etree.Element(fix["tag"])
        if fix.get("position") == "start":
            target.insert(0, new)
        else:
            target.append(new)
    elif op == "add_components":
        head = next((c for c in root if _local(c) == "head"), None)
        if head is None:
            head = etree.Element("head")
            root.insert(0, head)
        existing = {c.get("name"): c for c in head if _local(c) == "component"}
        n = len(existing)
        for comp in fix["components"]:
            if comp["name"] in existing:
                cur = existing[comp["name"]]
                if int(cur.get("level", "0") or 0) < int(comp["level"]):
                    cur.set("level", str(comp["level"]))
            else:
                head.insert(n, etree.Element("component", name=comp["name"], level=str(comp["level"])))
                n += 1
    elif op == "swap_def_use":
        use_el, def_el = _get(root, fix["use_path"]), _get(root, fix["def_path"])
        name = def_el.get("DEF")
        stub = etree.Element(_local(def_el), USE=name)
        if def_el.get("containerField"):
            stub.set("containerField", def_el.get("containerField"))
        stub.tail = def_el.tail
        moved = copy.deepcopy(def_el)
        moved.tail = use_el.tail
        if use_el.get("containerField"):
            moved.set("containerField", use_el.get("containerField"))
        elif def_el.get("containerField"):
            del moved.attrib["containerField"]
        def_el.getparent().replace(def_el, stub)
        use_el.getparent().replace(use_el, moved)
    else:
        raise FixError(f"unknown op {op!r}")
    tree = root.getroottree()
    body = etree.tostring(tree, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body


# ---------------------------------------------------------------------------
# convenience: check + hints, and a hint-only repair loop


def gate(xml_string: str) -> dict:
    """XSD validity plus error-level semantic diagnostics (the repair loop's gate).

    Returns {valid, errors, hints}; semantic errors are checked only once the XSD
    passes, as in the evaluation harness.
    """
    from validation.semantic import semantic_diagnostics
    from validation.validate import validate_xml_with_hints
    v = validate_xml_with_hints(xml_string)
    if not v["valid"]:
        return {"valid": False, "errors": list(v["errors"]), "hints": v.get("hints", []), "stage": "xsd"}
    try:
        diags = [d for d in semantic_diagnostics(xml_string) if d.level == "error"]
    except Exception as exc:
        return {"valid": False, "errors": [f"semantic: {exc}"], "hints": [], "stage": "semantic"}
    errors = [f"[{d.check}] {d.message}" for d in diags]
    hints = semantic_hints(xml_string) if diags else []
    for h in hints:                     # point each hint at the first diagnostic of its check
        idx = next((i for i, d in enumerate(diags) if d.check == h.get("check")), None)
        if idx is not None:
            h["error_index"] = idx
    return {"valid": not diags, "errors": errors, "hints": hints, "stage": "semantic"}


def repair_with_hints(xml_string: str, max_steps: int = 30) -> dict:
    """Apply suggested fixes one at a time, re-checking after each, until the gate passes.

    Returns {fixed, valid, applied, remaining_errors}. Only hint fixes are used.
    A fix that fails to apply, or leaves the same error first, is skipped.
    """
    doc = xml_string
    applied, tried = [], set()
    state = gate(doc)
    for _ in range(max_steps):
        if state["valid"]:
            break
        hint = next((h for h in state["hints"] if h.get("suggested_fix")
                     and json.dumps(h["suggested_fix"], sort_keys=True) not in tried), None)
        if hint is None:
            break
        key = json.dumps(hint["suggested_fix"], sort_keys=True)
        tried.add(key)
        try:
            new = apply_fix(doc, hint["suggested_fix"])
        except (FixError, etree.XMLSyntaxError, KeyError):
            continue
        doc = new
        applied.append({"rule": hint.get("rule"), "fix": hint["suggested_fix"]})
        state = gate(doc)
    return {"fixed": doc, "valid": state["valid"], "applied": applied, "remaining_errors": state["errors"]}
