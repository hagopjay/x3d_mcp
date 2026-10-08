"""X3D validation pipeline.

Validates X3D XML content against the XSD schema matching the document's
declared `version` (X3D 4.0 ratified or X3D 4.1 draft) using lxml, and X3D
JSON content against the Web3D Consortium X3D 4.0 JSON Schema using
jsonschema (Draft 2020-12).
"""

import json
from pathlib import Path
from lxml import etree
from jsonschema import Draft202012Validator


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


def validate_xml(xml_string: str, version: str | None = None) -> dict:
    """Validate X3D XML content against the XSD schema for its declared version.

    The schema is chosen from the document's `version` attribute (4.0 ratified,
    4.1 draft), or from `version` when given, which overrides the document.

    Returns {"valid": bool, "errors": list[str], "schema_version": str} plus
    "notes" when a substitution or a version hint applies.
    """
    errors = []

    # Step 1: Parse XML (well-formedness)
    try:
        doc = etree.fromstring(xml_string.encode("utf-8"))
    except etree.XMLSyntaxError as e:
        return {"valid": False, "errors": [f"XML parse error: {e}"]}

    # Step 2: Strip xsi attributes that x3d.py adds
    _strip_xsi(doc)

    # Step 3: Pick the schema the document asks for
    declared = version if version else doc.get("version")
    schema_version, note = resolve_xsd_version(declared)
    notes = [note] if note else []

    # Step 4: Validate against XSD
    schema = _get_schema(schema_version)
    valid = schema.validate(doc)
    if not valid:
        for entry in schema.error_log:
            errors.append(f"Line {entry.line}: {entry.message}")
        notes.extend(_version_hints(doc, schema_version))

    result = {"valid": valid, "errors": errors, "schema_version": schema_version}
    if notes:
        result["notes"] = notes
    return result


def validate_scene(scene_manager) -> dict:
    """Validate the current scene by serializing to XML and running XSD checks.

    Returns {"valid": bool, "errors": list[str]}.
    """
    xml_string = scene_manager.to_xml()
    return validate_xml(xml_string)


def _get_json_validator() -> Draft202012Validator:
    """Load and cache the X3D 4.0 JSON Schema validator."""
    global _json_validator
    if _json_validator is not None:
        return _json_validator
    with JSON_SCHEMA_PATH.open() as f:
        schema = json.load(f)
    _json_validator = Draft202012Validator(schema)
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
