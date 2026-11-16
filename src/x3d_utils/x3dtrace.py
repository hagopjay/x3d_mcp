"""x3dtrace 0.1: value-level evidence for X3D pipelines (quote, find, compare, locate).

A writer (a model, a tool, a person) states a value and copies a short exact QUOTE from the
source it read the value from. Plain code, with no model call, then answers three questions per
value and gives a verdict:

  FIND     is the quote in the source?          (whole-token run, number-aware lexer)
  COMPARE  is the value in the quote?           (numbers, vectors, colours, angles, units, text)
  LOCATE   where is it?                         (character span, line and column, W3C Web
                                                 Annotation selectors; for an X3D scene also the
                                                 node path, the DEF path and the world-space box)

Verdicts: verified | check | reworded | unchecked (names and item field names follow GAIK's
`source_tracing`, so a GAIK `TraceReport.to_dict()` and ours can be exchanged).

Concept, verdict vocabulary, JSON field names, default policy values and the README test vectors
come from the GAIK toolkit (https://github.com/GAIK-project/gaik-toolkit, MIT licence, Copyright
(c) 2026 GAIK - GenAI for knowledge mgt) and from Umair Ali Khan's article "How I Added
Traceability in Structured Data Extraction with AI". This module is an independent implementation,
written from a behavioural description: no GAIK source line is copied. See NOTICE-x3dtrace.md for
the MIT notice and the list of deliberate differences (sign, decimal point and exponent kept,
vectors compared per component, case-sensitive identifiers, typed conversions that need unit
evidence, selectors with prefix and suffix instead of "first hit").

Standard library only, Python 3.10 or newer.
"""
from __future__ import annotations

import bisect
import hashlib
import html
import json
import math
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from xml.parsers import expat

SCHEMA = "x3dtrace/0.1"
VERDICTS = ("verified", "check", "reworded", "unchecked")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _jsonable(o):
    if isinstance(o, Decimal):
        return {"$decimal": str(o)}
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if isinstance(o, tuple):
        return list(o)
    raise TypeError("not JSON serialisable: %r" % (type(o).__name__,))


def canonical_json(obj) -> str:
    """Compact canonical JSON: sorted keys, no spaces, UTF-8 characters kept. A Decimal is written as {"$decimal": "1.50"}."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_jsonable, allow_nan=False)


# --------------------------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------------------------
ALL_CONVERSIONS = ("deg2rad", "rad2deg", "percent", "rgb255", "hex", "colorname", "numword",
                   "unit", "broadcast", "axis")


@dataclass(frozen=True)
class TracePolicy:
    """Judgment calls. Defaults are the X3D profile; `TracePolicy.strict()` turns conversions off."""
    number_tolerance: float = 1e-6          # absolute, strict "<"
    round_to_printed: bool = True           # a value printed to more decimals than the quote matches its rounded display (not when the quote reads 0)
    match_reformatted_identifiers: bool = True   # prose only: 7200-4453-21 holds 7200445321
    reworded_is_problem: bool = False       # report "reworded" as "check"
    min_quote_words: int = 1                # a shorter quote is reported as "check"
    ambiguous_quote_is_check: bool = False  # a quote that occurs more than once and was not disambiguated
    moved_is_check: bool = False            # a quote found in another source than the cited one
    code_values_unchecked: bool = False     # GAIK parity switch: an absent lowercase snake_case word is "unchecked"
    conversions: tuple = ALL_CONVERSIONS    # typed conversions allowed (each needs unit evidence in the quote)
    length_scale: float = 1.0               # metres per scene length unit (from a <unit category="length"> statement)

    def allows(self, name: str) -> bool:
        return name in self.conversions

    @classmethod
    def strict(cls) -> "TracePolicy":
        return cls(reworded_is_problem=True, ambiguous_quote_is_check=True, moved_is_check=True,
                   conversions=(), round_to_printed=False)

    def to_dict(self) -> dict:
        """Strings, integers and booleans only (no floats), so the record hashes the same in Python and JavaScript."""
        return {"number_tolerance": repr(float(self.number_tolerance)), "round_to_printed": self.round_to_printed,
                "match_reformatted_identifiers": self.match_reformatted_identifiers,
                "reworded_is_problem": self.reworded_is_problem, "min_quote_words": self.min_quote_words,
                "ambiguous_quote_is_check": self.ambiguous_quote_is_check, "moved_is_check": self.moved_is_check,
                "code_values_unchecked": self.code_values_unchecked, "conversions": list(self.conversions),
                "length_scale": repr(float(self.length_scale))}

    @classmethod
    def from_dict(cls, d: dict) -> "TracePolicy":
        kw = dict(d)
        for k in ("number_tolerance", "length_scale"):
            if k in kw:
                kw[k] = float(kw[k])
        if "conversions" in kw:
            kw["conversions"] = tuple(kw["conversions"])
        return cls(**{k: v for k, v in kw.items() if k in cls.__dataclass_fields__})


# --------------------------------------------------------------------------------------------
# Lexer: number-aware tokens with character offsets
# --------------------------------------------------------------------------------------------
def _mark_class() -> str:
    """Regex class body of every combining mark (Mn, Mc, Me) up to U+1FFFF: Indic, Hebrew, Thai and Arabic words keep their marks."""
    out, a, b = [], None, None
    for c in range(0x300, 0x20000):
        if unicodedata.category(chr(c)) in ("Mn", "Mc", "Me"):
            if a is None:
                a = c
            b = c
        elif a is not None:
            out.append("%s-%s" % (re.escape(chr(a)), re.escape(chr(b))) if b > a else re.escape(chr(a)))
            a = None
    return "".join(out)


_COMB = _mark_class()
_ENT = r"&(?:#\d+|#[xX][0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);"
_PATTERNS = {
    # X3D / scene text: "." is the only decimal mark, "," and whitespace separate values, "_" is a name character
    "x3d": re.compile(
        r"(?P<ent>" + _ENT + r")"
        r"|(?P<num>(?<![\w.])[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?(?!\d))"
        r"|(?P<word>(?:\w|[" + _COMB + r"­])+)"),
    # prose: "," and "." may group thousands or mark decimals (read both ways at compare time); "_" separates words
    "prose": re.compile(
        r"(?P<num>(?<![\w.,])[+-]?(?:\d+(?:[.,'’]\d+)*|[.,]\d+)(?:[eE][+-]?\d+)?(?!\d))"
        r"|(?P<word>(?:[^\W_]|[" + _COMB + r"­])+)"),
}
_TRANS = {0x2212: "-", 0x2013: "-"}   # minus sign and en dash read as "-" (offsets stay 1:1)


@dataclass
class Lexed:
    text: str
    mode: str
    norms: list
    starts: list
    ends: list
    kinds: list   # "n" number, "w" word

    def __len__(self):
        return len(self.norms)


def lex(text: str, mode: str = "prose", normalizer=None) -> Lexed:
    """Tokens of `text`. Words are NFC-normalised (and casefolded in prose mode; scene identifiers keep
    their case); numbers keep sign, decimal point and exponent. Offsets refer to the ORIGINAL text."""
    pat = _PATTERNS[mode]
    norms, starts, ends, kinds = [], [], [], []
    for m in pat.finditer(text.translate(_TRANS)):
        k = m.lastgroup
        raw = m.group()
        if k == "ent":
            dec = html.unescape(raw)
            if dec == raw or not any(ch.isalnum() for ch in dec):
                continue
            raw, k = dec, "word"
        if k == "num":
            n = raw.lower()
            if n.startswith("+"):
                n = n[1:]
            kind = "n"
        else:
            n = unicodedata.normalize("NFC", raw.replace("­", ""))
            if mode == "prose":
                n = n.casefold()
            if normalizer is not None:
                n = normalizer(n)
            kind = "w"
            if not n:
                continue
        norms.append(n)
        starts.append(m.start())
        ends.append(m.end())
        kinds.append(kind)
    return Lexed(text, mode, norms, starts, ends, kinds)


def sniff_kind(text: str) -> str:
    """"x3d" for XML, classic-VRML or JSON X3D text, else "text"."""
    t = text.lstrip("﻿ \t\r\n")[:600]
    if t.startswith("<?xml") or t.startswith("<X3D") or t.startswith("#X3D") or t.startswith("#VRML"):
        return "x3d"
    if t.startswith("{") and '"X3D"' in t:
        return "x3d"
    return "text"


@dataclass
class Source:
    """A document the writer read: prose (kind "text") or an X3D scene (kind "x3d")."""
    id: str
    text: str
    kind: str = "auto"
    normalizer: object = None
    meta: dict = field(default_factory=dict)    # extra facts carried into the report (e.g. content_sha256 of a scene)
    sha256: str = ""
    _lex: Lexed = field(default=None, repr=False)
    _index: dict = field(default=None, repr=False)
    _scene: object = field(default=None, repr=False)
    _scene_tried: bool = field(default=False, repr=False)

    def __post_init__(self):
        if self.kind == "auto":
            self.kind = sniff_kind(self.text)
        self.sha256 = _sha(self.text.encode("utf-8", "surrogatepass"))

    @property
    def mode(self) -> str:
        return "x3d" if self.kind == "x3d" else "prose"

    @property
    def lexed(self) -> Lexed:
        if self._lex is None:
            self._lex = lex(self.text, self.mode, self.normalizer)
        return self._lex

    @property
    def index(self) -> dict:
        if self._index is None:
            idx: dict = {}
            for i, n in enumerate(self.lexed.norms):
                idx.setdefault(n, []).append(i)
            self._index = idx
        return self._index

    @property
    def scene(self):
        if not self._scene_tried:
            self._scene_tried = True
            if self.kind == "x3d" and self.text.lstrip("﻿ \t\r\n").startswith("<"):
                self._scene = SceneIndex(self.text)
        return self._scene

    def occurrences(self, needle: list) -> list:
        """Start indices of non-overlapping whole-token runs of `needle`, left to right."""
        if not needle:
            return []
        norms, out, nxt = self.lexed.norms, [], 0
        n = len(needle)
        for i in self.index.get(needle[0], ()):
            if i < nxt:
                continue
            if norms[i:i + n] == needle:
                out.append(i)
                nxt = i + n
        return out

    def info(self) -> dict:
        d = dict(self.meta)
        d.update({"id": self.id, "kind": self.kind, "sha256": self.sha256, "bytes": len(self.text.encode("utf-8", "surrogatepass"))})
        return d


def line_col(text: str, offset: int) -> tuple:
    """1-based line and column of a character offset."""
    line = text.count("\n", 0, offset) + 1
    col = offset - (text.rfind("\n", 0, offset) + 1) + 1
    return line, col


# --------------------------------------------------------------------------------------------
# Scene index: element spans, attribute spans, DEF paths, world-space boxes (XML encoding)
# --------------------------------------------------------------------------------------------
_ATTR_RE = re.compile(r"""([A-Za-z_:][\w:.\-]*)\s*=\s*("([^"]*)"|'([^']*)')""")


@dataclass
class Element:
    idx: int
    tag: str
    parent: int
    start: int            # char offset of "<"
    tag_end: int          # char offset just after the start tag's ">"
    end: int = -1         # char offset just after the whole element
    attrs: dict = field(default_factory=dict)       # name -> (value, value_start, value_end)
    children: list = field(default_factory=list)
    path: str = ""
    def_: str = ""
    use: str = ""


def _mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def _ident():
    return [[1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]


def _trans(t):
    m = _ident()
    m[0][3], m[1][3], m[2][3] = t
    return m


def _scale(s):
    m = _ident()
    m[0][0], m[1][1], m[2][2] = s
    return m


def _rot(axis, ang):
    x, y, z = axis
    n = math.sqrt(x * x + y * y + z * z)
    if n < 1e-12 or abs(ang) < 1e-15:
        return _ident()
    x, y, z = x / n, y / n, z / n
    c, s = math.cos(ang), math.sin(ang)
    t = 1 - c
    return [[t * x * x + c, t * x * y - s * z, t * x * z + s * y, 0.0],
            [t * x * y + s * z, t * y * y + c, t * y * z - s * x, 0.0],
            [t * x * z - s * y, t * y * z + s * x, t * z * z + c, 0.0],
            [0.0, 0.0, 0.0, 1.0]]


def _floats_of(s):
    try:
        out = [float(p) for p in re.split(r"[\s,]+", s.strip()) if p != ""]
    except (ValueError, AttributeError):
        return None
    return out if all(math.isfinite(v) for v in out) else None      # nan / inf / 1e999 read as "absent"


def _apply(m, p):
    return [m[i][0] * p[0] + m[i][1] * p[1] + m[i][2] * p[2] + m[i][3] for i in range(3)]


def _box_of_points(m, pts):
    ws = [_apply(m, p) for p in pts]
    return [[min(w[i] for w in ws) for i in range(3)], [max(w[i] for w in ws) for i in range(3)]]


def _union(boxes):
    boxes = [b for b in boxes if b and all(math.isfinite(v) for p in b for v in p)]
    if not boxes:
        return None
    return [[min(b[0][i] for b in boxes) for i in range(3)], [max(b[1][i] for b in boxes) for i in range(3)]]


_PASS = {"Group", "Billboard", "Anchor", "Collision", "StaticGroup", "Scene", "X3D", "CADLayer", "CADAssembly",
         "CADPart", "CADFace", "LayerSet", "Layer", "ViewpointGroup", "Appearance"}
_POINT_GEOM = {"IndexedFaceSet", "IndexedLineSet", "PointSet", "LineSet", "TriangleSet", "IndexedTriangleSet",
               "TriangleFanSet", "IndexedTriangleFanSet", "TriangleStripSet", "IndexedTriangleStripSet",
               "QuadSet", "IndexedQuadSet"}


_MAX_DEPTH = 256          # deeper XML is refused (ok False): real scenes are far shallower, and paths grow with depth
_MAX_WALK = 50000         # instances visited when boxes are built; USE can multiply a subtree exponentially
_MAX_INSTANCES = 64       # boxes kept per element (more instances are merged into the last one, which stays conservative)


class SceneIndex:
    """Element and attribute offsets of an X3D XML scene, DEF paths, and world-space boxes per instance.

    `ok` is False when the text is not well-formed XML (`error` says where); the other methods then
    return empty answers, so a broken scene is still traceable at text level."""

    def __init__(self, text: str):
        self.text = text
        self.ok = False
        self.error = ""
        self.elements: list = []
        self.defs: dict = {}
        self._boxes = None
        self._xpc = {}
        self._sib = ({}, {})
        self._starts = None
        self.unit_scale = {}
        try:
            self._parse()
            self.ok = True
        except expat.ExpatError as e:
            self.error = "%s (line %d, column %d)" % (expat.ErrorString(e.code), e.lineno, e.offset)
            self.elements = []
        except Exception as e:  # pragma: no cover - defensive
            self.error = "%s: %s" % (type(e).__name__, e)
            self.elements = []

    # -- parsing ------------------------------------------------------------------------------
    def _parse(self):
        text = self.text
        data = text.encode("utf-8", "surrogatepass")
        if text.isascii():
            def b2c(b):
                return b
        else:
            offs, acc = [0], 0
            for ch in text:
                acc += len(ch.encode("utf-8"))
                offs.append(acc)

            def b2c(b):
                return bisect.bisect_right(offs, b) - 1
        p = expat.ParserCreate("utf-8")      # str input is re-encoded as UTF-8 whatever the declaration says
        p.buffer_text = True
        try:
            p.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        except Exception:  # pragma: no cover
            pass
        stack: list = []
        els = self.elements
        depth = [0]

        def start(name, attrs):
            depth[0] += 1
            if depth[0] > _MAX_DEPTH:
                raise ValueError("elements nested deeper than %d" % _MAX_DEPTH)
            s = b2c(p.CurrentByteIndex)
            te = self._tag_end(s)
            e = Element(len(els), name, stack[-1] if stack else -1, s, te)
            for m in _ATTR_RE.finditer(text, s, te):
                v = m.group(3) if m.group(3) is not None else m.group(4)
                vs = m.start(3) if m.group(3) is not None else m.start(4)
                e.attrs[m.group(1)] = (v, vs, vs + len(v))
            e.def_ = attrs.get("DEF", "")
            e.use = attrs.get("USE", "")
            if stack:
                els[stack[-1]].children.append(e.idx)
            els.append(e)
            if text[te - 2:te] == "/>":
                e.end = te
                stack.append(e.idx)
                stack.append(-1)  # marker: the end handler pops both
            else:
                stack.append(e.idx)

        def end(name):
            depth[0] -= 1
            if stack and stack[-1] == -1:
                stack.pop()
                stack.pop()
                return
            i = stack.pop()
            b = b2c(p.CurrentByteIndex)
            gt = text.find(">", b)
            els[i].end = (gt + 1) if gt >= 0 else len(text)

        p.StartElementHandler = start
        p.EndElementHandler = end
        p.Parse(data, True)
        self._finish()

    def _tag_end(self, s):
        text, i, q = self.text, s, None
        while i < len(text):
            ch = text[i]
            if q:
                if ch == q:
                    q = None
            elif ch in "\"'":
                q = ch
            elif ch == ">":
                return i + 1
            i += 1
        return len(text)

    def _finish(self):
        els = self.elements
        for e in els:
            if e.def_:
                self.defs.setdefault(e.def_, e.idx)
        cnt, pos = {}, {}
        for e in els:
            if e.parent >= 0:
                k = (e.parent, e.tag)
                cnt[k] = cnt.get(k, 0) + 1
                pos[e.idx] = cnt[k]
        self._sib = (cnt, pos)
        for e in els:
            seg = e.tag + ("[DEF=%s]" % e.def_ if e.def_ else "")
            if e.parent >= 0 and e.use:
                seg = e.tag + "[USE=%s]" % e.use
            elif e.parent >= 0 and not e.def_ and cnt[(e.parent, e.tag)] > 1:
                seg = "%s[%d]" % (e.tag, pos[e.idx])
            e.path = (els[e.parent].path if e.parent >= 0 else "") + "/" + seg
        for e in els:
            if e.tag == "unit" and e.parent >= 0 and els[e.parent].tag == "head":
                cat = e.attrs.get("category", ("", 0, 0))[0]
                f = _floats_of(e.attrs.get("conversionFactor", ("", 0, 0))[0])
                if cat and f:
                    self.unit_scale[cat] = f[0]

    # -- lookup -------------------------------------------------------------------------------
    def element_at(self, offset: int):
        """Deepest element whose span holds the offset."""
        if self._starts is None:
            self._starts = [e.start for e in self.elements]
        i = bisect.bisect_right(self._starts, offset) - 1
        while i >= 0:                       # the last element that starts at or before the offset, or the nearest ancestor holding it
            e = self.elements[i]
            if e.start <= offset < (e.end if e.end >= 0 else len(self.text)):
                return e
            i = e.parent
        return None

    def attr_at(self, e, offset: int):
        for name, (v, vs, ve) in e.attrs.items():
            ns = self.text.rfind(name, e.start, vs)          # the attribute's name counts too
            if (ns if ns >= 0 else vs) <= offset <= ve:
                return name
        return None

    def xpath(self, e) -> str:
        done = self._xpc.get(e.idx)
        if done is not None:
            return done
        seg = e.tag
        if e.def_:
            seg += "[@DEF='%s']" % e.def_
        elif e.parent >= 0 and self._sib[0].get((e.parent, e.tag), 0) > 1:
            seg += "[%d]" % self._sib[1][e.idx]
        base = self.xpath(self.elements[e.parent]) if e.parent >= 0 else ""
        self._xpc[e.idx] = out = base + "/" + seg
        return out

    def resolve(self, fld: str):
        """(element, attribute name) that an XPath of the forms this module writes points at, or None:
        "/X3D/Scene/Transform[@DEF='A']/Shape/Box/@size" (the string xpath() gives plus "/@attr") and "//*[@DEF='A']/@translation"."""
        if not fld or "/@" not in fld:
            return None
        node, attr = fld.rsplit("/@", 1)
        if getattr(self, "_xp", None) is None:
            self._xp = {}
            for el in self.elements:
                self._xp.setdefault(self.xpath(el), el)
        # BUG-49: "//*[@DEF='A']/Shape/Sphere/@radius" (a path below a DEF) used to resolve to nothing, so a repeated quote fell back to the first hit
        m = re.match(r"^//\*\[@DEF=['\"]([^'\"]+)['\"]\]((?:/[A-Za-z_][\w.-]*(?:\[[^\]]+\])?)*)$", node)
        if m:
            e = self.find_def(m.group(1))
            if e is not None and m.group(2):
                e = self._xp.get(self.xpath(e) + m.group(2))
        else:
            e = self._xp.get(node)
        return (e, attr) if e is not None and attr in e.attrs else None

    def find_def(self, name: str):
        i = self.defs.get(name)
        return self.elements[i] if i is not None else None

    # -- boxes --------------------------------------------------------------------------------
    def boxes_for(self, e) -> list:
        """World-space axis-aligned boxes of the subtree rooted at `e`, one per instance (a DEF reused by
        USE shows up once per use). Empty when nothing under it has a computable box."""
        if self._boxes is None:
            self._build_boxes()
        return self._boxes.get(e.idx, [])

    def _build_boxes(self):
        self._boxes = {}
        els = self.elements
        if not els:
            return
        seen_stack: set = set()
        budget = [_MAX_WALK]

        def rec(i, u):
            lst = self._boxes.setdefault(i, [])
            if len(lst) < _MAX_INSTANCES:
                lst.append(u)
            else:
                lst[-1] = _union([lst[-1], u])

        def attr(e, name, default=None):
            v = e.attrs.get(name)
            return v[0] if v else default

        def vec(e, name, default):
            f = _floats_of(attr(e, name, "")) if attr(e, name) is not None else None
            return f if f and len(f) >= len(default) else list(default)

        def geometry_box(g, m):
            t = g.tag
            if t == "Box":
                sx, sy, sz = vec(g, "size", (2, 2, 2))[:3]
                h = (sx / 2, sy / 2, sz / 2)
                return _box_of_points(m, [(a * h[0], b * h[1], c * h[2]) for a in (-1, 1) for b in (-1, 1) for c in (-1, 1)])
            if t == "Sphere":
                r = (_floats_of(attr(g, "radius", "1")) or [1.0])[0]
                ctr = [m[i][3] for i in range(3)]
                half = [abs(r) * math.sqrt(sum(m[i][j] ** 2 for j in range(3))) for i in range(3)]
                return [[ctr[i] - half[i] for i in range(3)], [ctr[i] + half[i] for i in range(3)]]
            if t in ("Cylinder", "Cone"):
                r = (_floats_of(attr(g, "radius" if t == "Cylinder" else "bottomRadius", "1")) or [1.0])[0]
                hh = (_floats_of(attr(g, "height", "2")) or [2.0])[0] / 2
                return _box_of_points(m, [(a * r, b * hh, c * r) for a in (-1, 1) for b in (-1, 1) for c in (-1, 1)])
            if t == "Rectangle2D":
                sx, sy = vec(g, "size", (2, 2))[:2]
                return _box_of_points(m, [(a * sx / 2, b * sy / 2, 0.0) for a in (-1, 1) for b in (-1, 1)])
            if t in ("Disk2D", "Circle2D", "Arc2D", "ArcClose2D"):
                r = (_floats_of(attr(g, "outerRadius" if t == "Disk2D" else "radius", "1")) or [1.0])[0]
                return _box_of_points(m, [(a * r, b * r, 0.0) for a in (-1, 1) for b in (-1, 1)])
            if t in _POINT_GEOM:
                pts = None
                for c in g.children:
                    ce = els[c]
                    src = els[self.defs[ce.use]] if (ce.use and ce.use in self.defs) else ce
                    if src.tag in ("Coordinate", "CoordinateDouble"):
                        f = _floats_of(attr(src, "point", ""))
                        if f and len(f) >= 3:
                            pts = [tuple(f[i:i + 3]) for i in range(0, len(f) - 2, 3)]
                        break
                return _box_of_points(m, pts) if pts else None
            if t == "ElevationGrid":
                xd = int((_floats_of(attr(g, "xDimension", "0")) or [0])[0])
                zd = int((_floats_of(attr(g, "zDimension", "0")) or [0])[0])
                xs = (_floats_of(attr(g, "xSpacing", "1")) or [1.0])[0]
                zs = (_floats_of(attr(g, "zSpacing", "1")) or [1.0])[0]
                hs = _floats_of(attr(g, "height", "")) or [0.0]
                if xd < 1 or zd < 1:
                    return None
                x1, z1 = (xd - 1) * xs, (zd - 1) * zs
                lo, hi = min(hs), max(hs)
                return _box_of_points(m, [(a, b, c) for a in (0, x1) for b in (lo, hi) for c in (0, z1)])
            return None

        def walk(i, m, d=0):
            """Returns [union of the world boxes under element i (this instance)] or [], and records the union."""
            budget[0] -= 1
            if budget[0] < 0 or d > 600:
                return []
            e = els[i]
            if e.use:
                j = self.defs.get(e.use)
                if j is None or j in seen_stack:
                    return []
                seen_stack.add(j)
                try:
                    out = walk(j, m, d + 1)
                finally:
                    seen_stack.discard(j)
                u = _union(out)
                if u:
                    rec(i, u)
                return [u] if u else []
            out = []
            mm = m
            kids = list(e.children)
            if e.tag == "Transform" or e.tag == "HAnimJoint" or e.tag == "HAnimSite" or e.tag == "HAnimSegment":
                t = vec(e, "translation", (0, 0, 0))[:3]
                c = vec(e, "center", (0, 0, 0))[:3]
                r = vec(e, "rotation", (0, 0, 1, 0))[:4]
                s = vec(e, "scale", (1, 1, 1))[:3]
                so = vec(e, "scaleOrientation", (0, 0, 1, 0))[:4]
                try:
                    loc = _mat_mul(_trans(t), _trans(c))
                    loc = _mat_mul(loc, _rot(r[:3], r[3]))
                    loc = _mat_mul(loc, _rot(so[:3], so[3]))
                    loc = _mat_mul(loc, _scale(s))
                    loc = _mat_mul(loc, _rot(so[:3], -so[3]))
                    loc = _mat_mul(loc, _trans([-v for v in c]))
                    mm = _mat_mul(m, loc)
                except (ArithmeticError, ValueError):
                    return []
            elif e.tag == "Switch":
                w = int((_floats_of(attr(e, "whichChoice", "-1")) or [-1])[0])
                vis = [k for k in kids if els[k].attrs.get("containerField", ("",))[0] != "metadata"]
                kids = [vis[w]] if 0 <= w < len(vis) else []
            elif e.tag == "Shape":
                for k in kids:
                    ke = els[k]
                    g = els[self.defs[ke.use]] if (ke.use and ke.use in self.defs) else ke
                    if ke.tag in ("Appearance", "Material", "ImageTexture", "TextureTransform", "ComposedShader") or g.tag in ("Appearance",):
                        continue
                    try:
                        b = geometry_box(g, mm)
                    except (ArithmeticError, ValueError):
                        b = None
                    if b:
                        out.append(b)
                u = _union(out)
                if u:
                    rec(i, u)
                return [u] if u else []
            elif e.tag in ("Appearance", "Material", "Coordinate", "Normal", "Color", "TextureCoordinate", "Script", "head", "meta", "ProtoDeclare", "ExternalProtoDeclare"):
                return []
            for k in kids:
                out.extend(walk(k, mm, d + 1))
            u = _union(out)
            if u:
                rec(i, u)
            return [u] if u else []

        roots = [e.idx for e in els if e.parent < 0]
        for r in roots:
            walk(r, _ident())


def _round_box(b, nd=6):
    return [[round(v, nd) for v in b[0]], [round(v, nd) for v in b[1]]]


# --------------------------------------------------------------------------------------------
# COMPARE: is the value in the quote?
# --------------------------------------------------------------------------------------------
NUMWORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
    "half": 0.5, "dozen": 12,
}
# unit word -> (category, factor to the base unit: metre or second)
UNITS = {
    "m": ("length", 1.0), "meter": ("length", 1.0), "meters": ("length", 1.0), "metre": ("length", 1.0),
    "metres": ("length", 1.0), "cm": ("length", 0.01), "centimeter": ("length", 0.01),
    "centimeters": ("length", 0.01), "centimetre": ("length", 0.01), "centimetres": ("length", 0.01),
    "mm": ("length", 0.001), "millimeter": ("length", 0.001), "millimeters": ("length", 0.001),
    "millimetre": ("length", 0.001), "millimetres": ("length", 0.001), "km": ("length", 1000.0),
    "kilometer": ("length", 1000.0), "kilometers": ("length", 1000.0), "kilometre": ("length", 1000.0),
    "kilometres": ("length", 1000.0), "ft": ("length", 0.3048), "foot": ("length", 0.3048),
    "feet": ("length", 0.3048), "in": ("length", 0.0254), "inch": ("length", 0.0254),
    "inches": ("length", 0.0254), "yd": ("length", 0.9144), "yard": ("length", 0.9144),
    "yards": ("length", 0.9144), "mi": ("length", 1609.344), "mile": ("length", 1609.344),
    "miles": ("length", 1609.344),
    "s": ("time", 1.0), "sec": ("time", 1.0), "secs": ("time", 1.0), "second": ("time", 1.0),
    "seconds": ("time", 1.0), "ms": ("time", 0.001), "millisecond": ("time", 0.001),
    "milliseconds": ("time", 0.001), "min": ("time", 60.0), "mins": ("time", 60.0),
    "minute": ("time", 60.0), "minutes": ("time", 60.0), "h": ("time", 3600.0), "hr": ("time", 3600.0),
    "hrs": ("time", 3600.0), "hour": ("time", 3600.0), "hours": ("time", 3600.0),
}
COLORS = {
    "red": (1, 0, 0), "green": (0, 1, 0), "blue": (0, 0, 1), "yellow": (1, 1, 0), "cyan": (0, 1, 1),
    "magenta": (1, 0, 1), "white": (1, 1, 1), "black": (0, 0, 0), "orange": (1, 0.647, 0),
    "purple": (0.5, 0, 0.5), "pink": (1, 0.753, 0.796), "brown": (0.647, 0.165, 0.165),
    "gray": (0.5, 0.5, 0.5), "grey": (0.5, 0.5, 0.5), "gold": (1, 0.843, 0), "silver": (0.753, 0.753, 0.753),
    "teal": (0, 0.5, 0.5), "navy": (0, 0, 0.5), "lime": (0, 1, 0), "maroon": (0.5, 0, 0),
    "olive": (0.5, 0.5, 0), "violet": (0.933, 0.51, 0.933), "indigo": (0.294, 0, 0.51),
    "turquoise": (0.251, 0.878, 0.816), "beige": (0.961, 0.961, 0.863), "crimson": (0.863, 0.078, 0.235),
}
# attribute name -> semantics, used when a claim does not say what the field is
FIELD_SEM = {
    "rotation": "rotation", "orientation": "rotation", "angle": "angle", "startAngle": "angle", "endAngle": "angle",
    "diffuseColor": "color", "emissiveColor": "color", "specularColor": "color", "color": "color",
    "skyColor": "color", "groundColor": "color", "backColor": "color",
    "transparency": "fraction", "intensity": "fraction", "ambientIntensity": "fraction", "shininess": "fraction",
    "cycleInterval": "time", "startTime": "time", "stopTime": "time", "duration": "time",
    "radius": "length", "height": "length", "size": "length", "translation": "length", "bottomRadius": "length",
    "DEF": "identifier", "USE": "identifier", "name": "identifier", "containerField": "identifier",
}
_NUMSTR = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_THOUSANDS = re.compile(r"[1-9]\d{0,2}(?:(?:\.\d{3})+|(?:,\d{3})+)")
_DEG = re.compile(r"\s*(?:°|º|degs?\b|degrees?\b)", re.I)
_RAD = re.compile(r"\s*(?:rads?\b|radians?\b)", re.I)
_PCT = re.compile(r"\s*(?:%|percent\b|per cent\b|pct\b)", re.I)
_UNITW = re.compile(r"\s*([A-Za-z]+)")
_HEX = re.compile(r"(?<![\w])#?([0-9a-fA-F]{6})(?![\w])")
_MONTHS = {"jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5,
           "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
           "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12}
_CODE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")


def field_semantics(field_name: str, hint=None):
    if hint:
        return hint
    tail = re.split(r"[.@/\[\]]", field_name or "")
    tail = [t for t in tail if t][-1:] or [""]
    return FIELD_SEM.get(tail[0])


def decimals(x) -> int:
    """Printed decimals of a number or numeric string (0 for integral values)."""
    try:
        if isinstance(x, str):
            d = Decimal(x.strip())
        elif isinstance(x, float):
            if x == int(x):
                return 0
            d = Decimal(repr(x))
        else:
            d = Decimal(str(x))
        e = d.as_tuple().exponent
        return max(0, -e) if isinstance(e, int) else 0
    except (InvalidOperation, ValueError, OverflowError):
        return 0


@dataclass
class QNum:
    value: float
    dp: int
    idx: int          # token index in the quote
    raw: str
    after: str = ""   # raw text after the token (for unit evidence)
    word: bool = False


def _readings(raw: str) -> list:
    """Every way a prose number can be read: (value, decimals). 3.000 is 3 and 3000; 1.234,50 is 1234.5."""
    s = re.sub(r"['’]", "", raw)
    sign = -1.0 if s.startswith("-") else 1.0
    s = s.lstrip("+-")
    exp = ""
    m = re.match(r"^(.*?)[eE]([+-]?\d+)$", s)
    if m:
        s, exp = m.group(1), "e" + m.group(2)
    seps = {c for c in s if c in ",."}
    cands = set()
    if not seps:
        cands.add(s)
    elif len(seps) == 1 and (_THOUSANDS.fullmatch(s) or s.count(next(iter(seps))) > 1):
        cands.add(s.replace(",", "").replace(".", ""))
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        grp = "." if dec == "," else ","
        cands.add(s.replace(grp, "").replace(dec, "."))
    else:
        for sep in (",", "."):
            if s.count(sep) == 1:
                cands.add(s.replace(sep, "."))
    out = []
    for c in sorted(cands):
        try:
            out.append((sign * float(c + exp), decimals(c)))
        except ValueError:
            pass
    return out


def quote_numbers(q: Lexed, raw_quote: str, policy: TracePolicy) -> list:
    out: list = []
    for i, (n, k) in enumerate(zip(q.norms, q.kinds)):
        after = raw_quote[q.ends[i]:q.ends[i] + 16]
        if k == "n":
            if q.mode == "x3d":
                try:
                    out.append(QNum(float(n), decimals(n), i, n, after))
                except ValueError:
                    pass
            else:
                reads = _readings(n)
                before = raw_quote[max(0, q.starts[i] - 200):q.starts[i]]
                # a detached minus ("Discount (5%) - EUR 1,014.50", "temperature - 5") is not a range or a subtraction ("10 - 12 m", "pages 3 - 5"):
                # a digit just before the dash means the dash joins two numbers
                detached = re.search(r"(?:^|(?<=[^\d\s])\s|^\s*)[-−–]\s+(?:\S+\s+){0,2}$", before) is not None
                for v, dp in reads:
                    out.append(QNum(v, dp, i, n, after))
                    if detached and v > 0:
                        out.append(QNum(-v, dp, i, n, after))
        elif n in NUMWORDS and policy.allows("numword"):
            out.append(QNum(float(NUMWORDS[n]), 0, i, n, after, word=True))
    # thousands grouped by spaces in prose: "1 234", "1 234 567", "1 234,50" (also NBSP, narrow NBSP, thin space)
    if q.mode == "prose":
        seps = (" ", "\u00a0", "\u202f", "\u2009", "\u2007")
        n = len(q.norms)
        for i in range(n - 1):
            if q.kinds[i] != "n" or not re.fullmatch(r"[1-9]\d{0,2}", q.norms[i]):
                continue
            j, digits, tail = i, q.norms[i], ""
            while j + 1 < n and q.kinds[j + 1] == "n" and raw_quote[q.ends[j]:q.starts[j + 1]] in seps:
                m = re.fullmatch(r"(\d{3})(?:[.,](\d+))?", q.norms[j + 1])
                if not m:
                    break
                digits += m.group(1)
                j += 1
                if m.group(2) is not None:
                    tail = "." + m.group(2)
                    break
            if j > i:
                out.append(QNum(float(digits + tail), len(tail) - 1 if tail else 0, i, digits + tail,
                                raw_quote[q.ends[j]:q.ends[j] + 16]))
    return out


def _evidence(qn: QNum):
    a = qn.after
    if _DEG.match(a):
        return "deg", None
    if _RAD.match(a):
        return "rad", None
    if _PCT.match(a):
        return "pct", None
    m = _UNITW.match(a)
    if m and m.group(1).lower() in UNITS:
        w = m.group(1).lower()
        if w == "in" and not re.match(r"\s*(?:$|[^\sA-Za-z]|(?:wide|long|tall|high|deep|thick|across|diameter|radius|square|cube|by|x)\b)",
                                      a[m.end():], re.I):
            return None, None            # "2 in front of", "3 in the middle": a preposition
        return "unit", w
    return None, None


def _eq(a, dpa, b, dpb, pol):
    """"exact" | "near" | None. a is the claimed value, b a number read from the quote."""
    if abs(a - b) < pol.number_tolerance:
        return "exact"
    if pol.round_to_printed:
        if dpa > dpb >= 1 and abs(round(a, dpb) - b) <= 10 ** -(dpb + 3):
            return "near" if b == 0 else "exact"    # the quote shows the value rounded; a printed zero never supports a non-zero value (0.04 against "0.0")
        if 0 < dpa < dpb and abs(round(b, dpa) - a) <= 10 ** -(dpa + 3):
            return "near"         # the value is a rounded copy of the quote
    return None


def _derived_eq(a, dpa, t, pol, slack=0.0):
    if dpa == 0:
        return abs(a - t) < max(pol.number_tolerance, 1e-9 * max(1.0, abs(t)), slack)
    return abs(round(t, dpa) - a) <= 10 ** -(dpa + 3)


@dataclass
class Cmp:
    state: str = "unchecked"      # match | near | mismatch | unchecked
    derivation: list = field(default_factory=list)
    note: str = ""


def _cmp_scalar(x: float, dpx: int, qns: list, pol: TracePolicy, sem=None, angle=False) -> Cmp:
    best_conv = None
    near = None
    conflict = None
    for q in qns:
        ev, unit = _evidence(q)
        r = _eq(x, dpx, q.value, q.dp, pol)
        if r:
            if angle and ev == "deg" and pol.allows("deg2rad"):
                conflict = conflict or Cmp("mismatch", [], "degrees written where radians are expected")
            elif ev == "pct" and sem == "fraction" and abs(x) > 1 and pol.allows("percent"):
                conflict = conflict or Cmp("mismatch", [], "percent written where a 0..1 fraction is expected")
            elif ev == "unit" and sem in ("length", "time") and pol.allows("unit"):
                cat, f = UNITS[unit]
                scale = f / pol.length_scale if cat == "length" else f
                if cat == sem and abs(scale - 1.0) > 1e-9:
                    conflict = conflict or Cmp("mismatch", [], "unit %s differs from the scene unit" % unit)
                else:
                    if r == "exact":
                        return Cmp("match", ["numword"] if q.word else [], "")
                    near = near or Cmp("near", [], "")
            elif r == "exact":
                return Cmp("match", ["numword"] if q.word else [], "")
            else:
                near = near or Cmp("near", [], "")
        if ev == "deg" and pol.allows("deg2rad") and _derived_eq(x, dpx, q.value * math.pi / 180.0, pol):
            best_conv = best_conv or Cmp("match", ["deg2rad"])
        elif ev == "rad" and pol.allows("rad2deg") and _derived_eq(x, dpx, q.value * 180.0 / math.pi, pol,
                                                                     0.5 * 10 ** -q.dp * 180.0 / math.pi * 1.001 if q.dp >= 3 else 0.0):
            best_conv = best_conv or Cmp("match", ["rad2deg"])
        elif ev == "pct" and pol.allows("percent") and _derived_eq(x, dpx, q.value / 100.0, pol):
            best_conv = best_conv or Cmp("match", ["percent"])
        elif ev == "unit" and pol.allows("unit"):
            cat, f = UNITS[unit]
            t = q.value * f / pol.length_scale if cat == "length" else q.value * f
            if abs(t - q.value) > 1e-12 and _derived_eq(x, dpx, t, pol):
                best_conv = best_conv or Cmp("match", ["unit:%s" % unit])
    return best_conv or near or conflict or Cmp("mismatch")


def _floats(value):
    """List of floats for a vector-like value (list, tuple, or "1 2 3" / "1,2,3" text); None when it is not one."""
    if isinstance(value, (list, tuple)):
        try:
            return [float(v) for v in value if not isinstance(v, bool)] if all(
                (isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)) or
                (isinstance(v, str) and _NUMSTR.fullmatch(v.strip())) for v in value) else None
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        parts = [p for p in re.split(r"[\s,]+", value.strip()) if p != ""]
        if len(parts) >= 2 and all(_NUMSTR.fullmatch(p) for p in parts):
            return [float(p) for p in parts]
    return None


def _value_decimals(value) -> list:
    if isinstance(value, str):
        return [decimals(p) for p in re.split(r"[\s,]+", value.strip()) if p != ""]
    return [decimals(v) for v in value]


def _hue_class(rgb, name):
    r, g, b = rgb
    mx, mn = max(rgb), min(rgb)
    v, s = mx, (0 if mx == 0 else (mx - mn) / mx)
    if name == "white":
        return v >= 0.85 and s <= 0.15
    if name == "black":
        return v <= 0.15
    if name in ("gray", "grey", "silver"):
        return s <= 0.15 and 0.25 <= v <= 0.85
    if s < 0.35 or v < 0.25:
        return False
    d = mx - mn
    if mx == r:
        h = ((g - b) / d) % 6
    elif mx == g:
        h = (b - r) / d + 2
    else:
        h = (r - g) / d + 4
    h *= 60
    cr, cg, cb = COLORS[name]
    cm, cn = max(cr, cg, cb), min(cr, cg, cb)
    cd = cm - cn
    if cd == 0:
        return False
    if cm == cr:
        ch = ((cg - cb) / cd) % 6
    elif cm == cg:
        ch = (cb - cr) / cd + 2
    else:
        ch = (cr - cg) / cd + 4
    ch *= 60
    dh = abs(h - ch)
    return min(dh, 360 - dh) <= 30


def _cmp_color(comps, qraw, q: Lexed, pol) -> Cmp:
    if len(comps) != 3 or not all(-1e-9 <= c <= 1 + 1e-9 for c in comps):
        return Cmp("mismatch")
    if pol.allows("hex"):
        for m in _HEX.finditer(qraw):
            hx = m.group(1)
            if not (m.group(0).startswith("#") or (re.search(r"\d", hx) and re.search(r"[a-fA-F]", hx))):
                continue                      # "123456" is a number and "facade" a word
            rgb = [int(hx[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
            if all(abs(a - b) <= 0.5 / 255 + 1e-9 for a, b in zip(comps, rgb)):
                return Cmp("match", ["hex"])
    if pol.allows("rgb255"):
        qn = [n for n in quote_numbers(q, qraw, pol) if not n.word]
        ints = [n for n in qn if math.isfinite(n.value) and n.value == int(n.value) and 0 <= n.value <= 255]
        if len(ints) >= 3 and max(n.value for n in ints) > 1:
            for i in range(len(ints) - 2):
                trip = ints[i:i + 3]
                if all(abs(c * 255 - n.value) <= 0.5 + 1e-9 for c, n in zip(comps, trip)):
                    return Cmp("match", ["rgb255"])
    if pol.allows("colorname"):
        best = None
        for n in q.norms:
            if n in COLORS:
                rgb = COLORS[n]
                if all(abs(a - b) <= 0.05 for a, b in zip(comps, rgb)):
                    return Cmp("match", ["colorname:%s" % n])
                if _hue_class(comps, n):
                    best = best or Cmp("near", ["colorname-class:%s" % n])
        if best:
            return best
    return Cmp("mismatch")


def _cmp_vector(comps, dps, qraw, q: Lexed, pol, sem) -> Cmp:
    qns = quote_numbers(q, qraw, pol)
    k = len(comps)
    usem = sem if sem in ("length", "time") else None              # BUG-11: unit conflicts apply per component
    ranks = sorted({n.idx for n in qns})
    by_rank = [[n for n in qns if n.idx == ix] for ix in ranks]    # one list of readings per number token of the quote
    note = ""

    def run(seq):
        """seq = [(component index, value, decimals)] must sit on CONSECUTIVE numbers of the quote (BUG-10)."""
        nonlocal note
        for s0 in range(len(by_rank) - len(seq) + 1):
            derivs, near, ok = [], False, True
            for off, (ci, c, dp) in enumerate(seq):
                angle = sem == "rotation" and k == 4 and ci == 3   # BUG-12: the degrees-for-radians guard holds on every path
                hit = None
                for qn in by_rank[s0 + off]:
                    r = _cmp_scalar(c, dp, [qn], pol, usem, angle)
                    if r.state in ("match", "near"):
                        hit = r
                        break
                    note = note or r.note
                if hit is None:
                    ok = False
                    break
                derivs += hit.derivation
                near = near or hit.state == "near"
            if ok:
                return near, derivs
        return None

    # 1. every component, in order, on consecutive numbers of the quote (direct, then per-component conversions)
    full = run([(i, c, dp) for i, (c, dp) in enumerate(zip(comps, dps))])
    if full:
        return Cmp("near" if full[0] else "match", sorted(set(full[1])))
    # 1b. only the non-zero components are in the quote ("2 m above": 0 2 0), and only when the quote does not state
    #     every component (BUG-4: 'translation="2 0 0"' says where the 2 goes)
    nz = [(i, c, dp) for i, (c, dp) in enumerate(zip(comps, dps)) if abs(c) > 1e-12]
    if 0 < len(nz) < k and sem != "color" and len(by_rank) < k:
        part = run(nz)
        if part:
            return Cmp("near", sorted(set(["zero-fill"] + part[1])), "components the quote does not mention are zero")
    # 2. colour: hex, 0-255, names
    if sem == "color" and k == 3:
        r = _cmp_color(comps, qraw, q, pol)
        if r.state != "mismatch":
            return r
    # 3. rotation from an axis letter and one angle ((-axis, -angle) is the same turn as (axis, angle); (-axis, +angle) is not)
    if sem == "rotation" and k == 4 and pol.allows("axis"):
        axes = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1), "vertical": (0, 1, 0), "up": (0, 1, 0)}
        words = [n for n, kd in zip(q.norms, q.kinds) if kd == "w" and n in axes]
        ang_q = [n for n in qns if not n.word]
        if words and ang_q:
            ax = axes[words[0]]
            same = all(abs(a - b) < 1e-6 for a, b in zip(comps[:3], ax))
            flip = all(abs(a + b) < 1e-6 for a, b in zip(comps[:3], ax))
            if same or flip:
                ang = -comps[3] if flip else comps[3]
                for qn in ang_q:
                    r = _cmp_scalar(ang, dps[3], [qn], pol, None, True)
                    if r.state in ("match", "near"):
                        return Cmp(r.state, sorted(set(["axis:" + words[0]] + r.derivation)))
                    note = note or r.note
    # 4. one number repeated (a cube of side 2)
    if pol.allows("broadcast") and k >= 2 and len(set(comps)) == 1:
        r = _cmp_scalar(comps[0], dps[0], qns, pol, usem, False)
        if r.state in ("match", "near"):
            return Cmp(r.state, sorted(set(["broadcast"] + r.derivation)))
        note = note or r.note
    # BUG-13: a colour name that names another colour is a contradiction, not "nothing to compare"
    if sem == "color" and k == 3 and pol.allows("colorname") and any(n in COLORS for n in q.norms):
        return Cmp("mismatch", [], note or "the colour named in the quote is another colour")
    return Cmp("mismatch", [], note) if qns else Cmp("unchecked")


def _date_parts(s: str):
    parts = re.findall(r"\d+", s)
    if len(parts) != 3 or not re.fullmatch(r"[\d\s./-]+", s.strip()):
        return None
    n = [int(p) for p in parts]
    # BUG-47: a dotted triple with no four-digit year ("16.7.0", "5.8.4") is a version, not a date: every permutation of it would verify
    if re.fullmatch(r"\d+\.\d+\.\d+", s.strip()) and not any(1000 <= x <= 2999 for x in n):
        return None
    for y in range(3):
        m, d = (n[i] for i in range(3) if i != y)
        if (1000 <= n[y] <= 2999 or n[y] < 100) and 1 <= m <= 31 and 1 <= d <= 31 and min(m, d) <= 12:
            return n
    return None


def _cmp_date(parts, q: Lexed) -> Cmp:
    vals = []
    for n, k in zip(q.norms, q.kinds):
        if k == "n" and re.fullmatch(r"\d+(?:[./'\u2019,]\d+)*", n):
            groups = re.findall(r"\d+", n)
            for g in groups[:-1]:
                v = int(g)
                vals.append({v, v + 2000} if v < 100 else {v})
            v = int(groups[-1])
            vals.append({v, v + 2000} if v < 100 else {v})
        elif k == "w" and n in _MONTHS:
            vals.append({_MONTHS[n]})
        else:
            vals.append(set())
    def assign(todo, window, used):
        return not todo or any(j not in used and todo[0] in s and assign(todo[1:], window, used | {j})
                               for j, s in enumerate(window))

    for i in range(len(vals)):
        if assign(list(parts), vals[i:i + 6], frozenset()):
            return Cmp("match", ["date"])
    return Cmp("mismatch")


def _is_identifier(text: str) -> bool:
    return re.fullmatch(r"[A-Za-z_][\w.\-]*", text) is not None


def compare(value, quote: str, q: Lexed, pol: TracePolicy, sem=None) -> Cmp:
    """Is `value` in `quote`? `q` is the quote lexed in the mode of the source it was found in."""
    if value is None or isinstance(value, (bool, dict)):
        return Cmp("unchecked")
    comps = _floats(value)
    if comps is not None and len(comps) >= 2:
        if len(comps) > 64:   # long arrays: only a verbatim (numeric, consecutive) copy of the attribute text counts
            vals = []
            for n, kd in zip(q.norms, q.kinds):
                try:
                    vals.append(float(n) if kd == "n" else None)
                except ValueError:
                    vals.append(None)
            return Cmp("match", ["verbatim"]) if _contains(vals, comps) else Cmp("mismatch")
        return _cmp_vector(comps, _value_decimals(value), quote, q, pol, sem)
    if isinstance(value, (list, tuple)):
        return Cmp("unchecked")
    num = None
    if isinstance(value, (int, float, Decimal)):
        num = float(value)
    elif isinstance(value, str) and _NUMSTR.fullmatch(value.strip()):
        s = value.strip()
        if not (len(s) > 1 and s[0] == "0" and s[1].isdigit()) and sem != "identifier":
            num = float(s)
    if num is not None:
        if math.isnan(num) or math.isinf(num):
            return Cmp("mismatch")
        r = _cmp_scalar(num, decimals(value), quote_numbers(q, quote, pol), pol, sem if sem in ("length", "time", "fraction") else None,
                        sem in ("angle", "rotation"))
        if (r.state == "mismatch" and isinstance(value, str) and q.mode == "prose" and pol.match_reformatted_identifiers
                and _spells(q.norms, re.sub(r"\D", "", value))):
            return Cmp("match", ["reformatted"])
        return r
    text = str(value)
    parts = _date_parts(text)
    if parts is not None:
        return _cmp_date(parts, q)
    wanted = lex(text, q.mode).norms
    if not wanted:
        return Cmp("unchecked")
    if _contains(q.norms, wanted):
        return Cmp("match")
    if q.mode == "prose" and pol.match_reformatted_identifiers and _spells(q.norms, "".join(wanted)):
        return Cmp("match", ["reformatted"])
    if pol.code_values_unchecked and _CODE.fullmatch(text):
        return Cmp("unchecked")
    return Cmp("mismatch")


def _contains(hay: list, needle: list) -> bool:
    n = len(needle)
    if not n:
        return False
    return any(hay[i:i + n] == needle for i in range(len(hay) - n + 1) if hay[i] == needle[0])


def _spells(words: list, squeezed: str) -> bool:
    if len(squeezed) < 4:
        return False
    for start in range(len(words)):
        joined = ""
        for w in words[start:]:
            joined += w
            if joined == squeezed:
                return True
            if not squeezed.startswith(joined):
                break
    return False


def verdict_of(status: str, vstate: str, value, quote_words: int, pol: TracePolicy, occurrences: int = 1,
               identifier: bool = False, disambiguated: bool = False) -> str:
    """The verdict table (same branches as GAIK's, plus the "near" state and the moved policy)."""
    if status == "no_text_layer":
        return "unchecked"
    if status == "no_quote":
        return "unchecked"
    if status not in ("found", "moved") or quote_words < pol.min_quote_words:
        return "check"
    if status == "moved" and pol.moved_is_check:
        return "check"
    if vstate == "match":
        return "check" if (pol.ambiguous_quote_is_check and occurrences > 1 and not disambiguated) else "verified"
    if vstate == "near":
        return "check" if pol.reworded_is_problem else "reworded"
    if vstate == "mismatch":
        numeric = isinstance(value, (int, float, Decimal)) or _floats(value) is not None or (
            isinstance(value, str) and (_NUMSTR.fullmatch(value.strip()) is not None or _date_parts(value) is not None
                                        or re.fullmatch(r"\d+(?:\.\d+){2,}", value.strip()) is not None))
        if not numeric and not identifier:
            return "check" if pol.reworded_is_problem else "reworded"
        return "check"
    return "unchecked"


# --------------------------------------------------------------------------------------------
# Claims, items, report
# --------------------------------------------------------------------------------------------
@dataclass
class Claim:
    """One value and the quote that supports it. `source` is a source id (or None for the only source);
    `document` is GAIK's 1-based source number. prefix/suffix/path/line pick between repeated quotes."""
    field: str
    value: object
    quote: str = ""
    source: object = None
    document: int = 0
    page: int = 1
    prefix: str = ""
    suffix: str = ""
    path: str = ""
    line: int = 0
    ftype: str = ""
    record: object = None

    @classmethod
    def of(cls, d) -> "Claim":
        if isinstance(d, Claim):
            return d
        known = {f for f in cls.__dataclass_fields__}
        kw = {k: v for k, v in d.items() if k in known}
        for k, v in (d.get("hints") or {}).items():     # hints as carried inside a report item
            if k in ("prefix", "suffix", "path", "line"):
                kw[k] = v
        return cls(**kw)


def portable_value(v):
    """Values travel as text or integers: a float becomes its shortest repr string, a Decimal its plain string,
    so a hashed record means the same in every language."""
    if isinstance(v, bool) or v is None or isinstance(v, str):
        return v
    if isinstance(v, int):
        if abs(v) < 2 ** 53:
            return v
        try:
            return str(v)
        except ValueError:                       # more than 4300 digits
            return "<integer of %d bits>" % v.bit_length()
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, (list, tuple)):
        return [portable_value(x) for x in v]
    return str(v)


@dataclass
class TraceItem:
    field: str
    value: object
    quote: str
    source: str = ""
    document: int = 1
    page: int = 1
    cited_page: int = 1
    status: str = "not_found"
    value_status: str = "unchecked"
    verdict: str = "unchecked"
    rects: list = field(default_factory=list)
    value_rects: list = field(default_factory=list)
    span: object = None
    occurrences: int = 0
    record: object = None
    derivation: list = field(default_factory=list)
    note: str = ""
    selectors: list = field(default_factory=list)
    locus: dict = field(default_factory=dict)
    disambiguated_by: str = ""
    hints: dict = field(default_factory=dict)       # the claim's prefix / suffix / path / line, kept so the check can be replayed
    target: dict = field(default_factory=dict)      # where the claimed value sits in the scene the claim is about (when one is given)

    def to_dict(self) -> dict:
        d = {"field": self.field, "value": portable_value(self.value), "quote": self.quote, "source": self.source,
             "document": self.document, "page": self.page, "cited_page": self.cited_page, "status": self.status,
             "value_status": self.value_status, "verdict": self.verdict, "rects": self.rects,
             "value_rects": self.value_rects, "span": list(self.span) if self.span else None,
             "occurrences": self.occurrences, "record": self.record, "derivation": self.derivation,
             "note": self.note, "selectors": self.selectors, "locus": self.locus,
             "disambiguated_by": self.disambiguated_by}
        if self.hints:
            d["hints"] = dict(self.hints)
        if self.target:
            d["target"] = self.target
        return d


@dataclass
class TraceReport:
    items: list
    sources: list                  # Source.info() dicts
    policy: TracePolicy
    expected: object = None        # fields the writer was asked to evidence (None = unknown)

    def summary(self) -> dict:
        c = {v: 0 for v in VERDICTS}
        for it in self.items:
            c[it.verdict] += 1
        c["total"] = len(self.items)
        c["moved"] = sum(1 for it in self.items if it.status == "moved")
        c["ambiguous"] = sum(1 for it in self.items if it.occurrences > 1 and not it.disambiguated_by)
        if any(it.target for it in self.items):
            c["scene_mismatch"] = sum(1 for it in self.items if it.target.get("found") and it.target.get("scene_value") == "mismatch")
            c["scene_unresolved"] = sum(1 for it in self.items if it.target and not it.target.get("found"))
        c["coverage_ppm"] = None if self.coverage is None else int(round(self.coverage * 1e6))
        c["verified_coverage_ppm"] = None if self.verified_coverage is None else int(round(self.verified_coverage * 1e6))
        return c

    @property
    def coverage(self):
        """Share of the expected fields that came with a quote; None when no expectation was given."""
        if not self.expected:
            return None
        cited = {it.field for it in self.items if it.status != "no_quote"}
        return round(len(cited & set(self.expected)) / len(set(self.expected)), 6)

    @property
    def verified_coverage(self):
        """Share of the expected fields whose quote was verified (the number to trust, GAIK counts only citations)."""
        if not self.expected:
            return None
        ver = {it.field for it in self.items if it.verdict == "verified"}
        return round(len(ver & set(self.expected)) / len(set(self.expected)), 6)

    def to_dict(self) -> dict:
        return {"schema": SCHEMA, "summary": self.summary(), "documents": [s["id"] for s in self.sources],
                "sources": [it.to_dict() for it in self.items], "source_files": self.sources,
                "policy": self.policy.to_dict(), "expected": self.expected}

    def canonical(self) -> str:
        return canonical_json(self.to_dict())

    def sha256(self) -> str:
        return _sha(self.canonical().encode("utf-8"))

    def to_gaik_dict(self) -> dict:
        """The report in GAIK's `TraceReport.to_dict()` shape ("near" becomes "mismatch")."""
        d = self.to_dict()
        for it in d["sources"]:
            if it["value_status"] == "near":
                it["value_status"] = "mismatch"
        sm = {k: d["summary"][k] for k in (*VERDICTS, "total")}
        sm["coverage"] = self.coverage
        return {"summary": sm, "documents": d["documents"], "sources": d["sources"]}


def _as_sources(sources) -> list:
    if isinstance(sources, (str, Source)):
        sources = [sources]
    out = []
    for i, s in enumerate(sources):
        out.append(s if isinstance(s, Source) else Source("source-%d" % (i + 1), s))
    return out


_DEF_XPATH = re.compile(r"^//\*\[@DEF=['\"]([^'\"]+)['\"]\](?:/[A-Za-z_][\w.-]*(?:\[[^\]]+\])?)*$")   # BUG-49: also a path below the DEF


def _node_hint(fld: str) -> str:
    """The node part of a field that names a scene attribute ("/X3D/Scene/Transform[DEF=Arm]/Shape@size",
    "//*[@DEF='Arm']/@translation"): where a quote about that attribute must sit when it occurs more than once."""
    if not fld or "@" not in fld:
        return ""
    head = fld[: fld.rindex("/@")] if "/@" in fld else fld[: fld.rindex("@")]
    head = head.rstrip("/")
    m = _DEF_XPATH.match(head)
    return m.group(1) if m else head


def _choose(src: Source, occ: list, q_norms: list, claim: Claim) -> tuple:
    """Pick one of several occurrences from the claim's hints. Returns (start token index, how)."""
    if len(occ) == 1:
        return occ[0], ""
    lx = src.lexed
    cands, how = list(occ), ""
    n = len(q_norms)
    if claim.prefix:
        pre = lex(claim.prefix, src.mode, src.normalizer).norms
        keep = [i for i in cands if lx.norms[max(0, i - len(pre)):i] == pre]
        if keep:
            cands, how = keep, "prefix"
    if claim.suffix and len(cands) > 1:
        suf = lex(claim.suffix, src.mode, src.normalizer).norms
        keep = [i for i in cands if lx.norms[i + n:i + n + len(suf)] == suf]
        if keep:
            cands, how = keep, (how + "+suffix" if how else "suffix")
    hint = claim.path or _node_hint(claim.field)
    if hint and len(cands) > 1 and src.scene is not None and src.scene.ok:
        sc = src.scene
        keep = []
        for i in cands:
            e = sc.element_at(lx.starts[i])
            cur = e
            while cur is not None:
                if hint in (cur.path, sc.xpath(cur), cur.def_) or cur.path.endswith("/" + hint.lstrip("/")):
                    keep.append(i)
                    break
                cur = sc.elements[cur.parent] if cur.parent >= 0 else None
        if keep:
            cands, how = keep, (how + "+path" if how else "path") if claim.path else (how + "+field" if how else "field")
    if isinstance(claim.line, int) and claim.line > 0 and len(cands) > 1:
        nl = [m.start() for m in re.finditer("\n", src.text)]
        on = [i for i in cands if bisect.bisect_left(nl, lx.starts[i]) + 1 == claim.line]
        if on:
            cands = on[:1] if len(on) == 1 else on
            how = (how + "+line" if how else "line") if len(on) == 1 else how
    return cands[0], (how if len(cands) == 1 else "")


def _locus(src: Source, start: int) -> dict:
    line, col = line_col(src.text, start)
    loc = {"line": line, "col": col}
    sc = src.scene
    if sc is not None and sc.ok:
        e = sc.element_at(start)
        if e is not None:
            loc.update({"node": e.tag, "path": e.path, "xpath": sc.xpath(e), "def": e.def_ or None,
                        "attr": sc.attr_at(e, start)})
            cur, boxes, owner = e, [], None
            while cur is not None:
                boxes = sc.boxes_for(cur)
                if boxes:
                    owner = cur
                    break
                cur = sc.elements[cur.parent] if cur.parent >= 0 else None
            boxes = [b for b in boxes if all(math.isfinite(v) and abs(v) < 9e9 for p in b for v in p)]
            loc["aabb_e6"] = [[int(round(v * 1e6)) for v in b[0]] + [int(round(v * 1e6)) for v in b[1]] for b in boxes]
            loc["aabb_owner"] = owner.path if owner is not None else None
    elif sc is not None and not sc.ok:
        loc["parse_error"] = sc.error
    return loc


def _selectors(src: Source, s: int, e: int, loc: dict) -> list:
    t = src.text
    sel = [{"type": "TextQuoteSelector", "exact": t[s:e], "prefix": t[max(0, s - 32):s], "suffix": t[e:e + 32]},
           {"type": "TextPositionSelector", "start": s, "end": e}]
    if loc.get("xpath"):
        sel.append({"type": "XPathSelector", "value": loc["xpath"]})
    return sel


def _target_of(c: Claim, target: "Source", pol: TracePolicy, sem) -> dict:
    """Where the claimed value sits in the scene the claim is about, and whether the scene says the same."""
    sc = target.scene
    if sc is None or not sc.ok:
        return {"found": False, "why": "the scene is not well-formed XML"}
    hit = sc.resolve(c.field)
    if hit is None:
        return {"found": False, "why": "the field names no attribute of the scene"}
    e, a = hit
    v, vs, ve = e.attrs[a]
    cm = compare(c.value, v, lex(v, "x3d"), pol, sem)
    if isinstance(c.value, bool):
        na = None
    elif isinstance(c.value, (int, float, Decimal)) or (isinstance(c.value, str) and _NUMSTR.fullmatch(c.value.strip())):
        na = [float(c.value)]
    else:
        na = _floats(c.value)
    nb = _floats_of(v)
    if cm.state in ("match", "near") and na and nb and len(na) != len(nb) and not (
            len(na) == 1 and all(abs(x - na[0]) < pol.number_tolerance for x in nb)):
        cm = Cmp("mismatch")                  # 1 2 is not 1 2 3; a single number holds for a uniform vector only
    loc = _locus(target, vs)
    out = {"found": True, "text": v, "scene_value": "match" if cm.state in ("match", "near") else "mismatch",
           "span": [vs, ve]}
    for k in ("node", "path", "xpath", "def", "aabb_e6", "aabb_owner"):
        if loc.get(k) not in (None, []):
            out[k] = loc[k]
    return out


def check_claims(claims, sources, policy: TracePolicy = None, expected_fields=None, target=None) -> TraceReport:
    """Check every claim against the sources. No model is involved. `target` is the scene the claims are about (a Source of
    kind "x3d"): each item then says where its field sits in that scene and whether the scene holds the claimed value."""
    if isinstance(target, str):
        target = Source("target", target, "x3d")
    pol = policy or TracePolicy()
    srcs = _as_sources(sources)
    by_id = {s.id: i for i, s in enumerate(srcs)}
    items = []
    for raw in claims:
        try:
            c = Claim.of(raw)
            sem = field_semantics(c.field, c.ftype)
            pref = by_id.get(c.source) if c.source is not None else None
            unknown = c.source is not None and pref is None            # BUG-31: the writer cited a source that does not exist
            if pref is None:
                pref = c.document - 1 if 1 <= (c.document or 0) <= len(srcs) else 0
            it = TraceItem(field=c.field, value=c.value, quote=c.quote, source=srcs[pref].id if srcs else "",
                           document=pref + 1, page=1, cited_page=c.page or 1, record=c.record,
                           hints={k: getattr(c, k) for k in ("prefix", "suffix", "path", "line") if getattr(c, k)})
            if target is not None and c.field.startswith("/"):
                it.target = _target_of(c, target, pol, sem)
            if not (c.quote or "").strip():
                it.status, it.verdict = "no_quote", "unchecked"
                items.append(it)
                continue
            if not any(s.kind != "binary" for s in srcs):                # BUG-24: nothing with a text layer to look in
                it.status, it.verdict = "no_text_layer", "unchecked"
                items.append(it)
                continue
            order = [pref] + [i for i in range(len(srcs)) if i != pref]
            found = None
            for i in order:
                s = srcs[i]
                if s.kind == "binary":
                    continue
                q = lex(c.quote, s.mode, s.normalizer)
                occ = s.occurrences(q.norms)
                if occ:
                    found = (i, s, q, occ)
                    break
            if found is None:
                it.status, it.verdict = "not_found", "check"
                items.append(it)
                continue
            i, s, q, occ = found
            at, how = _choose(s, occ, q.norms, c)
            lx = s.lexed
            n = len(q.norms)
            st, en = lx.starts[at], lx.ends[at + n - 1]
            it.document, it.source = i + 1, s.id
            it.status = "found" if (i == pref and not unknown) else "moved"
            it.occurrences = len(occ)
            it.span = (st, en)
            it.disambiguated_by = how
            cmpr = compare(c.value, c.quote, q, pol, sem)
            # a long text value that runs through the quote counts as supported
            if cmpr.state == "mismatch" and isinstance(c.value, str) and not _NUMSTR.fullmatch(c.value.strip()):
                want = lex(c.value, s.mode, s.normalizer).norms
                if want and len(want) > n:
                    for j in s.occurrences(want):
                        if j < at + n and at < j + len(want):
                            cmpr = Cmp("match", ["value-extends-quote"])
                            break
            it.value_status, it.derivation, it.note = cmpr.state, cmpr.derivation, cmpr.note
            if unknown:
                it.note = ("the cited source %r is not among the sources. " % (c.source,)) + it.note
            ident = sem == "identifier" or (s.kind == "x3d" and isinstance(c.value, str) and _is_identifier(c.value)
                                            and not _NUMSTR.fullmatch(c.value))
            it.verdict = verdict_of(it.status, cmpr.state, c.value, n, pol, len(occ), ident, bool(how))
            if it.verdict == "check" and cmpr.state == "mismatch" and not cmpr.note and not re.search(r"\d", c.quote) and not (
                    pol.allows("numword") and any(w in NUMWORDS for w in q.norms)) and (
                    isinstance(c.value, (int, float, Decimal)) or _floats(c.value) is not None):
                it.verdict = "unchecked"   # a number written in words that no table read
            it.locus = _locus(s, st)
            it.selectors = _selectors(s, st, en, it.locus)
            items.append(it)
        except Exception as e:                       # BUG-25: report the claim as unchecked and go on
            d = raw if isinstance(raw, dict) else getattr(raw, "__dict__", {})
            items.append(TraceItem(field=str(d.get("field", "")), value=portable_value(d.get("value")),
                                   quote=str(d.get("quote") or ""), source=srcs[0].id if srcs else "", status="no_quote",
                                   verdict="unchecked", note="claim not checked: %s: %s" % (type(e).__name__, e)))

    return TraceReport(items, [s.info() for s in srcs], pol,
                       sorted(set(expected_fields)) if expected_fields else None)


def claims_sha256(claims) -> str:
    """Digest of the claim list as written (field, value, quote and the selectors that choose between repeated quotes)."""
    rows = []
    for c in claims:
        c = Claim.of(c)
        row = {k: getattr(c, k) for k in Claim.__dataclass_fields__}
        row["value"] = portable_value(row["value"])          # the same text in every language: 1.0 and 1e-06 are not floats here
        rows.append(row)
    return _sha(canonical_json(rows).encode("utf-8"))


_RECHECK_KEYS = ("field", "value", "quote", "verdict", "status", "value_status", "span", "source", "derivation",
                 "occurrences", "note", "selectors", "locus", "disambiguated_by", "target", "document", "page", "cited_page",
                 "record", "rects", "value_rects", "hints")


def recompute(report_dict: dict, claims, sources, policy: TracePolicy = None, target=None) -> list:
    """Re-run the check from (claims, sources) and list every field of every item that differs from `report_dict`.
    An empty list means the report is exactly what the code says today, down to the node path and world box."""
    if policy is None and isinstance(report_dict.get("policy"), dict):
        policy = TracePolicy.from_dict(report_dict["policy"])           # BUG-9: replay under the policy the report was made with
    rep = check_claims(claims, sources, policy, report_dict.get("expected"), target).to_dict()
    old = report_dict.get("sources", [])
    diffs = []
    if len(old) != len(rep["sources"]):
        return [{"index": -1, "what": "item count", "old": len(old), "new": len(rep["sources"])}]
    for k, (a, b) in enumerate(zip(old, rep["sources"])):
        for key in _RECHECK_KEYS:
            if canonical_json(a.get(key)) != canonical_json(b.get(key)):
                diffs.append({"index": k, "field": b["field"], "what": key, "old": a.get(key), "new": b.get(key)})
    for key in ("schema", "summary", "documents", "source_files", "expected", "policy"):
        if canonical_json(report_dict.get(key)) != canonical_json(rep.get(key)):
            diffs.append({"index": -2, "field": "(report)", "what": key, "old": report_dict.get(key), "new": rep.get(key)})
    return diffs


def recheck(items, source_texts: dict, policy: TracePolicy = None, kinds: dict = None, target_text: str = None) -> list:
    """Replay a report: rebuild each claim from its item (field, value, quote, source and the hints), re-run the check against
    `source_texts` {id: text} and return fresh item dicts, in order. No model, no state. `kinds` maps an id to "x3d" or "text"
    (default: sniffed). A claim whose source text was not supplied comes back `unchecked` with status "source_missing"."""
    srcs = [Source(i, t, (kinds or {}).get(i, "auto")) for i, t in source_texts.items()]
    tgt = Source("target", target_text, "x3d") if target_text else None      # one lexed copy for every item
    out = []
    for it in items:
        if it.get("source") not in source_texts:
            d = dict(it)
            d.update(status="source_missing", verdict="unchecked", span=None, occurrences=0)
            out.append(d)
            continue
        rep = check_claims([it], srcs, policy, None, tgt)
        out.append(rep.items[0].to_dict())
    return out


GRADE_CAP = {"verified": "Confirmed", "reworded": "Likely", "unchecked": "Likely", "check": "Suspected"}


def grade_cap(verdict: str, has_quote: bool = True) -> str:
    """The highest grade (Confirmed / Likely / Suspected) a claim may carry on this evidence."""
    return GRADE_CAP.get(verdict, "Suspected") if has_quote else "Suspected"


def explain(it: TraceItem) -> str:
    """One plain sentence for a review screen."""
    if it.status == "no_quote":
        return "No quote came with this value, so there is nothing to check."
    if it.status == "no_text_layer":
        return "The source has no text to check against."
    if it.status == "not_found":
        return "The quote is not in the source. The model may have paraphrased it or made it up."
    where = " (found in another source than the one cited)" if it.status == "moved" else ""
    amb = " It occurs %d times; " % it.occurrences + (
        "context picked one by %s." % it.disambiguated_by if it.disambiguated_by else "the first was used.") if it.occurrences > 1 else ""
    if it.verdict == "verified":
        via = " through %s" % ", ".join(it.derivation) if it.derivation else ""
        return "The quote is in the source and holds the value%s%s.%s" % (via, where, amb)
    if it.verdict == "reworded":
        return "The quote is real but the value is not literally in it%s: the writer's own wording or a close instance.%s" % (where, amb)
    if it.verdict == "check":
        why = it.note or "the quote holds a different number than the value"
        return "Look at this one: %s%s.%s" % (why, where, amb)
    return "This value cannot be compared with its quote; read it by hand.%s" % amb


# --------------------------------------------------------------------------------------------
# Asking a writer for evidence
# --------------------------------------------------------------------------------------------
CLAIM_INSTRUCTION = """EVIDENCE (required): after the answer, list one claim for every value you wrote that came from the source.
Each claim has: field (the node and attribute, e.g. "Material[ball].diffuseColor" or the DEF name and the attribute),
value (exactly as written in the scene), source (the id of the source it came from), and quote.
The quote is a SHORT snippet (at most about 80 characters) copied EXACTLY from the source with the value or the words
that give it, including signs, decimal points, exponents and units. Do not paraphrase, translate or round it.
If the same text occurs more than once, add "prefix" and "suffix" (the few words just before and after it).
If a value has no support in the source (your own choice), say so with an empty quote instead of inventing one."""

CLAIMS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "field": {"type": "string"}, "value": {}, "source": {"type": "string"}, "quote": {"type": "string"},
            "prefix": {"type": "string"}, "suffix": {"type": "string"}, "path": {"type": "string"},
            "ftype": {"type": "string"},
        },
        "required": ["field", "value", "quote"],
    },
}


def augment_prompt(task: str) -> str:
    """The task text with the evidence request appended. The schema of the scene itself is not touched."""
    return task.rstrip() + "\n\n" + CLAIM_INSTRUCTION


# --------------------------------------------------------------------------------------------
# What a scene says (inventory) and a model-free first guess at where it came from
# --------------------------------------------------------------------------------------------
INVENTORY_ATTRS = ("size", "radius", "height", "bottomRadius", "translation", "rotation", "scale", "center",
                   "diffuseColor", "emissiveColor", "specularColor", "transparency", "shininess", "ambientIntensity",
                   "intensity", "color", "direction", "location", "cycleInterval", "fieldOfView", "position",
                   "orientation", "skyColor", "groundColor", "speed", "string", "whichChoice", "loop")


def inventory(scene_text: str, attrs=INVENTORY_ATTRS) -> list:
    """Value-bearing attributes of a scene: [{field, node, attr, value, sem, span}]. `field` is an XPath 1.0 to the attribute
    (DEF-predicated where a DEF exists, positional otherwise), the same string a writer is asked to use."""
    sc = SceneIndex(scene_text)
    out = []
    if not sc.ok:
        return out
    for e in sc.elements:
        if e.use:
            continue
        for a in attrs:
            if a in e.attrs:
                v, vs, ve = e.attrs[a]
                out.append({"field": "%s/@%s" % (sc.xpath(e), a), "node": e.tag, "attr": a, "value": v,
                            "sem": field_semantics(a), "span": (vs, ve)})
    return out


def suggest_claims(scene_text: str, prompt_text: str, pol: TracePolicy = None, max_window: int = 10) -> tuple:
    """Model-free first guess at the evidence: for each inventoried value look for the smallest window of the
    prompt that supports it. Returns (claims, unsupported inventory rows). This is a LEXICAL UPPER BOUND on what a
    writer could cite, not what any model did: use it to size the problem and as a baseline critic."""
    pol = pol or TracePolicy()
    pl = lex(prompt_text, "prose")
    anchors = [j for j, (n, k) in enumerate(zip(pl.norms, pl.kinds))
               if k == "n" or n in NUMWORDS or n in COLORS or n in ("x", "y", "z", "vertical", "up")]
    claims, rest = [], []
    for row in inventory(scene_text):
        best = None
        for size in range(1, max_window + 1):
            for j in anchors:
                for a in range(max(0, j - size + 1), j + 1):
                    b = min(len(pl.norms) - 1, a + size - 1)
                    if b < j:
                        continue
                    quote = prompt_text[pl.starts[a]:pl.ends[b]]
                    q = lex(quote, "prose")
                    r = compare(row["value"], quote, q, pol, row["sem"])
                    if r.state in ("match", "near"):
                        best = quote
                        break
                if best:
                    break
            if best:
                break
        if best:
            claims.append(Claim(row["field"], row["value"], best, source="prompt", ftype=row["sem"] or ""))
        else:
            rest.append(row)
    return claims, rest


# --------------------------------------------------------------------------------------------
# Command line: python3 x3dtrace.py check|suggest|inventory ...
# --------------------------------------------------------------------------------------------
def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _render_table(rep: TraceReport) -> str:
    rows = []
    for it in rep.items:
        where = it.locus.get("path") or ("line %s" % it.locus.get("line", "?")) if it.locus else ""
        conv = ("  [" + ", ".join(it.derivation) + "]") if it.derivation else ""
        rows.append("%-10s %-9s %s = %s%s" % (it.verdict, it.value_status, it.field, canonical_json(portable_value(it.value)), conv))
        if it.verdict != "verified":
            rows.append("           %s" % explain(it))
    s = rep.summary()
    rows.append("")
    rows.append("verified %d, reworded %d, check %d, unchecked %d of %d (ambiguous %d)" % (
        s["verified"], s["reworded"], s["check"], s["unchecked"], s["total"], s["ambiguous"]))
    return "\n".join(rows)


def main(argv=None) -> int:
    try:
        return _main(argv)
    except (OSError, ValueError, UnicodeError) as e:        # missing file, bad JSON, bad claims file: exit 2, not a traceback
        print("x3dtrace: error: %s" % e, file=sys.stderr)
        return 2


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="x3dtrace", description="Check the quote beside each value: FIND, COMPARE, LOCATE (no model call).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="check claims.json against sources")
    c.add_argument("claims", help="JSON list of {field, value, quote, source[, prefix, suffix, path, line]}")
    c.add_argument("--source", action="append", default=[], metavar="ID=PATH", help="a source file (kind sniffed: x3d or text); repeat")
    c.add_argument("--json", action="store_true", help="print the full report as JSON")
    c.add_argument("--strict", action="store_true", help="TracePolicy.strict(): conversions off, ambiguous and moved quotes are check")
    i = sub.add_parser("inventory", help="list the value-bearing attributes of a scene with their XPath")
    i.add_argument("scene")
    g = sub.add_parser("suggest", help="lexical first guess: which attribute values a prompt text could support (an upper bound, not a model)")
    g.add_argument("scene")
    g.add_argument("prompt")
    a = ap.parse_args(argv)
    if a.cmd == "inventory":
        for r in inventory(_read(a.scene)):
            print("%s = %s" % (r["field"], r["value"]))
        return 0
    if a.cmd == "suggest":
        claims, rest = suggest_claims(_read(a.scene), _read(a.prompt))
        print(canonical_json([c.__dict__ for c in claims]))
        print("# supported %d of %d; unsupported: %s" % (len(claims), len(claims) + len(rest), ", ".join(r["field"] for r in rest[:8])), file=sys.stderr)
        return 0
    srcs = []
    for spec in a.source:
        sid, _, path = spec.partition("=")
        srcs.append(Source(sid, _read(path)))
    claims = json.loads(_read(a.claims))
    if not isinstance(claims, list) or not all(isinstance(c, dict) for c in claims):
        raise ValueError("the claims file must hold a JSON list of objects")
    rep = check_claims(claims, srcs, TracePolicy.strict() if a.strict else TracePolicy())
    print(canonical_json(rep.to_dict()) if a.json else _render_table(rep))
    s = rep.summary()
    return 0 if s["check"] == 0 and s["unchecked"] == 0 else 1


if __name__ == "__main__":
    import sys
    raise SystemExit(main())
