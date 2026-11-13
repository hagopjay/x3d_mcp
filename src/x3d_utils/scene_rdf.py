"""Convert an X3D scene to RDF the way the Web3D Consortium's X3dToTurtle.xslt does, in plain Python.

The Consortium publishes Turtle for its semantics examples with X3dToTurtle.xslt (XSLT 2.0, run by Saxon
with DTD default attributes expanded). This module reproduces that output without an XSLT processor:

  * every element becomes an individual `:<name> a owl:NamedIndividual, x3do:<Element>`. The name is the
    DEF; a USE becomes a new individual `<USE>-USE-<n>` with `owl:sameAs :<USE>`; any other node gets a
    positional name `<Element>_<i>_<j>...` (1-based element index at each level below X3D, so `Scene`
    is `_2` when a head comes first). Positional names change when a node is inserted before them.
  * `:child x3do:hasParent :parent` and `:parent x3do:has<Field> :child`, where <Field> is the child's
    containerField. The containerField default comes from the X3D DTD of the document's version, as in
    the official build, so a Sphere gives `x3do:hasGeometry`, never `x3do:hasSphere`. A node that DTD
    does not declare has no default, and its field is named after the element as the stylesheet does
    (`x3do:hasGaussianSplats` in a 4.0 document); a warning gives the X3DUOM's containerField.
  * each attribute that is not a default value becomes `x3do:<field> <value>`: a bare number or boolean
    for SFBool/SFInt32/SFFloat/SFDouble, an rdf:List for the other numeric types (SFVec3f, SFColor,
    MFFloat, SFRotation...), and a string otherwise (SFTime, SFString, MFString kept as one literal).
    The default-value tests and the field-type table are the stylesheet's own XPath rule texts, copied
    into X3dToTurtle-tables.json by evaluation/build_scene_rdf_tables.py.
  * ROUTEs are `:Scene x3do:hasROUTE :ROUTE_2_8` with fromNode/toNode as strings (the DEF names),
    head meta are `x3do:hasMeta` individuals plus `:meta dcterms:<name> "<content>"`, Script and Shader
    source text is `x3do:sourceCode`.

Base IRI: the `identifier` meta up to '.x3d', plus '.ttl#'; without one, the stylesheet's shared
`https://www.web3d.org/semantics/scenes/UnnamedX3dModel.ttl#` (so two unnamed scenes collide on merge).

Known differences from the stylesheet are reported in `warnings`, never silent: a value the stylesheet
would write as broken Turtle (a non-number in a numeric field) is kept as a string here.
"""

import hashlib
import json
import re
import time
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

import rdflib
from lxml import etree
from rdflib import BNode, Literal, URIRef
from rdflib.namespace import OWL, RDF, XSD

from x3d_utils.xpath_mini import compile_expr, evaluate

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "validation" / "schemas"
TABLES_PATH = SCHEMAS_DIR / "X3dToTurtle-tables.json"

X3DO_NS = "https://www.web3d.org/specifications/X3dOntology4.1#"
X3D_XSD_NS = "https://www.web3d.org/specifications/x3d-4.1.xsd#"
DCTERMS_NS = "http://purl.org/dc/terms/"
UNNAMED_BASE = "https://www.web3d.org/semantics/scenes/UnnamedX3dModel.ttl#"
DSIG_NS = "http://www.w3.org/2000/09/xmldsig#"
X3DO = rdflib.Namespace(X3DO_NS)
DCTERMS = rdflib.Namespace(DCTERMS_NS)

PREFIX_LINES = (
    ("owl", "http://www.w3.org/2002/07/owl#"),
    ("rdf", "http://www.w3.org/1999/02/22-rdf-syntax-ns#"),
    ("rdfs", "http://www.w3.org/2000/01/rdf-schema#"),
    ("schema", "http://schema.org/"),
    ("dcterms", DCTERMS_NS),
    ("xsd", "http://www.w3.org/2001/XMLSchema#"),
    ("x3d", X3D_XSD_NS),
    ("x3do", X3DO_NS),
)

_PARSER = etree.XMLParser(huge_tree=True, resolve_entities=False, no_network=True, load_dtd=False)
_XML_WS = re.compile(r"[ \t\r\n]+")
_INTEGER = re.compile(r"[-+]?[0-9]+")
_DECIMAL = re.compile(r"[-+]?[0-9]*\.[0-9]+")
_DOUBLE = re.compile(r"[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)[eE][-+]?[0-9]+")
# A local name that can be written as :name in Turtle without escapes (else the full IRI is written).
_PN_LOCAL = re.compile(r"[A-Za-z0-9_](?:[A-Za-z0-9_.\-]*[A-Za-z0-9_\-])?")
_IRI_BAD = re.compile(r'[\x00-\x20<>"{}|^`\\]')
_ESCAPES = {"t": "\t", "b": "\b", "n": "\n", "r": "\r", "f": "\f", '"': '"', "'": "'", "\\": "\\"}
_UCHAR = re.compile(r"\\u([0-9A-Fa-f]{4})|\\U([0-9A-Fa-f]{8})")

SCRIPT_ELEMENTS = ("Script", "ShaderProgram", "ShaderPart")


def _norm(s: str) -> str:
    """XPath normalize-space."""
    return _XML_WS.sub(" ", s).strip(" ")


def _local(el) -> str:
    return etree.QName(el).localname


def _elements(el) -> list:
    return [c for c in el if isinstance(c.tag, str)]


def turtle_unescape(text: str) -> tuple[str, bool]:
    """Read `text` as the inside of a Turtle string, as a Turtle parser does with the stylesheet's output.
    Returns (value, ok); ok is False when it holds an escape Turtle does not allow (kept literally)."""
    if "\\" not in text:
        return text, True
    out, i, ok = [], 0, True
    while i < len(text):
        ch = text[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        m = _UCHAR.match(text, i)
        if m:
            out.append(chr(int(m.group(1) or m.group(2), 16)))
            i = m.end()
            continue
        nxt = text[i + 1:i + 2]
        if nxt in _ESCAPES:
            out.append(_ESCAPES[nxt])
            i += 2
            continue
        ok = False
        out.append(ch)
        i += 1
    return "".join(out), ok


def _turtle_string(value: str) -> str:
    out = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    return '"' + out.replace("\t", "\\t") + '"'


class _Rules:
    """The stylesheet's attribute tests and type table, compiled once."""

    def __init__(self, path: Path = TABLES_PATH):
        data = json.loads(path.read_text(encoding="utf-8"))
        rules = data["rules"]
        self.sources = data["sources"]
        self.keep_vars = {name: compile_expr(text) for name, text in rules["keep_vars"].items()}
        self.keep_test = compile_expr(rules["keep_test"])
        self.type_rules = [(compile_expr(test), result) for test, result in rules["type_rules"]]
        self.dc_same = set(rules["dcterms_same"])
        self.dc_renamed = dict(rules["dcterms_renamed"])
        self.dtd = data["dtd"]
        self.keep = lru_cache(maxsize=65536)(self._keep)
        self.field_type = lru_cache(maxsize=65536)(self._field_type)

    def _keep(self, parent: str, name: str, value: str, ctx_vars: tuple) -> bool:
        base = dict(ctx_vars)
        ctx = {"name": name, "parent": parent, "value": value}

        class _Lazy(dict):
            def __missing__(inner, key):            # noqa: N805 - evaluate a keep variable on first use
                if key in self.keep_vars:
                    v = evaluate(self.keep_vars[key], ctx)
                    inner[key] = v
                    return v
                raise KeyError(key)

        ctx["vars"] = _Lazy(base)
        return bool(evaluate(self.keep_test, ctx))

    def _field_type(self, parent: str, name_full: str, local_type: str, version: str) -> str:
        if parent in ("Script", "ProtoInstance") or "Shader" in parent:
            name = name_full
        elif name_full.startswith("set_"):
            name = name_full[4:]
        elif name_full.endswith("_changed"):
            name = name_full.partition("_changed")[0]
        else:
            name = name_full
        ctx = {"name": name_full, "parent": parent, "value": "",
               "vars": {"parentElementName": parent, "attributeName": name, "attributeNameFull": name_full,
                        "localFieldType": local_type, "x3dVersion": version}}
        for test, result in self.type_rules:
            if evaluate(test, ctx):
                return local_type if result == "$localFieldType" else result
        if local_type:
            return local_type
        if parent in ("field", "fieldValue"):
            return local_type
        if "Proto" in parent or parent == "meta":
            return ""
        return "TYPE_UNKNOWN"


_RULES = None


def get_rules() -> _Rules:
    global _RULES
    if _RULES is None:
        _RULES = _Rules()
    return _RULES


def tables_sha256() -> str:
    return hashlib.sha256(TABLES_PATH.read_bytes()).hexdigest()


def dtd_version(version: str) -> str:
    """The DTD whose defaults the official build would expand for this X3D version."""
    v = (version or "").strip()
    if v.startswith("3"):
        return "3.3"
    if v == "4.0":
        return "4.0"
    return "4.1"


class SceneRDF:
    """The result of a conversion: rdflib graph, Turtle text and what was decided along the way."""

    def __init__(self):
        self.graph = rdflib.Graph()
        self.turtle = ""
        self.base = ""
        self.version = ""
        self.dtd = ""
        self.warnings: list[str] = []
        self.individuals = 0
        self.elapsed_ms = 0.0

    @property
    def triples(self) -> int:
        return len(self.graph)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.turtle.encode("utf-8")).hexdigest()


class _Converter:
    def __init__(self, root, base_iri: str, rules: _Rules):
        self.root = root
        self.rules = rules
        self.out = SceneRDF()
        self.version = root.get("version", "")
        self.out.version = self.version.strip()
        self.out.dtd = dtd_version(self.version)
        if not self.out.version:
            self.out.warnings.append("X3D has no version attribute; the X3D 4.1 DTD defaults were used")
        self.dtd = rules.dtd[self.out.dtd]
        nv = _norm(self.version)
        self.is_x3d3, self.is_x3d4 = nv.startswith("3"), nv.startswith("4")
        self.signatures = sum(1 for _ in root.iter(f"{{{DSIG_NS}}}Signature"))
        self._attrs: dict = {}
        self._uom = None
        self._warned: set = set()
        self.out.base = base_iri or self._base_from_meta()
        self.lines: list[str] = []
        self.g = self.out.graph

    # ---------------------------------------------------------------- attributes with DTD defaults
    def warn(self, message: str):
        if message not in self._warned:
            self._warned.add(message)
            self.out.warnings.append(message)

    def uom_container_field(self, name: str):
        if self._uom is None:
            from x3d_utils.x3duom import get_x3duom
            self._uom = get_x3duom()
        return self._uom.get_container_field(name)

    def attrs(self, el) -> dict:
        """Attributes by local name (the first one when two share a local name); see attr_items."""
        return self.attr_items_and_map(el)[1]

    def attr_items(self, el) -> list:
        return self.attr_items_and_map(el)[0]

    def attr_items_and_map(self, el):
        """(local name, value) pairs as a DTD-aware parser reports them: explicit attributes first,
        normalized when the DTD declares a tokenized type, then the DTD defaults that are missing."""
        cached = self._attrs.get(el)
        if cached is not None:
            return cached
        local = _local(el)
        # The DTD declares elements by qualified name, as libxml2 looks them up: <q:Box> is not Box (no
        # defaults), while an element in a default namespace still is.
        declared_as = f"{el.prefix}:{local}" if el.prefix else local
        decl = self.dtd.get(declared_as)
        tokenized = set(decl["tokenized"]) if decl else set()
        items, explicit_q, plain = [], set(), set()
        nsmap = el.nsmap
        rev = {uri: p for p, uri in nsmap.items() if p}
        for key, value in el.attrib.items():
            q = etree.QName(key)
            prefix = "xml" if q.namespace == "http://www.w3.org/XML/1998/namespace" else (
                rev.get(q.namespace) if q.namespace else None)
            qname = f"{prefix}:{q.localname}" if prefix else q.localname
            explicit_q.add(qname)
            if qname in tokenized:
                value = _norm(value)
            items.append((q.localname, value))
            if not prefix:
                plain.add(len(items) - 1)
        if decl:
            for qname, default in decl["defaults"].items():
                prefix, _, name = qname.rpartition(":")
                if qname in explicit_q:
                    continue
                if prefix and prefix in nsmap and f"{{{nsmap[prefix]}}}{name}" in el.attrib:
                    continue
                items.append((name, default))
                if not prefix:
                    plain.add(len(items) - 1)
        elif local != "X3D" and "containerField" not in explicit_q:
            # As in the official build: with no DTD declaration there is no containerField default, and
            # the stylesheet names the parent's field after the element. The X3DUOM's default is a hint.
            cf = self.uom_container_field(local)
            field = f"x3do:has{local[:1].upper()}{local[1:]}"
            where = (f"{declared_as} is not declared in the X3D {self.out.dtd} DTD (it declares {local} "
                     f"without a prefix)" if el.prefix else f"{local} is not in the X3D {self.out.dtd} DTD")
            if cf:
                fix = (f"drop the {el.prefix}: prefix" if el.prefix and local in self.dtd else
                       f"declare a version whose DTD has {local}")
                self.warn(f"{where}, so it has no containerField default and its parent field is named after "
                          f"the element ({field}), as the stylesheet does; the X3DUOM default is '{cf}' "
                          f"({fix}, or write containerField='{cf}')")
            else:
                self.warn(f"{where} and not in the X3DUOM; its parent field is named after the element ({field})")
        mapping = {}            # what @name reads in the stylesheet: attributes with no namespace only
        for i, (name, value) in enumerate(items):
            if i in plain:
                mapping.setdefault(name, value)
        self._attrs[el] = (items, mapping)
        return items, mapping

    # ---------------------------------------------------------------- names
    def _base_from_meta(self) -> str:
        metas = [m for m in self.root.iter("meta") if m.get("name") == "identifier"]
        content = metas[0].get("content", "") if metas else ""
        if len(metas) > 1:
            self.warn("more than one meta name='identifier'; the first one names the graph")
        if len(metas) != 1 or len(content) > 0:
            before = content.split(".x3d", 1)[0] if ".x3d" in content else ""
            if not before or content.startswith("*enter"):
                if not metas:
                    self.warn("no meta name='identifier': the stylesheet's shared base IRI "
                              f"{UNNAMED_BASE} is used, so unnamed scenes collide when merged")
                model = UNNAMED_BASE[:-len(".ttl#")]
            else:
                model = before
            return model.replace(" ", "_") + ".ttl#"
        self.warn("meta name='identifier' is empty; the stylesheet writes a relative base <#>, "
                  f"{UNNAMED_BASE} is used instead")
        return UNNAMED_BASE

    def assign_names(self):
        self.names = {}
        use_seen: dict = {}

        def walk(el, path, ancestors_use):
            local = _local(el)
            attrs = self.attrs(el)
            if "DEF" in el.attrib:
                name = _norm(attrs.get("DEF", "")).replace(" ", "")
            elif "USE" in el.attrib:
                use = attrs.get("USE", "")
                n = use_seen.get(use, 0) - ancestors_use.count(use) + 1
                name = _norm(use).replace(" ", "") + "-USE-" + str(n)
            elif local in ("head", "Scene"):
                name = local
            else:
                name = local + "".join(f"_{i}" for i in path)
            self.names[el] = name
            if "USE" in el.attrib:
                use = attrs.get("USE", "")
                use_seen[use] = use_seen.get(use, 0) + 1
                ancestors_use = ancestors_use + [use]
            for i, child in enumerate(_elements(el), 1):
                walk(child, path + [i], ancestors_use)

        walk(self.root, [], [])

    def local_part(self, name: str) -> str:
        """`name` as it goes after the base IRI. Characters an IRI cannot hold (space, controls, quote,
        backslash, <>{}|^`) are percent-encoded; the stylesheet writes them as they are, which no Turtle
        parser reads, so this changes nothing a parser could have read."""
        if not _IRI_BAD.search(name):
            return name
        safe = _IRI_BAD.sub(lambda m: "".join(f"%{b:02X}" for b in m.group(0).encode("utf-8")), name)
        self.warn(f"the name {name!r} has characters an IRI cannot hold; its IRI uses {safe!r} "
                  "(the x3do:DEF string keeps the name as written)")
        return safe

    def iri(self, name: str) -> URIRef:
        return URIRef(self.out.base + self.local_part(name))

    def ref(self, name: str) -> str:
        local = self.local_part(name)
        return ":" + local if _PN_LOCAL.fullmatch(local) else f"<{self.out.base}{local}>"

    # ---------------------------------------------------------------- values
    def token(self, tok: str, where: str):
        """A bare Turtle token as rdflib reads it: (turtle_text, Literal)."""
        if tok in ("true", "false"):
            return tok, Literal(tok, datatype=XSD.boolean)
        if _DOUBLE.fullmatch(tok):
            return tok, Literal(tok, datatype=XSD.double)
        if _DECIMAL.fullmatch(tok):
            try:
                value = str(Decimal(tok))
            except InvalidOperation:
                value = tok
            return tok, Literal("0" if value == "-0" else value, datatype=XSD.decimal)
        if _INTEGER.fullmatch(tok):
            return tok, Literal(str(int(tok)), datatype=XSD.integer)
        self.warn(f"{where}: '{tok}' is not a Turtle number or boolean; kept as a string "
                  "(the stylesheet would write Turtle that does not parse)")
        return _turtle_string(tok), Literal(tok)

    def string_value(self, raw: str, where: str):
        value, ok = turtle_unescape(raw)
        if not ok:
            self.warn(f"{where}: a backslash escape Turtle does not allow was kept literally")
            value = raw
        return _turtle_string(value), Literal(value)

    def encode(self, value: str, ftype: str, where: str):
        """(turtle_text, rdflib object) for an attribute value, following the stylesheet's xsl:choose."""
        if "'" in value:
            return self.string_value(value.replace("'", "&apos;"), where)
        nv = _norm(value)
        if (nv.startswith('"') and nv.endswith('"')) or ftype in ("SFString", "MFString"):
            return self.string_value(value, where)
        cleaned = re.sub(r"[-][.]", "-0.", _norm(re.sub(r"[.][ ]", " ", value.replace(",", " ") + " ")))
        numeric = any(k in ftype for k in ("Bool", "Int32", "Float", "Double"))
        if ftype.startswith("SF") and numeric:
            toks = cleaned.split(" ") if cleaned else []
            if len(toks) == 1:
                return self.token(toks[0], where)
            self.warn(f"{where}: {ftype} value '{value}' is not one number; kept as a string "
                      "(the stylesheet would write Turtle that does not parse)")
            return _turtle_string(value), Literal(value)
        if (numeric or any(k in ftype for k in ("Vec", "Color", "Rotation", "Matrix"))) and "0x" not in value:
            toks = cleaned.split(" ") if cleaned else []
            items = [self.token(t, where) for t in toks]
            return ("( " + " ".join(t for t, _ in items) + " )", [o for _, o in items])
        return self.string_value(_norm(value.replace(",", " ")), where)

    def add_list(self, items: list):
        if not items:
            return RDF.nil
        head = node = BNode()
        for i, item in enumerate(items):
            self.g.add((node, RDF.first, item))
            nxt = BNode() if i + 1 < len(items) else RDF.nil
            self.g.add((node, RDF.rest, nxt))
            node = nxt
        return head

    # ---------------------------------------------------------------- field types the stylesheet looks up
    def local_field_type(self, el, attr: str) -> str:
        parent = _local(el)
        if parent in ("field", "fieldValue") and attr in ("name", "type", "accessType", "appinfo", "documentation"):
            return "SFString"
        if parent == "Script":
            # the stylesheet looks for //Script[@DEF = ''] (its DEF parameter is never passed)
            types = [f.get("type", "") for s in self.root.iter("Script") if s.get("DEF") == ""
                     for f in s.iter("field") if f.get("name") == attr]
            return _norm(" ".join(types))
        if parent == "ProtoInstance":
            return self._proto_instance_type(attr)
        own_type = self.attrs(el).get("type", "")
        if _norm(own_type):
            return _norm(own_type)
        if parent == "fieldValue":
            pi = el.getparent()
            proto = pi.get("name") if pi is not None else None
            field_name = el.get("name")
            for decl in self.root.iter("ProtoDeclare"):
                if decl.get("name") == proto:
                    return " ".join(f.get("type", "") for d in self.root.iter("ProtoDeclare")
                                    if d.get("name") == proto for iface in d.iter("ProtoInterface")
                                    for f in iface if isinstance(f.tag, str) and _local(f) == "field"
                                    and f.get("name") == field_name)
            for decl in self.root.iter("ExternProtoDeclare"):
                if decl.get("name") == proto:
                    return " ".join(f.get("type", "") for d in self.root.iter("ExternProtoDeclare")
                                    if d.get("name") == proto for f in d
                                    if isinstance(f.tag, str) and _local(f) == "field" and f.get("name") == field_name)
            self.warn(f"fieldValue '{field_name}': no ProtoDeclare or ExternProtoDeclare named '{proto}'; "
                      "treated as SFString as the stylesheet does")
            return "SFString"
        return "FieldTypeNotFound"

    def _proto_instance_type(self, attr: str) -> str:
        # The stylesheet resolves the prototype from //ProtoInstance[@DEF = ''] (DEF parameter not passed).
        names = [p.get("name", "") for p in self.root.iter("ProtoInstance") if p.get("DEF") == ""]
        proto = names[0] if names and len(names[0]) > 1 else "UnknownPrototype"
        if any(d.get("name") == proto for d in self.root.iter("ProtoDeclare")):
            types = [f.get("type", "") for d in self.root.iter("ProtoDeclare") if d.get("name") == proto
                     for iface in d.iter("ProtoInterface") for f in iface
                     if isinstance(f.tag, str) and _local(f) == "field" and f.get("name") == attr]
            return " ".join(types) if any(types) else "UnknownField"
        if any(d.get("name") == proto for d in self.root.iter("ExternProtoDeclare")):
            types = [f.get("type", "") for d in self.root.iter("ExternProtoDeclare") if d.get("name") == proto
                     for f in d if isinstance(f.tag, str) and _local(f) == "field" and f.get("name") == attr]
            if any(types):
                return " ".join(types)
        return "UnknownFieldType"

    def hanim_flags(self, el) -> tuple[bool, bool]:
        h1 = h2 = False
        node = el
        while node is not None:
            if isinstance(node.tag, str) and _local(node) == "HAnimHumanoid":
                version = self.attrs(node).get("version", "")
                if version.startswith("1") or len(version) == 0:
                    h1 = True
                if version.startswith("2"):
                    h2 = True
            node = node.getparent()
        is1 = self.is_x3d3 and h1
        return is1, (self.is_x3d4 and h2 and not is1)

    # ---------------------------------------------------------------- the walk
    def run(self) -> SceneRDF:
        self.assign_names()
        lines = self.lines
        lines.append(f"@prefix :        <{self.out.base}> .")
        for p, ns in PREFIX_LINES:
            lines.append(f"@prefix {p + ':':<8} <{ns}> .")
        lines.append("")
        self.g.bind("", self.out.base)
        for p, ns in PREFIX_LINES:
            self.g.bind(p, ns)
        self.element(self.root, None)
        self.out.turtle = "\n".join(lines) + "\n"
        return self.out

    def element(self, el, parent_name):
        g, lines = self.g, self.lines
        local = _local(el)
        name = self.names[el]
        s = self.iri(name)
        self.out.individuals += 1
        g.add((s, RDF.type, OWL.NamedIndividual))
        g.add((s, RDF.type, X3DO[local]))
        parts = [f"{self.ref(name)} a owl:NamedIndividual, x3do:{local}"]
        if parent_name is not None:
            g.add((s, X3DO.hasParent, self.iri(parent_name)))
            parts.append(f"x3do:hasParent {self.ref(parent_name)}")
        attrs = self.attrs(el)
        use = attrs.get("USE", "")
        if use:
            g.add((s, OWL.sameAs, self.iri(use)))
            parts.append(f"owl:sameAs {self.ref(use)}")
        else:
            self.child_fields(el, s, parts)
            self.attributes(el, local, attrs, s, parts)
        if local in SCRIPT_ELEMENTS:
            self.source_code(el, s, parts)
        lines.append(" ;\n  ".join(parts) + " ." + (" # DEF matching this USE" if use else ""))
        for child in _elements(el):
            self.element(child, name)
        if local == "meta" and not any(_local(f) == "meta" for f in el.itersiblings() if isinstance(f.tag, str)):
            self.dublin_core()

    def child_fields(self, el, s, parts):
        kids = _elements(el)
        keys = []
        for k in kids:
            a = self.attrs(k)
            keys.append((a["containerField"] if "containerField" in a else None, _local(k)))
        for i, k in enumerate(kids):
            cf, tag = keys[i]
            field = cf if cf is not None else tag
            if any(pcf == field or ptag == field for pcf, ptag in keys[:i]):
                continue
            members = [k] + [f for j, f in enumerate(kids[i + 1:], i + 1)
                             if keys[j][0] == field or keys[j][1] == field]
            first = field[:1]
            if "a" <= first <= "z":
                first = first.upper()
            pred_local = "has" + first + field[1:]
            pred = X3DO[pred_local]
            for m in members:
                self.g.add((s, pred, self.iri(self.names[m])))
            parts.append(f"x3do:{pred_local} " + ", ".join(self.ref(self.names[m]) for m in members))

    def attributes(self, el, local, attrs, s, parts):
        h1, h2 = self.hanim_flags(el)
        connects = any(_local(c) == "connect" and c.get("nodeField") == "whichChoice"
                       for i in _elements(el) if _local(i) == "IS" for c in _elements(i))
        ctx_vars = (("isHAnim1", h1), ("isHAnim2", h2), ("isX3D3", self.is_x3d3), ("isX3D4", self.is_x3d4),
                    ("signatureCount", float(self.signatures)), ("parentISConnectsWhichChoice", connects))
        for name, value in self.attr_items(el):
            if name == "containerField":
                continue
            if not self.rules.keep(local, name, value, ctx_vars):
                continue
            field = "cssStyle" if local in ("FontStyle", "ScreenFontStyle") and name == "style" else name
            ftype = self.rules.field_type(local, name, self.local_field_type(el, name), self.version)
            text, obj = self.encode(value, ftype, f"{local}@{name}")
            if isinstance(obj, list):
                obj = self.add_list(obj)
            self.g.add((s, X3DO[field], obj))
            parts.append(f"x3do:{field} {text}")

    def source_code(self, el, s, parts):
        texts = [el.text] + [c.tail for c in el]
        for t in texts:
            if t is None or not _norm(t):
                continue
            body = t.strip(" \t\r\n")
            raw = "\n" + ("" if _norm(t).startswith("ecmascript:") else "ecmascript:\n") + body + "\n"
            text, obj = self.string_value(raw, f"{_local(el)} source")
            self.g.add((s, X3DO.sourceCode, obj))
            parts.append(f"x3do:sourceCode {text}")

    def dublin_core(self):
        subject = self.iri("meta")
        for meta in self.root.iter("meta"):
            name = (meta.get("name") or "").lower()
            if name in self.rules.dc_same:
                term = name
            elif name in self.rules.dc_renamed:
                term = self.rules.dc_renamed[name]
            else:
                continue
            raw = (self.attrs(meta).get("content") or "").replace('"', '\\"')
            text, obj = self.string_value(raw, "meta content")
            self.g.add((subject, DCTERMS[term], obj))
            self.lines.append(f"{self.ref('meta')} dcterms:{term} {text} .")


def parse_x3d(text: str):
    """Parse X3D XML without loading any DTD or entity. Raises ValueError with a readable message."""
    data = text.encode("utf-8") if isinstance(text, str) else text
    try:
        root = etree.fromstring(data, _PARSER)
    except etree.XMLSyntaxError as e:
        raise ValueError(f"X3D does not parse as XML: {e}") from None
    if _local(root) != "X3D":
        raise ValueError(f"the root element is {_local(root)}, not X3D (convert a whole X3D document)")
    return root


def scene_to_rdf(text: str, base_iri: str = "") -> SceneRDF:
    """Convert an X3D XML document to RDF. Raises ValueError if it does not parse or is not X3D."""
    t0 = time.perf_counter()
    root = parse_x3d(text)
    conv = _Converter(root, base_iri, get_rules())
    if etree.QName(root).namespace:
        conv.warn("the X3D element has a namespace; the stylesheet matches /X3D without one and would write "
                  "no triples")
    out = conv.run()
    out.elapsed_ms = (time.perf_counter() - t0) * 1000
    return out
