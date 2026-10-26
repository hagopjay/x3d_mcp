"""Semantic-web query tools backed by the X3D Ontology.

Exposes the bundled X3D Ontology (Turtle encoding) for SPARQL queries
and canned relationship lookups. These tools answer questions about the
X3D type system that the X3DUOM index cannot, such as the closed set of
node types whose fields may legally contain a given node.

Ontology answers are advisory: the ontology's property ranges are in
places broader than the XSD content models, so XSD validation (level 3)
and semantic checks (level 4) remain the ground truth for validity.
"""

import hashlib
import json
from collections import OrderedDict
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from x3d_utils import placement
from x3d_utils.ontology import ONTOLOGY_PATH, PREFIXES, get_ontology
from x3d_utils.ontology_guard import (
    QueryRefused, QueryTimeout, guard_sparql, max_rows, run_bounded,
)

_PAGES: "OrderedDict[tuple, list]" = OrderedDict()     # sorted result rows, so paging a slow query runs it once
_PAGES_KEEP = 4


def _run_query(sparql: str, include_extensions: bool, fold_blank: bool, cap: int):
    """Runs in the bounded worker: execute, then cut to cap+1 rows so a huge answer is not sent whole."""
    rows = get_ontology().query(sparql, include_extensions=include_extensions, fold_blank=fold_blank)
    return len(rows), rows[:cap + 1]


def _row_key(row: dict) -> str:
    return json.dumps(row, sort_keys=True, ensure_ascii=False)


def _ask(sparql: str, include_extensions: bool, fold_blank: bool = False):
    """Guard, run with a deadline, cap. Returns (rows, total, truncated); raises QueryRefused/QueryTimeout."""
    guard_sparql(PREFIXES + sparql)
    get_ontology()          # load it here: a forked worker that loads it parses the file again on every call
    cap = max_rows()
    total, rows = run_bounded(_run_query, sparql, include_extensions, fold_blank, cap)
    truncated = total > cap
    return rows[:cap], total, truncated


def register(mcp: FastMCP):

    @mcp.tool()
    def query_ontology(sparql: str, include_extensions: bool = False) -> str:
        """Run a SPARQL SELECT or ASK query against the X3D Ontology.

        Standard prefixes are pre-declared: x3d, owl, rdf, rdfs, dcterms.
        The x3d prefix is the X3D Ontology namespace; classes are node
        type names (x3d:Sphere) and the containment lattice uses
        x3d:hasChild / x3d:hasParent with per-field subproperties
        carrying rdfs:domain and rdfs:range.

        Extension ontologies (e.g. the spatial-cognition ontology of
        3D_perspective_skill, prefix `sc:`) are loaded into a separate graph
        only when the server is started with X3D_MCP_EXTRA_ONTOLOGIES set;
        pass include_extensions=True to query the union with them. Default
        queries are unaffected.

        Args:
            sparql: The SPARQL query text (SELECT or ASK).
            include_extensions: Also query the loaded extension ontologies.
        """
        try:
            rows, total, truncated = _ask(sparql, include_extensions)
        except QueryRefused as e:
            return json.dumps({"error": f"Query refused: {e}"})
        except QueryTimeout as e:
            return json.dumps({"error": str(e)})
        except Exception as e:
            return json.dumps({"error": f"SPARQL error: {e}"})
        out = {"rows": rows, "count": len(rows)}
        if truncated:
            out["truncated"] = True
            out["total"] = total
            out["note"] = (f"{total} rows matched; the first {len(rows)} are shown. "
                           "Use query_ontology_page to read the rest in pages.")
        return json.dumps(out, indent=2)

    @mcp.tool(structured_output=False)
    def query_ontology_page(sparql: str, offset: int = 0, limit: int = 50, include_extensions: bool = False) -> str:
        """Run a SPARQL SELECT or ASK query and return one page of the rows.

        Rows are sorted, and blank nodes are shown as "_:blank", so the same query gives the same
        page every time and a page has a stable hash (page_sha256). The query runs once; the
        next pages are read from a small server-side cache (the four most recent queries).
        The same limits apply as for query_ontology: SELECT and ASK only, no SERVICE or FROM,
        a deadline (X3D_MCP_SPARQL_TIMEOUT, 20 s) and a row cap (X3D_MCP_ONTOLOGY_MAX_ROWS).

        Args:
            sparql: The SPARQL query text.
            offset: First row of the page (0-based).
            limit: Rows per page, 1 to 500 (default 50).
            include_extensions: Also query the loaded extension ontologies.

        Returns JSON {rows, total, offset, next_offset, page_sha256, truncated?}; next_offset is null on the last page.
        """
        key = (sparql, bool(include_extensions))
        try:
            if key not in _PAGES:
                rows, total, truncated = _ask(sparql, include_extensions, fold_blank=True)
                rows = sorted(rows, key=_row_key)
                _PAGES[key] = (rows, total, truncated)
                while len(_PAGES) > _PAGES_KEEP:
                    _PAGES.popitem(last=False)
            else:
                _PAGES.move_to_end(key)
            rows, total, truncated = _PAGES[key]
        except QueryRefused as e:
            return json.dumps({"error": f"Query refused: {e}"})
        except QueryTimeout as e:
            return json.dumps({"error": str(e)})
        except Exception as e:
            return json.dumps({"error": f"SPARQL error: {e}"})
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 500))
        chunk = rows[offset:offset + limit]
        nxt = offset + limit
        out = {
            "rows": chunk,
            "total": total,
            "offset": offset,
            "next_offset": nxt if nxt < len(rows) else None,
            "page_sha256": hashlib.sha256(_row_key({"rows": chunk}).encode()).hexdigest(),
        }
        if truncated:
            out["truncated"] = True
        return json.dumps(out, separators=(",", ":"))

    @mcp.tool()
    def node_parents(node_type: str) -> str:
        """List node types whose fields may contain the given node type.

        Answers "which node types may legally parent this node" from the
        X3D Ontology's containment property lattice. Each result names
        the candidate parent class and the field property that admits the
        child. Results are advisory; validate composed scenes with
        validate_x3d for ground truth.

        Args:
            node_type: The X3D node type name (e.g. "Sphere", "Material").
        """
        onto = get_ontology()
        try:
            rows = onto.node_parents(node_type)
        except QueryRefused as e:
            return json.dumps({"error": f"Query refused: {e}"})
        if not rows:
            return json.dumps({
                "error": f"No parents found; is '{node_type}' an X3D node type?"
            })
        return json.dumps({"nodeType": node_type, "parents": rows}, indent=2)

    @mcp.tool()
    def describe_ontology_term(term: str) -> str:
        """Describe an X3D Ontology class or property.

        Returns labels, rdf types, super/sub classes and properties,
        domains, ranges, and inverse properties for the named term.

        Args:
            term: Ontology term name (e.g. "Sphere", "hasChild", "geometry").
        """
        onto = get_ontology()
        return json.dumps(onto.describe_term(term), indent=2)

    @mcp.tool(structured_output=False)
    def where_can_place(child: str, offset: int = 0, limit: int = 50) -> str:
        """List the parent nodes and fields where a node type may be placed, from the X3DUOM.

        Unlike node_parents (the ontology's looser containment lattice) this reads the X3DUOM's
        acceptableNodeTypes on every node-valued field, so each row is a real placement:
        the parent node, the field, and whether the child needs an explicit containerField.
        Rows are sorted, and the whole table is versioned by table_sha256. Most nodes have fewer
        than 50 rows; MovieTexture and the Metadata* nodes have more, so page with offset.

        Args:
            child: The X3D node type name (e.g. "Sphere", "Material").
            offset: First row of the page (0-based).
            limit: Rows per page, 1 to 500 (default 50).

        Returns JSON {child, source, table_sha256, total, offset, rows, next_offset}.
        """
        try:
            return json.dumps(placement.where_can_place(child, offset, limit), separators=(",", ":"))
        except KeyError:
            return json.dumps({"error": f"Unknown node type: {child}"})

    @mcp.resource("x3d-ontology://turtle", mime_type="text/turtle")
    def ontology_turtle() -> str:
        """The bundled X3D Ontology as Turtle (a resource read costs about 1.1 times the bytes; a tool answer 2 times)."""
        return Path(ONTOLOGY_PATH).read_text(encoding="utf-8")

    @mcp.resource("x3d-ontology://placement/{child}", mime_type="application/json")
    def placement_resource(child: str) -> str:
        """Every place a node type may go (the whole where_can_place answer, unpaged)."""
        try:
            return json.dumps(placement.where_can_place(child, 0, 500), separators=(",", ":"))
        except KeyError:
            return json.dumps({"error": f"Unknown node type: {child}"})
