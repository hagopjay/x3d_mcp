"""One structured validator report for a scene: the data the loop, the page
and the run log all read.

Layers, in the order an author meets them:

1. ``xml``       well-formedness (lxml)
2. ``xsd``       the X3D XSD for the declared version (validation.validate)
3. ``semantic``  DEF/USE, ROUTE fields and types, containerField, profile and
                 component statements, tuple counts, GaussianSplats data, ...
                 (validation.semantic)

A scene passes when it is well-formed, schema-valid, and has no semantic
error. Warnings and infos are reported and never block.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import asdict

from lxml import etree

from validation.repair import repair_x3d
from validation.semantic import NoSceneError, semantic_diagnostics
from validation.validate import validate_xml

_LINE_RE = re.compile(r"^Line (\d+): (.*)$", re.S)
_PARSE_LINE_RE = re.compile(r"line (\d+)", re.I)
_PARSER = etree.XMLParser(huge_tree=True, resolve_entities=False, no_network=True)

# Plain-language hints for the XSD messages a model meets most. The XSD text is
# exact but terse; the hint names the usual cause. Matched in order.
_XSD_HINTS = [
    (re.compile(r"attribute '(\w+)' is not allowed", re.I),
     "'{0}' is not a field of this node in the declared X3D version: check the "
     "spelling, or whether it was added in a later version."),
    (re.compile(r"Element '(\w+)': This element is not expected", re.I),
     "'{0}' is not allowed here: the parent does not accept it, it belongs in a "
     "different place (head vs Scene), or it is not a node of this X3D version."),
    (re.compile(r"is not a valid value of the atomic type 'xs:boolean'", re.I),
     "SFBool values are written true or false (lower case)."),
    (re.compile(r"is not a valid value of the (?:list|atomic) type '(\w+)'", re.I),
     "the value does not match the field type {0}: check the number of values "
     "and that every value is a number."),
    (re.compile(r"Missing child element", re.I),
     "a required child is missing (for example a Shape needs geometry, a "
     "GeoLOD inline child needs a GeoOrigin sibling)."),
    (re.compile(r"is not an element of the set", re.I),
     "the value is not one of the enumerated choices for this field."),
]


def _hint(message: str) -> str:
    for pattern, text in _XSD_HINTS:
        m = pattern.search(message)
        if m:
            return text.format(*m.groups())
    return ""


def _xsd_errors(raw: list[str]) -> list[dict]:
    out = []
    for item in raw:
        m = _LINE_RE.match(item)
        line, msg = (int(m.group(1)), m.group(2)) if m else (None, item)
        if msg.startswith("XML parse error"):
            pm = _PARSE_LINE_RE.search(msg)
            out.append({"layer": "xml", "level": "error", "line": int(pm.group(1)) if pm else None,
                        "check": "well-formed", "message": msg, "hint":
                        "the document is not well-formed XML: an unclosed tag, a stray "
                        "character, or text cut off before the end."})
            continue
        out.append({"layer": "xsd", "level": "error", "line": line, "check": "schema",
                    "message": msg, "hint": _hint(msg)})
    return out


_ATTR_RE = re.compile(r"\b(\w+)='([^']+)'")


def _locate(root: etree._Element | None, node_tag: str, def_name: str,
            message: str = "") -> int | None:
    """Best line for a semantic diagnostic: an element carrying an attribute the
    message quotes (toField='set_rotaton'), else its DEF, else the first node of
    its type. Semantic checks do not carry lines, so the page marks these as
    located, not reported."""
    if root is None:
        return None
    for attr, value in _ATTR_RE.findall(message):
        if attr in ("DEF", "level", "name"):
            continue
        for el in root.iter():
            if isinstance(el.tag, str) and el.get(attr) == value:
                return el.sourceline
    if def_name:
        for el in root.iter():
            if isinstance(el.tag, str) and (el.get("DEF") == def_name):
                return el.sourceline
        for el in root.iter():
            if isinstance(el.tag, str) and (el.get("USE") == def_name
                                             or el.get("fromNode") == def_name
                                             or el.get("toNode") == def_name):
                return el.sourceline
    if node_tag:
        for el in root.iter():
            if isinstance(el.tag, str) and el.tag.split("}")[-1] == node_tag:
                return el.sourceline
    return None


def _stats(root: etree._Element | None, text: str) -> dict:
    stats = {"bytes": len(text.encode("utf-8")), "lines": text.count("\n") + 1,
             "nodes": 0, "defs": 0, "uses": 0, "routes": 0, "version": None, "profile": None}
    if root is None:
        return stats
    stats["version"] = root.get("version")
    stats["profile"] = root.get("profile")
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        tag = el.tag.split("}")[-1]
        if tag in ("X3D", "head", "meta", "component", "unit", "Scene"):
            continue
        if tag == "ROUTE":
            stats["routes"] += 1
            continue
        stats["nodes"] += 1
        stats["defs"] += 1 if el.get("DEF") else 0
        stats["uses"] += 1 if el.get("USE") else 0
    return stats


def scene_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def full_report(text: str, *, with_repair_preview: bool = True) -> dict:
    """Validate ``text`` on every layer and return one JSON-ready report.

    Keys: verdict ("pass" | "fail"), counts, findings (each with layer, level,
    line, check, message, hint), schema_version, notes, stats, sha256, timings
    in ms and, when asked, what repair_x3d could fix mechanically.
    """
    t0 = time.perf_counter()
    root = None
    findings: list[dict] = []
    try:
        root = etree.fromstring(text.encode("utf-8"), _PARSER)
    except etree.XMLSyntaxError as exc:
        findings.append({"layer": "xml", "level": "error", "line": exc.lineno,
                         "check": "well-formed", "message": str(exc), "hint":
                         "the document is not well-formed XML: an unclosed tag, a stray "
                         "character, or text cut off before the end."})
    t_parse = time.perf_counter()

    schema_version, notes, xsd_valid = None, [], False
    if root is not None:
        xsd = validate_xml(text)
        xsd_valid = bool(xsd.get("valid"))
        schema_version = xsd.get("schema_version")
        notes = list(xsd.get("notes", []))
        findings.extend(_xsd_errors(xsd.get("errors", [])))
    t_xsd = time.perf_counter()

    if root is not None:
        try:
            for d in semantic_diagnostics(text):
                item = asdict(d)
                findings.append({"layer": "semantic", "level": item["level"],
                                 "line": _locate(root, item["node_tag"], item["def_name"],
                                                 item["message"]),
                                 "located": True,
                                 "check": item["check"], "message": item["message"],
                                 "hint": "", "node": item["node_tag"] or None,
                                 "def": item["def_name"] or None})
        except NoSceneError as exc:
            findings.append({"layer": "semantic", "level": "error", "line": None,
                             "check": "no-scene", "message": str(exc), "hint":
                             "every X3D document needs a <Scene> element after <head>."})
        except etree.XMLSyntaxError:
            pass
    t_sem = time.perf_counter()

    errors = [f for f in findings if f["level"] == "error"]
    counts = {
        "errors": len(errors),
        "warnings": sum(1 for f in findings if f["level"] == "warning"),
        "infos": sum(1 for f in findings if f["level"] == "info"),
        "xml": sum(1 for f in errors if f["layer"] == "xml"),
        "xsd": sum(1 for f in errors if f["layer"] == "xsd"),
        "semantic": sum(1 for f in errors if f["layer"] == "semantic"),
    }
    report = {
        "verdict": "pass" if (root is not None and xsd_valid and counts["semantic"] == 0) else "fail",
        "wellformed": root is not None,
        "xsd_valid": xsd_valid,
        "schema_version": schema_version,
        "notes": notes,
        "counts": counts,
        "findings": findings,
        "stats": _stats(root, text),
        "sha256": scene_hash(text),
        "ms": {"parse": round((t_parse - t0) * 1000, 1),
               "xsd": round((t_xsd - t_parse) * 1000, 1),
               "semantic": round((t_sem - t_xsd) * 1000, 1)},
    }
    if with_repair_preview and root is not None and report["verdict"] == "fail":
        report["auto_repair"] = repair_preview(text)
    report["ms"]["total"] = round((time.perf_counter() - t0) * 1000, 1)
    return report


def repair_preview(text: str) -> dict:
    """What repair_x3d would change, without the fixed document."""
    try:
        r = repair_x3d(text)
    except Exception as exc:  # repair is advisory; never fail the report on it
        return {"available": False, "error": str(exc)[:200]}
    if r.get("error"):
        return {"available": False, "error": r["error"]}
    return {"available": bool(r.get("changes")), "changes": r.get("changes", []),
            "unfixable": r.get("unfixable", []), "remaining": r.get("remaining", {})}


def apply_auto_repair(text: str) -> tuple[str, list[dict]] | None:
    """Run repair_x3d; return (fixed, changes) when it changed something."""
    try:
        r = repair_x3d(text)
    except Exception:
        return None
    if r.get("error") or not r.get("changes"):
        return None
    return r["fixed"], r["changes"]


def defects_for_prompt(report: dict, limit: int = 40) -> str:
    """The exact defect list a writer receives, one line each."""
    lines = []
    for f in report["findings"]:
        if f["level"] != "error":
            continue
        where = f"line {f['line']}" if f.get("line") else "document"
        hint = f" Hint: {f['hint']}" if f.get("hint") else ""
        lines.append(f"- [{f['layer']}/{f['check']}] {where}: {f['message']}{hint}")
        if len(lines) >= limit:
            lines.append(f"- ... and {report['counts']['errors'] - limit} more")
            break
    for n in report.get("notes", [])[:6]:
        lines.append(f"- [note] {n}")
    return "\n".join(lines)
