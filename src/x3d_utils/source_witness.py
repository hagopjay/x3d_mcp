"""Source witnesses and guarded single-attribute edits for X3D documents.

Adapted from the Semantic Twin Lab Evidence Edition companion (xml_witness.py, 16 Nov 2026), contributed
to this project by its owner. A lexical XML inspector, not an XSD or X3D semantic validator: external DTDs
are never loaded, source bytes are never reserialised, and custom entity declarations are refused.

audit_scene(source)      byte-located witnesses for duplicate DEF, unresolved / forward / wrong-type USE and
                         dangling ROUTE endpoints, each bound to the exact source sha256
patch_attribute(...)     replace one existing attribute value on a unique DEF, refusing a stale source hash,
                         an ambiguous target or an unexpected old value; every other byte is kept
"""
from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from xml.parsers import expat

MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_ELEMENTS = 100_000
MAX_TAG_BYTES = 65_536
MAX_DEPTH = 256
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\Z")


class SourceError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class _Element:
    name: str
    attrs: dict
    start: int
    end: int
    path: str
    scope: int
    line: int
    column: int


def _digest(source: bytes) -> str:
    return hashlib.sha256(source).hexdigest()


def _source_bytes(source: bytes) -> bytes:
    if not isinstance(source, bytes):
        raise TypeError("source must be bytes")
    if len(source) > MAX_SOURCE_BYTES:
        raise SourceError("source-too-large", f"source exceeds {MAX_SOURCE_BYTES} bytes")
    try:
        source.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceError("encoding-unsupported", "only UTF-8 XML is supported") from exc
    return source


def _tag_end(source: bytes, start: int) -> int:
    quote = None
    for i in range(start, min(len(source), start + MAX_TAG_BYTES)):
        char = source[i]
        if quote:
            if char == quote:
                quote = None
        elif char in (34, 39):
            quote = char
        elif char == 62:
            return i + 1
    raise SourceError("tag-too-large", "unterminated or oversized XML start tag")


def _parse(source: bytes) -> tuple[list[_Element], dict]:
    source = _source_bytes(source)
    parser = expat.ParserCreate()
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.ExternalEntityRefHandler = lambda *args: 1
    elements = []
    stack = []
    scopes = 0
    doctype = {"present": False, "external_dtd_loaded": False}

    def declaration(version, encoding, standalone):
        if encoding and encoding.lower().replace("-", "") != "utf8":
            raise SourceError("encoding-unsupported", "only UTF-8 XML is supported")

    def entity(*args):
        raise SourceError("entity-declaration-refused", "custom XML entity declarations are not supported")

    def dtd(name, system, public, internal):
        doctype.update(present=True, name=name, has_external_identifier=bool(system or public),
                       has_internal_subset=bool(internal))

    def start(name, attrs):
        nonlocal scopes
        if len(elements) >= MAX_ELEMENTS:
            raise SourceError("too-many-elements", f"source exceeds {MAX_ELEMENTS} elements")
        if len(stack) >= MAX_DEPTH:
            raise SourceError("source-too-deep", f"source exceeds {MAX_DEPTH} nested elements")
        if stack:
            counts = stack[-1][2]
            counts[name] = counts.get(name, 0) + 1
            path = f"{stack[-1][0]}/{name}[{counts[name]}]"
            scope = stack[-1][1]
        else:
            path, scope = f"/{name}[1]", 0
        if name in ("Scene", "ProtoBody"):
            scopes += 1
            scope = scopes
        offset = parser.CurrentByteIndex
        line, column = parser.CurrentLineNumber, parser.CurrentColumnNumber + 1
        elements.append(_Element(name, dict(attrs), offset, _tag_end(source, offset), path,
                                 scope, line, column))
        stack.append((path, scope, {}))

    parser.XmlDeclHandler = declaration
    parser.EntityDeclHandler = entity
    parser.StartDoctypeDeclHandler = dtd
    parser.StartElementHandler = start
    parser.EndElementHandler = lambda name: stack.pop()
    try:
        parser.Parse(source, True)
    except SourceError:
        raise
    except expat.ExpatError as exc:
        raise SourceError("xml-invalid", f"XML is not well formed at line {exc.lineno}, column {exc.offset + 1}: {expat.ErrorString(exc.code)}") from exc
    return elements, doctype


def _witness(element: _Element, digest: str) -> dict:
    return {"source_sha256": digest, "start_byte": element.start, "end_byte": element.end,
            "line": element.line, "column": element.column, "element": element.name,
            "def": element.attrs.get("DEF"), "xml_path": element.path}


def audit_scene(source: bytes) -> dict:
    """Inspect source identity/order without resolving entities or fetching a DTD.

    ``ok`` means the inspector ran; ``clean`` means its bounded checks found no
    errors. Neither value certifies schema validity, visibility, or completeness.
    Witness byte ranges point to opening tags in the exact hashed source.
    """
    if not isinstance(source, bytes):
        raise TypeError("source must be bytes")
    digest = _digest(source)
    base = {"schema": "semantic-twin-source-audit/v1", "source_sha256": digest,
            "source_bytes": len(source), "limitations": [
                "No XSD, field-value, containerField, PROTO expansion, imported-scene, or render validation.",
                "DEF/USE/ROUTE checks apply within lexical Scene and ProtoBody scopes; external DTD defaults are not loaded.",
                "Witnesses locate opening tags within the exact source hash; they are not stable across edits."]}
    try:
        elements, doctype = _parse(source)
    except SourceError as exc:
        return {**base, "ok": False, "clean": False, "elements": None,
                "findings": [{"severity": "error", "code": exc.code, "message": str(exc)}]}
    findings = []
    definitions = {}
    imports = {}
    for el in elements:
        if "DEF" in el.attrs:
            definitions.setdefault((el.scope, el.attrs["DEF"]), []).append(el)
        if el.name == "IMPORT":
            alias = el.attrs.get("AS", el.attrs.get("importedDEF"))
            if alias:
                imports.setdefault(el.scope, set()).add(alias)

    def add(code, message, el, **extra):
        findings.append({"severity": "error", "code": code, "message": message,
                         "witness": _witness(el, digest), **extra})

    for (scope, name), defs in definitions.items():
        for el in defs[1:]:
            add("duplicate-def", f"DEF {name!r} appears more than once in this lexical execution scope", el,
                related_witness=_witness(defs[0], digest))
    for el in elements:
        use = el.attrs.get("USE")
        if use is not None:
            defs = definitions.get((el.scope, use), [])
            if not defs:
                add("unresolved-use", f"USE {use!r} has no DEF in this lexical execution scope", el)
            else:
                prior = [d for d in defs if d.start < el.start]
                if not prior:
                    add("forward-use", f"USE {use!r} precedes its DEF", el,
                        related_witness=_witness(defs[0], digest))
                target = prior[0] if prior else defs[0]
                if target.name != el.name:
                    add("use-type-mismatch", f"{el.name} USE {use!r} refers to a {target.name}", el,
                        related_witness=_witness(target, digest))
        if el.name == "ROUTE":
            for field in ("fromNode", "toNode"):
                target = el.attrs.get(field)
                if target and (el.scope, target) not in definitions and target not in imports.get(el.scope, set()):
                    add("dangling-route", f"ROUTE {field} {target!r} has no local DEF or IMPORT alias", el,
                        field=field, target=target)
    return {**base, "ok": True, "clean": not findings, "elements": len(elements),
            "definitions": sum(map(len, definitions.values())), "doctype": doctype,
            "findings": findings}


def _attribute_spans(source: bytes, element: _Element) -> dict:
    """Tokenise one already-parsed start tag; return value-only byte ranges."""
    i, end = element.start + 1, element.end
    while i < end and source[i] not in b" \t\r\n/>":
        i += 1
    spans = {}
    while i < end:
        while i < end and source[i] in b" \t\r\n":
            i += 1
        if i >= end or source[i] in b"/>":
            break
        first = i
        while i < end and source[i] not in b" \t\r\n=":
            i += 1
        name = source[first:i].decode("utf-8")
        while i < end and source[i] in b" \t\r\n":
            i += 1
        if i >= end or source[i] != 61:
            raise SourceError("attribute-scan-failed", "cannot locate attribute assignment")
        i += 1
        while i < end and source[i] in b" \t\r\n":
            i += 1
        if i >= end or source[i] not in (34, 39):
            raise SourceError("attribute-scan-failed", "cannot locate attribute quote")
        quote = source[i]
        value_start = i + 1
        value_end = source.find(bytes([quote]), value_start, end)
        if value_end < 0:
            raise SourceError("attribute-scan-failed", "cannot locate closing attribute quote")
        spans[name] = (value_start, value_end, chr(quote))
        i = value_end + 1
    return spans


def _escape(value: str, quote: str) -> bytes:
    if not isinstance(value, str):
        raise TypeError("value must be text")
    for char in value:
        point = ord(char)
        if not (point in (9, 10, 13) or 0x20 <= point <= 0xD7FF or 0xE000 <= point <= 0xFFFD or 0x10000 <= point <= 0x10FFFF):
            raise SourceError("xml-character-invalid", "value contains a character forbidden in XML 1.0")
    escaped = value.replace("&", "&amp;").replace("<", "&lt;").replace(
        quote, "&quot;" if quote == '"' else "&apos;")
    escaped = escaped.replace("\t", "&#9;").replace("\r", "&#13;").replace("\n", "&#10;")
    return escaped.encode("utf-8")


def patch_attribute(source: bytes, *, def_name: str, attribute: str, value: str,
                    expected_sha256: str, expected_value: str | None = None) -> tuple[bytes, dict]:
    """Replace exactly one existing field value, guarded by the complete source.

    Unique DEF selection is required across the document. DEF, USE and XML
    namespace edits are refused. The field value is XML-escaped but is not
    checked against the X3D field type or schema.
    """
    source = _source_bytes(source)
    digest = _digest(source)
    if not isinstance(expected_sha256, str) or not hmac.compare_digest(digest, expected_sha256):
        raise SourceError("source-conflict", "source SHA-256 differs from the expected revision")
    if not isinstance(def_name, str) or not def_name:
        raise SourceError("target-invalid", "a nonempty DEF name is required")
    if not isinstance(attribute, str) or not _NAME.fullmatch(attribute) or attribute in ("DEF", "USE", "xmlns"):
        raise SourceError("attribute-refused", "edit one existing nonidentity, nonnamespace XML attribute")
    elements, _ = _parse(source)
    targets = [el for el in elements if el.attrs.get("DEF") == def_name]
    if len(targets) != 1:
        raise SourceError("target-ambiguous" if targets else "target-missing", "the DEF target must occur exactly once in the source")
    el = targets[0]
    if attribute not in el.attrs:
        raise SourceError("attribute-missing", "only existing attributes can be edited; DTD defaults are not loaded")
    before = el.attrs[attribute]
    if expected_value is not None and before != expected_value:
        raise SourceError("value-conflict", "attribute value differs from the expected decoded XML value")
    start, end, quote = _attribute_spans(source, el)[attribute]
    replacement = _escape(value, quote)
    output = source[:start] + replacement + source[end:]
    _parse(output)  # Check that the one replacement remains bounded, well-formed XML.
    receipt = {"schema": "semantic-twin-attribute-edit/v1", "operation": "replace-existing-attribute",
               "source_sha256": digest, "output_sha256": _digest(output),
               "witness": _witness(el, digest), "attribute": attribute,
               "before_value": before, "after_value": value,
               "replaced_byte_range": [start, end],
               "replacement_bytes": len(replacement), "quote": quote,
               "validation": "well-formed XML only; X3D schema and field semantics were not checked"}
    return output, receipt
