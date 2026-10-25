"""X3D JSON encoding (ISO/IEC 19776-5, as in x3d-4.0-JSONSchema.json) to X3D XML.

Strict and model-independent: no repairs. A wrong shape becomes wrong XML (or a
JsonConversionError that names the JSON path), so the exact validators see it.
What the converter adds is not repair but the information XML carries and a JSON
object does not:

- the fixed root boilerplate (xmlns:xsd and the schema location for the version);
- containerField, when a '-field' key names a field other than the node's default
  (defaults come from the bundled X3DUOM, which agrees with the XSD for every node);
- member order where the XSD fixes one. JSON object members are unordered, XML
  children are not, so the writer sorts: X3D = head, Scene; head = component, unit,
  meta; ProtoDeclare = ProtoInterface, ProtoBody; IS first and a metadata field
  next in every other node; field before IS in Script and the shader nodes. Everything else
  keeps the order written, and array items always do;
- Script and shader source: "#sourceCode" (a string or a list of lines) becomes
  character content in a CDATA section.

Dropped on purpose: "#comment" members, and the root members "encoding" and
"JSON schema" that only exist for the JSON file format.
"""

import json
import math
import re
from xml.sax.saxutils import escape, quoteattr

from x3d_utils.x3duom import get_x3duom

XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
SCHEMA_LOCATION = "https://www.web3d.org/specifications/x3d-{}.xsd"
KNOWN_VERSIONS = ("4.0", "4.1")

# Deepest node nesting accepted. Real scenes stay well under 50 (the 65-scene
# control peaks at 12 nodes deep); the cap keeps a hostile document from reaching
# the interpreter's recursion limit.
MAX_DEPTH = 200

# Most elements accepted. The largest scene measured (a 37.6 MB JSON file of mesh
# arrays) has about 5,400; a million nodes of a few bytes each would otherwise cost
# seconds of CPU and several gigabytes per 100 MB of input. Arrays are not counted:
# they are one attribute.
MAX_ELEMENTS = 1_000_000

# Indentation stops growing at this depth. It is cosmetic (XML ignores it between
# elements) and without a cap a document of {"-children":[{"Group":...}]} levels
# written 200 deep grows about 30-fold on output.
MAX_INDENT = 24

# Namespace prefixes the writer can use without declaring them: xml is built in,
# xsd is declared on the root, xmlns is the declaration syntax itself.
_PREFIXES = frozenset({"xml", "xsd", "xmlns"})

# Longest JSON path quoted in an error message.
_PATH_CLIP = 240

# Members that exist for the JSON file format, not for the scene.
_IGNORED_MEMBERS = frozenset({"#comment", "encoding", "JSON schema"})

# Where the XSD fixes the order of a parent's children. Names not listed sort
# last, in the order written.
_RANK = {
    "X3D": {"head": 0, "Scene": 1},
    "head": {"component": 0, "unit": 1, "meta": 2},
    "ProtoDeclare": {"ProtoInterface": 0, "ProtoBody": 1},
    "Script": {"field": 0, "IS": 1},
    "ComposedShader": {"field": 0, "IS": 1},
    "PackagedShader": {"field": 0, "IS": 1},
    "ShaderProgram": {"field": 0, "IS": 1},
}

_CHUNK = 1 << 16
_NUMBER_TYPES = {int, float}      # bool is a separate type here, so True and False take the general path
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*(:[A-Za-z_][A-Za-z0-9_.-]*)?$")
_XML_BAD_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


class JsonConversionError(ValueError):
    """The document is not X3D JSON that can be written as XML; the message names the JSON path."""


def _fail(path: list[str], message: str):
    where = "/".join(path) or "(root)"
    if len(where) > _PATH_CLIP:                  # a hostile document can nest 200 levels
        where = f"{where[:_PATH_CLIP // 2]}.../{where[-_PATH_CLIP // 2:]}"
    raise JsonConversionError(f"{where}: {message}")


def _check_name(name: str, path: list[str], what: str) -> str:
    if not _NAME_RE.match(name):
        _fail(path, f"{what} name {name[:60]!r} is not a valid XML name")
    prefix = name.partition(":")[0] if ":" in name else None
    if prefix is not None and prefix not in _PREFIXES:
        _fail(path, f"{what} name {name[:60]!r} uses the namespace prefix {prefix!r}, which the document does not declare")
    return name


def _string(value: str, path: list[str]) -> str:
    if _XML_BAD_CHARS.search(value):
        _fail(path, "string holds a character XML 1.0 cannot carry (a control character, a lone surrogate or U+FFFE/U+FFFF)")
    return value


def _scalar(value, path: list[str]) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            _fail(path, f"{value!r} is not an X3D number")
        return repr(value)
    if isinstance(value, str):
        return _string(value, path)
    _fail(path, f"unsupported attribute value of type {type(value).__name__}")


def _attribute(value, path: list[str]) -> str:
    """The XML attribute text for a JSON field value (the SF/MF text encodings)."""
    if not isinstance(value, list):
        return _scalar(value, path)
    if set(map(type, value)) <= _NUMBER_TYPES:        # the common case, millions of numbers: no copy, no per-item call
        text = " ".join([" ".join(map(repr, value[i:i + _CHUNK])) for i in range(0, len(value), _CHUNK)])   # chunks keep the strings alive briefly
        if "n" in text:                                # repr of a non-finite float is nan, inf or -inf
            bad = next(v for v in value if isinstance(v, float) and not math.isfinite(v))
            _fail(path, f"{bad!r} is not an X3D number")
        return text
    flat: list = []
    stack = [iter(value)]
    while stack:                                 # nested arrays (tuples) flatten
        for item in stack[-1]:
            if isinstance(item, list):
                stack.append(iter(item))
                break
            flat.append(item)
        else:
            stack.pop()
    if flat and all(isinstance(i, str) for i in flat):          # MFString
        return " ".join('"' + _string(i, path).replace("\\", "\\\\").replace('"', '\\"') + '"'
                        for i in flat)
    return " ".join(_scalar(i, path) for i in flat)


def _default_container_field(name: str) -> str | None:
    return get_x3duom().get_container_field(name)


class _Writer:
    def __init__(self, canonical: bool):
        self.canonical = canonical
        self.out: list[str] = []
        self.elements = 0

    def _rank(self, parent: str, name: str, field: str | None) -> int:
        table = _RANK.get(parent)
        if table is not None:
            return table.get(name, 99)
        if name == "IS":
            return 0
        return 1 if (field or _default_container_field(name)) == "metadata" else 2

    def node(self, name: str, body, field: str | None, depth: int, path: list[str]) -> None:
        if depth > MAX_DEPTH:
            _fail(path, f"nodes nested deeper than {MAX_DEPTH}")
        self.elements += 1
        if self.elements > MAX_ELEMENTS:
            _fail(path, f"more than {MAX_ELEMENTS:,} elements; send a scene this large as XML")
        if not isinstance(body, dict):
            _fail(path, f"{name} is a {type(body).__name__}, not an object")
        attrs: list[tuple[str, str]] = []
        kids: list[tuple[str, str | None, object, list[str]]] = []     # (name, field, body, path)
        text = None
        for key, value in body.items():
            kpath = path + [key]
            if key.startswith("@"):
                if value is not None:
                    attrs.append((_check_name(key[1:], kpath, "attribute"), _attribute(value, kpath)))
            elif key == "-children":
                if not isinstance(value, list):
                    _fail(kpath, "-children is not an array")
                self._nodes(kids, None, value, path, key)
            elif key.startswith("-"):
                if key == "-":
                    _fail(kpath, "'-' names no field")
                self._nodes(kids, key[1:], value if isinstance(value, list) else [value], path, key)
            elif key == "#sourceCode" and self.canonical:
                lines = value if isinstance(value, list) else [value]
                if not all(isinstance(x, str) for x in lines):
                    _fail(kpath, "#sourceCode is not a string or a list of strings")
                text = _string("\n".join(lines), kpath)
            elif key in _IGNORED_MEMBERS:
                continue
            else:                                # statements: head.meta, IS.connect, Script.field, ...
                items = value if isinstance(value, list) else [value]
                for i, item in enumerate(items):
                    kids.append((_check_name(key, kpath, "node"), None, item,
                                 path + [f"{key}[{i}]" if isinstance(value, list) else key]))
        default = _default_container_field(name) if field else None
        if field and default not in (None, field):
            if any(a == "containerField" for a, _ in attrs):
                _fail(path, f"@containerField is given and the member -{field} sets it too")
            attrs.append(("containerField", field))
        if self.canonical:
            kids.sort(key=lambda k: self._rank(name, k[0], k[1]))     # stable
        pad = "  " * min(depth, MAX_INDENT)
        open_tag = f"{pad}<{name}" + "".join(f" {a}={quoteattr(b)}" for a, b in attrs)
        if not kids and text is None:
            self.out.append(open_tag + " />")
            return
        self.out.append(open_tag + ">")
        for kname, kfield, kbody, kpath in kids:
            self.node(kname, kbody, kfield, depth + 1, kpath)
        if text is not None:
            self.out.append(f"{pad}  <![CDATA[{text.replace(']]>', ']]]]><![CDATA[>')}]]>")
        self.out.append(f"{pad}</{name}>")

    @staticmethod
    def _nodes(kids, field, items, path, key):
        """Collect the {NodeName: body} items of a '-children' or '-field' member."""
        for i, item in enumerate(items):
            ipath = path + [f"{key}[{i}]"]
            if not isinstance(item, dict):
                _fail(ipath, f"child is a {type(item).__name__}, not an object")
            for cname, cbody in item.items():
                if cname != "#comment":
                    kids.append((_check_name(cname, ipath + [cname], "node"), field, cbody, ipath + [cname]))


def _no_duplicate_members(pairs: list[tuple[str, object]]) -> dict:
    """json's object_pairs_hook: a member written twice in one object is an error.

    json.loads keeps the last of two equal keys, so a reader that keeps the first
    (or a person reading the file) sees a different scene. "#comment" may repeat:
    it is dropped anyway.
    """
    obj = dict(pairs)
    if len(obj) != len(pairs):
        seen: set[str] = set()
        for key, _ in pairs:
            if key in seen and key != "#comment":
                raise JsonConversionError(f"(root): member {key[:60]!r} is written twice in one object")
            seen.add(key)
    return obj


def json_to_xml(text: str | dict, canonical: bool = True) -> str:
    """Convert X3D JSON (text or an already parsed object) to X3D XML text.

    Raises JsonConversionError when the document is not JSON, has no top-level
    "X3D" object, writes a member twice, nests too deeply, or holds a value XML
    cannot carry (NaN, infinity, control characters, undeclared namespace prefixes).
    canonical=False keeps the member order as written (the A/B "v1" mode, for
    sensitivity checks).
    """
    if isinstance(text, str):
        try:
            doc = json.loads(text, object_pairs_hook=_no_duplicate_members)
        except RecursionError:
            raise JsonConversionError("(root): JSON nested too deeply") from None
        except JsonConversionError:
            raise
        except ValueError as exc:
            raise JsonConversionError(f"(root): invalid JSON: {str(exc)[:120]}") from None
    else:
        doc = text
    if not isinstance(doc, dict) or "X3D" not in doc:
        raise JsonConversionError("(root): no top-level X3D key")
    root = doc["X3D"]
    if not isinstance(root, dict):
        raise JsonConversionError("X3D: is not an object")
    root = {k: v for k, v in root.items() if not (k.startswith("@xsd") or k == "@xmlns:xsd")}
    version = root.get("@version")
    location = SCHEMA_LOCATION.format(version if version in KNOWN_VERSIONS else KNOWN_VERSIONS[0])
    writer = _Writer(canonical)
    writer.node("X3D", root, None, 0, ["X3D"])
    first = writer.out[0]
    boiler = f" xmlns:xsd='{XSI_NS}' xsd:noNamespaceSchemaLocation='{location}'"
    cut = first.index("<X3D") + 4
    writer.out[0] = first[:cut] + boiler + first[cut:]
    return "\n".join(writer.out)
