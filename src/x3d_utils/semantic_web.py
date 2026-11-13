"""Scene graphs and the work behind the semantic-web tools (scene_to_rdf, sparql_scene, shacl_validate,
rdf_infer, ontology_vs_gate). The MCP tools in tools/semantic_web.py are thin wrappers over this module.

A scene graph is the RDF form of one X3D document, made by x3d_utils.scene_rdf (the X3dToTurtle.xslt
conventions, namespace X3dOntology4.1#). The ontology joined to it is chosen by the document's X3D version
(ontology_version_for): the 4.1 release for 4.1 documents, 4.0 for 4.0 and 3.x documents. The 4.0 file
is read into the 4.1 namespace here (its IRIs rewritten), so one set of prefixes serves every scene.

Every answer carries `instrument`: the x3d_mcp commit, the sha256 of the ontology, shapes, X3DUOM and
converter tables, and the rdflib / pyshacl / owlrl versions, so a verdict can be traced to what made it.
"""

import hashlib
import json
import os
import platform
import re
import statistics
import subprocess
from collections import Counter, OrderedDict
from functools import lru_cache
from pathlib import Path

import rdflib
from rdflib import BNode, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from x3d_utils import scene_rdf
from x3d_utils.ontology import (
    ONTOLOGIES, X3D_41_NS, _alias_x3d_namespaces, check_ontology_version, get_ontology, ontology_sha256,
    ontology_version_for,
)
from x3d_utils.ontology_guard import QueryRefused, guard_sparql, max_rows, run_bounded

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "validation" / "schemas"
SHAPES_DIR = SCHEMAS_DIR / "shapes"
SHAPES = {"x3d-core": SHAPES_DIR / "x3d-core.shacl.ttl"}
X3DUOM_PATH = SCHEMAS_DIR / "X3dUnifiedObjectModel-4.1.xml"
CONTAINMENT_PATH = SCHEMAS_DIR / "verified-containment.json"
REPO_DIR = Path(__file__).resolve().parents[2]

X3DO = rdflib.Namespace(X3D_41_NS)
SH = rdflib.Namespace("http://www.w3.org/ns/shacl#")
XS = rdflib.Namespace("urn:x3d-mcp:shapes:x3d-core#")

MAX_TRIPLES_ENV = "X3D_MCP_SCENE_MAX_TRIPLES"
DEFAULT_MAX_TRIPLES = 300_000
MAX_SHAPES_CHARS = 256_000
INFER_PROFILES = ("subclass", "rdfs", "owl-rl")
SHACL_INFERENCE = ("none", "rdfs", "owlrl", "both")

COMPACT = (
    ("x3do:", X3D_41_NS),
    ("rdf:", str(RDF)),
    ("rdfs:", str(RDFS)),
    ("owl:", str(OWL)),
    ("xsd:", "http://www.w3.org/2001/XMLSchema#"),
    ("sh:", str(SH)),
    ("xs:", str(XS)),
    ("dcterms:", "http://purl.org/dc/terms/"),
)


class SceneError(ValueError):
    """The scene cannot be converted or is over a limit (message says which)."""


# ------------------------------------------------------------------------------------------- instrument

def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@lru_cache(maxsize=None)
def _file_sha(path: str) -> str:
    return _sha(Path(path))


@lru_cache(maxsize=1)
def _commit() -> dict:
    """The x3d_mcp checkout this server runs from; X3D_MCP_COMMIT overrides it where there is no .git."""
    pinned = os.environ.get("X3D_MCP_COMMIT", "").strip()
    if pinned:
        return {"x3d_mcp_commit": pinned, "x3d_mcp_dirty": None}
    try:
        sha = subprocess.run(["git", "-C", str(REPO_DIR), "rev-parse", "HEAD"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        status = subprocess.run(["git", "-C", str(REPO_DIR), "status", "--porcelain", "--untracked-files=no"],
                                capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return {"x3d_mcp_commit": "unknown", "x3d_mcp_dirty": None}
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        return {"x3d_mcp_commit": "unknown", "x3d_mcp_dirty": None}
    return {"x3d_mcp_commit": sha, "x3d_mcp_dirty": bool(status)}


def _version(module: str):
    try:
        from importlib.metadata import version
        return version(module)
    except Exception:                       # not installed
        return None


def instrument(ontology_version: str | None = None, shapes: str = "x3d-core") -> dict:
    """What produced an answer: commit, file hashes and library versions."""
    ov = check_ontology_version(ontology_version or "4.1")
    tables = scene_rdf.get_rules().sources
    out = dict(_commit())
    out.update({
        "ontology": {"version": ov, "file": ONTOLOGIES[ov][0].name, "sha256": ontology_sha256(ov)},
        "shapes": {"name": shapes, "sha256": _file_sha(str(SHAPES[shapes]))} if shapes in SHAPES else None,
        "x3duom_sha256": _file_sha(str(X3DUOM_PATH)),
        "converter": {"stylesheet": "X3dToTurtle.xslt r40388",
                      "stylesheet_sha256": tables["X3dToTurtle.xslt"]["sha256"],
                      "tables_sha256": scene_rdf.tables_sha256()},
        "rdflib": rdflib.__version__,
        "pyshacl": _version("pyshacl"),
        "owlrl": _version("owlrl"),
        "python": platform.python_version(),
    })
    return out


# ------------------------------------------------------------------------------------------- ontology graphs

_SCENE_ONTOLOGY: dict = {}
_SLICES: dict = {}


def scene_ontology(version: str) -> rdflib.Graph:
    """The bundled ontology `version` in the X3dOntology4.1# namespace (the 4.0 file is rewritten)."""
    version = check_ontology_version(version)
    if version not in _SCENE_ONTOLOGY:
        graph = get_ontology(version).graph
        if ONTOLOGIES[version][1] != X3D_41_NS:
            graph = _alias_x3d_namespaces(graph, X3D_41_NS, (ONTOLOGIES[version][1],))
        _SCENE_ONTOLOGY[version] = graph
    return _SCENE_ONTOLOGY[version]


def ontology_slice(version: str) -> rdflib.Graph:
    """The part of the ontology the shapes read: rdfs:subClassOf between named classes, and the
    sub-properties of hasChild with their rdfs:domain values."""
    version = check_ontology_version(version)
    if version not in _SLICES:
        onto = scene_ontology(version)
        g = rdflib.Graph()
        for s, o in onto.subject_objects(RDFS.subClassOf):
            if isinstance(s, URIRef) and isinstance(o, URIRef):
                g.add((s, RDFS.subClassOf, o))
        for p in onto.transitive_subjects(RDFS.subPropertyOf, X3DO.hasChild):
            if p == X3DO.hasChild:
                continue
            for parent in onto.objects(p, RDFS.subPropertyOf):
                g.add((p, RDFS.subPropertyOf, parent))
            for d in onto.objects(p, RDFS.domain):
                if isinstance(d, URIRef):
                    g.add((p, RDFS.domain, d))
        _SLICES[version] = g
    return _SLICES[version]


@lru_cache(maxsize=4)
def _superclasses(version: str) -> dict:
    """class -> every named superclass (rdfs:subClassOf+), from the ontology `version`."""
    onto = scene_ontology(version)
    direct: dict = {}
    for s, o in onto.subject_objects(RDFS.subClassOf):
        if isinstance(s, URIRef) and isinstance(o, URIRef) and s != o:
            direct.setdefault(s, set()).add(o)
    closed: dict = {}

    def up(c, stack=()):
        if c in closed:
            return closed[c]
        out = set()
        for d in direct.get(c, ()):
            if d in stack:
                continue
            out.add(d)
            out |= up(d, stack + (c,))
        closed[c] = out
        return out

    for c in list(direct):
        up(c)
    return closed


# ------------------------------------------------------------------------------------------- scene graphs

def max_triples() -> int:
    raw = os.environ.get(MAX_TRIPLES_ENV, "").strip()
    try:
        v = int(raw) if raw else DEFAULT_MAX_TRIPLES
    except ValueError:
        v = DEFAULT_MAX_TRIPLES
    return v if v > 0 else DEFAULT_MAX_TRIPLES


class SceneGraph:
    """One converted scene, as kept in a session's graph cache."""

    def __init__(self, res: scene_rdf.SceneRDF, source_sha256: str):
        self.graph = res.graph
        self.turtle = res.turtle
        self.sha256 = res.sha256
        self.graph_id = res.sha256[:16]
        self.base = res.base
        self.x3d_version = res.version
        self.dtd = res.dtd
        self.ontology_version = ontology_version_for(res.version)
        self.warnings = list(res.warnings)
        self.triples = res.triples
        self.individuals = res.individuals
        self.convert_ms = round(res.elapsed_ms, 1)
        self.source_sha256 = source_sha256

    def describe(self) -> dict:
        return {"graph_id": self.graph_id, "graph_sha256": self.sha256, "triples": self.triples,
                "individuals": self.individuals, "base": self.base, "x3d_version": self.x3d_version or None,
                "dtd_defaults": self.dtd, "ontology_version": self.ontology_version}


def convert(text: str, base_iri: str = "") -> SceneGraph:
    """X3D text to a SceneGraph. Raises SceneError for a document that is not X3D or is over the cap."""
    try:
        res = scene_rdf.scene_to_rdf(text, base_iri)
    except ValueError as e:
        raise SceneError(str(e)) from None
    cap = max_triples()
    if res.triples > cap:
        raise SceneError(f"the scene gives {res.triples} triples; the limit is {cap} ({MAX_TRIPLES_ENV})")
    return SceneGraph(res, hashlib.sha256(text.encode("utf-8")).hexdigest())


class GraphCache:
    """Scene graphs by graph_id, most recent last; the oldest is dropped past `keep`."""

    def __init__(self, keep: int = 8):
        self.keep = keep
        self._d: "OrderedDict[str, SceneGraph]" = OrderedDict()

    def put(self, sg: SceneGraph) -> SceneGraph:
        self._d[sg.graph_id] = sg
        self._d.move_to_end(sg.graph_id)
        while len(self._d) > self.keep:
            self._d.popitem(last=False)
        return sg

    def get(self, graph_id: str) -> SceneGraph | None:
        key = (graph_id or "").strip()
        if key.startswith("sha256:"):
            key = key[7:]
        sg = self._d.get(key[:16]) if len(key) >= 16 else None
        if sg is not None:
            self._d.move_to_end(sg.graph_id)
        return sg

    def __len__(self):
        return len(self._d)


# ------------------------------------------------------------------------------------------- rendering

def compact(term, base: str = "") -> str:
    """A term as a short string: :Name for scene individuals, x3do:Box for ontology terms, the lexical
    form for literals, _:blank for blank nodes."""
    if term is None:
        return ""
    if isinstance(term, BNode):
        return "_:blank"
    if isinstance(term, Literal):
        return str(term)
    text = str(term)
    if base and text.startswith(base):
        return ":" + text[len(base):]
    for prefix, ns in COMPACT:
        if text.startswith(ns):
            return prefix + text[len(ns):]
    return text


_X3D_PNAME = re.compile(r"(?<![\w.-])x3d:")
_X3DO_PNAME = re.compile(r"(?<![\w.-])x3do:")


def scene_prefixes(base: str, query: str = "") -> str:
    """The PREFIX block a scene query gets: `:` is the scene's base, `x3do:` the 4.1 ontology namespace
    (the stylesheet's prefix). rdflib keeps one prefix per namespace, so `x3d:` (the query_ontology habit)
    is declared for the same namespace instead of `x3do:` when the query uses only `x3d:`."""
    lines = [f"PREFIX : <{base}>"] if base else []
    alias = bool(_X3D_PNAME.search(query)) and not _X3DO_PNAME.search(query)
    for p, ns in COMPACT:
        if p in ("sh:", "xs:"):
            continue
        lines.append(f"PREFIX {'x3d:' if alias and p == 'x3do:' else p} <{ns}>")
    return "\n".join(lines) + "\n"


def tbox_slice(sg: SceneGraph, version: str) -> tuple[str, int, int]:
    """Turtle for the classes the scene uses and their superclasses: (text, classes, triples)."""
    sup = _superclasses(version)
    used = {o for o in sg.graph.objects(None, RDF.type) if isinstance(o, URIRef) and str(o).startswith(X3D_41_NS)}
    classes = set(used)
    for c in used:
        classes |= sup.get(c, set())
    onto = scene_ontology(version)
    lines, n = [f"# X3D Ontology {version} classes used by this scene and their superclasses"], 0
    for c in sorted(classes, key=str):
        parents = sorted((o for o in onto.objects(c, RDFS.subClassOf) if isinstance(o, URIRef)), key=str)
        head = f"x3do:{str(c)[len(X3D_41_NS):]} a owl:Class"
        n += 1
        if parents:
            head += " ;\n  rdfs:subClassOf " + ", ".join(compact(p) for p in parents)
            n += len(parents)
        lines.append(head + " .")
    return "\n".join(lines) + "\n", len(classes), n


# ------------------------------------------------------------------------------------------- SPARQL

def _run_select(graph, text: str, base: str, cap: int):
    result = graph.query(text)
    if result.type == "ASK":
        return 1, [{"ask": bool(result.askAnswer)}]
    rows = []
    for binding in result:
        rows.append({str(v): compact(binding[v], base) for v in result.vars})
        if len(rows) > cap:
            break
    return len(rows), rows


def _row_key(row: dict) -> str:
    return json.dumps(row, sort_keys=True, ensure_ascii=False)


def sparql(sg: SceneGraph, query: str, include_ontology: bool = True, ontology_version: str = "") -> dict:
    """Guard, then run a SELECT or ASK over the scene (and the ontology), with the server's deadline and
    row cap. Rows are sorted so pages are stable. Raises QueryRefused / QueryTimeout / RuntimeError."""
    prefixes = scene_prefixes(sg.base, query)
    text = prefixes + query
    guard_sparql(text)
    ov = check_ontology_version(ontology_version or sg.ontology_version)
    graph = sg.graph
    if include_ontology:
        graph = rdflib.graph.ReadOnlyGraphAggregate([sg.graph, scene_ontology(ov)])
    cap = max_rows()
    total, rows = run_bounded(_run_select, graph, text, sg.base, cap)
    truncated = total > cap
    rows = sorted(rows[:cap], key=_row_key)
    return {"rows": rows, "total": min(total, cap), "truncated": truncated,
            "ontology_version": ov if include_ontology else None}


# ------------------------------------------------------------------------------------------- SHACL

@lru_cache(maxsize=4)
def _bundled_shapes(name: str) -> rdflib.Graph:
    g = rdflib.Graph()
    g.parse(str(SHAPES[name]), format="turtle")
    return g


def _declared_prefixes(g: rdflib.Graph) -> str:
    lines = []
    for decl in g.objects(None, SH.declare):
        p, ns = g.value(decl, SH.prefix), g.value(decl, SH.namespace)
        if p is not None and ns is not None:
            lines.append(f"PREFIX {p}: <{ns}>")
    return "\n".join(sorted(set(lines))) + "\n"


def shapes_graph(shapes: str = "x3d-core", extra_shapes_ttl: str = "") -> rdflib.Graph:
    """The shapes to validate with. Raises SceneError for an unknown name, an oversize or unparsable
    extra shapes text, or a SPARQL constraint that is not a guarded SELECT/ASK."""
    g = rdflib.Graph()
    names = [s.strip() for s in (shapes or "").split(",") if s.strip()]
    for name in names:
        if name in ("", "none"):
            continue
        if name not in SHAPES:
            raise SceneError(f"unknown shapes {name!r}; bundled: {sorted(SHAPES)} (or 'none' with extra_shapes_ttl)")
        g += _bundled_shapes(name)
    if extra_shapes_ttl and extra_shapes_ttl.strip():
        if len(extra_shapes_ttl) > MAX_SHAPES_CHARS:
            raise SceneError(f"extra_shapes_ttl is {len(extra_shapes_ttl)} characters; the limit is {MAX_SHAPES_CHARS}")
        extra = rdflib.Graph()
        try:
            extra.parse(data=extra_shapes_ttl, format="turtle")
        except Exception as e:                  # rdflib raises several syntax error types
            raise SceneError(f"extra_shapes_ttl is not Turtle: {str(e)[:300]}") from None
        if (None, OWL.imports, None) in extra:
            raise SceneError("owl:imports is not allowed in extra_shapes_ttl (the server fetches nothing)")
        for pred in (SH.construct, SH.update):
            if (None, pred, None) in extra:
                raise SceneError("SHACL rules (sh:construct) and updates are not run; use sh:select or sh:ask")
        g += extra
    if not len(g):
        raise SceneError("no shapes: name bundled shapes or pass extra_shapes_ttl")
    prefixes = _declared_prefixes(g)
    for pred in (SH.select, SH.ask):
        for q in g.objects(None, pred):
            try:
                guard_sparql(prefixes + str(q))
            except QueryRefused as e:
                raise SceneError(f"shape SPARQL refused: {e}") from None
    return g


def _shape_note(shapes: rdflib.Graph, node, prop):
    if node is None:
        return None
    v = shapes.value(node, prop)
    if v is not None:
        return str(v)
    for owner in shapes.subjects(SH.property, node):     # an anonymous property shape: ask its node shape
        v = shapes.value(owner, prop)
        if v is not None:
            return str(v)
    return None


def _render_path(shapes: rdflib.Graph, path) -> str:
    if path is None:
        return ""
    if isinstance(path, URIRef):
        return compact(path)
    if (path, RDF.first, None) in shapes:
        return "/".join(_render_path(shapes, p) for p in rdflib.collection.Collection(shapes, path))
    for key, suffix in ((SH.zeroOrMorePath, "*"), (SH.oneOrMorePath, "+"), (SH.zeroOrOnePath, "?")):
        inner = shapes.value(path, key)
        if inner is not None:
            return _render_path(shapes, inner) + suffix
    inner = shapes.value(path, SH.inversePath)
    if inner is not None:
        return "^" + _render_path(shapes, inner)
    return "_:path"


def _validate(data: rdflib.Graph, shapes: rdflib.Graph, inference: str, ont, base: str) -> dict:
    """Runs in the bounded worker: pyshacl, then the results as plain dicts."""
    from pyshacl import validate
    conforms, report, text = validate(data, shacl_graph=shapes, ont_graph=ont, inference=inference,
                                      allow_warnings=True, allow_infos=True, advanced=False,
                                      do_owl_imports=False, meta_shacl=False, js=False)
    if not isinstance(report, rdflib.Graph):           # pyshacl reports a shapes error this way
        raise ValueError(f"the shapes could not be used: {str(text or report)[:400]}")
    results = []
    names = {"Violation": "Violation", "Warning": "Warning", "Info": "Info"}
    for r in report.subjects(RDF.type, SH.ValidationResult):
        focus = report.value(r, SH.focusNode)
        source = report.value(r, SH.sourceShape)
        constraint = report.value(r, SH.sourceConstraint)
        sev = report.value(r, SH.resultSeverity)
        sev = names.get(str(sev).rpartition("#")[2], str(sev))
        node_def = data.value(focus, X3DO.DEF) if focus is not None else None
        if node_def is None and focus is not None:
            target = data.value(focus, OWL.sameAs)
            if target is not None:
                node_def = data.value(target, X3DO.DEF) or compact(target, base)
        types = sorted(compact(t) for t in data.objects(focus, RDF.type)
                       if isinstance(t, URIRef) and str(t).startswith(X3D_41_NS)) if focus is not None else []
        shape_name = compact(source) if not isinstance(source, BNode) else next(
            (compact(o) for o in shapes.subjects(SH.property, source)), "_:blank")
        check = _shape_note(shapes, constraint, XS.checkId) or _shape_note(shapes, source, XS.checkId)
        hint = _shape_note(shapes, constraint, XS.fixHint) or _shape_note(shapes, source, XS.fixHint)
        value = report.value(r, SH.value)
        results.append({
            "focus": compact(focus, base),
            "def": str(node_def) if node_def is not None else None,
            "node_type": types[0] if types else None,
            "path": _render_path(shapes, report.value(r, SH.resultPath)),
            "value": compact(value, base) if value is not None else None,
            "severity": sev,
            "message": str(report.value(r, SH.resultMessage) or ""),
            "shape": shape_name,
            "check_id": check or shape_name.rpartition(":")[2],
            "fix_hint": hint,
        })
    results.sort(key=lambda x: (x["check_id"], x["focus"], x["path"], x["message"], x["value"] or ""))
    return {"conforms": bool(conforms), "results": results}


SHACL_ONTOLOGY = "4.1"


def shacl(sg: SceneGraph, shapes: str = "x3d-core", extra_shapes_ttl: str = "", inference: str = "none",
          ontology_version: str = SHACL_ONTOLOGY, timeout: float | None = None) -> dict:
    """Validate a scene graph. The data graph is the scene plus the ontology slice the shapes read;
    with inference other than 'none' pyshacl also gets the whole ontology as ont_graph and expands it.

    The x3d-core shapes are written against the 4.1 ontology for every document version: the bundled
    4.0 file lists fewer rdfs:domain classes for several fields (hasChildren, hasGeometry, hasCoord), so
    S6 read against it flags fields that X3D 3.3 and 4.0 scenes use legally."""
    if inference not in SHACL_INFERENCE:
        raise SceneError(f"inference must be one of {SHACL_INFERENCE}")
    ov = check_ontology_version(ontology_version or SHACL_ONTOLOGY)
    sgraph = shapes_graph(shapes, extra_shapes_ttl)
    data = rdflib.Graph()
    data += sg.graph
    data += ontology_slice(ov)
    ont = scene_ontology(ov) if inference != "none" else None
    import pyshacl  # noqa: F401  (imported before the worker forks, so it is loaded once)
    out = run_bounded(_validate, data, sgraph, inference, ont, sg.base, timeout=timeout)
    counts = Counter(r["severity"] for r in out["results"])
    out["counts"] = {"Violation": counts.get("Violation", 0), "Warning": counts.get("Warning", 0),
                     "Info": counts.get("Info", 0)}
    out["by_check"] = dict(sorted(Counter(r["check_id"] for r in out["results"]).items()))
    out["ontology_version"] = ov
    out["warnings"] = []
    if inference != "none":
        out["warnings"].append(
            f"inference={inference} expands the scene with the whole X3D Ontology {ov} before validating. "
            "RDFS reads several rdfs:domain or rdfs:range values of one property as all applying at once, "
            "so nodes gain types they do not have in X3D; results with inference can include findings that "
            "come from those added types and can hide real ones. inference='none' is the default for that reason.")
    return out


# ------------------------------------------------------------------------------------------- inference

def _types_per_node(g: rdflib.Graph, base: str) -> dict:
    out = {}
    for s in set(g.subjects(RDF.type, OWL.NamedIndividual)):
        if isinstance(s, URIRef) and str(s).startswith(base):
            out[s] = {o for o in g.objects(s, RDF.type) if isinstance(o, URIRef)}
    return out


def _domain_report(version: str) -> dict:
    onto = scene_ontology(version)
    counts = Counter()
    for p, d in onto.subject_objects(RDFS.domain):
        counts[p] += 1
    multi = sum(1 for n in counts.values() if n > 1)
    return {"properties_with_domain": len(counts), "properties_with_several_domains": multi,
            "domains_of_DEF": counts.get(X3DO.DEF, 0)}


def _closure(scene: rdflib.Graph, onto: rdflib.Graph, profile: str, base: str):
    """Runs in the bounded worker: owlrl closure of scene + ontology, reported per node."""
    import owlrl
    g = rdflib.Graph()
    g += onto
    g += scene
    before_types = _types_per_node(g, base)
    before = len(g)
    semantics = owlrl.RDFS_Semantics if profile == "rdfs" else owlrl.OWLRL_Semantics
    owlrl.DeductiveClosure(semantics).expand(g)
    after_types = _types_per_node(g, base)
    added = []
    for s, before_set in before_types.items():
        for t in sorted(after_types.get(s, set()) - before_set, key=str):
            added.append((compact(s, base), "rdf:type", compact(t)))
    return {"before": before, "after": len(g),
            "types": {compact(s, base): (len(before_types[s]), len(after_types.get(s, ())))
                      for s in before_types},
            "type_triples": added}


def infer(sg: SceneGraph, profile: str = "subclass", budget: float = 60.0, ontology_version: str = "",
          sample: int = 20) -> dict:
    """Add inferred rdf:type triples and report what changed per node.

    subclass: each node's x3do class gains its superclasses (rdfs:subClassOf+); nothing else.
    rdfs / owl-rl: the owlrl closure of scene + ontology, within `budget` seconds."""
    if profile not in INFER_PROFILES:
        raise SceneError(f"profile must be one of {INFER_PROFILES}")
    ov = check_ontology_version(ontology_version or sg.ontology_version)
    base = sg.base
    warnings = []
    if profile == "subclass":
        sup = _superclasses(ov)
        types = _types_per_node(sg.graph, base)
        added, per = [], {}
        for s, ts in types.items():
            new = set()
            for t in ts:
                new |= sup.get(t, set())
            new -= ts
            per[compact(s, base)] = (len(ts), len(ts) + len(new))
            added += [(compact(s, base), "rdf:type", compact(t)) for t in new]
        added_count, total_after = len(added), len(sg.graph) + len(added)
        triples_added = added_count
    else:
        import owlrl  # noqa: F401  (imported before the worker forks)
        out = run_bounded(_closure, sg.graph, scene_ontology(ov), profile, base, timeout=budget)
        per = out["types"]
        added = out["type_triples"]
        triples_added = out["after"] - out["before"]
        total_after = out["after"]
        dom = _domain_report(ov)
        worst = max(per.items(), key=lambda kv: kv[1][1]) if per else None
        warnings.append(
            f"profile={profile} reads every rdfs:domain and rdfs:range of a property as applying together "
            "(an intersection), as RDFS and OWL define them. In the X3D Ontology "
            f"{ov} as bundled, {dom['properties_with_several_domains']} of {dom['properties_with_domain']} "
            f"properties with a domain list more than one class, and x3do:DEF lists {dom['domains_of_DEF']}; "
            "the ontology uses such lists to say which node types have a field. Under this profile every "
            "node that has the field is inferred to belong to all listed classes"
            + (f" (here {worst[0]} goes from {worst[1][0]} to {worst[1][1]} types)" if worst else "")
            + ". Use profile='subclass' for class membership.")
    added.sort()
    counts = sorted(((n, b, a) for n, (b, a) in per.items()), key=lambda x: (-x[2], x[0]))
    totals = [a for _, _, a in counts] or [0]
    return {
        "profile": profile,
        "ontology_version": ov,
        "added": triples_added,
        "type_triples_added": len(added),
        "triples_after": total_after,
        "sample": [" ".join(t) for t in added[:max(0, min(int(sample), 200))]],
        "per_node_type_counts": [{"node": n, "explicit": b, "after": a} for n, b, a in counts[:50]],
        "nodes": len(counts),
        "types_per_node": {"min": min(totals), "median": statistics.median(totals), "max": max(totals)},
        "warnings": warnings,
    }


# ------------------------------------------------------------------------------------------- map vs gate

_WRAP = {
    "geometry": ("<Shape>", "</Shape>"),
    "appearance": ("<Shape>", "</Shape>"),
    "material": ("<Shape><Appearance>", "</Appearance></Shape>"),
    "texture": ("<Shape><Appearance>", "</Appearance></Shape>"),
    "coord": ("<Shape><IndexedFaceSet>", "</IndexedFaceSet></Shape>"),
    "color": ("<Shape><IndexedFaceSet>", "</IndexedFaceSet></Shape>"),
    "normal": ("<Shape><IndexedFaceSet>", "</IndexedFaceSet></Shape>"),
    "texCoord": ("<Shape><IndexedFaceSet>", "</IndexedFaceSet></Shape>"),
    "layers": ("<LayerSet>", "</LayerSet>"),
    "emitter": ("<ParticleSystem>", "</ParticleSystem>"),
    "physics": ("<ParticleSystem>", "</ParticleSystem>"),
    "joints": ("<HAnimHumanoid>", "</HAnimHumanoid>"),
    "segments": ("<HAnimHumanoid>", "</HAnimHumanoid>"),
    "trimmingContour": ("<Shape><NurbsTrimmedSurface>", "</NurbsTrimmedSurface></Shape>"),
}


def _field_of(prop: str) -> str:
    """hasChildren -> children (the ontology names a field property has<Field>)."""
    if prop.startswith("has") and len(prop) > 3:
        return prop[3].lower() + prop[4:]
    return prop


def _xsd_live(parent: str, field: str, child: str, child_cf: str | None, parent_cf: str | None,
              version: str) -> tuple[str, str]:
    from validation.validate import validate_xml
    cf = f" containerField='{field}'" if field != child_cf else ""
    pre, post = _WRAP.get(parent_cf or "children", ("", ""))
    doc = (f"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<X3D profile='Full' version='{version}'>\n  <Scene>\n"
           f"    {pre}<{parent}>\n      <{child}{cf}/>\n    </{parent}>{post}\n  </Scene>\n</X3D>")
    v = validate_xml(doc)
    if v.get("valid"):
        return "A", ""
    first = (v.get("errors") or [""])[0]
    if f"'{child}'" in first:
        return "R", first[:160]
    return "I", first[:160]


def ontology_vs_gate(child: str, ontology_version: str = "4.1", xsd: bool = True, live_limit: int = 120) -> dict:
    """Where `child` may go, three ways: the ontology's map (node_parents), the X3DUOM gate
    (where_can_place) and the XSD (verified table for X3DUOM pairs; a minimal document for map-only pairs)."""
    from x3d_utils import placement
    from x3d_utils.ontology_guard import check_node_type
    from x3d_utils.x3duom import get_x3duom

    check_node_type(child)
    ov = check_ontology_version(ontology_version)
    nodes = get_x3duom().get_concrete_nodes()
    if child not in nodes:
        raise SceneError(f"{child} is not an X3D node type in the X3DUOM 4.1")
    onto = get_ontology(ov)
    map_rows = onto.node_parents(child)
    map_pairs = sorted({(r["parent"], _field_of(r["field"])) for r in map_rows})
    gate_rows = placement.where_can_place(child, 0, 500)["rows"]
    gate_pairs = sorted({(r["parent"], r["field"]) for r in gate_rows})
    table = json.loads(CONTAINMENT_PATH.read_text(encoding="utf-8"))["versions"].get(ov, {})
    exceptions = table.get("exceptions", {})

    def table_verdict(parent, field):
        exc = exceptions.get(f"{parent}.{field}", {})
        return "R" if child in exc.get("R", []) else ("I" if child in exc.get("I", []) else "A")

    parents: dict = {}
    for p, f in map_pairs:
        parents.setdefault(p, {"parent": p, "map_fields": [], "gate_fields": [], "concrete": p in nodes})
        parents[p]["map_fields"].append(f)
    for p, f in gate_pairs:
        parents.setdefault(p, {"parent": p, "map_fields": [], "gate_fields": [], "concrete": p in nodes})
        parents[p]["gate_fields"].append(f)

    live = 0
    xsd_counts = Counter()
    for info in parents.values():
        verdicts = {}
        if xsd:
            for f in info["gate_fields"]:
                verdicts[f] = {"verdict": table_verdict(info["parent"], f), "source": "verified-containment.json"}
            for f in info["map_fields"]:
                if f in verdicts or not info["concrete"]:
                    continue
                if live >= live_limit:
                    verdicts[f] = {"verdict": "not-run", "source": f"live limit {live_limit}"}
                    continue
                live += 1
                v, err = _xsd_live(info["parent"], f, child, nodes[child].get("containerField"),
                                   nodes[info["parent"]].get("containerField"), ov)
                verdicts[f] = {"verdict": v, "source": "minimal document, validate_xml", **({"error": err} if err else {})}
            for f, v in verdicts.items():
                xsd_counts[("gate" if f in info["gate_fields"] else "map-only", v["verdict"])] += 1
        info["xsd"] = verdicts
        info["in_map"] = bool(info["map_fields"])
        info["in_gate"] = bool(info["gate_fields"])

    rows = sorted(parents.values(), key=lambda x: x["parent"])
    map_parents = {r["parent"] for r in rows if r["in_map"]}
    gate_parents = {r["parent"] for r in rows if r["in_gate"]}
    concrete_map = {p for p in map_parents if p in nodes}
    counts = {
        "map_rows": len(map_rows),
        "map_parent_field_pairs": len(map_pairs),
        "map_parents": len(map_parents),
        "map_concrete_parents": len(concrete_map),
        "map_other_parents": sorted(map_parents - concrete_map),
        "gate_parent_field_pairs": len(gate_pairs),
        "gate_parents": len(gate_parents),
        "both": len(map_parents & gate_parents),
        "map_only": len(map_parents - gate_parents),
        "gate_only": len(gate_parents - map_parents),
    }
    if xsd:
        counts["xsd"] = {f"{where}:{verdict}": n for (where, verdict), n in sorted(xsd_counts.items())}
    return {
        "child": child,
        "ontology_version": ov,
        "counts": counts,
        "parents": rows,
        "counting_rules": [
            "map = node_parents on the X3D Ontology "
            f"{ov}: a row is a (parent class, has<Field> property) where the property is a sub-property of "
            "hasChild, its rdfs:range subsumes the child, and the range is a named class other than X3DNode; "
            "rows can repeat a pair through several ranges, so pairs are counted after de-duplication.",
            "gate = where_can_place on the X3DUOM 4.1: a row is a (concrete parent, SFNode/MFNode field) whose "
            "acceptableNodeTypes admits the child or one of its base types; inputOnly/outputOnly fields are not counted.",
            "parents are distinct names. map parents can be statements (Scene, ProtoBody) or abstract classes; "
            "map_concrete_parents counts only X3DUOM concrete nodes, and only those are compared with the XSD.",
            "fields are compared by name: the ontology property has<Field> is read as field <field>.",
            "xsd verdicts: A accepts, R rejects, I the harness could not tell. Gate pairs read the shipped "
            "verified-containment.json (built from the XSD); map-only pairs are checked live with a minimal "
            "X3D document (profile Full, the parent wrapped where its own containerField needs it).",
        ],
    }
