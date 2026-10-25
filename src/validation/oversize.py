"""What to do when libxml2 refuses an X3D document because one start tag is over 10,000,000 bytes.

libxml2 (2.14.6 here) stops at ERR_RESOURCE_LIMIT, "Buffer size limit exceeded", when a single start tag, all its
attributes together, is larger than 10 MB. lxml reports that as "XML parse error", which reads like a malformed file.
This module (1) recognises that error, (2) finds the big tags with a regex scan that has no size limit, (3) runs the
splat array checks on them, (4) builds a copy of the document with the big values swapped for a minimal valid value
so the XSD can still check everything else, and (5) holds the policy for the optional, bounded huge_tree retry.
"""

import os
import re
from dataclasses import dataclass

from lxml import etree

from validation.splat_arrays import Finding, check_splat_arrays

START_TAG_LIMIT = 10_000_000
REPORT_MIN_BYTES = 1_000_000
SUBSTITUTE_MIN_BYTES = 65_536
DEFAULT_HUGE_MAX_BYTES = 128 * 1024 * 1024
MAX_VALUE_BYTES = 256 * 1024 * 1024        # attribute text parsed to float64 (up to 4x its size) by the array check

# A start tag is read one token at a time with str.find. A regex with a repeated attribute group keeps backtracking state
# for every repetition (about 500 bytes each in Python 3.13), so an unterminated tag of a few million attributes cost
# gigabytes. Real nodes carry a few dozen attributes; past MAX_ATTRS the tag is treated as malformed.
MAX_ATTRS = 256
_NAME = re.compile(r"[A-Za-z_][\w.:-]*")
_ATTR_HEAD = re.compile(r"""([\w.:-]+)\s*=\s*(["'])""")
_WS = re.compile(r"\s*")


def parse_start_tag(text: str, pos: int, max_attrs: int = MAX_ATTRS):
    """Parse the start tag whose '<' is at text[pos].

    Returns (name, attrs, end) with attrs mapping each attribute to the (start, end) of its value in `text` and `end`
    just past the closing '>', or None when it is not a well-formed start tag: unterminated, a '<' inside a value, a
    value without quotes, no space between attributes, or more than `max_attrs` attributes.
    """
    m = _NAME.match(text, pos + 1)
    if not m:
        return None
    name, i, attrs = m.group(0), m.end(), {}
    for _ in range(max_attrs + 1):
        before = i
        i = _WS.match(text, i).end()
        spaced = i > before
        c = text[i:i + 1]
        if c == ">":
            return name, attrs, i + 1
        if c == "/" and text[i + 1:i + 2] == ">":
            return name, attrs, i + 2
        h = _ATTR_HEAD.match(text, i)
        if not h or not spaced:
            return None
        close = text.find(h.group(2), h.end())
        if close < 0 or text.find("<", h.end(), close) >= 0:
            return None
        attrs[h.group(1)] = (h.end(), close)
        i = close + 1
    return None


# a minimal value the XSD accepts for a multi-valued field, by X3D field type
_PLACEHOLDER = {"MFFloat": "0", "MFDouble": "0", "MFTime": "0", "MFInt32": "0", "MFVec2f": "0 0", "MFVec2d": "0 0",
                "MFVec3f": "0 0 0", "MFVec3d": "0 0 0", "MFVec4f": "0 0 0 0", "MFVec4d": "0 0 0 0",
                "MFColor": "0 0 0", "MFColorRGBA": "0 0 0 1", "MFRotation": "0 0 1 0", "MFQuaternion": "0 0 0 1",
                "MFBool": "true", "MFString": '"x"'}


def is_resource_limit(exc: etree.XMLSyntaxError) -> bool:
    """True when this parse failed on a libxml2 size limit rather than on malformed markup.

    Reads the exception's own code. `exc.error_log` is the parser's log and keeps entries from earlier failed parses
    in the same thread (lxml 6.1.3, libxml2 2.14.6), so scanning it would call a malformed document oversize once any
    oversize document had been seen.
    """
    return exc.code == etree.ErrorTypes.ERR_RESOURCE_LIMIT


def limit_kind(exc: etree.XMLSyntaxError) -> str:
    """Which limit: "start-tag" (a start tag or attribute value over 10,000,000 bytes), "depth", "text-node" or "other"."""
    msg = str(exc)
    if "Buffer size limit" in msg:
        return "start-tag"
    if "Excessive depth" in msg:
        return "depth"
    if "Text node too long" in msg:
        return "text-node"
    return "other"


def huge_enabled() -> bool:
    return os.environ.get("X3D_MCP_XML_HUGE", "off").strip().lower() in ("1", "on", "true", "yes")


def huge_max_bytes() -> int:
    try:
        return int(os.environ.get("X3D_MCP_XML_HUGE_MAX_BYTES", DEFAULT_HUGE_MAX_BYTES))
    except ValueError:
        return DEFAULT_HUGE_MAX_BYTES


def huge_refusal(text: str) -> str:
    """"" when a huge_tree retry is allowed for this document, else the reason it is not."""
    if not huge_enabled():
        return "off (set X3D_MCP_XML_HUGE=on to allow a bounded retry)"
    size = len(text)            # characters; the UTF-8 size differs only for non-ASCII text, which X3D numbers are not
    if size > huge_max_bytes():
        return f"document is {size:,} characters, above X3D_MCP_XML_HUGE_MAX_BYTES={huge_max_bytes():,}"
    if "<!ENTITY" in text or "<!DOCTYPE" in text:
        return "document has a DOCTYPE or declares entities; those documents are never parsed with huge_tree"
    return ""


def parse_huge(text: str) -> etree._Element:
    """The bounded retry parser: huge_tree, but no entity resolution, no network, no DTD."""
    parser = etree.XMLParser(huge_tree=True, resolve_entities=False, no_network=True, load_dtd=False, dtd_validation=False)
    return etree.fromstring(text.encode("utf-8"), parser)


@dataclass
class BigTag:
    name: str
    start: int
    end: int
    line: int
    size: int
    attrs: dict[str, tuple[int, int]]      # attribute -> (value start, value end) in the document text


def find_big_start_tags(text: str, min_bytes: int = REPORT_MIN_BYTES) -> list[BigTag]:
    """Start tags of at least `min_bytes`, found without libxml2. A candidate that is not a well-formed start tag is skipped."""
    out = []
    # A '<' followed by min_bytes non-'<' characters. The name is not part of the pattern (a name group before the long run
    # backtracks quadratically on a long run of word characters); parse_start_tag checks it.
    cand = re.compile(rf"<[^<]{{{min_bytes},}}")
    for m in cand.finditer(text):
        t = parse_start_tag(text, m.start())
        if t:
            name, attrs, end = t
            out.append(BigTag(name, m.start(), end, text.count("\n", 0, m.start()) + 1, end - m.start(), attrs))
    return out


def _sizes(tag: BigTag) -> dict:
    return {k: {"bytes": e - s} for k, (s, e) in tag.attrs.items() if e - s >= 1000}


def splat_findings(text: str, tag: BigTag, counts: dict[str, int]) -> list[Finding]:
    attrs = {k: text[s:e] for k, (s, e) in tag.attrs.items()}
    parse = sum(e - s for s, e in tag.attrs.values()) <= MAX_VALUE_BYTES
    return check_splat_arrays(attrs, strict_numbers=True, counts_out=counts, values=parse)


def reduced_document(text: str, tags: list[BigTag], field_types) -> tuple[str, list[str]]:
    """Copy of `text` with every attribute value over 64 KB swapped for a minimal valid value, plus the list of
    attributes that could not be swapped (unknown field type). `field_types(node, attr)` returns the X3D type name."""
    edits, skipped = [], []
    for tag in tags:
        for attr, (s, e) in tag.attrs.items():
            if e - s < SUBSTITUTE_MIN_BYTES:
                continue
            ftype = field_types(tag.name, attr)
            sub = "0" if ftype is None else _PLACEHOLDER.get(ftype)     # an unknown attribute is the XSD's to report
            if sub is None:
                skipped.append(f"{tag.name}.{attr}")
            else:
                edits.append((s, e, sub))
    if skipped:
        return text, skipped
    for s, e, sub in sorted(edits, reverse=True):
        text = text[:s] + sub + text[e:]
    return text, []


def limit_result(*, kind: str, libxml2_message: str, schema_version: str, huge_note: str) -> dict:
    """The validate_xml result for a document stopped by a libxml2 limit other than the start-tag size (depth, text node)."""
    what = {"start-tag": f"has a start tag larger than libxml2's {START_TAG_LIMIT:,}-byte limit",
            "depth": "nests elements deeper than libxml2's 256-level limit",
            "text-node": "has a text node longer than libxml2's 10,000,000-byte limit"}.get(kind, "hit a libxml2 resource limit")
    message = (f"Not checkable at this size: the document {what}, so the XSD check did not run on it. "
               f"This is not a well-formedness error. libxml2 said: {libxml2_message.strip().splitlines()[0]}")
    return {"valid": False, "checked": False, "verdict": "not-checkable-at-this-size", "errors": [message],
            "schema_version": schema_version,
            "oversize": {"limit": kind, "huge_tree_retry": huge_note},
            "notes": ["Set X3D_MCP_XML_HUGE=on on the server to allow a bounded retry (see oversize.huge_tree_retry)."]}


def oversize_result(text: str, *, schema_version: str, validate_reduced, field_types, huge_note: str) -> dict:
    """The validate_xml result for a document libxml2 would not parse because of a start-tag size limit.

    `valid` stays False (nothing was proven) and `checked` is False; the errors say why in plain words.
    """
    tags = find_big_start_tags(text)
    over = [t for t in tags if t.size >= START_TAG_LIMIT - 10_000]
    report = {
        "start_tag_limit_bytes": START_TAG_LIMIT,
        "huge_tree_retry": huge_note,
        "tags": [],
    }
    findings_all: list[Finding] = []
    for t in tags:
        entry = {"element": t.name, "line": t.line, "start_tag_bytes": t.size, "attributes": _sizes(t)}
        if t.name == "GaussianSplats":
            counts: dict[str, int] = {}
            fs = splat_findings(text, t, counts)
            for k, n in counts.items():
                entry["attributes"].setdefault(k, {})["numbers"] = n
            entry["splats"] = counts.get("positions", 0) // 3
            entry["array_check"] = {"errors": [f.message for f in fs if f.level == "error"],
                                    "warnings": [f.message for f in fs if f.level == "warning"]}
            findings_all += fs
        report["tags"].append(entry)
    reduced, skipped = reduced_document(text, tags, field_types)
    if not tags:
        report["structure_check"] = {"ran": False, "reason": "the oversized start tag could not be located, so nothing could be replaced"}
    elif skipped:
        report["structure_check"] = {"ran": False, "reason": "no placeholder for " + ", ".join(skipped)}
    else:
        sr = validate_reduced(reduced)
        if sr.get("checked") is False:        # still over a libxml2 limit after the big values were replaced
            report["structure_check"] = {"ran": False, "reason": sr["errors"][0]}
        else:
            unchecked = [f"{t.name}.{k} (line {t.line})" for t in tags if t.name != "GaussianSplats"
                         for k, (a, b) in t.attrs.items() if b - a >= SUBSTITUTE_MIN_BYTES]
            note = ("the XSD ran on a copy with each big attribute value replaced by one minimal valid value; the replaced "
                    "values of GaussianSplats nodes were checked by the array check")
            note += ("; the replaced values listed in values_not_checked were not checked at all (their counts and numbers are "
                     "unverified: validate_semantic checks tuple counts)" if unchecked else "")
            report["structure_check"] = {"ran": True, "valid": sr["valid"], "errors": sr["errors"][:10], "note": note,
                                         **({"values_not_checked": unchecked[:20]} if unchecked else {})}
    if tags:
        names = ", ".join(f"{t.name} (line {t.line}, {t.size:,} bytes)" for t in over or tags[:3])
        message = (f"Not checkable at this size: {names} is larger than libxml2's {START_TAG_LIMIT:,}-byte start-tag limit, "
                   "so the XSD check did not run on this document. This is not a well-formedness error.")
    else:      # libxml2 hit the limit, but the oversized tag cannot be read as a well-formed tag here either
        message = (f"Not checkable at this size: libxml2 stopped at its {START_TAG_LIMIT:,}-byte start-tag limit, and the oversized "
                   "start tag could not be read as a well-formed tag either (unterminated, an unquoted value, or more than "
                   f"{MAX_ATTRS} attributes), so the document may also be malformed. The XSD check did not run.")
    errors = [message] + [f.message for f in findings_all if f.level == "error"]
    return {"valid": False, "checked": False, "verdict": "not-checkable-at-this-size", "errors": errors,
            "schema_version": schema_version, "oversize": report,
            "notes": ["Split the splats into chunks (splat_chunk sizes each one to stay under the limit: about 60,000 splats per node at "
                      "6-decimal digits without spherical harmonics, fewer with them) so every node can be checked exactly, "
                      "or set X3D_MCP_XML_HUGE=on on the server to allow a bounded retry (see oversize.huge_tree_retry)."]}
