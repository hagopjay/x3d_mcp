"""X3D validation pipeline.

Validates X3D XML content against the XSD schema matching the document's
declared `version` (X3D 4.0 ratified or X3D 4.1 draft) using lxml, and X3D
JSON content against the Web3D Consortium X3D 4.0 JSON Schema using
jsonschema (Draft 2020-12).
"""

import json
import re
import threading
from pathlib import Path
from lxml import etree
from jsonschema import Draft202012Validator

from types import SimpleNamespace

from validation import listvalues, oversize


SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas"
JSON_SCHEMA_PATH = SCHEMAS_DIR / "x3d-4.0-JSONSchema.json"

# Schema used when the document declares no version or an unknown one.
DEFAULT_XSD_VERSION = "4.1"

# Bundled XSD schemas, keyed by the X3D `version` attribute they validate.
# X3D 3.x documents are checked against 4.0: X3D 4 is a superset of 3.3
# (ISO/IEC 19775-1:2023 clause 4.3.1), so a 3.x document that passes is
# conformant, and the 3.x schemas are not bundled.
XSD_PATHS = {
    "4.0": SCHEMAS_DIR / "x3d-4.0.xsd",
    "4.1": SCHEMAS_DIR / "x3d-4.1.xsd",
}
_LEGACY_VERSIONS = {"3.0", "3.1", "3.2", "3.3"}

# Kept for callers that import the old name; always the default schema.
XSD_PATH = XSD_PATHS[DEFAULT_XSD_VERSION]

# XSI namespace attribute that x3d.py adds -- must be stripped before validation
XSI_NS = "https://www.w3.org/2001/XMLSchema-instance"

_schemas: dict[str, etree.XMLSchema] = {}
_json_validator: Draft202012Validator | None = None
_version_nodes: dict[str, set[str]] | None = None


def resolve_xsd_version(declared: str | None) -> tuple[str, str | None]:
    """Map a document's `version` attribute to a bundled schema version.

    Returns (schema_version, note). `note` explains any substitution so the
    caller can surface it; it is None when the declared version has its own
    bundled schema.
    """
    if declared in XSD_PATHS:
        return declared, None
    if declared in _LEGACY_VERSIONS:
        return "4.0", (
            f"X3D {declared} has no bundled schema; validated against X3D 4.0, "
            f"of which X3D {declared} is a subset."
        )
    if declared is None or declared == "":
        return DEFAULT_XSD_VERSION, (
            f"No version attribute; validated against X3D {DEFAULT_XSD_VERSION}."
        )
    return DEFAULT_XSD_VERSION, (
        f"Unknown version '{declared}'; validated against X3D {DEFAULT_XSD_VERSION}."
    )


def _get_schema(version: str = DEFAULT_XSD_VERSION) -> etree.XMLSchema:
    """Load and cache the X3D XSD schema for a bundled version ("4.0" or "4.1")."""
    schema = _schemas.get(version)
    if schema is not None:
        return schema
    # Parse with the schemas dir as base so includes resolve correctly
    schema_doc = etree.parse(str(XSD_PATHS[version]))
    schema = etree.XMLSchema(schema_doc)
    _schemas[version] = schema
    return schema


def _nodes_by_version() -> dict[str, set[str]]:
    """Concrete node names per bundled X3DUOM version, for version hints."""
    global _version_nodes
    if _version_nodes is None:
        from x3d_utils.x3duom import X3DUOM
        _version_nodes = {
            v: set(X3DUOM(SCHEMAS_DIR / f"X3dUnifiedObjectModel-{v}.xml").get_concrete_nodes())
            for v in XSD_PATHS
        }
    return _version_nodes


def _version_hints(doc: etree._Element, schema_version: str) -> list[str]:
    """Explain failures caused by nodes that only exist in a later X3D version.

    A model trained on mixed-era examples reaches for 4.1 nodes (EnvironmentLight,
    GaussianSplats, ...) in a document declaring version 4.0. The XSD only says
    "element not expected"; this names the node and the version that has it.
    """
    nodes = _nodes_by_version()
    known_here = nodes[schema_version]
    hints = []
    seen: set[str] = set()
    for el in doc.iter():
        tag = el.tag if isinstance(el.tag, str) else ""
        if "}" in tag:
            tag = tag.split("}", 1)[1]
        if not tag or tag in known_here or tag in seen:
            continue
        later = sorted(v for v, names in nodes.items() if tag in names and v > schema_version)
        if later:
            seen.add(tag)
            hints.append(
                f"Node '{tag}' is not in X3D {schema_version}; it was added in X3D "
                f"{later[0]}. Declare version='{later[0]}' or remove the node."
            )
    return hints


_MISSING_CHILD_RE = re.compile(r"Element '([A-Za-z0-9_]+)': Missing child element")


def _geolod_hint(el: etree._Element) -> str:
    geo_system = el.get("geoSystem", '"GD" "WE"')
    center = el.get("center", "0 0 0")
    return (
        f"GeoLOD at line {el.sourceline}: the bundled XSD's content model is "
        "(rootNode children, GeoOrigin, rootNode children), so any inline rootNode "
        "child requires a GeoOrigin sibling, although ISO/IEC 19775-1 makes geoOrigin "
        "optional. Either add <GeoOrigin containerField='geoOrigin' "
        f"geoSystem='{geo_system}' geoCoords='{center}'/> inside the GeoLOD (a "
        "GeoOrigin re-anchors the local frame, so give every geospatial node the same "
        "one by USE), or remove the inline children and load the low-detail model "
        "through rootUrl."
    )


# Elements whose XSD "Missing child element(s)" error misleads: lxml lists only
# the first ten expected names, so the one that matters can be left out.
_CONTENT_MODEL_HINTS = {"GeoLOD": _geolod_hint}


def _content_model_hints(doc: etree._Element, errors: list[str]) -> list[str]:
    """Explain a truncated 'Missing child element(s)' error for known elements."""
    hints: list[str] = []
    seen: set[str] = set()
    for err in errors:
        m = _MISSING_CHILD_RE.search(err)
        if not m or m.group(1) in seen or m.group(1) not in _CONTENT_MODEL_HINTS:
            continue
        seen.add(m.group(1))
        tag = m.group(1)
        for el in doc.iter():
            name = el.tag if isinstance(el.tag, str) else ""
            if name.split("}", 1)[-1] == tag:
                hints.append(_CONTENT_MODEL_HINTS[tag](el))
                break
    return hints


def _strip_xsi(root: etree._Element) -> None:
    """Remove xsi namespace attributes that x3d.py injects."""
    for attr in list(root.attrib):
        if XSI_NS in attr:
            del root.attrib[attr]
    # Also remove the xsd namespace prefix declaration if present
    nsmap = dict(root.nsmap)
    if "xsd" in nsmap or "xsi" in nsmap:
        # Can't remove nsmap directly, so rebuild the element
        pass


_reentry = threading.local()
_VERSION_RE = re.compile(r"<X3D\b[^>]{0,2000}?\bversion\s*=\s*[\"']([^\"']*)[\"']")
_field_lookups: dict[str, object] = {}


def _field_type_lookup(schema_version: str):
    """Return f(node, attribute) -> X3D field type name, from the X3DUOM of `schema_version`."""
    lookup = _field_lookups.get(schema_version)
    if lookup is None:
        from x3d_utils.x3duom import X3DUOM
        nodes = X3DUOM(SCHEMAS_DIR / f"X3dUnifiedObjectModel-{schema_version}.xml").get_concrete_nodes()

        def lookup(node: str, attr: str) -> str | None:
            return next((f.get("type") for f in nodes.get(node, {}).get("fields", []) if f["name"] == attr), None)

        _field_lookups[schema_version] = lookup
    return lookup


def _not_checkable(xml_string: str, version: str | None, why: str, exc: etree.XMLSyntaxError | None = None) -> dict:
    """The result for a document an XML limit stopped before the XSD could run (see validation.oversize).

    `exc` is the parse error that hit the limit; without it (the XSD engine gave up after a huge_tree parse) the
    cause is a big start tag.
    """
    declared = version or (_VERSION_RE.search(xml_string[:4096]) or [None, None])[1]
    schema_version, note = resolve_xsd_version(declared)
    kind = oversize.limit_kind(exc) if exc is not None else "start-tag"
    if kind == "start-tag" and not getattr(_reentry, "active", False):
        _reentry.active = True            # the structure check re-enters validate_xml; a second oversize result must not recurse
        try:
            result = oversize.oversize_result(
                xml_string, schema_version=schema_version, huge_note=why,
                validate_reduced=lambda reduced: validate_xml(reduced, schema_version),
                field_types=_field_type_lookup(schema_version))
        finally:
            _reentry.active = False
    else:
        result = oversize.limit_result(kind=kind, libxml2_message=str(exc), schema_version=schema_version, huge_note=why)
    if note:
        result["notes"].insert(0, note)
    return result


def validate_xml(xml_string: str, version: str | None = None) -> dict:
    """Validate X3D XML content against the XSD schema for its declared version.

    The schema is chosen from the document's `version` attribute (4.0 ratified,
    4.1 draft), or from `version` when given, which overrides the document.

    Returns {"valid": bool, "errors": list[str], "schema_version": str} plus
    "notes" when a substitution or a version hint applies. validate_xml_with_hints
    returns the same plus exact-fix "hints". A document with one
    start tag over libxml2's 10,000,000-byte limit cannot be parsed by default:
    the result then has "checked": false, "verdict": "not-checkable-at-this-size"
    and an "oversize" report (see validation.oversize); X3D_MCP_XML_HUGE=on allows
    a bounded retry with huge_tree.
    """
    return _validate_xml(xml_string, version, False)


def validate_xml_with_hints(xml_string: str, version: str | None = None) -> dict:
    """validate_xml, plus "hints" when the document is invalid.

    One structured exact-fix hint per XSD error the hint layer understands
    (validation.hints); each names its error by `error_index` in "errors".
    """
    return _validate_xml(xml_string, version, True)


def _validate_xml(xml_string: str, version: str | None, want_hints: bool) -> dict:
    errors = []
    huge_used = False

    # Step 1: Parse XML (well-formedness)
    try:
        doc = etree.fromstring(xml_string.encode("utf-8"))
    except etree.XMLSyntaxError as e:
        if not oversize.is_resource_limit(e):
            return {"valid": False, "errors": [f"XML parse error: {e}"]}
        refusal = oversize.huge_refusal(xml_string)
        if refusal:
            return _not_checkable(xml_string, version, refusal, e)
        try:
            doc = oversize.parse_huge(xml_string)
        except etree.XMLSyntaxError as e2:
            return {"valid": False, "errors": [f"XML parse error: {e2}"]}
        huge_used = True

    # Step 2: Strip xsi attributes that x3d.py adds
    _strip_xsi(doc)

    # Step 3: Pick the schema the document asks for
    declared = version if version else doc.get("version")
    schema_version, note = resolve_xsd_version(declared)
    notes = [note] if note else []

    # Step 4: Validate against XSD
    schema = _get_schema(schema_version)
    value_entries: list = []
    try:
        valid = schema.validate(doc)
    except etree.XMLSchemaValidateError as e:
        if huge_used:
            # libxml2's regexp engine gives up on one attribute of about 1.3 million numbers
            return _not_checkable(xml_string, version, f"used, but the XSD engine failed: {e}")
        retried = _after_engine_failure(doc, schema, schema_version, e)
        if retried is None:
            return _engine_gave_up(e, schema_version, notes)
        valid, value_entries, engine_notes = retried
        notes.extend(engine_notes)
    hints: list[dict] = []
    if not valid:
        entries = value_entries + list(schema.error_log)
        for entry in entries:
            errors.append(f"Line {entry.line}: {entry.message}")
        notes.extend(_version_hints(doc, schema_version))
        notes.extend(_content_model_hints(doc, errors))
        if want_hints:
            hints = _exact_fix_hints(doc, entries, schema_version)
    if huge_used:
        notes.append("Parsed with huge_tree (X3D_MCP_XML_HUGE=on): libxml2's 10 MB start-tag limit was lifted for this document.")

    result = {"valid": valid, "errors": errors, "schema_version": schema_version}
    if notes:
        result["notes"] = notes
    if hints:
        result["hints"] = hints
    return result


# libxml2 matches SF/MF values with backtracking regexps. On a list value that does not
# match, the backtracking grows with the length of the list before the bad entry, and past
# about a dozen tuples the engine stops with SCHEMAV_INTERNAL ("Internal error in XML Schema
# validation") instead of a pattern error; validation of the whole document ends there.
# libxml2 2.14.6 does this for an out-of-range colour from the 13th entry of an MFColor on,
# and for a bad number in MFVec3f, MFVec2f, MFFloat or MFRotation lists, after about 1.7 s.
_ENGINE_RETRIES = 20


def _engine_targets(doc: etree._Element, log) -> list[etree._Element]:
    """The elements named by the SCHEMAV_INTERNAL entries of an error log (by path, else by line)."""
    out: list[etree._Element] = []
    for entry in log:
        hits = []
        if getattr(entry, "path", None):
            try:
                hits = [h for h in doc.getroottree().xpath(entry.path)
                        if isinstance(h, etree._Element) and h.sourceline == entry.line]
            except etree.XPathError:
                hits = []
        hits = hits or [el for el in doc.iter() if isinstance(el.tag, str) and el.sourceline == entry.line]
        out.extend(h for h in hits if h not in out)
    return out


def _after_engine_failure(doc: etree._Element, schema: etree.XMLSchema, schema_version: str, exc) -> tuple | None:
    """Finish the XSD check of a normal-size document after libxml2's pattern engine gave up.

    Reads the numeric attributes of the element the internal error names number by number
    (validation.listvalues), reports each bad one as a "[value]" error, replaces it with a
    minimal valid value, and runs the XSD again so the rest of the document is checked.
    The document is restored before returning. Returns (valid, value_entries, notes), or
    None when no bad value can be found that way (the engine gave up on something else).
    """
    lookup = _field_type_lookup(schema_version)
    entries, notes, swapped = [], [], []
    try:
        for _ in range(_ENGINE_RETRIES):
            found = False
            for el in _engine_targets(doc, exc.error_log):
                tag = etree.QName(el).localname
                for attr, value in list(el.attrib.items()):
                    ftype = lookup(tag, attr)
                    why = listvalues.problem(value, ftype) if ftype else None
                    if not why:
                        continue
                    found = True
                    entries.append(SimpleNamespace(
                        line=el.sourceline, path=doc.getroottree().getpath(el),
                        message=f"Element '{tag}', attribute '{attr}': [value] {why}."))
                    notes.append(f"libxml2's XSD pattern engine gave up on {tag} {attr} (line {el.sourceline}) "
                                 "with an internal error, which it does on a long list with a bad entry; this is "
                                 "not a size limit. The value was read number by number instead, and the XSD "
                                 "ran again with it replaced, so the rest of the document was checked.")
                    swapped.append((el, attr, value))
                    el.set(attr, listvalues.placeholder(ftype))
            if not found:
                return None
            try:
                valid = schema.validate(doc)
                return (valid and not entries), entries, notes
            except etree.XMLSchemaValidateError as e:
                exc = e
        return None
    finally:
        for el, attr, value in reversed(swapped):
            el.set(attr, value)


def _engine_gave_up(exc, schema_version: str, notes: list[str]) -> dict:
    """The result when libxml2's XSD engine stopped on a normal-size document and no bad value was found."""
    first = next(iter(exc.error_log), None)
    where = (f" on {first.path.rsplit('/', 1)[-1]} at line {first.line}"
             if first is not None and getattr(first, "path", None) else "")
    message = (f"Not checked: libxml2's XSD engine stopped with an internal error{where} before it finished, "
               "so the XSD check is incomplete. This is not a size limit and not a well-formedness error; it "
               "usually means a long list value has a bad entry that the engine could not reject in time.")
    return {"valid": False, "checked": False, "verdict": "xsd-engine-failure", "errors": [message],
            "schema_version": schema_version, **({"notes": notes} if notes else {})}


# Errors past this many get no hint: a document with thousands of errors needs a
# different fix first, and the hints would swamp the report.
MAX_HINTED_ERRORS = 50


def _exact_fix_hints(doc: etree._Element, entries: list, schema_version: str) -> list[dict]:
    """Structured exact-fix hints for XSD errors (see validation.hints); never raises."""
    try:
        from validation.hints import xsd_hints
        return xsd_hints(doc, entries[:MAX_HINTED_ERRORS], schema_version)
    except Exception:          # hints are advisory; validation results must not depend on them
        return []


def validate_scene(scene_manager) -> dict:
    """Validate the current scene by serializing to XML and running XSD checks.

    Returns {"valid": bool, "errors": list[str]}.
    """
    xml_string = scene_manager.to_xml()
    return validate_xml(xml_string)


def _fix_navigationinfo_type(schema: dict) -> None:
    """NavigationInfo.type is an MFString but the 4.0 schema types it as a plain string.

    Of the 2,771 X3D type annotations in the schema ($comment "MFString inputOutput"
    and the like) this is the only one that disagrees with its JSON type, so the
    array form x3d-tidy writes (["EXAMINE", "ANY"]) is rejected and a bare string
    accepted. No-op once upstream corrects it.
    """
    node = schema.get("$defs", {}).get("NavigationInfo", {})
    for variant in node.get("oneOf", [node]):
        prop = variant.get("properties", {}).get("@type")
        if (prop is not None and prop.get("type") == "string"
                and prop.get("$comment", "").startswith("MFString")):
            prop["type"] = "array"
            prop["items"] = {"type": "string"}


# Corrections applied in memory to the bundled upstream schema, which stays byte-identical.
JSON_SCHEMA_FIXES = (_fix_navigationinfo_type,)


def load_json_schema() -> dict:
    """The X3D 4.0 JSON Schema with JSON_SCHEMA_FIXES applied (a fresh copy per call)."""
    with JSON_SCHEMA_PATH.open() as f:
        schema = json.load(f)
    for fix in JSON_SCHEMA_FIXES:
        fix(schema)
    return schema


def _get_json_validator() -> Draft202012Validator:
    """Load and cache the X3D 4.0 JSON Schema validator."""
    global _json_validator
    if _json_validator is not None:
        return _json_validator
    _json_validator = Draft202012Validator(load_json_schema())
    return _json_validator


def _format_json_path(path) -> str:
    """Format a jsonschema absolute_path deque as 'X3D/head/meta/0' or '(root)'."""
    parts = [str(p) for p in path]
    return "/".join(parts) if parts else "(root)"


def validate_json(json_string: str) -> dict:
    """Validate X3D JSON content against the Web3D X3D 4.0 JSON Schema.

    Catches misspelled keys (head/meta/etc.), unknown nodes inside Scene,
    missing required fields, and wrong-type values via Draft 2020-12
    schema validation. Returns {"valid": bool, "errors": list[str]}.
    """
    try:
        data = json.loads(json_string)
    except json.JSONDecodeError as e:
        return {"valid": False, "errors": [f"JSON parse error: {e}"]}

    validator = _get_json_validator()
    errors = [
        f"{_format_json_path(err.absolute_path)}: {err.message}"
        for err in validator.iter_errors(data)
    ]
    return {"valid": len(errors) == 0, "errors": errors}
