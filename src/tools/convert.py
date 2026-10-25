"""X3D encoding conversion tools.

Converts X3D content between XML, JSON, and ClassicVRML encodings
using x3d.py's built-in serialization.
"""

import collections
import json as json_mod
import keyword
from lxml import etree
import x3d.x3d as x3d_mod
from x3d.x3d import X3D, Scene, component, head, meta, unit
from mcp.server.fastmcp import Context, FastMCP

from transfer.blobs import get_config
from transfer.outputs import XML_TYPE, document_info, envelope, error_envelope, input_info, parse_mode
from transfer.tooling import with_blob_slot, require_blob_access, resolve_input, run_heavy
from validation.defuse import reorder_defs_xml
from x3d_utils.gen_schema import DEFAULT_MAX_NODES, SchemaRequestError, build_generation_schema
from x3d_utils.json2xml import JsonConversionError, json_to_xml
from x3d_utils.x3duom import get_x3duom

_XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>\n'
_HUGE_PARSER = etree.XMLParser(huge_tree=True)    # counting elements must not hit lxml's per-attribute size limit
_MEDIA_TYPES = {"xml": XML_TYPE, "json": "model/x3d+json", "vrml": "model/x3d+vrml"}
SCHEMA_TYPE = "application/schema+json"

# x3d.py indents VRML by depth without a cap: a document 200 levels deep with 80,000
# leaves grows 145-fold (0.9 MB to 129 MB, 19 s). Measured, a level of an element costs
# about 8 bytes of output; a conversion expected to write more than this is refused.
_VRML_BYTES_PER_DEPTH = 8
MAX_VRML_BYTES = 64 << 20


def _parse_xml_to_model(xml_string: str) -> X3D:
    """Parse X3D XML and rebuild as x3d.py model."""
    root = etree.fromstring(xml_string.encode("utf-8"))
    profile = root.get("profile", "Interchange")
    version = root.get("version", "4.1")

    head_node = None
    head_el = root.find("head")
    if head_el is not None:
        statements = []
        for h in head_el:
            if h.tag == "meta":
                statements.append(meta(name=h.get("name", ""), content=h.get("content", "")))
            elif h.tag == "component":
                statements.append(component(name=h.get("name", ""), level=int(h.get("level", "1"))))
            elif h.tag == "unit":
                statements.append(unit(category=h.get("category", ""), name=h.get("name", ""),
                                       conversionFactor=float(h.get("conversionFactor", "1"))))
        if statements:
            head_node = head(children=statements)

    scene_el = root.find("Scene")
    children = []
    if scene_el is not None:
        children = _parse_children(scene_el)

    return X3D(
        profile=profile,
        version=version,
        head=head_node,
        Scene=Scene(children=children),
    )


# Statement elements that are no nodes in the X3DUOM but that nodes hold: the
# attributes x3d.py takes for each (Script and shader fields, IS connections).
_STATEMENT_ATTRS = {
    "field": ("name", "type", "accessType", "value", "appinfo", "documentation"),
    "connect": ("nodeField", "protoField"),
    "IS": (),
}


def _parse_children(parent_el: etree._Element) -> list:
    """Recursively parse XML elements into x3d.py node objects."""
    return [node for node, _ in _parse_nodes(parent_el)]


def _parse_nodes(parent_el: etree._Element) -> list[tuple[object, str | None]]:
    """Parse child elements into (x3d.py node, containerField) pairs.

    The containerField is the one written on the element, else the node's
    default; it is None for statements (ROUTE) that no field holds.
    """
    uom = get_x3duom()
    nodes = uom.get_concrete_nodes()
    children = []

    for el in parent_el:
        tag = el.tag
        if not isinstance(tag, str):      # comments and processing instructions
            continue
        if tag in _STATEMENT_ATTRS and tag not in nodes:
            stmt = {k: el.get(k) for k in _STATEMENT_ATTRS[tag] if el.get(k) is not None}
            raw_value = stmt.pop("value", None) if tag == "field" else None
            if tag == "IS":
                stmt["connect"] = _parse_children(el)
            obj = getattr(x3d_mod, tag)(**stmt)
            if raw_value is not None:
                # x3d.py type-checks a field's value, then writes any type but SFBool as the Python
                # repr of the tuple or list ("(1.0, 2.0, 3.0)"). The XML text goes in as written,
                # which its XML() writes back unchanged.
                obj._value = raw_value
            children.append((obj, tag))   # held by the field of that name
            continue
        if tag == "ROUTE":
            from x3d.x3d import ROUTE
            children.append((ROUTE(
                fromNode=el.get("fromNode", ""),
                fromField=el.get("fromField", ""),
                toNode=el.get("toNode", ""),
                toField=el.get("toField", ""),
            ), None))
            continue

        cls = getattr(x3d_mod, tag, None)
        if cls is None:
            continue

        kwargs = {}
        node_info = nodes.get(tag, {})
        field_map = {f["name"]: f for f in node_info.get("fields", [])}

        for attr_name, attr_value in el.attrib.items():
            if attr_name in ("DEF", "USE"):
                kwargs[attr_name] = attr_value
                continue
            if attr_name in ("class", "id", "style"):
                kwargs[attr_name + "_"] = attr_value
                continue

            field_info = field_map.get(attr_name)
            if field_info:        # x3d.py spells a field named like a Python keyword (DirectionalLight.global) global_
                key = attr_name + "_" if keyword.iskeyword(attr_name) else attr_name
                kwargs[key] = _coerce_value(attr_value, field_info["type"])

        if "sourceCode" in field_map and "sourceCode" not in kwargs:   # Script and shader source, in CDATA
            source = (el.text or "") + "".join(c.tail or "" for c in el)
            if source.strip():
                kwargs["sourceCode"] = source.strip()

        for sub, cf in _parse_nodes(el):
            if cf not in field_map and "children" in field_map:
                cf = "children"
            if cf not in field_map:
                continue
            if field_map[cf]["type"] == "MFNode":
                kwargs.setdefault(cf, []).append(sub)
            else:
                kwargs[cf] = sub

        cf = el.get("containerField") or node_info.get("containerField")
        children.append((cls(**kwargs), cf))

    return children


def _coerce_value(value_str: str, field_type: str):
    """Convert an XML attribute string to the appropriate Python type."""
    if field_type == "SFBool":
        return value_str.lower() == "true"
    elif field_type in ("SFInt32",):
        return int(value_str)
    elif field_type in ("SFFloat", "SFDouble", "SFTime"):
        return float(value_str)
    elif field_type in ("SFVec2f", "SFVec2d"):
        parts = value_str.split()
        return tuple(float(p) for p in parts[:2])
    elif field_type in ("SFVec3f", "SFVec3d"):
        parts = value_str.split()
        return tuple(float(p) for p in parts[:3])
    elif field_type in ("SFVec4f", "SFVec4d", "SFRotation", "SFQuaternion"):
        parts = value_str.split()
        return tuple(float(p) for p in parts[:4])
    elif field_type in ("SFMatrix3f", "SFMatrix3d"):
        return tuple(float(p) for p in value_str.replace(",", " ").split()[:9])
    elif field_type in ("SFMatrix4f", "SFMatrix4d"):
        return tuple(float(p) for p in value_str.replace(",", " ").split()[:16])
    elif field_type in ("SFColor",):
        parts = value_str.split()
        return tuple(float(p) for p in parts[:3])
    elif field_type in ("SFColorRGBA",):
        parts = value_str.split()
        return tuple(float(p) for p in parts[:4])
    elif field_type in ("MFFloat", "MFDouble", "MFTime"):
        parts = value_str.replace(",", " ").split()
        return [float(p) for p in parts]
    elif field_type in ("MFInt32",):
        parts = value_str.replace(",", " ").split()
        return [int(p) for p in parts]
    elif field_type in ("MFVec3f", "MFVec3d"):
        parts = value_str.replace(",", " ").split()
        floats = [float(p) for p in parts]
        return [tuple(floats[i:i+3]) for i in range(0, len(floats), 3)]
    elif field_type in ("MFVec2f", "MFVec2d"):
        floats = [float(p) for p in value_str.replace(",", " ").split()]
        return [tuple(floats[i:i+2]) for i in range(0, len(floats), 2)]
    elif field_type in ("MFVec4f", "MFVec4d", "MFRotation", "MFQuaternion", "MFColorRGBA"):
        parts = value_str.replace(",", " ").split()
        floats = [float(p) for p in parts]
        return [tuple(floats[i:i+4]) for i in range(0, len(floats), 4)]
    elif field_type in ("MFColor",):
        parts = value_str.replace(",", " ").split()
        floats = [float(p) for p in parts]
        return [tuple(floats[i:i+3]) for i in range(0, len(floats), 3)]
    elif field_type in ("MFString",):
        # MFString in XML is quoted strings
        import re
        return re.findall(r'"([^"]*)"', value_str) or [value_str]
    elif field_type in ("MFBool",):
        parts = value_str.replace(",", " ").split()
        return [p.lower() == "true" for p in parts]
    return value_str




def _json_scene_as_xml(xml: str) -> str:
    """The XML json_to_xml wrote, with every DEF ahead of its USEs and an XML declaration.

    Raises etree.XMLSyntaxError if the text is not well-formed XML even with the parser's
    size limits lifted (json_to_xml should never write that; this is the check).
    """
    try:
        xml = reorder_defs_xml(xml)
    except etree.XMLSyntaxError:
        # Either a parser size limit (one attribute above about 10 MB), which is no fault of
        # the document: keep the text as written. Or ill-formed XML, which must not pass as a result.
        etree.fromstring(xml.encode("utf-8"), _HUGE_PARSER)
    return xml if xml.startswith("<?xml") else _XML_DECLARATION + xml


def _dropped_elements(source_xml: str, written_xml: str) -> dict[str, int]:
    """Elements of source_xml that written_xml (x3d.py's rendering of it) holds fewer of."""
    def counts(xml: str) -> collections.Counter:
        root = etree.fromstring(xml.encode("utf-8"), _HUGE_PARSER)
        return collections.Counter(e.tag for e in root.iter() if isinstance(e.tag, str))
    before, after = counts(source_xml), counts(written_xml)
    return {tag: n - after.get(tag, 0) for tag, n in before.items() if n > after.get(tag, 0)}


def _convert_json(content: str, to_encoding: str) -> tuple[str, bool]:
    """(output, True) for a conversion of X3D JSON; (message, False) for a refusal."""
    stats: dict = {}
    try:
        xml = json_to_xml(content, stats=stats)
    except JsonConversionError as exc:
        return f"Cannot read X3D JSON: {exc}", False
    except RecursionError:
        return "Cannot read X3D JSON: (root): nested too deeply", False
    if to_encoding == "xml":
        try:
            return _json_scene_as_xml(xml), True
        except etree.XMLSyntaxError as exc:
            return f"Cannot read X3D JSON: the XML written for it is not well-formed: {str(exc)[:160]}", False
    if to_encoding == "json":
        return content, True          # already JSON; json_to_xml has checked that it reads
    if to_encoding != "vrml":
        return f"Unknown target encoding: {to_encoding}", False
    expected = len(xml) + stats["indent_units"] * _VRML_BYTES_PER_DEPTH
    if expected > MAX_VRML_BYTES:
        return (f"Cannot convert to {to_encoding}: the {to_encoding} text would be about {expected >> 20} MB "
                f"(x3d.py indents by depth; the limit is {MAX_VRML_BYTES >> 20} MB). "
                "Use to_encoding='xml' for a scene this large or this deep."), False
    try:
        model = _parse_xml_to_model(xml)
        dropped = _dropped_elements(xml, model.XML())
    except etree.XMLSyntaxError as exc:
        return f"Cannot convert to {to_encoding}: {exc}. Use to_encoding='xml' for a scene this large.", False
    except (x3d_mod.X3DError, ValueError, TypeError, AttributeError, RecursionError) as exc:      # x3d.py refusing a value: "oops" for a SFVec3f
        return (f"Cannot convert to {to_encoding}: x3d.py cannot hold this scene ({type(exc).__name__}: "
                f"{str(exc)[:160]}). Use to_encoding='xml', which keeps the scene as written."), False
    if dropped:
        names = ", ".join(f"{tag} x{n}" for tag, n in sorted(dropped.items()))
        return (f"Cannot convert to {to_encoding}: x3d.py would drop {names}. "
                "Use to_encoding='xml', which keeps the scene as written."), False
    return model.VRML(), True


def _convert(content: str, from_encoding: str, to_encoding: str) -> tuple[str, bool]:
    """(output, True) for a conversion; (message, False) for an unsupported encoding or a refusal."""
    if from_encoding == "json":
        return _convert_json(content, to_encoding)
    if from_encoding == "xml":
        try:
            model = _parse_xml_to_model(content)
            if to_encoding == "xml":
                return reorder_defs_xml(model.XML()), True      # x3d.py writes joints before bodies
            elif to_encoding == "json":
                return model.JSON(), True
            elif to_encoding == "vrml":
                return model.VRML(), True
        except x3d_mod.X3DError as exc:                         # x3d.py refusing a value it cannot hold
            return f"Cannot convert to {to_encoding}: x3d.py cannot hold this scene ({type(exc).__name__}: {str(exc)[:160]}).", False
        return f"Unknown target encoding: {to_encoding}", False
    return f"Parsing from '{from_encoding}' is not yet supported. Use 'xml' or 'json' as source.", False


def register(mcp: FastMCP):

    @mcp.tool()
    @with_blob_slot
    async def convert_x3d(content: str, from_encoding: str, to_encoding: str,
                          blob_id: str = "", return_mode: str = "full",
                          ctx: Context = None) -> str:
        """Convert X3D content between encodings.

        JSON is read as the X3D JSON encoding (ISO/IEC 19776-5, the layout of
        x3d-4.0-JSONSchema.json): "@" members are attributes, "-" members hold
        child nodes, "#sourceCode" is Script source. Member order does not matter;
        "#comment" members are dropped. To "xml" the scene comes back as written (DEFs
        moved ahead of their USEs); "json" returns it unchanged; "vrml" goes through x3d.py and is
        refused, naming the elements, if x3d.py would drop any. That check covers a JSON
        source only: XML read by x3d.py still loses what it has no class for (the field
        of an ExternProtoDeclare, MetadataString inside WorldInfo, unknown elements).

        Args:
            content: The X3D content string. Pass an empty string when blob_id is given.
            from_encoding: Source encoding - "xml" or "json".
            to_encoding: Target encoding - "xml", "json", or "vrml".
            blob_id: Id of a stored blob holding the document (instead of content).
            return_mode: "full" (default) returns the converted document. "summary" returns
                JSON: the result's sha256, size and (for xml) node counts, not the document.
                "blob" also stores the result and returns its blob_id.
        """
        try:
            mode = parse_mode(return_mode)
        except ValueError as e:
            return str(e)
        label = "inline content"
        try:
            if blob_id.strip():
                content, label = await resolve_input(content, "", blob_id, ctx)
            if mode == "blob":
                require_blob_access(ctx)
        except ValueError as e:
            return str(e) if mode == "full" else error_envelope(mode, str(e))
        # JSON input is amplified on the way (5x to XML, much more to VRML), so its job always goes to the worker thread
        size = max(len(content), get_config().offload_min) if from_encoding == "json" else len(content)
        out, ok = await run_heavy(_convert, content, from_encoding, to_encoding, size=size)
        if mode == "full":
            return out
        if not ok:
            return error_envelope(mode, out, label)
        media = _MEDIA_TYPES[to_encoding]
        try:
            doc = await run_heavy(document_info, out, media, mode == "blob", size=len(out))
            inp = await run_heavy(input_info, content, blob_id, size=len(content))
        except ValueError as e:
            return error_envelope(mode, str(e), label)
        return envelope(mode, label, ok=True, input=inp, document=doc)

    @mcp.tool(structured_output=False)
    async def x3d_generation_schema(profile: str = "Interchange", nodes: list[str] | None = None,
                                    max_nodes: int = DEFAULT_MAX_NODES, version: str = "4.0",
                                    include_schema: bool = True, return_mode: str = "full",
                                    ctx: Context = None) -> str:
        """A strict JSON Schema for generating an X3D scene as JSON under schema-constrained decoding.

        Give it to a provider's structured-output option (or a local grammar) so the
        model can only write nodes of the profile, closed objects, real field names and
        enumerated values. Turn the JSON it writes into XML with convert_x3d
        (from_encoding="json"), then run validate_x3d and validate_semantic: the schema
        is a first filter, the validators decide.

        The schema is a subset of the official X3D 4.0 JSON Schema (with its
        NavigationInfo.type fixed to an array of strings), so it is small: about 30 KB
        for Interchange, 40 KB for Interactive. Providers cap schema size and some reject
        large ones; lower max_nodes or name only the nodes you need if yours does.

        The schema is for the program that calls the model, not for the model to read: with
        return_mode="blob" it stays on the server and comes back as a blob_id to fetch
        (GET /blob/<id>, or blob_read), so it never passes through the model's context.

        Args:
            profile: Profile whose nodes are offered - Core, Interchange, Interactive,
                Immersive, Full, CADInterchange, MedicalInterchange or MPEG4Interactive.
            nodes: Node names to offer in addition (each outside the profile needs a
                component statement, listed in the report).
            max_nodes: Most profile nodes offered, commonly used ones first; nodes
                named in `nodes` are always offered.
            version: "4.0" (what the official JSON Schema describes) or "4.1".
            include_schema: false returns the report alone, to check the size first.
            return_mode: "full" (default) returns report, notes and schema as JSON.
                "summary" returns the report, the notes and the schema's sha256 and size,
                without the schema. "blob" also stores the schema (application/schema+json)
                and adds its blob_id.
        """
        try:
            mode = parse_mode(return_mode)
        except ValueError as e:
            return json_mod.dumps({"error": str(e)})
        try:
            if mode == "blob" and include_schema:
                require_blob_access(ctx)
        except ValueError as e:
            return error_envelope(mode, str(e))
        try:
            built = build_generation_schema(profile, nodes, max_nodes, version)
        except SchemaRequestError as exc:
            return json_mod.dumps({"error": str(exc)}) if mode == "full" else error_envelope(mode, str(exc))
        report = built["report"]
        notes = [
            f"{report['size']['bytes']} bytes, {report['node_count']} nodes. Providers cap schema size and "
            "complexity and some reject large schemas (a 76 KB, 119-node subset of this kind was rejected by "
            "some); lower max_nodes or pass nodes=[...] if yours does.",
            "First filter only: convert_x3d(from_encoding='json', to_encoding='xml'), then validate_x3d and "
            "validate_semantic decide.",
            "Objects are closed and optional members stay optional; a provider whose strict mode requires "
            "every property to be listed as required needs the schema adapted.",
        ]
        if report["outside_profile"]:
            declare = ", ".join(f"{c} level {lvl}" for c, lvl in sorted(report["components_to_declare"].items()))
            notes.append(f"Nodes outside {profile} need head component statements: {declare}.")
        if report["unknown_nodes"]:
            notes.append("Not in the X3D 4.0 JSON Schema, so not offered: " + ", ".join(report["unknown_nodes"]))
        if version == "4.1":
            notes.append("No X3D 4.1 JSON Schema exists: the nodes are the 4.0 set, and validate_x3d(encoding="
                         "'json') still rejects @version 4.1 because the 4.0 schema's enum stops there.")
        if mode == "full":
            result = {"report": report, "notes": notes}
            if include_schema:
                result["schema"] = built["schema"]
            return json_mod.dumps(result, separators=(",", ":"))
        fields = {"report": report, "notes": notes}
        if include_schema:
            text = json_mod.dumps(built["schema"], separators=(",", ":"))
            try:
                fields["document"] = await run_heavy(document_info, text, SCHEMA_TYPE, mode == "blob", size=len(text))
            except ValueError as e:
                return error_envelope(mode, str(e))
        return envelope(mode, f"generated {profile} {version}", ok=True, **fields)
