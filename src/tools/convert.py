"""X3D encoding conversion tools.

Converts X3D content between XML, JSON, and ClassicVRML encodings
using x3d.py's built-in serialization.
"""

import json as json_mod
from lxml import etree
import x3d.x3d as x3d_mod
from x3d.x3d import X3D, Scene, head, meta
from mcp.server.fastmcp import FastMCP

from validation.defuse import reorder_defs_xml
from x3d_utils.x3duom import get_x3duom


def _parse_xml_to_model(xml_string: str) -> X3D:
    """Parse X3D XML and rebuild as x3d.py model."""
    root = etree.fromstring(xml_string.encode("utf-8"))
    profile = root.get("profile", "Interchange")
    version = root.get("version", "4.1")

    head_node = None
    head_el = root.find("head")
    if head_el is not None:
        metas = []
        for m in head_el.findall("meta"):
            metas.append(meta(name=m.get("name", ""), content=m.get("content", "")))
        if metas:
            head_node = head(children=metas)

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
            if field_info:
                kwargs[attr_name] = _coerce_value(attr_value, field_info["type"])

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
    elif field_type in ("MFVec4f", "MFVec4d", "MFRotation", "MFQuaternion"):
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


def register(mcp: FastMCP):

    @mcp.tool()
    def convert_x3d(content: str, from_encoding: str,
                    to_encoding: str) -> str:
        """Convert X3D content between encodings.

        Args:
            content: The X3D content string.
            from_encoding: Source encoding - "xml".
            to_encoding: Target encoding - "xml", "json", or "vrml".
        """
        if from_encoding == "xml":
            model = _parse_xml_to_model(content)
        else:
            return f"Parsing from '{from_encoding}' is not yet supported. Use 'xml' as source."

        if to_encoding == "xml":
            return reorder_defs_xml(model.XML())      # x3d.py writes joints before bodies
        elif to_encoding == "json":
            return model.JSON()
        elif to_encoding == "vrml":
            return model.VRML()
        else:
            return f"Unknown target encoding: {to_encoding}"
