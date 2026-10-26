"""Loader for the X3D Ontology for Semantic Web (Turtle encoding).

Parses the bundled X3dOntology4.0.ttl into an rdflib Graph and provides
SPARQL query access. The ontology complements the X3DUOM: the X3DUOM
records each node's own metadata (fields, types, containerField), while
the ontology's property lattice (hasChild/hasParent with domains and
ranges) supports relationship queries such as "which node types may
parent a given node" that the X3DUOM index cannot answer directly.

Ontology answers are advisory; XSD and semantic validation remain the
ground truth for document validity.
"""

import os
from pathlib import Path

import rdflib

from x3d_utils.ontology_guard import check_node_type


SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "validation" / "schemas"
ONTOLOGY_PATH = SCHEMAS_DIR / "X3dOntology4.0.ttl"

X3D_NS = "https://www.web3d.org/specifications/X3dOntology4.0#"

# Optional extension ontologies (kept in a *separate* graph, see below).
# EXTENSIONS_ENV is an os.pathsep-separated list of .ttl files and/or
# directories of .ttl files, e.g. the 3D_perspective_skill `ontology/` folder.
EXTENSIONS_ENV = "X3D_MCP_EXTRA_ONTOLOGIES"
SPATIAL_NS = "https://w3id.org/spatial-cognition#"

# Extension ontologies may ground their concepts in a different release of the
# X3D Ontology than the one bundled here (the spatial-cognition ontology uses
# 4.1, this server loads 4.0). IRIs in these namespaces are rewritten to X3D_NS
# as the extension is loaded, so a query can join them to the bundled classes.
# 4.1-only nodes have no 4.0 counterpart and are reported as unresolved.
X3D_ALIAS_NAMESPACES = (
    "https://www.web3d.org/specifications/X3dOntology4.1#",
)

PREFIXES = f"""\
PREFIX x3d: <{X3D_NS}>
PREFIX sc: <{SPATIAL_NS}>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX dcterms: <http://purl.org/dc/terms/>
"""


def _extension_files(spec: str) -> list[Path]:
    """Expand an os.pathsep-separated list of files/directories to .ttl files."""
    files: list[Path] = []
    for item in filter(None, (part.strip() for part in spec.split(os.pathsep))):
        p = Path(item).expanduser()
        if p.is_dir():
            files.extend(sorted(p.glob("*.ttl")))
        elif p.is_file():
            files.append(p)
        else:
            raise FileNotFoundError(f"{EXTENSIONS_ENV}: no such file or directory: {item}")
    return files


def _alias_x3d_namespaces(graph: rdflib.Graph) -> rdflib.Graph:
    """Return a copy of `graph` with alias X3D namespaces rewritten to X3D_NS."""
    def fix(term):
        if isinstance(term, rdflib.URIRef):
            text = str(term)
            for alias in X3D_ALIAS_NAMESPACES:
                if text.startswith(alias):
                    return rdflib.URIRef(X3D_NS + text[len(alias):])
        return term

    out = rdflib.Graph()
    for prefix, ns in graph.namespaces():
        if str(ns) not in X3D_ALIAS_NAMESPACES:
            out.bind(prefix, ns)
    for s_, p_, o_ in graph:
        out.add((fix(s_), fix(p_), fix(o_)))
    return out


class X3DOntology:
    """Parsed representation of the X3D Ontology.

    The bundled X3D Ontology is always loaded into `graph`. Optional extension
    ontologies (see EXTENSIONS_ENV) are loaded into a *separate* `extensions`
    graph: the default graph, and every default query, is unchanged unless a
    caller asks for `include_extensions=True`.
    """

    def __init__(self, path: Path = ONTOLOGY_PATH, extensions: str | None = None):
        self._graph = rdflib.Graph()
        self._graph.parse(str(path), format="turtle")
        self._ns = rdflib.Namespace(X3D_NS)
        self._extension_files: list[Path] = []
        self._extensions = rdflib.Graph()
        spec = os.environ.get(EXTENSIONS_ENV, "") if extensions is None else extensions
        if spec:
            self._load_extensions(spec)

    def _load_extensions(self, spec: str) -> None:
        raw = rdflib.Graph()
        self._extension_files = _extension_files(spec)
        for f in self._extension_files:
            raw.parse(str(f), format="turtle")
        self._extensions = _alias_x3d_namespaces(raw)

    @property
    def extensions(self) -> rdflib.Graph:
        return self._extensions

    def extension_status(self) -> dict:
        """What was loaded, and which X3D classes it references that the bundled
        ontology does not define (e.g. nodes that exist only in X3D 4.1)."""
        ns = rdflib.Namespace(X3D_NS)
        referenced = {
            t for triple in self._extensions for t in triple
            if isinstance(t, rdflib.URIRef) and str(t).startswith(X3D_NS)
        }
        unresolved = sorted(
            str(t)[len(X3D_NS):] for t in referenced
            if (t, None, None) not in self._graph and (None, None, t) not in self._graph
        )
        return {
            "files": [str(f) for f in self._extension_files],
            "triples": len(self._extensions),
            "x3d_terms_referenced": len(referenced),
            "unresolved_x3d_terms": unresolved,
        }

    @property
    def graph(self) -> rdflib.Graph:
        return self._graph

    def query(self, sparql: str, include_extensions: bool = False, fold_blank: bool = False) -> list[dict]:
        """Run a SPARQL SELECT/ASK query, prepending standard prefixes.

        With `include_extensions`, the query runs over the union of the bundled
        ontology and any extension ontologies (a no-op if none are loaded).

        Returns a list of {variable: value} dicts for SELECT, or a single
        {"ask": bool} dict for ASK.

        With `fold_blank`, a blank node renders as "_:blank" instead of its random label, so the same
        query gives the same rows on every run (the label is never needed to join, only to display).
        """
        graph = self._graph
        if include_extensions and len(self._extensions):
            graph = rdflib.graph.ReadOnlyGraphAggregate([self._graph, self._extensions])
        result = graph.query(PREFIXES + sparql)
        if result.type == "ASK":
            return [{"ask": bool(result.askAnswer)}]
        rows = []
        for binding in result:
            row = {}
            for var in result.vars:
                value = binding[var]
                row[str(var)] = "_:blank" if fold_blank and isinstance(value, rdflib.BNode) else self._render(value)
            rows.append(row)
        return rows

    def node_parents(self, node_type: str) -> list[dict]:
        """Return node types whose fields may contain the given node type.

        Walks properties that are subproperties of x3d:hasChild whose
        rdfs:range subsumes the node type, reporting each property's
        rdfs:domain as a candidate parent.

        `node_type` goes into the query text, so it must be a plain name; anything else
        is refused with ValueError (QueryRefused) before a query is built.
        """
        check_node_type(node_type)
        sparql = """
SELECT DISTINCT ?parent ?field WHERE {
  ?field rdfs:subPropertyOf+ x3d:hasChild ;
         rdfs:range ?range ;
         rdfs:domain ?parent .
  x3d:%s rdfs:subClassOf* ?range .
  # Named, specific ranges only: the aggregate hasChildren-family
  # properties also carry a bare X3DNode range and anonymous union
  # ranges, which would admit every node type as a child.
  FILTER(isIRI(?range) && ?range != x3d:X3DNode)
}
ORDER BY ?parent
""" % node_type
        return self.query(sparql)

    def describe_term(self, term: str) -> dict:
        """Return labels, types, and super/sub relations for an ontology term."""
        uri = self._ns[term]
        g = self._graph
        if (uri, None, None) not in g:
            return {"error": f"Term not found in ontology: {term}"}

        def q(items):
            return sorted({self._render(x) for x in items})

        return {
            "term": term,
            "types": q(g.objects(uri, rdflib.RDF.type)),
            "labels": q(g.objects(uri, rdflib.RDFS.label)),
            "superClasses": q(g.objects(uri, rdflib.RDFS.subClassOf)),
            "subClasses": q(g.subjects(rdflib.RDFS.subClassOf, uri)),
            "superProperties": q(g.objects(uri, rdflib.RDFS.subPropertyOf)),
            "subProperties": q(g.subjects(rdflib.RDFS.subPropertyOf, uri)),
            "domains": q(g.objects(uri, rdflib.RDFS.domain)),
            "ranges": q(g.objects(uri, rdflib.RDFS.range)),
            "inverseOf": q(
                list(g.objects(uri, rdflib.OWL.inverseOf))
                + list(g.subjects(rdflib.OWL.inverseOf, uri))
            ),
        }

    def _render(self, value) -> str:
        """Render an rdflib term compactly (strip the x3d namespace)."""
        if isinstance(value, rdflib.URIRef):
            text = str(value)
            if text.startswith(X3D_NS):
                return text[len(X3D_NS):]
            return text
        if value is None:
            return ""
        return str(value)


# Module-level singleton
_instance = None


def get_ontology() -> X3DOntology:
    """Return the cached X3DOntology singleton."""
    global _instance
    if _instance is None:
        _instance = X3DOntology()
    return _instance
