"""Repair an X3D document so it passes the checks the validators run.

The validators say what is wrong; this applies the fixes they name, in the
order an author would, and re-validates so the caller sees what is left:

1. containerField mistakes (via ``autofix_containerfields``).
2. Header drift: a missing ``profile`` or ``version``, and nodes that exist
   only in a later X3D version than the document declares. The declaration
   is raised to the version that has every node (content is never removed),
   and the DOCTYPE and schema-location attributes follow it.
3. Profile and component conformance (ISO/IEC 19775-1 clause 7.2.5): the
   ``<component>`` statements ``check_profile`` reports as missing are added
   to ``<head>`` (created when absent) or raised to the needed level.
4. The ``https`` XMLSchema-instance namespace x3d.py writes, which the XSD
   rejects, is dropped; a correct ``noNamespaceSchemaLocation`` is written.

Each change is recorded so an agent can explain, or decline, what was done.
Structural problems (USE before DEF, a node in a parent that accepts no such
type, unknown nodes) are reported under ``unfixable`` and left alone.
"""

import re
from lxml import etree

from validation.autofix import autofix_containerfields
from validation.profile import check_profile, component_statements, _local_tag
from validation.validate import (
    XSD_PATHS, XSI_NS, _nodes_by_version, resolve_xsd_version, validate_xml,
)

XSI_NS_CORRECT = "http://www.w3.org/2001/XMLSchema-instance"
DEFAULT_PROFILE = "Immersive"
_SPEC_BASE = "https://www.web3d.org/specifications"

_DOCTYPE_RE = re.compile(r"<!DOCTYPE\s+X3D[^>]*>", re.S)


def _doctype(version: str) -> str:
    return (f'<!DOCTYPE X3D PUBLIC "ISO//Web3D//DTD X3D {version}//EN" '
            f'"{_SPEC_BASE}/x3d-{version}.dtd">')


def _lowest_version_with(tags: set[str], declared: str | None) -> str | None:
    """Return the lowest bundled version whose X3DUOM has every tag, at or
    above the declared one. None when no bundled version has them all."""
    nodes = _nodes_by_version()
    floor, _ = resolve_xsd_version(declared) if declared else ("4.0", None)
    if declared and declared not in XSD_PATHS and declared not in {"3.0", "3.1", "3.2", "3.3"}:
        floor = "4.0"
    for version in sorted(XSD_PATHS):
        if version < floor:
            continue
        if tags <= nodes[version]:
            return version
    return None


def _scene_tags(root: etree._Element) -> set[str]:
    nodes_all = set().union(*_nodes_by_version().values())
    tags: set[str] = set()
    for el in root.iter():
        tag = _local_tag(el)
        if tag in nodes_all:
            tags.add(tag)
    return tags


def _find_head(root: etree._Element) -> etree._Element:
    for child in root:
        if _local_tag(child) == "head":
            return child
    head = etree.Element("head")
    root.insert(0, head)
    return head


def _fix_header(root: etree._Element, changes: list[dict], unfixable: list[dict]) -> None:
    """Set profile and version so the declaration covers the content."""
    if not root.get("profile"):
        root.set("profile", DEFAULT_PROFILE)
        changes.append({"kind": "profile", "from": None, "to": DEFAULT_PROFILE,
                        "reason": "profile is required; set the default, component "
                                  "statements cover anything outside it."})

    declared = root.get("version")
    tags = _scene_tags(root)
    target = _lowest_version_with(tags, declared)
    if target is None:
        known = set().union(*_nodes_by_version().values())
        unfixable.append({"kind": "version", "reason": "no bundled X3D version has every "
                          "node used", "nodes": sorted(tags - known)})
        target = resolve_xsd_version(declared)[0]
    if declared != target:
        if declared in XSD_PATHS or declared is None:
            later = sorted(t for t in tags if t not in _nodes_by_version().get(declared or "4.0", set()))
            reason = (f"nodes added in X3D {target}: {', '.join(later)}" if later
                      else "version is required")
        else:
            reason = f"X3D {declared} has no bundled schema; {target} is a superset"
        root.set("version", target)
        changes.append({"kind": "version", "from": declared, "to": target, "reason": reason})


def _fix_components(root: etree._Element, changes: list[dict]) -> None:
    report = check_profile(root)
    if report.ok:
        return
    head = _find_head(root)
    existing = {el.get("name"): el for el in head if _local_tag(el) == "component"}
    for line in component_statements(report):
        name = re.search(r"name='([^']+)'", line).group(1)
        level = re.search(r"level='(\d+)'", line).group(1)
        el = existing.get(name)
        if el is not None:
            before = el.get("level")
            el.set("level", level)
            changes.append({"kind": "component", "name": name, "from": before, "to": level,
                            "reason": "raised to the level the scene's nodes need"})
        else:
            el = etree.SubElement(head, "component", name=name, level=level)
            # component statements precede unit and meta statements in head
            head.remove(el)
            idx = len([c for c in head if _local_tag(c) == "component"])
            head.insert(idx, el)
            changes.append({"kind": "component", "name": name, "from": None, "to": level,
                            "reason": f"nodes outside profile '{report.profile}'"})


def _fix_xsi(root: etree._Element, changes: list[dict]) -> None:
    version = root.get("version")
    location = f"{_SPEC_BASE}/x3d-{version}.xsd"
    for attr in list(root.attrib):
        if attr.startswith("{" + XSI_NS + "}"):
            del root.attrib[attr]
            changes.append({"kind": "xsi", "from": attr, "to": f"{{{XSI_NS_CORRECT}}}noNamespaceSchemaLocation",
                            "reason": "XMLSchema-instance namespace must be http, not https"})
    key = "{%s}noNamespaceSchemaLocation" % XSI_NS_CORRECT
    if root.get(key) not in (None, location):
        changes.append({"kind": "xsi", "from": root.get(key), "to": location,
                        "reason": "schema location follows the version"})
    if key in root.attrib or any(c["kind"] == "xsi" for c in changes):
        root.set(key, location)


def repair_x3d(xml_string: str) -> dict:
    """Return {fixed, changes, unfixable, remaining} for an X3D XML document."""
    first = autofix_containerfields(xml_string)
    if first.get("error"):
        return {"error": first["error"], "fixed": "", "changes": [], "unfixable": [],
                "remaining": {}}
    changes = [dict(c, kind="containerField") for c in first["changes"]]
    unfixable = [dict(u, kind="containerField") for u in first["unfixable"]]

    parser = etree.XMLParser(remove_blank_text=True)
    root = etree.fromstring(first["fixed"].encode("utf-8"), parser)
    if _local_tag(root) != "X3D":
        return {"error": "Root element is not X3D", "fixed": "", "changes": [],
                "unfixable": [], "remaining": {}}

    _fix_header(root, changes, unfixable)
    _fix_components(root, changes)
    _fix_xsi(root, changes)

    report = check_profile(root)
    for tag in report.unknown:
        unfixable.append({"kind": "unknown-node", "node": tag,
                          "reason": "not an X3D node in any bundled X3DUOM"})

    version = root.get("version")
    doctype = None
    m = _DOCTYPE_RE.search(xml_string)
    if m:
        doctype = _doctype(version)
        if m.group(0) != doctype:
            changes.append({"kind": "doctype", "from": m.group(0), "to": doctype,
                            "reason": "DOCTYPE follows the version"})
    body = etree.tostring(root, pretty_print=True, encoding="unicode",
                          doctype=doctype)
    fixed = '<?xml version="1.0" encoding="UTF-8"?>\n' + body

    schema = validate_xml(fixed)
    remaining = {
        "schema_valid": schema["valid"],
        "schema_errors": schema["errors"],
        "profile_ok": check_profile(root).ok,
        "unfixable": len(unfixable),
    }
    return {"fixed": fixed, "changes": changes, "unfixable": unfixable,
            "remaining": remaining}
