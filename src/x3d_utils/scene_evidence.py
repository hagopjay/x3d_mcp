"""scene_evidence adapter: claims about a scene's values, checked against the sources they came from.

It follows the `claims` / `scene_brief` split:
this module is pure functions over the vendored engine (`x3d_utils/x3dtrace.py`, stdlib only, no model call), and
`tools/scene_evidence.py` is the thin MCP wrapper.

What it adds to the engine, for a model-facing tool:
  * BIND: a claim's `field` is resolved to an attribute of the scene (DEF@attr, Tag[DEF].attr, n<k>@attr, or the
    inventory path), and the claimed value is compared with what the scene holds. The engine's FIND and COMPARE tell
    you that the quote supports the value; BIND tells you that the value is the one in the scene.
  * A closed output: like scene_brief, nothing a document or a claim wrote is echoed unless text="quoted". Items carry
    verdict words, offsets, handles `n<k>` (the same 1-based document-order index as scene_brief) and vetted
    X3DUOM names; a quote or value appears only as a length and an 8-hex digest.
  * Coverage over the scene's own value-bearing attributes (`expect="inventory"`).
"""

from __future__ import annotations

import bisect
import hashlib
import json
import re

from x3d_utils import x3dtrace as xt
from x3d_utils.scene_brief import _canon, _safe_field, _safe_node

SCHEMA = "x3d-scene-evidence/1"
MAX_CLAIMS = 2000
MAX_QUOTE = 2000
MAX_SOURCES = 16
MAX_SOURCE_CHARS = 8_000_000
TEXT_MODES = ("counts", "off", "quoted")
SAFE_FIELD = re.compile(r"^[A-Za-z0-9_.@:/\[\]=\-]{1,160}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9_.\-]{1,48}$")
_POLICY_KEYS = {"number_tolerance": float, "round_to_printed": bool, "match_reformatted_identifiers": bool,
                "reworded_is_problem": bool, "min_quote_words": int, "ambiguous_quote_is_check": bool,
                "moved_is_check": bool, "code_values_unchecked": bool, "length_scale": float}


class EvidenceError(ValueError):
    """The input cannot be checked. `code` is a fixed word; never document text."""

    def __init__(self, code: str, **extra):
        super().__init__(code)
        self.code = code
        self.extra = extra


def _sha8(s) -> str:
    return hashlib.sha256(str(s).encode("utf-8", "surrogatepass")).hexdigest()[:8]


# --- inputs -------------------------------------------------------------------------------------------------
def parse_policy(spec: str) -> xt.TracePolicy:
    spec = (spec or "default").strip()
    if spec in ("", "default"):
        return xt.TracePolicy()
    if spec == "strict":
        return xt.TracePolicy.strict()
    try:
        raw = json.loads(spec)
    except json.JSONDecodeError:
        raise EvidenceError("bad-policy")
    if not isinstance(raw, dict):
        raise EvidenceError("bad-policy")
    kw = {}
    for k, v in raw.items():
        if k == "conversions":
            if not isinstance(v, list) or any(c not in xt.ALL_CONVERSIONS for c in v):
                raise EvidenceError("bad-policy", allowed=list(xt.ALL_CONVERSIONS))
            kw[k] = tuple(v)
        elif k in _POLICY_KEYS:
            want = _POLICY_KEYS[k]
            if isinstance(v, bool) != (want is bool) or not isinstance(v, (int, float, bool)):
                raise EvidenceError("bad-policy", key=k if SAFE_ID.match(k) else "?")
            kw[k] = want(v)
        else:
            raise EvidenceError("bad-policy", allowed=sorted(_POLICY_KEYS) + ["conversions"])
    return xt.TracePolicy(**kw)


def parse_claims(text: str) -> list[dict]:
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        raise EvidenceError("claims-not-json")
    if isinstance(raw, dict) and isinstance(raw.get("claims"), list):
        raw = raw["claims"]
    if not isinstance(raw, list):
        raise EvidenceError("claims-not-a-list")
    if len(raw) > MAX_CLAIMS:
        raise EvidenceError("too-many-claims", max=MAX_CLAIMS)
    known = set(xt.Claim.__dataclass_fields__) | {"id"}
    out = []
    for i, c in enumerate(raw):
        if not isinstance(c, dict) or not isinstance(c.get("field"), str) or "value" not in c:
            raise EvidenceError("claim-needs-field-and-value", index=i)
        c = {k: v for k, v in c.items() if k in known}
        c["quote"] = c.get("quote") if isinstance(c.get("quote"), str) else ""
        if len(c["quote"]) > MAX_QUOTE:
            raise EvidenceError("quote-too-long", index=i, max=MAX_QUOTE)
        out.append(c)
    return out


def build_sources(items: list[dict], scene_text: str) -> list[xt.Source]:
    """items: [{id, text, kind?}]; the scene is always present as source `scene` (kind x3d) unless a source has that id."""
    if len(items) > MAX_SOURCES:
        raise EvidenceError("too-many-sources", max=MAX_SOURCES)
    out, seen = [], set()
    for i, it in enumerate(items):
        sid, txt = it.get("id"), it.get("text")
        if not isinstance(sid, str) or not SAFE_ID.match(sid) or sid in seen or not isinstance(txt, str):
            raise EvidenceError("bad-source", index=i)
        if len(txt) > MAX_SOURCE_CHARS:
            raise EvidenceError("source-too-large", index=i, max_chars=MAX_SOURCE_CHARS)
        kind = it.get("kind", "auto")
        if kind not in ("auto", "text", "x3d"):
            raise EvidenceError("bad-source", index=i)
        seen.add(sid)
        out.append(xt.Source(sid, txt, kind))
    if "scene" not in seen:
        out.append(xt.Source("scene", scene_text, "x3d"))
    return out


# --- BIND: claim field -> scene attribute -----------------------------------------------------------------
_HANDLE = re.compile(r"^n([1-9][0-9]{0,8})$")
_TAGDEF = re.compile(r"^([A-Za-z_][\w]*)\[([^\]]{1,64})\]$")


class _Maps:
    """Per-scene lookup tables, built once: the claims of one call all resolve against the same index."""

    def __init__(self, sc):
        self.root = next((e.idx for e in sc.elements if e.tag == "Scene"), -1)
        self.by_path: dict = {}
        self.by_xpath: dict = {}
        for e in sc.elements:
            self.by_path.setdefault(e.path, e)          # first element wins, as a linear scan would
            self.by_xpath.setdefault(sc.xpath(e), e)
        self._nl = None

    def line(self, sc, pos: int) -> int:
        if self._nl is None:
            self._nl = [i for i, ch in enumerate(sc.text) if ch == "\n"]
        return bisect.bisect_left(self._nl, pos) + 1


def _maps(sc) -> _Maps:
    m = getattr(sc, "_ev_maps", None)
    if m is None:
        m = sc._ev_maps = _Maps(sc)
    return m


def _scene_root(sc) -> int:
    return _maps(sc).root


def handle_of(sc, e) -> str | None:
    root = _scene_root(sc)
    if root < 0 or e.idx < root:
        return None
    r = sc.elements[root]
    return f"n{e.idx - root + 1}" if e.start >= r.start and (r.end < 0 or e.start < r.end) else None


def resolve_field(sc, field: str):
    """(element, attr) for a claim's `field`, or None. Forms: <inventory path>@attr, DEF@attr, DEF.attr,
    Tag[DEF].attr, n<k>@attr."""
    f = (field or "").strip()
    if f.startswith("/") and "/@" in f:
        # the engine's own inventory form: an XPath 1.0 to the attribute, DEF-predicated where a DEF exists
        ref, attr = f.rsplit("/@", 1)
        el = _maps(sc).by_xpath.get(ref)
        if el is None or attr not in el.attrs:
            return (el, None) if el is not None else None
        return el, attr
    if "@" in f:
        ref, attr = f.rsplit("@", 1)
    elif "." in f:
        ref, attr = f.rsplit(".", 1)
    else:
        return None
    if not ref or not attr:
        return None
    el = None
    m = _HANDLE.match(ref)
    if m:
        root = _scene_root(sc)
        k = int(m.group(1))
        if root >= 0 and root + k - 1 < len(sc.elements):
            el = sc.elements[root + k - 1]
    if el is None:
        el = _maps(sc).by_path.get(ref)
    if el is None:
        t = _TAGDEF.match(ref)
        d = t.group(2) if t else ref
        el = sc.find_def(d)
        if el is not None and t and el.tag != t.group(1):
            el = None
    if el is None or attr not in el.attrs:
        return (el, None) if el is not None else None
    return el, attr


def canonical_field(sc, field: str):
    r = resolve_field(sc, field)
    if r is None or r[1] is None:
        return None
    return f"{sc.xpath(r[0])}/@{r[1]}"       # the same string xt.inventory() gives as `field`


def _num_list(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return [float(v)]
    if isinstance(v, list):
        out = []
        for x in v:
            if isinstance(x, bool) or not isinstance(x, (int, float)):
                return None
            out.append(float(x))
        return out
    if isinstance(v, str):
        try:
            return [float(x) for x in v.replace(",", " ").split()]
        except ValueError:
            return None
    return None


def bind_one(sc, claim: dict, tol: float) -> dict:
    """{state, handle?, line?, attr?}: state is match | mismatch | no-field | no-attribute | not-checked."""
    r = resolve_field(sc, claim.get("field", ""))
    if r is None:
        return {"state": "no-field"}
    el, attr = r
    if attr is None:
        return {"state": "no-attribute", "handle": handle_of(sc, el)}
    raw = el.attrs[attr][0]
    out = {"handle": handle_of(sc, el), "attr": _safe_field(el.tag, attr), "node": _safe_node(el.tag),
           "line": _maps(sc).line(sc, el.start)}
    a, b = _num_list(claim["value"]), _num_list(raw)
    if a is not None and b is not None:
        same = len(a) == len(b) and all(abs(x - y) < tol for x, y in zip(a, b))
    else:
        v = claim["value"]
        same = str(v).strip() == raw.strip() if isinstance(v, (str, int, float)) and not isinstance(v, bool) else False
        if isinstance(v, bool):
            same = str(v).lower() == raw.strip().lower()
    out["state"] = "match" if same else "mismatch"
    return out


# --- the check -------------------------------------------------------------------------------------------
def scene_evidence(scene_text: str, claims: list[dict], source_items: list[dict] | None = None, *,
                   policy: xt.TracePolicy | None = None, expect: str = "", bind: bool = True,
                   text_mode: str = "counts", text_cap: int = 80, max_items: int = 200,
                   expect_report_sha256: str = "") -> dict:
    if text_mode not in TEXT_MODES:
        raise EvidenceError("bad-text-mode")
    pol = policy or xt.TracePolicy()
    sc = xt.SceneIndex(scene_text)
    if not sc.ok:
        raise EvidenceError("scene-not-well-formed")
    sources = build_sources(source_items or [], scene_text)
    scene_src = next(s for s in sources if s.id == "scene")
    # every claim needs a stable id for the output; the caller's own id is used only when it is identifier-shaped
    expected = None
    if expect == "inventory":
        expected = [r["field"] for r in xt.inventory(scene_text)]
    elif expect.strip():
        expected = []
        for f in (x.strip() for x in expect.split(",")):
            c = canonical_field(sc, f)
            if c:
                expected.append(c)
    rep = xt.check_claims(claims, sources, pol, None)
    items, tol = [], pol.number_tolerance
    cited, verified = set(), set()
    for i, (it, raw) in enumerate(zip(rep.items, claims)):
        b = bind_one(sc, raw, tol) if bind else {"state": "not-checked"}
        canon = canonical_field(sc, raw["field"])
        if canon and it.status != "no_quote":
            cited.add(canon)
            if it.verdict == "verified":
                verified.add(canon)
        o = {"i": i, "verdict": it.verdict, "status": it.status, "value_status": it.value_status,
             "grade_cap": xt.grade_cap(it.verdict, it.status != "no_quote"), "bind": b,
             "quote": {"chars": len(it.quote or ""), "sha256_8": _sha8(it.quote or "")}}
        cid = raw.get("id")
        o["id"] = cid if isinstance(cid, str) and SAFE_ID.match(cid) else None
        o["field"] = raw["field"] if SAFE_FIELD.match(raw["field"]) else "?"
        o["field_sha256_8"] = _sha8(raw["field"])
        if it.source and SAFE_ID.match(it.source):
            o["source"] = it.source
        if it.span:
            o["span"] = list(it.span)
            lc = it.locus or {}
            o["at"] = {k: lc[k] for k in ("line", "col") if k in lc}
            if lc.get("node"):
                o["at"]["node"] = _safe_node(lc["node"])
            if lc.get("attr"):
                o["at"]["attr"] = _safe_field(lc.get("node") or "", lc["attr"])
            if it.source == "scene":
                e = sc.element_at(it.span[0])
                if e is not None:
                    o["at"]["handle"] = handle_of(sc, e)
        if it.occurrences:
            o["occurrences"] = it.occurrences
        if it.derivation:
            o["derivation"] = [d.split(":")[0] for d in it.derivation][:8]
        if it.disambiguated_by:
            o["disambiguated_by"] = it.disambiguated_by
        items.append(o)
    summary = rep.summary()
    summary.pop("coverage", None)
    summary.pop("verified_coverage", None)
    summary["bound"] = sum(1 for o in items if o["bind"]["state"] == "match")
    summary["unbound"] = sum(1 for o in items if o["bind"]["state"] in ("mismatch", "no-field", "no-attribute"))
    if expected:
        ex = set(expected)
        summary["coverage"] = round(len(cited & ex) / len(ex), 6)
        summary["verified_coverage"] = round(len(verified & ex) / len(ex), 6)
        summary["expected"] = len(ex)
    full_sha = rep.sha256()
    body = {
        "schema": SCHEMA, "ok": True, "engine": xt.SCHEMA, "tier": "oracle",
        "scene": {"sha256": hashlib.sha256(scene_text.encode("utf-8", "surrogatepass")).hexdigest()[:16],
                  "bytes": len(scene_text.encode("utf-8", "surrogatepass"))},
        "sources": [{"id": s["id"], "kind": s["kind"], "sha256": s["sha256"][:16], "bytes": s["bytes"]} for s in rep.sources],
        "summary": summary, "policy": pol.to_dict(),
        "claims_sha256": hashlib.sha256(_canon(claims).encode()).hexdigest(),
        "report_sha256": full_sha,
        "items_total": len(items), "items": items[:max_items],
    }
    if len(items) > max_items:
        body["items_truncated"] = True
    if expect_report_sha256:
        body["report_matches"] = expect_report_sha256.strip().lower() == full_sha
    if text_mode == "quoted":
        q = []
        for o, it in list(zip(items, rep.items))[:max_items]:
            if it.quote:
                q.append({"i": o["i"], "quote": it.quote[:text_cap], "cut": len(it.quote) > text_cap})
        body["untrusted"] = {"tier": "untrusted", "quoted": q}
    return body


def render(body: dict) -> str:
    return _canon(body)


def card(body: dict) -> dict:
    s = body["summary"]
    keep = {k: s[k] for k in ("verified", "check", "reworded", "unchecked", "total", "bound", "unbound") if k in s}
    for k in ("coverage", "verified_coverage"):
        if k in s:
            keep[k] = s[k]
    needs = [{"i": o["i"], "verdict": o["verdict"], "bind": o["bind"]["state"]} for o in body["items"]
             if o["verdict"] != "verified" or o["bind"]["state"] not in ("match", "not-checked")][:20]
    return {"schema": SCHEMA, "ok": True, "scene": body["scene"], "summary": keep, "needs_attention": needs,
            "report_sha256": body["report_sha256"], "claims_sha256": body["claims_sha256"]}
