"""Semantic validation for X3D scenes beyond XSD schema checks.

Detects common authoring issues that XSD cannot catch: nodes outside the
declared profile and component statements, missing geometry, unused DEFs,
broken ROUTE references, empty groups, duplicate DEF names, and missing
viewpoints.

Adapted from semantic_check.py in https://github.com/niknarra/x3d-mcp by
Nikhil Narra and Nicholas Polys (Virginia Tech / Web3D Consortium).
"""

import math
import re
from dataclasses import dataclass
from lxml import etree

from x3d_utils.x3duom import get_x3duom


_GROUPING_NODES = {
    "Transform", "Group", "Switch", "Collision", "LOD", "Anchor",
    "Billboard", "StaticGroup", "CADAssembly", "CADLayer", "CADPart",
    "GeoLOD", "EspduTransform", "ReceiverPdu", "SignalPdu",
    "TransmitterPdu", "HAnimJoint", "HAnimSegment", "HAnimSite",
    "LayoutGroup", "ScreenGroup", "Viewport",
}

_GEOMETRY_NODES: set[str] | None = None


def _get_geometry_nodes() -> set[str]:
    """Return the set of concrete node names that can serve as geometry in a Shape.

    Walks X3DUOM inheritance to find every node ultimately based on
    X3DGeometryNode, then unions a hardcoded core set for safety.
    """
    global _GEOMETRY_NODES
    if _GEOMETRY_NODES is not None:
        return _GEOMETRY_NODES

    uom = get_x3duom()
    nodes = uom.get_concrete_nodes()
    abstracts = uom.get_abstract_types()

    geometry_types: set[str] = set()
    for name, info in nodes.items():
        if _inherits_from(info, "X3DGeometryNode", nodes, abstracts):
            geometry_types.add(name)

    geometry_types.update({
        "Box", "Sphere", "Cylinder", "Cone", "Text",
        "IndexedFaceSet", "IndexedLineSet", "IndexedTriangleSet",
        "PointSet", "LineSet", "TriangleSet",
        "ElevationGrid", "Extrusion",
    })

    _GEOMETRY_NODES = geometry_types
    return _GEOMETRY_NODES


def _inherits_from(info: dict, target: str, nodes: dict, abstracts: dict,
                   visited: set | None = None) -> bool:
    """Walk the baseType chain to determine if a node inherits from target."""
    if visited is None:
        visited = set()
    base = info.get("baseType")
    if not base or base in visited:
        return False
    visited.add(base)
    if base == target:
        return True
    parent = abstracts.get(base) or nodes.get(base)
    if parent is None:
        return False
    return _inherits_from(parent, target, nodes, abstracts, visited)


@dataclass
class Diagnostic:
    level: str
    check: str
    message: str
    node_tag: str = ""
    def_name: str = ""


def _local_tag(el: etree._Element) -> str:
    """Strip XML namespace prefix from an element tag.

    Returns an empty string for non-element nodes (Comment,
    ProcessingInstruction, Entity) whose `el.tag` is a Cython
    callable rather than a string.
    """
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _check_duplicate_defs(scene: etree._Element) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    seen: dict[str, str] = {}
    for el in scene.iter():
        def_name = el.get("DEF")
        if not def_name:
            continue
        tag = _local_tag(el)
        if def_name in seen:
            diagnostics.append(Diagnostic(
                level="error",
                check="duplicate-def",
                message=f"Duplicate DEF name '{def_name}': first used on {seen[def_name]}, "
                        f"also used on {tag}. DEF names must be unique within a scene.",
                node_tag=tag,
                def_name=def_name,
            ))
        else:
            seen[def_name] = tag
    return diagnostics


def _check_def_use_consistency(scene: etree._Element) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    defs: dict[str, str] = {}
    uses: set[str] = set()
    referenced: set[str] = set()   # DEFs named by ROUTE, IMPORT/EXPORT or Script fields
    for el in scene.iter():
        tag = _local_tag(el)
        def_name = el.get("DEF")
        if def_name and def_name not in defs:
            defs[def_name] = tag
        use_name = el.get("USE")
        if use_name:
            uses.add(use_name)
        if tag == "ROUTE":
            referenced.update(n for n in (el.get("fromNode"), el.get("toNode")) if n)
        elif tag == "EXPORT":
            if el.get("localDEF"):
                referenced.add(el.get("localDEF"))
        elif tag == "IMPORT":
            # IMPORT names the Inline by DEF (inlineDEF), e.g. <IMPORT inlineDEF='Inl' importedDEF='X'/>
            if el.get("inlineDEF"):
                referenced.add(el.get("inlineDEF"))

    for use_name in sorted(uses):
        if use_name not in defs:
            available = ", ".join(sorted(defs.keys())) or "(none)"
            diagnostics.append(Diagnostic(
                level="error",
                check="use-undefined-def",
                message=f"USE='{use_name}' references a DEF that does not exist in this scene. "
                        f"Available DEF names: {available}.",
                def_name=use_name,
            ))

    for def_name in sorted(set(defs.keys()) - uses - referenced):
        diagnostics.append(Diagnostic(
            level="info",
            check="unused-def",
            message=f"DEF='{def_name}' ({defs[def_name]}) is defined but never USE'd "
                    f"and no ROUTE, IMPORT or EXPORT names it. "
                    f"This is fine if you reference it externally (Script, SAI).",
            node_tag=defs[def_name],
            def_name=def_name,
        ))

    return diagnostics


def _check_shape_completeness(scene: etree._Element) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    geometry_nodes = _get_geometry_nodes()
    for shape in scene.iter("Shape"):
        if shape.get("USE"):
            # A USE reference reuses the DEF'd Shape wholesale, geometry and
            # appearance included; it is childless by definition.
            continue
        def_name = shape.get("DEF", "")
        child_tags = {_local_tag(child) for child in shape}
        has_geometry = bool(child_tags & geometry_nodes)
        has_appearance = "Appearance" in child_tags
        label = f" (DEF={def_name!r})" if def_name else ""
        if not has_geometry:
            diagnostics.append(Diagnostic(
                level="warning",
                check="shape-no-geometry",
                message=f"Shape{label} has no geometry child. "
                        f"Add a geometry node like Box, Sphere, or IndexedFaceSet.",
                node_tag="Shape",
                def_name=def_name,
            ))
        if not has_appearance:
            diagnostics.append(Diagnostic(
                level="info",
                check="shape-no-appearance",
                message=f"Shape{label} has no Appearance. "
                        f"The shape will render with a default white material.",
                node_tag="Shape",
                def_name=def_name,
            ))
    return diagnostics


# HAnim grouping nodes are meaningful even when childless: an HAnimJoint
# defines an articulation center, and HAnimSegment/HAnimSite mark named body
# locations -- LOA-3/LOA-4 skeletons legitimately have many empty leaf joints.
_EMPTY_GROUP_EXEMPT = {"HAnimJoint", "HAnimSegment", "HAnimSite"}


def _check_empty_groups(scene: etree._Element) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for tag in _GROUPING_NODES:
        if tag in _EMPTY_GROUP_EXEMPT:
            continue
        for el in scene.iter(tag):
            if len(el) == 0:
                if el.get("USE"):
                    # A USE reference reuses the DEF'd node wholesale; it is
                    # intentionally childless and not an empty group.
                    continue
                def_name = el.get("DEF", "")
                label = f" (DEF={def_name!r})" if def_name else ""
                diagnostics.append(Diagnostic(
                    level="warning",
                    check="empty-group",
                    message=f"{tag}{label} has no children. "
                            f"Empty grouping nodes have no effect.",
                    node_tag=tag,
                    def_name=def_name,
                ))
    return diagnostics


def _resolve_route_field(fields: dict, field_name: str, direction: str) -> dict | None:
    """Look up a ROUTE field, honoring X3D event-model aliases.

    Every inputOutput field X implicitly provides an inputOnly set_X and an
    outputOnly X_changed (ISO 19775-1 clause 4.4.2.2), so ROUTEs may name
    either form. direction is "in" for toField, "out" for fromField.
    """
    info = fields.get(field_name)
    if info is not None:
        return info
    if direction == "in" and field_name.startswith("set_"):
        base = fields.get(field_name[len("set_"):])
        if base is not None and base.get("accessType") == "inputOutput":
            return base
    if direction == "out" and field_name.endswith("_changed"):
        base = fields.get(field_name[: -len("_changed")])
        if base is not None and base.get("accessType") == "inputOutput":
            return base
    return None


# Nodes whose interface is extended by user-declared <field> children: Script and
# the programmable shaders (X3DProgrammableShaderObject: ComposedShader,
# PackagedShader, ShaderProgram), whose fields become GLSL uniforms.
_DECLARED_FIELD_NODES = ("Script", "ComposedShader", "PackagedShader", "ShaderProgram")


def _script_fields(el: etree._Element) -> dict:
    """A Script's or shader's interface is user-declared via <field> children
    (name/type/accessType), not in X3DUOM. Extract it so ROUTEs to/from those
    fields validate."""
    out: dict[str, dict] = {}
    for child in el:
        if _local_tag(child) == "field" and child.get("name"):
            out[child.get("name")] = {"name": child.get("name"),
                                      "type": child.get("type", ""),
                                      "accessType": child.get("accessType", "")}
    return out


def _proto_interfaces(scene: etree._Element) -> dict:
    """{protoName: {field: info}} from ProtoDeclare/ExternProtoDeclare. A
    ProtoInstance exposes exactly its declared interface, which X3DUOM cannot know."""
    out: dict[str, dict] = {}
    for el in scene.iter():
        tag, name = _local_tag(el), el.get("name")
        if not name:
            continue
        if tag == "ProtoDeclare":
            fields: dict[str, dict] = {}
            for sub in el:
                if _local_tag(sub) == "ProtoInterface":
                    for f in sub:
                        if _local_tag(f) == "field" and f.get("name"):
                            fields[f.get("name")] = {"name": f.get("name"),
                                "type": f.get("type", ""), "accessType": f.get("accessType", "")}
            out[name] = fields
        elif tag == "ExternProtoDeclare":
            out[name] = {f.get("name"): {"name": f.get("name"), "type": f.get("type", ""),
                                         "accessType": f.get("accessType", "")}
                         for f in el if _local_tag(f) == "field" and f.get("name")}
    return out


def _endpoint_fields(el: etree._Element, tag: str, nodes: dict, protos: dict) -> dict:
    """Resolve a ROUTE endpoint's fields, honoring the DYNAMIC interfaces X3DUOM
    misses: a Script's or shader's declared <field>s and a ProtoInstance's interface."""
    fields = {f["name"]: f for f in nodes.get(tag, {}).get("fields", [])}
    if tag in _DECLARED_FIELD_NODES:
        fields.update(_script_fields(el))     # declared fields augment the built-ins
    else:
        # <ProtoInstance name="P"> exposes ProtoDeclare P's interface; the shorthand
        # <P .../> element form is kept for hand-written scenes.
        pname = el.get("name") if tag == "ProtoInstance" else tag
        if pname in protos:
            fields = {**protos[pname], **fields}   # a proto instance == its interface
    return fields


def _check_route_validity(scene: etree._Element) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    uom = get_x3duom()
    nodes = uom.get_concrete_nodes()
    proto_ifaces = _proto_interfaces(scene)

    def_map: dict[str, tuple[etree._Element, str]] = {}
    for el in scene.iter():
        def_name = el.get("DEF")
        if def_name:
            def_map[def_name] = (el, _local_tag(el))

    for route in scene.iter("ROUTE"):
        from_node = route.get("fromNode", "")
        from_field = route.get("fromField", "")
        to_node = route.get("toNode", "")
        to_field = route.get("toField", "")

        if from_node not in def_map:
            available = ", ".join(sorted(def_map.keys())) or "(none)"
            diagnostics.append(Diagnostic(
                level="error",
                check="route-missing-from-node",
                message=f"ROUTE fromNode='{from_node}' not found. "
                        f"Available DEFs: {available}.",
            ))
            continue

        if to_node not in def_map:
            available = ", ".join(sorted(def_map.keys())) or "(none)"
            diagnostics.append(Diagnostic(
                level="error",
                check="route-missing-to-node",
                message=f"ROUTE toNode='{to_node}' not found. "
                        f"Available DEFs: {available}.",
            ))
            continue

        from_el, from_type = def_map[from_node]
        to_el, to_type = def_map[to_node]

        # A ProtoInstance whose ProtoDeclare/ExternProtoDeclare is not in this file
        # (e.g. an unresolved EXTERNPROTO) has an interface we cannot know: skip.
        if any(t == "ProtoInstance" and e.get("name") not in proto_ifaces
               for e, t in ((from_el, from_type), (to_el, to_type))):
            continue

        from_fields = _endpoint_fields(from_el, from_type, nodes, proto_ifaces)
        to_fields = _endpoint_fields(to_el, to_type, nodes, proto_ifaces)

        from_field_info = _resolve_route_field(from_fields, from_field, "out")
        if from_field_info is None:
            diagnostics.append(Diagnostic(
                level="error",
                check="route-invalid-from-field",
                message=f"ROUTE fromField='{from_field}' does not exist on {from_type} "
                        f"(DEF='{from_node}').",
            ))
            continue

        to_field_info = _resolve_route_field(to_fields, to_field, "in")
        if to_field_info is None:
            diagnostics.append(Diagnostic(
                level="error",
                check="route-invalid-to-field",
                message=f"ROUTE toField='{to_field}' does not exist on {to_type} "
                        f"(DEF='{to_node}').",
            ))
            continue

        from_access = from_field_info.get("accessType", "")
        if from_access not in ("outputOnly", "inputOutput"):
            diagnostics.append(Diagnostic(
                level="error",
                check="route-wrong-access-type",
                message=f"ROUTE fromField='{from_field}' on {from_type} has "
                        f"accessType='{from_access}' -- must be 'outputOnly' or "
                        f"'inputOutput' to be a ROUTE source.",
            ))

        to_access = to_field_info.get("accessType", "")
        if to_access not in ("inputOnly", "inputOutput"):
            diagnostics.append(Diagnostic(
                level="error",
                check="route-wrong-access-type",
                message=f"ROUTE toField='{to_field}' on {to_type} has "
                        f"accessType='{to_access}' -- must be 'inputOnly' or "
                        f"'inputOutput' to be a ROUTE destination.",
            ))

        from_type_name = from_field_info.get("type", "")
        to_type_name = to_field_info.get("type", "")
        if from_type_name and to_type_name and from_type_name != to_type_name:
            diagnostics.append(Diagnostic(
                level="error",
                check="route-type-mismatch",
                message=f"ROUTE type mismatch: {from_node}.{from_field} is {from_type_name} "
                        f"but {to_node}.{to_field} is {to_type_name}. "
                        f"ROUTE requires matching field types.",
            ))

    return diagnostics


_NODE_FIELD_TYPES = {"SFNode", "MFNode"}


def _type_ancestry(typename: str, nodes: dict, abstracts: dict) -> set[str]:
    """Return {typename} plus every abstract base it inherits from, including the
    X3DUOM AdditionalInheritance interfaces (MetadataString is an X3DMetadataObject
    only that way)."""
    chain = {typename}
    todo = [typename]
    while todo:
        name = todo.pop()
        info = nodes.get(name) or abstracts.get(name)
        if not info:
            continue
        for base in [info.get("baseType"), *info.get("additionalBaseTypes", [])]:
            if base and base not in chain:
                chain.add(base)
                todo.append(base)
    return chain


def _node_field_map(typename: str, nodes: dict) -> dict:
    return {f["name"]: f for f in nodes.get(typename, {}).get("fields", [])}


def _field_accepts(field: dict, ancestry: set[str]) -> bool:
    """True if a parent field can hold a node whose type ancestry is given."""
    if field.get("type") not in _NODE_FIELD_TYPES:
        return False
    acc = field.get("acceptableNodeTypes")
    if not acc:
        return True                      # unspecified -> be lenient
    # acceptableNodeTypes is '|'-separated (some sources space-separate); accept both.
    return bool(ancestry & set(re.split(r"[|\s]+", acc.strip())))


def _check_containerfield(scene: etree._Element) -> list[Diagnostic]:
    """Verify every child node's containerField names a parent field that accepts it.

    The single most common silent failure: a node's *default* containerField does
    not fit the parent it's placed under, so a conforming viewer files it in the
    wrong field (or drops it). Examples this catches: an ImageTexture (default
    'texture') inside a PhysicalMaterial (needs baseTexture/normalTexture/...), or
    an HAnimJoint (default 'children') used as an HAnimHumanoid skeleton root
    (needs 'skeleton'). X3D requires an explicit containerField in these cases.
    """
    diagnostics: list[Diagnostic] = []
    uom = get_x3duom()
    nodes = uom.get_concrete_nodes()
    abstracts = uom.get_abstract_types()

    for parent in scene.iter():
        ptag = _local_tag(parent)
        if ptag not in nodes:
            continue
        pfields = _node_field_map(ptag, nodes)
        for child in parent:
            ctag = _local_tag(child)
            if ctag not in nodes:        # skip ROUTE/field/IS/meta/comments
                continue
            if ctag == "ProtoInstance":  # a prototype instance stands in for any node type
                continue
            cf = child.get("containerField") or nodes[ctag]["containerField"]
            if not cf:
                continue
            anc = _type_ancestry(ctag, nodes, abstracts)
            explicit = child.get("containerField") is not None
            candidates = [n for n, f in pfields.items() if _field_accepts(f, anc)]
            suggest = ""
            if candidates:
                head = f"containerField='{candidates[0]}'"
                more = f" (or {', '.join(candidates[1:])})" if len(candidates) > 1 else ""
                suggest = f" Use {head}{more} to place a {ctag} here."
            field = pfields.get(cf)
            hint = "" if explicit else (
                f" {ctag}'s default containerField is '{cf}', which does not fit a {ptag} "
                f"-- an explicit containerField is required here.")
            if field is None:
                diagnostics.append(Diagnostic(
                    level="error", check="containerfield-unknown",
                    message=f"{ctag} containerField='{cf}' but {ptag} has no field "
                            f"named '{cf}'.{hint}{suggest}",
                    node_tag=ctag, def_name=child.get("DEF", "")))
            elif field.get("type") not in _NODE_FIELD_TYPES:
                diagnostics.append(Diagnostic(
                    level="error", check="containerfield-not-node",
                    message=f"{ctag} containerField='{cf}' targets {ptag}.{cf}, which is a "
                            f"{field.get('type')} value field, not a node container.{suggest}",
                    node_tag=ctag, def_name=child.get("DEF", "")))
            elif not _field_accepts(field, anc):
                acc = field.get("acceptableNodeTypes", "")
                alt = [c for c in candidates if c != cf]
                s2 = (f" Use containerField='{alt[0]}'." if alt else "")
                diagnostics.append(Diagnostic(
                    level="error", check="containerfield-type-mismatch",
                    message=f"{ptag}.{cf} does not accept a {ctag} (accepts: {acc}).{s2}",
                    node_tag=ctag, def_name=child.get("DEF", "")))
    return diagnostics


def _check_use_before_def(scene: etree._Element) -> list[Diagnostic]:
    """A USE must reference a DEF that PRECEDES it in document order (ISO 19775-1).

    The existing DEF/USE check confirms the DEF exists somewhere; this catches the
    distinct failure where the DEF is authored *after* the USE -- valid-looking but
    unresolvable in a single-pass reader.
    """
    diagnostics: list[Diagnostic] = []
    all_defs = {el.get("DEF") for el in scene.iter() if el.get("DEF")}
    seen: set[str] = set()
    for el in scene.iter():
        use_name = el.get("USE")
        if use_name and use_name not in seen and use_name in all_defs:
            diagnostics.append(Diagnostic(
                level="error", check="use-before-def",
                message=f"USE='{use_name}' ({_local_tag(el)}) appears before its DEF in "
                        f"document order. A USE must follow the DEF it references.",
                node_tag=_local_tag(el), def_name=use_name))
        def_name = el.get("DEF")
        if def_name:
            seen.add(def_name)
    return diagnostics


# Floats per key for each interpolator's keyValue (None = a variable multiple of
# the base tuple, e.g. CoordinateInterpolator stores numCoords*3 per key).
_INTERP_ARITY = {
    "ScalarInterpolator": 1, "SplineScalarInterpolator": 1,
    "PositionInterpolator2D": 2, "SplinePositionInterpolator2D": 2,
    "PositionInterpolator": 3, "SplinePositionInterpolator": 3, "GeoPositionInterpolator": 3,
    "ColorInterpolator": 3,
    "OrientationInterpolator": 4, "SquadOrientationInterpolator": 4,
    "CoordinateInterpolator": None, "NormalInterpolator": None,
    "CoordinateInterpolator2D": None,
}
_INTERP_BASE = {"CoordinateInterpolator": 3, "NormalInterpolator": 3,
                "CoordinateInterpolator2D": 2}


def _count(attr: str) -> int:
    return len([x for x in re.split(r"[\s,]+", attr.strip()) if x])


def _check_interpolator_keys(scene: etree._Element) -> list[Diagnostic]:
    """key and keyValue array lengths must agree (ISO 19775-1 19.x).

    A mismatch is a common silent animation bug: the interpolator clamps or
    produces garbage, and nothing warns. e.g. an OrientationInterpolator needs
    exactly 4 keyValue floats (one SFRotation) per key.
    """
    diagnostics: list[Diagnostic] = []
    for tag, arity in _INTERP_ARITY.items():
        for el in scene.iter(tag):
            key, kv = el.get("key"), el.get("keyValue")
            if key is None or kv is None:
                continue
            nk, nv = _count(key), _count(kv)
            label = f" (DEF={el.get('DEF')!r})" if el.get("DEF") else ""
            if nk == 0:
                continue
            if nv % nk != 0:
                diagnostics.append(Diagnostic(
                    level="error", check="interpolator-key-length",
                    message=f"{tag}{label}: keyValue has {nv} values, not divisible by the "
                            f"{nk} key(s). Each key needs a whole keyValue entry.",
                    node_tag=tag, def_name=el.get("DEF", "")))
                continue
            per = nv // nk
            if arity is not None and per != arity:
                diagnostics.append(Diagnostic(
                    level="error", check="interpolator-key-length",
                    message=f"{tag}{label}: expected {arity} keyValue float(s) per key "
                            f"({nk} keys -> {nk * arity}), got {per} per key ({nv} total).",
                    node_tag=tag, def_name=el.get("DEF", "")))
            elif arity is None:
                base = _INTERP_BASE.get(tag, 1)
                if per % base != 0:
                    diagnostics.append(Diagnostic(
                        level="error", check="interpolator-key-length",
                        message=f"{tag}{label}: {per} keyValue float(s) per key is not a "
                                f"multiple of {base} (one coordinate is {base} floats).",
                        node_tag=tag, def_name=el.get("DEF", "")))
    return diagnostics


def _check_tuple_counts(scene: etree._Element) -> list[Diagnostic]:
    """Each MF tuple field (MFVec3f, MFRotation, MFColor, ...) needs a whole number of tuples.

    The X3D 4.0 and 4.1 XSD patterns allow an empty separator between tuples, so a
    digit run can be split across two tuples: point="1 2 34 5 6" (five numbers)
    validates as two SFVec3f values. Counting the numbers catches it.
    GaussianSplats is skipped here; _check_gaussian_splats counts its arrays.
    """
    uom = get_x3duom()
    nodes, types = uom.get_concrete_nodes(), uom.get_field_types()
    diagnostics: list[Diagnostic] = []
    for el in scene.iter():
        tag = _local_tag(el)
        if tag not in nodes or tag == "GaussianSplats":
            continue
        fields = _node_field_map(tag, nodes)
        for name, value in el.attrib.items():
            info = types.get(fields.get(name, {}).get("type", ""))
            if not info or not info["isArray"] or info["tupleSize"] < 2:
                continue
            n, size = _count(value), info["tupleSize"]
            if n % size:
                label = f" (DEF={el.get('DEF')!r})" if el.get("DEF") else ""
                diagnostics.append(Diagnostic(
                    level="error", check="tuple-count",
                    message=f"{tag}{label} {name}: {n} numbers is not a whole number of "
                            f"{fields[name]['type']} values ({size} numbers each). The XSD "
                            f"pattern lets this through when a number splits across two tuples.",
                    node_tag=tag, def_name=el.get("DEF", "")))
    return diagnostics


_ALWAYS_FALSE = {("BooleanFilter", "inputFalse")}
_TRUE_ONLY = {("IntegerTrigger", "set_boolean"), ("TimeTrigger", "set_boolean"),
              ("BooleanToggle", "set_boolean")}


def _check_dead_boolean_routes(scene: etree._Element) -> list[Diagnostic]:
    """A ROUTE from BooleanFilter.inputFalse into a set_boolean that acts only on TRUE never fires.

    inputFalse only ever sends FALSE. In X3D 4.0 IntegerTrigger and TimeTrigger fire
    only when set_boolean is TRUE (X_ITE keeps any-value triggering for files declared
    3.3 or earlier), and BooleanToggle only toggles on TRUE. The scene validates and a
    still frame looks right, but the "off" half of a toggle or hover never happens.
    Found by the X3D-Bench behaviour harness in two writers' toggle and hover scenes.
    """
    version = scene.getroottree().getroot().get("version", "")
    try:
        legacy = float(version) <= 3.3
    except ValueError:
        legacy = False
    defs = {el.get("DEF"): _local_tag(el) for el in scene.iter()
            if isinstance(el.tag, str) and el.get("DEF")}
    diagnostics: list[Diagnostic] = []
    for route in scene.iter("ROUTE", "{*}ROUTE"):
        src = (defs.get(route.get("fromNode")), route.get("fromField"))
        dst = (defs.get(route.get("toNode")), route.get("toField"))
        if src not in _ALWAYS_FALSE or dst not in _TRUE_ONLY:
            continue
        if legacy and dst[0] != "BooleanToggle":
            continue
        diagnostics.append(Diagnostic(
            level="warning", check="route-never-fires",
            message=f"ROUTE {route.get('fromNode')}.inputFalse -> {route.get('toNode')}.set_boolean "
                    f"never fires: inputFalse only sends FALSE and {dst[0]} acts only on TRUE"
                    f"{' in X3D 4.0' if dst[0] != 'BooleanToggle' else ''}. To react when the input "
                    f"turns FALSE, route inputNegate instead.",
            node_tag="ROUTE", def_name=route.get("toNode", "")))
    return diagnostics


_SPLAT_ARRAYS = {"positions": 3, "scales": 3, "orientations": 4, "opacities": 1}
_SPLAT_SH = {(d, c): f"sphericalHarmonicsDegree{d}Coef{c}" for d in range(4) for c in range(2 * d + 1)}
_SPLAT_ORDER_MIN = 100  # too few splats to guess the quaternion order below this


def _floats(attr: str) -> list[float]:
    try:
        return [float(x) for x in re.split(r"[\s,]+", attr.strip()) if x]
    except ValueError:
        return []


def _check_gaussian_splats(scene: etree._Element) -> list[Diagnostic]:
    """GaussianSplats arrays must agree on the splat count, and hold values in the right space.

    The X3D 4.1 draft node stores one splat per entry of positions, scales,
    orientations, opacities and each sphericalHarmonicsDegree{d}Coef{c} field.
    Neither the XSD nor X3DUOM ties those lengths together, and the classic
    writer mistakes (ln(scale) or logit(opacity) copied straight from a 3DGS PLY,
    a partly written SH degree) all validate. These checks catch them. The
    quaternion order (x y z w versus w x y z) can only be guessed from the data:
    trainers start every splat at the identity, so a trained file usually keeps w
    as the largest component, and a w x y z file shows it in the first slot instead.
    """
    diagnostics: list[Diagnostic] = []
    for el in scene.iter("GaussianSplats", "{*}GaussianSplats"):
        label = f" (DEF={el.get('DEF')!r})" if el.get("DEF") else ""

        def report(level: str, check: str, message: str) -> None:
            diagnostics.append(Diagnostic(level=level, check=check,
                                          message=f"GaussianSplats{label}: {message}",
                                          node_tag="GaussianSplats", def_name=el.get("DEF", "")))

        arrays = dict(_SPLAT_ARRAYS, **{name: 3 for name in _SPLAT_SH.values()})
        counts = {}
        for name, size in arrays.items():
            if el.get(name) is None:
                continue
            n = _count(el.get(name))
            if n % size:
                report("error", "tuple-count", f"{name} has {n} numbers, not a multiple of {size}.")
            counts[name] = n // size
        if not counts:
            continue
        expected = counts.get("positions", max(counts.values()))
        for name, n in counts.items():
            if n != expected:
                report("error", "splat-array-length",
                       f"{name} holds {n} splats but positions holds {expected}. "
                       "Every per-splat array needs one entry per splat.")
        present = {d for (d, c), name in _SPLAT_SH.items() if name in counts}
        for d in sorted(present):
            missing = [name for (dd, c), name in _SPLAT_SH.items() if dd == d and name not in counts]
            if missing:
                report("warning", "splat-sh-partial",
                       f"SH degree {d} is missing {', '.join(missing)}. Renderers drop a partial "
                       "degree (X_ITE 16.4.3 treats it as absent), so write all 2d+1 coefficients or none.")
            elif d and d - 1 not in present:
                report("warning", "splat-sh-partial", f"SH degree {d} is written but degree {d - 1} is not.")
        if 0 not in present:
            report("warning", "splat-no-color", "no sphericalHarmonicsDegree0Coef0, so the splats have no base colour.")
        scales = _floats(el.get("scales", ""))
        if scales and min(scales) < 0:
            neg = sum(v < 0 for v in scales)
            report("error", "splat-scale-space",
                   f"{neg} of {len(scales)} scale values are negative. X3D scales are linear "
                   "standard deviations; a 3DGS PLY stores ln(scale), so apply exp() when converting.")
        opacities = _floats(el.get("opacities", ""))
        if opacities and (min(opacities) < 0 or max(opacities) > 1):
            bad = sum(not 0 <= v <= 1 for v in opacities)
            report("error", "splat-opacity-space",
                   f"{bad} of {len(opacities)} opacities are outside 0 to 1. A 3DGS PLY stores "
                   "logit(opacity), so apply the sigmoid when converting.")
        quats = _floats(el.get("orientations", ""))
        if len(quats) >= 4:
            norms = [(quats[i] ** 2 + quats[i + 1] ** 2 + quats[i + 2] ** 2 + quats[i + 3] ** 2) ** 0.5
                     for i in range(0, len(quats) - 3, 4)]
            zero = sum(n == 0 for n in norms)
            off = sum(abs(n - 1) > 1e-3 for n in norms) - zero
            if zero:
                report("error", "splat-quaternion-norm", f"{zero} orientations are all zero (no rotation is defined).")
            if off:
                report("warning", "splat-quaternion-norm",
                       f"{off} of {len(norms)} orientations are not unit length (|q| off by more than 1e-3); "
                       "renderers normalise them, but a unit quaternion round-trips exactly.")
            if len(norms) >= _SPLAT_ORDER_MIN:
                lead = [0, 0, 0, 0]
                for i in range(0, 4 * len(norms), 4):
                    q = [abs(v) for v in quats[i:i + 4]]
                    lead[q.index(max(q))] += 1
                if lead[0] > 0.6 * len(norms) and lead[3] < 0.2 * len(norms):
                    report("warning", "splat-quaternion-order",
                           f"{lead[0]} of {len(norms)} orientations have their largest component first and "
                           f"only {lead[3]} last. Trained splats usually stay near the identity (0 0 0 1), "
                           "so these look written w x y z; X3D 4.1 reads x y z w.")
    return diagnostics


_NAMING_HINTS_URL = (
    "https://www.web3d.org/x3d/content/examples/"
    "X3dSceneAuthoringHints.html#NamingConventions"
)


def _check_naming_conventions(scene: etree._Element) -> list[Diagnostic]:
    """Flag DEF/USE/name values that break cross-encoding compatibility.

    Periods collide with ClassicVRML ROUTE syntax and hyphens become
    subtraction operators when names are turned into programming-language
    identifiers (X3D Scene Authoring Hints, Naming Conventions). x3d.py's
    NMTOKEN check rejects periods on the construction path, but hyphens
    pass it, and content edited as raw XML never reaches x3d.py at all --
    this check is the only guard covering both paths.
    """
    diagnostics: list[Diagnostic] = []
    seen: set[tuple[str, str]] = set()
    for el in scene.iter():
        tag = _local_tag(el)
        if not tag:
            continue
        for attr in ("DEF", "USE", "name"):
            value = el.get(attr)
            if not value or (attr, value) in seen:
                continue
            seen.add((attr, value))
            problems = []
            if "." in value:
                problems.append("'.' interferes with ClassicVRML ROUTE syntax")
            if "-" in value:
                problems.append("'-' becomes a subtraction operator in "
                                "generated class/variable names")
            if problems:
                diagnostics.append(Diagnostic(
                    level="warning",
                    check="naming-convention",
                    message=f"{attr}='{value}' on {tag}: " + "; ".join(problems)
                            + f". See {_NAMING_HINTS_URL}",
                    node_tag=tag,
                    def_name=value if attr in ("DEF", "USE") else "",
                ))
            if value != value.strip() or any(c.isspace() for c in value):
                diagnostics.append(Diagnostic(
                    level="error",
                    check="naming-convention",
                    message=f"{attr}='{value}' on {tag} contains whitespace, "
                            f"which is illegal in ID/NMTOKEN values.",
                    node_tag=tag,
                    def_name=value if attr in ("DEF", "USE") else "",
                ))
            elif value[0].isdigit():
                diagnostics.append(Diagnostic(
                    level="error",
                    check="naming-convention",
                    message=f"{attr}='{value}' on {tag} starts with a digit, "
                            f"which is illegal for XML names.",
                    node_tag=tag,
                    def_name=value if attr in ("DEF", "USE") else "",
                ))
    return diagnostics


def _check_missing_viewpoint(scene: etree._Element) -> list[Diagnostic]:
    if list(scene.iter("Viewpoint")):
        return []
    return [Diagnostic(
        level="info",
        check="no-viewpoint",
        message="Scene has no Viewpoint node. The browser will use a default camera. "
                "Consider adding a Viewpoint for a defined initial view.",
    )]


def _check_profile_components(root: etree._Element) -> list[Diagnostic]:
    """Every node must be covered by the declared profile or a component statement.

    Runs on the document root (the profile and component statements live in the
    X3D element and its head), unlike the scene-level checks below. X_ITE and
    x3d-tidy refuse to load a scene that fails this, even though the XSD passes it.
    """
    from validation.profile import check_profile, component_statements

    report = check_profile(root)
    diagnostics: list[Diagnostic] = []
    if report.ok:
        return diagnostics
    fixes = " ".join(component_statements(report))
    for m in report.missing:
        have = (f"declared at level {m['declared_level']}" if m["declared_level"]
                else "not declared")
        diagnostics.append(Diagnostic(
            level="error",
            check="profile-component",
            message=f"{m['node']} (component {m['component']} level {m['level']}, "
                    f"{have}) is outside profile '{report.profile}'. Browsers that "
                    f"enforce the header (X_ITE, x3d-tidy) refuse to load it.",
            node_tag=m["node"],
        ))
    diagnostics.append(Diagnostic(
        level="info",
        check="profile-component-fix",
        message=f"Add to <head>: {fixes} -- or raise the profile "
                f"(Immersive covers most content, Full covers everything).",
    ))
    return diagnostics


_JOINT_AXIS_ATTRS = ("axis", "axis1", "axis2")
_NUMERIC_FIELD = re.compile(
    r"^(SF|MF)(Float|Double|Time|Vec\d[fd]|Rotation|Matrix\d[fd]|Color|ColorRGBA|Quaternion)$")


def _check_rigid_body_physics(scene: etree._Element) -> list[Diagnostic]:
    """Catch RigidBodyPhysics defects that validate against the XSD but break a simulation.

    Mass that is not positive, joint axes and body orientations of zero length, NaN or
    infinity in a field, joints that name no body, the same body twice, or a body from
    another collection, and bodies or joints that sit outside any RigidBodyCollection.
    Found by the physics stress test, where 23 of 40 seeded defect classes passed clean.
    """
    nodes = get_x3duom().get_concrete_nodes()
    physics = {t for t, info in nodes.items() if info.get("component") == "RigidBodyPhysics"}
    joints = {t for t in physics if t.endswith("Joint")}
    defs: dict[str, etree._Element] = {}
    for el in scene.iter():
        if isinstance(el.tag, str) and el.get("DEF") and el.get("DEF") not in defs:
            defs[el.get("DEF")] = el

    def resolve(el: etree._Element) -> etree._Element | None:
        return defs.get(el.get("USE")) if el.get("USE") else el

    diagnostics: list[Diagnostic] = []

    def report(level: str, check: str, el: etree._Element, message: str) -> None:
        diagnostics.append(Diagnostic(
            level=level, check=check, message=message,
            node_tag=_local_tag(el), def_name=el.get("DEF") or el.get("USE") or ""))

    def label(el: etree._Element) -> str:
        name = el.get("DEF") or el.get("USE")
        return f"{_local_tag(el)} '{name}'" if name else _local_tag(el)

    for el in scene.iter():
        tag = _local_tag(el)
        if tag not in physics:
            continue
        if not el.get("USE"):
            fmap = _node_field_map(tag, nodes)
            for attr, value in el.attrib.items():
                ftype = fmap.get(attr, {}).get("type", "")
                if _NUMERIC_FIELD.match(ftype) and not all(
                        math.isfinite(x) for x in _floats(value)):
                    report("error", "physics-non-finite", el,
                           f"{label(el)}.{attr}='{value}' contains NaN or infinity; a "
                           f"simulation step with it poisons every body it touches.")

        parent = el.getparent()
        ptag = _local_tag(parent) if parent is not None else ""
        cf = el.get("containerField") or nodes[tag]["containerField"]

        if tag == "RigidBody" and not el.get("USE"):
            mass = _floats(el.get("mass", "1"))[:1]
            if mass and (mass[0] < 0 or (mass[0] == 0 and el.get("fixed") != "true")):
                report("error", "physics-mass", el,
                       f"{label(el)} has mass='{el.get('mass')}'; a dynamic body needs a "
                       f"mass above zero (mass 0 only makes sense with fixed='true').")
            orient = _floats(el.get("orientation", "0 0 1 0"))
            if len(orient) == 4 and not any(orient[:3]):
                report("error", "physics-zero-axis", el,
                       f"{label(el)} has orientation='{el.get('orientation')}' with a "
                       f"zero-length axis; the rotation is undefined.")
            if ptag != "RigidBodyCollection" and not (ptag in joints and cf in ("body1", "body2")):
                report("warning", "physics-not-in-collection", el,
                       f"{label(el)} is not a child of a RigidBodyCollection, so no "
                       f"simulation will include it.")

        if tag in joints:
            for attr in _JOINT_AXIS_ATTRS:
                axis = _floats(el.get(attr, "")) if el.get(attr) else []
                if len(axis) == 3 and not any(axis):
                    report("error", "physics-zero-axis", el,
                           f"{label(el)}.{attr}='{el.get(attr)}' has zero length; the "
                           f"joint axis must be a non-zero vector.")
            if not el.get("USE") and ptag != "RigidBodyCollection":
                report("warning", "physics-not-in-collection", el,
                       f"{label(el)} is not a child of a RigidBodyCollection, so no "
                       f"simulation will include it.")
            ends = {c.get("containerField"): c for c in el
                    if isinstance(c.tag, str) and c.get("containerField") in ("body1", "body2")}
            for name in ("body1", "body2"):
                if name not in ends:
                    report("warning", "physics-joint-body", el,
                           f"{label(el)} has no {name}; the joint constrains nothing "
                           f"without two bodies.")
            bodies = {name: resolve(c) for name, c in ends.items()}
            for name, body in bodies.items():
                if body is not None and _local_tag(body) != "RigidBody":
                    report("error", "physics-joint-body", el,
                           f"{label(el)}.{name} refers to a {_local_tag(body)}, not a RigidBody.")
            if len(bodies) == 2 and bodies["body1"] is not None and bodies["body1"] is bodies["body2"]:
                report("error", "physics-joint-body", el,
                       f"{label(el)} joins {label(bodies['body1'])} to itself (body1 and "
                       f"body2 are the same body).")
            if ptag == "RigidBodyCollection":
                members = {resolve(c) for c in parent
                           if _local_tag(c) == "RigidBody"
                           and (c.get("containerField") or "bodies") == "bodies"}
                for name, body in bodies.items():
                    if body is not None and _local_tag(body) == "RigidBody" and body not in members:
                        report("error", "physics-joint-cross-collection", el,
                               f"{label(el)}.{name} is {label(body)}, which is not in the "
                               f"same RigidBodyCollection as the joint.")
    return diagnostics


def _check_undeclared_protos(scene: etree._Element) -> list[Diagnostic]:
    """A ProtoInstance must name a ProtoDeclare or ExternProtoDeclare in the same file (19775-1 4.4.4).

    Browsers drop an instance of an unknown prototype, usually with only a console
    message, so whatever it was meant to draw is missing. XSD cannot see this
    because the name is a plain string.
    """
    declared = {el.get("name") for el in scene.getroottree().iter()
                if _local_tag(el) in ("ProtoDeclare", "ExternProtoDeclare")}
    diagnostics: list[Diagnostic] = []
    for el in scene.iter():
        if _local_tag(el) != "ProtoInstance" or el.get("USE"):
            continue
        name = el.get("name")
        if name and name not in declared:
            diagnostics.append(Diagnostic(
                level="error", check="proto-undeclared",
                message=f"ProtoInstance name={name!r} has no ProtoDeclare or ExternProtoDeclare "
                        f"in this file. Declare the prototype (or an ExternProtoDeclare pointing "
                        f"at it) before the instance.",
                node_tag="ProtoInstance", def_name=el.get("DEF", "")))
    return diagnostics


def _check_field_values(scene: etree._Element) -> list[Diagnostic]:
    """Field values the spec restricts but the XSD lets through.

    - SFRotation with a zero axis and a non-zero angle: the axis must be a
      normalized vector (19775-1 5.3.14), so the rotation is undefined.
    - TimeSensor cycleInterval is (0,inf) in 19775-1 8.4.1 but the XSD allows 0, so 0 is a warning.
    - Box size, and Sphere, Cylinder and Cone radii and heights, must be greater than 0.
    - A few enumerated strings that browsers silently fall back on when misspelt.
    """
    uom = get_x3duom()
    nodes, types = uom.get_concrete_nodes(), uom.get_field_types()
    diagnostics: list[Diagnostic] = []

    def add(tag, el, check, msg, level="error"):
        diagnostics.append(Diagnostic(level=level, check=check,
                                      message=f"{tag}{_label(el)} {msg}", node_tag=tag,
                                      def_name=el.get("DEF", "")))

    for el in scene.iter():
        tag = _local_tag(el)
        if tag not in nodes or el.get("USE"):
            continue
        fields = _node_field_map(tag, nodes)
        for name, value in el.attrib.items():
            if fields.get(name, {}).get("type") != "SFRotation":
                continue
            v = _floats(value)
            if len(v) == 4 and abs(v[3]) > 1e-9 and v[0] ** 2 + v[1] ** 2 + v[2] ** 2 < 1e-12:
                add(tag, el, "rotation-zero-axis",
                    f"{name}={value!r} turns {v[3]} radians about a zero-length axis, which is "
                    f"undefined. Give a unit axis, or use '0 0 1 0' for no rotation.")
        if tag == "TimeSensor" and el.get("cycleInterval") is not None:
            v = _floats(el.get("cycleInterval"))
            if v and v[0] <= 0:   # warning: the spec says (0,inf), the XSD and X3DUOM allow 0
                add(tag, el, "cycle-interval-zero",
                    f"cycleInterval={el.get('cycleInterval')!r}: 19775-1 gives the range as greater "
                    f"than 0, so the sensor never completes a cycle (the schema itself allows 0).",
                    level="warning")
        for name in _POSITIVE.get(tag, ()):
            if el.get(name) is None:
                continue
            v = _floats(el.get(name))
            if v and min(v) <= 0:
                add(tag, el, "value-out-of-range",
                    f"{name}={el.get(name)!r} must be greater than 0.")
        for name, allowed in _ENUMS.get(tag, {}).items():
            if el.get(name) is None:
                continue
            raw = el.get(name)
            vals = re.findall(r'"([^"]*)"', raw) if fields.get(name, {}).get("type") == "MFString" else [raw.strip()]
            bad = [x for x in vals if x not in allowed]
            if bad:
                add(tag, el, "enum-invalid",
                    f"{name} value {bad[0]!r} is not one of {', '.join(sorted(a for a in allowed if a))}.")
    return diagnostics


_POSITIVE = {"Box": ("size",), "Sphere": ("radius",), "Cylinder": ("radius", "height"),
             "Cone": ("bottomRadius", "height")}
_BOUNDARY = {"CLAMP", "CLAMP_TO_EDGE", "CLAMP_TO_BOUNDARY", "MIRRORED_REPEAT", "REPEAT"}
_ENUMS = {
    "Fog": {"fogType": {"LINEAR", "EXPONENTIAL"}},
    "LocalFog": {"fogType": {"LINEAR", "EXPONENTIAL"}},
    "NavigationInfo": {"transitionType": {"TELEPORT", "LINEAR", "ANIMATE"}},
    "FontStyle": {"justify": {"BEGIN", "END", "FIRST", "MIDDLE", ""},
                  "style": {"PLAIN", "BOLD", "ITALIC", "BOLDITALIC", ""}},
    "ScreenFontStyle": {"justify": {"BEGIN", "END", "FIRST", "MIDDLE", ""},
                        "style": {"PLAIN", "BOLD", "ITALIC", "BOLDITALIC", ""}},
    "TextureProperties": {"boundaryModeS": _BOUNDARY, "boundaryModeT": _BOUNDARY, "boundaryModeR": _BOUNDARY},
}


def _label(el: etree._Element) -> str:
    return f" (DEF={el.get('DEF')!r})" if el.get("DEF") else ""


_INDEXED = {"IndexedFaceSet": "coordIndex", "IndexedLineSet": "coordIndex",
            "IndexedTriangleSet": "index", "IndexedTriangleFanSet": "index",
            "IndexedTriangleStripSet": "index", "IndexedQuadSet": "index"}


def _check_index_bounds(scene: etree._Element) -> list[Diagnostic]:
    """Every coordIndex / index entry must point at an existing coordinate (19775-1 11.2.1, 13.3).

    An index past the end draws garbage or nothing, depending on the browser, and the
    XSD only checks that the numbers are integers. -1 is the face separator in the
    coordIndex fields; any other negative index is also an error.
    """
    defs = {el.get("DEF"): el for el in scene.getroottree().iter()
            if isinstance(el.tag, str) and el.get("DEF")}
    diagnostics: list[Diagnostic] = []
    for el in scene.iter():
        tag = _local_tag(el)
        field = _INDEXED.get(tag)
        if not field or el.get("USE") or el.get(field) is None:
            continue
        coord = None
        for child in el:
            if _local_tag(child) in ("Coordinate", "CoordinateDouble") and \
                    child.get("containerField", "coord") == "coord":
                coord = defs.get(child.get("USE")) if child.get("USE") else child
                break
        if coord is None or coord.get("point") is None:
            continue
        n_points = _count(coord.get("point")) // 3
        try:
            idx = [int(x) for x in re.split(r"[\s,]+", el.get(field).strip()) if x]
        except ValueError:
            continue
        sep_ok = field == "coordIndex"
        bad = [i for i in idx if i >= n_points or (i < 0 and not (sep_ok and i == -1))]
        if bad:
            diagnostics.append(Diagnostic(
                level="error", check="index-out-of-range",
                message=f"{tag}{_label(el)} {field} uses {bad[0]} but the coordinate node has "
                        f"{n_points} point(s) (valid 0 to {n_points - 1}"
                        f"{', -1 ends a face' if sep_ok else ''}); {len(bad)} bad entr"
                        f"{'y' if len(bad) == 1 else 'ies'}.",
                node_tag=tag, def_name=el.get("DEF", "")))
    return diagnostics


def _child_node(el: etree._Element, tags: tuple, field: str, defs: dict) -> etree._Element | None:
    """The child node in `field` (by containerField) with one of `tags`, resolving USE."""
    for child in el:
        if isinstance(child.tag, str) and _local_tag(child) in tags and child.get("containerField", field) == field:
            return defs.get(child.get("USE")) if child.get("USE") else child
    return None


# (index field, child tags, child containerField, values per entry, attribute holding the values,
#  per-vertex flag that allows -1 separators)
_ATTR_INDICES = {
    "IndexedFaceSet": [("texCoordIndex", ("TextureCoordinate",), "texCoord", 2, "point", None),
                       ("colorIndex", ("Color", "ColorRGBA"), "color", None, "color", "colorPerVertex"),
                       ("normalIndex", ("Normal",), "normal", 3, "vector", "normalPerVertex")],
    "IndexedLineSet": [("colorIndex", ("Color", "ColorRGBA"), "color", None, "color", "colorPerVertex")],
}


def _check_attribute_index_bounds(scene: etree._Element) -> list[Diagnostic]:
    """texCoordIndex, colorIndex and normalIndex must point at existing values (19775-1 13.3.6, 11.3.3).

    The same rule as coordIndex, for the per-vertex attributes. The XSD only checks that the
    numbers are integers. -1 separates faces or polylines when the attribute is per vertex.
    """
    defs = {el.get("DEF"): el for el in scene.getroottree().iter()
            if isinstance(el.tag, str) and el.get("DEF")}
    diagnostics: list[Diagnostic] = []
    for el in scene.iter():
        tag = _local_tag(el)
        if tag not in _ATTR_INDICES or el.get("USE"):
            continue
        for field, kids, cfield, per, values, per_vertex in _ATTR_INDICES[tag]:
            if not el.get(field, "").strip():
                continue
            node = _child_node(el, kids, cfield, defs)
            if node is None or node.get(values) is None:
                continue
            width = per or (4 if _local_tag(node) == "ColorRGBA" else 3)
            n = _count(node.get(values)) // width
            try:
                idx = [int(x) for x in re.split(r"[\s,]+", el.get(field).strip()) if x]
            except ValueError:
                continue
            # -1 separates faces in per-vertex lists; exporters also write it in per-face lists, which
            # browsers skip, so only an index past the end or below -1 is reported.
            sep_ok = True
            bad = [i for i in idx if i >= n or i < -1]
            if bad:
                diagnostics.append(Diagnostic(
                    level="error", check="index-out-of-range",
                    message=f"{tag}{_label(el)} {field} uses {bad[0]} but the {_local_tag(node)} node has "
                            f"{n} value(s) (valid 0 to {n - 1}, -1 as a separator); "
                            f"{len(bad)} bad entr{'y' if len(bad) == 1 else 'ies'}.",
                    node_tag=tag, def_name=el.get("DEF", "")))
    return diagnostics


def _check_array_counts(scene: etree._Element) -> list[Diagnostic]:
    """Array lengths the spec ties together, which the XSD cannot see.

    - ElevationGrid: height has xDimension x zDimension values (19775-1 13.3.4).
    - IndexedTriangleSet: index count is a multiple of 3 (11.3.6).
    - Background, TextureBackground: skyColor has one more colour than skyAngle, and groundColor one
      more than groundAngle when groundAngle is set (24.2.1). Too few is an error; extra colours are
      unused and only warned about.
    - LOD: range values increase (25.3.3).
    """
    diagnostics: list[Diagnostic] = []

    def err(el, check, msg, level="error"):
        diagnostics.append(Diagnostic(level=level, check=check, message=f"{_local_tag(el)}{_label(el)} {msg}",
                                      node_tag=_local_tag(el), def_name=el.get("DEF", "")))

    for el in scene.iter():
        if not isinstance(el.tag, str) or el.get("USE"):
            continue
        tag = _local_tag(el)
        if tag == "ElevationGrid" and el.get("height") is not None:
            try:
                x, z = int(el.get("xDimension", "0")), int(el.get("zDimension", "0"))
            except ValueError:
                continue
            n = _count(el.get("height"))
            if n != x * z:
                err(el, "array-count", f"height has {n} value(s) but xDimension x zDimension = {x} x {z} = {x * z}.")
        elif tag == "IndexedTriangleSet" and el.get("index"):
            n = _count(el.get("index"))
            if n % 3:
                err(el, "array-count", f"index has {n} entries, not a multiple of 3; the last "
                                       f"{n % 3} do not make a triangle.")
        elif tag in ("Background", "TextureBackground"):
            for angle, color, default in (("skyAngle", "skyColor", "0 0 0"), ("groundAngle", "groundColor", "")):
                a = _count(el.get(angle, ""))
                if a == 0 and angle == "groundAngle":
                    continue
                c = _count(el.get(color, default)) // 3
                if c < a + 1:
                    err(el, "array-count", f"{color} has {c} colour(s) but {angle} has {a} angle(s); "
                                           f"{color} needs {a + 1} (one more than {angle}).")
                elif c > a + 1:   # extra colours are unused, not broken
                    err(el, "array-count-extra", f"{color} has {c} colour(s) but {angle} has {a} angle(s); "
                                                 f"only the first {a + 1} are used.", level="warning")
        elif tag == "LOD" and el.get("range"):
            r = _floats(el.get("range"))
            drops = [i for i in range(1, len(r)) if r[i] < r[i - 1]]
            if drops:
                i = drops[0]
                err(el, "lod-range-order", f"range is not increasing: range[{i - 1}]={r[i - 1]} is followed "
                                           f"by range[{i}]={r[i]}. Each switch distance must be larger than the last.")
    return diagnostics


def _check_interpolator_key_order(scene: etree._Element) -> list[Diagnostic]:
    """Interpolator and sequencer keys must be non-decreasing (19775-1 19.2.2, 30.2).

    Out-of-order keys make the interpolation between them undefined; browsers
    typically jump or freeze. The XSD sees only a list of floats.
    """
    diagnostics: list[Diagnostic] = []
    for el in scene.iter():
        tag = _local_tag(el)
        if not (tag.endswith("Interpolator") or tag.endswith("Sequencer")) or el.get("key") is None:
            continue
        k = _floats(el.get("key"))
        drops = [i for i in range(1, len(k)) if k[i] < k[i - 1]]
        if drops:
            i = drops[0]
            diagnostics.append(Diagnostic(
                level="error", check="interpolator-key-order",
                message=f"{tag}{_label(el)} key is not in increasing order: key[{i - 1}]={k[i - 1]} "
                        f"is followed by key[{i}]={k[i]}. Sort the keys and their keyValue "
                        f"entries together.",
                node_tag=tag, def_name=el.get("DEF", "")))
    return diagnostics


_ALL_CHECKS = [
    _check_duplicate_defs,
    _check_def_use_consistency,
    _check_use_before_def,
    _check_containerfield,
    _check_shape_completeness,
    _check_empty_groups,
    _check_route_validity,
    _check_naming_conventions,
    _check_interpolator_keys,
    _check_tuple_counts,
    _check_gaussian_splats,
    _check_dead_boolean_routes,
    _check_rigid_body_physics,
    _check_missing_viewpoint,
    _check_undeclared_protos,
    _check_field_values,
    _check_index_bounds,
    _check_interpolator_key_order,
    _check_attribute_index_bounds,
    _check_array_counts,
]


def _find_scene(root: etree._Element) -> etree._Element | None:
    """Find the Scene element under the X3D root, namespace-tolerant."""
    if _local_tag(root) == "Scene":
        return root
    for child in root.iter():
        if _local_tag(child) == "Scene":
            return child
    return None


# huge_tree lifts libxml2's 10 MB limit on one text node or attribute, which a
# GaussianSplats node passes at about 18,000 SH degree 3 splats. Entities stay
# unresolved and nothing is fetched, so a large document cannot expand or reach out.
_PARSER = etree.XMLParser(huge_tree=True, resolve_entities=False, no_network=True)


class NoSceneError(ValueError):
    """The document parsed but has no Scene element."""


def semantic_diagnostics(xml_string: str) -> list[Diagnostic]:
    """Run every semantic check and return the diagnostics as data.

    The structured counterpart of validate_semantic, for callers that render
    or count the findings themselves. Raises etree.XMLSyntaxError when the
    document is not well-formed and NoSceneError when it has no Scene.
    """
    root = etree.fromstring(xml_string.encode("utf-8"), _PARSER)
    scene = _find_scene(root)
    if scene is None:
        raise NoSceneError("No Scene element found in the X3D document.")
    diagnostics: list[Diagnostic] = _check_profile_components(root)
    for check_fn in _ALL_CHECKS:
        diagnostics.extend(check_fn(scene))
    return diagnostics


def validate_semantic(xml_string: str) -> str:
    """Run all semantic checks on an X3D XML string and return a markdown report."""
    try:
        all_diagnostics = semantic_diagnostics(xml_string)
    except etree.XMLSyntaxError as e:
        return f"# Semantic Check: Parse Error\n\n{e}"
    except NoSceneError as e:
        return f"# Semantic Check: No Scene\n\n{e}"

    if not all_diagnostics:
        return (
            "# Semantic Check: All Clear\n\n"
            "No semantic issues found. The scene looks well-structured.\n"
            "Note: This checks common authoring issues beyond XSD schema validation. "
            "Use validate_x3d for schema-level validation."
        )

    errors = [d for d in all_diagnostics if d.level == "error"]
    warnings = [d for d in all_diagnostics if d.level == "warning"]
    infos = [d for d in all_diagnostics if d.level == "info"]

    lines = [
        "# Semantic Check Report",
        "",
        f"Found {len(errors)} error(s), {len(warnings)} warning(s), {len(infos)} info(s).",
        "",
    ]

    if errors:
        lines.append("## Errors")
        lines.append("")
        for d in errors:
            lines.append(f"- **[{d.check}]** {d.message}")
        lines.append("")

    if warnings:
        lines.append("## Warnings")
        lines.append("")
        for d in warnings:
            lines.append(f"- **[{d.check}]** {d.message}")
        lines.append("")

    if infos:
        lines.append("## Info")
        lines.append("")
        for d in infos:
            lines.append(f"- **[{d.check}]** {d.message}")

    return "\n".join(lines)
