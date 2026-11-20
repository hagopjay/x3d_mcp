"""Semantic-web tools for a scene: its RDF form, SPARQL over it, SHACL shapes, inference, map vs gate.

    scene_to_rdf      X3D -> Turtle, the Consortium X3dToTurtle.xslt conventions, in Python (x3d_utils.scene_rdf)
    sparql_scene      SELECT/ASK over the scene graph, with the X3D Ontology joined (guarded, paged)
    shacl_validate    the bundled x3d-core shapes (or your own) on the scene graph
    rdf_infer         what reasoning adds: subclass-only by default, RDFS / OWL-RL with a report per node
    ontology_vs_gate  where a node may go: ontology map vs X3DUOM gate vs XSD

A converted scene is kept for the session under its graph_id (the first 16 hex digits of the Turtle's
sha256), so later calls name it instead of sending the document again. The ontology proposes, the
shapes dispose, and validate_x3d / validate_semantic remain the ground truth for document validity.
Every answer carries `instrument` (commit, file hashes, library versions).
"""

import hashlib
import json
import os
import time
import weakref
from collections import OrderedDict

from mcp.server.fastmcp import Context, FastMCP

from transfer.outputs import document_info, input_info, parse_mode
from transfer.tooling import holds_input, require_blob_access, resolve_input, run_heavy
from x3d_utils import semantic_web as sw
from x3d_utils.ontology_guard import QueryRefused, QueryTimeout

TURTLE = "text/turtle"
INLINE_ENV = "X3D_MCP_TURTLE_INLINE_MAX"
DEFAULT_INLINE = 120_000
SUMMARY_RESULTS = 25

_fallback = sw.GraphCache()
_session_caches: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_PAGES: "OrderedDict[tuple, dict]" = OrderedDict()
_PAGES_KEEP = 8
_REPORTS: "OrderedDict[tuple, dict]" = OrderedDict()


def _remember(store: OrderedDict, key: tuple, value: dict) -> dict:
    store[key] = value
    while len(store) > _PAGES_KEEP:
        store.popitem(last=False)
    return value


def _cache(ctx) -> sw.GraphCache:
    """The calling session's scene graphs (a module-level cache when there is no live session)."""
    if ctx is None:
        return _fallback
    try:
        session = ctx.session
    except (ValueError, AttributeError):
        return _fallback
    if session is None:
        return _fallback
    cache = _session_caches.get(session)
    if cache is None:
        cache = _session_caches[session] = sw.GraphCache()
    return cache


def _inline_max() -> int:
    try:
        v = int(os.environ.get(INLINE_ENV, "") or DEFAULT_INLINE)
    except ValueError:
        v = DEFAULT_INLINE
    return v if v > 0 else DEFAULT_INLINE


def _err(message: str, **extra) -> str:
    return json.dumps({"ok": False, "error": str(message), **extra}, indent=2)


async def _scene(ctx, content: str, path: str, blob_id: str, graph_id: str = "", base_iri: str = ""):
    """(SceneGraph, input digest or None, cached?) from a graph_id of this session, or by converting the input."""
    cache = _cache(ctx)
    has_input = any((x or "").strip() for x in (content, path, blob_id))
    if (graph_id or "").strip() and not has_input:
        sg = cache.get(graph_id)
        if sg is None:
            raise ValueError(f"no scene graph {graph_id!r} in this session (graphs are kept per session, "
                             f"the {cache.keep} most recent); pass content, path or blob_id to convert it again")
        return sg, None, True
    if not has_input:
        raise ValueError("pass the scene as content, path or blob_id, or name a converted scene with graph_id")
    text, _label = await resolve_input(content, path, blob_id, ctx)
    sg = await run_heavy(sw.convert, text, base_iri, size=len(text))
    if graph_id and graph_id.strip() and not sg.graph_id.startswith(graph_id.strip()[:16]):
        sg.warnings.append(f"graph_id {graph_id!r} was ignored: the input converts to {sg.graph_id}")
    cache.put(sg)
    return sg, input_info(text, blob_id), False


def register(mcp: FastMCP):

    @mcp.tool(structured_output=False)
    @holds_input
    async def scene_to_rdf(content: str = "", path: str = "", blob_id: str = "", format: str = "turtle",
                           include_ontology: bool = False, base_iri: str = "", return_mode: str = "full",
                           ctx: Context = None) -> str:
        """Convert an X3D scene to RDF (Turtle), the way the Web3D Consortium's X3dToTurtle.xslt does.

        Each node becomes an individual named by its DEF (USE copies are <DEF>-USE-<n> with owl:sameAs;
        unnamed nodes get positional names such as Shape_2_4_2, which change if nodes are inserted),
        typed owl:NamedIndividual and x3do:<NodeType>. Parent and child are linked both ways
        (x3do:hasParent, x3do:has<containerField>), with containerField defaults from the X3D DTD, so a
        Sphere is x3do:hasGeometry. Non-default fields become x3do:<field> values (numbers, rdf:Lists
        for vectors and colours, strings). ROUTEs are x3do:hasROUTE individuals with fromNode/toNode as
        DEF strings. Namespace: x3do = https://www.web3d.org/specifications/X3dOntology4.1#.

        The graph is kept for this session under `graph_id`; pass it to sparql_scene, shacl_validate
        and rdf_infer instead of resending the document.

        Args:
            content: X3D XML text. Provide exactly one of content, path or blob_id.
            path: An X3D file (stdio only).
            blob_id: A stored blob holding the document.
            format: "turtle" (default; the stylesheet's layout) or "ntriples".
            include_ontology: Append the X3D Ontology classes the scene uses and their superclasses.
            base_iri: Namespace for the scene's individuals; default from the identifier meta.
            return_mode: "full" (default) includes the text, clipped past X3D_MCP_TURTLE_INLINE_MAX
                  characters; "summary" leaves it out; "blob" stores it and returns its blob_id.

        Returns JSON {graph_id, sha256, triples, individuals, base, ontology_version, warnings, turtle?,
        turtle_blob?, ontology_classes?, input, instrument, ms}. sha256 is the hash of the Turtle text.
        """
        t0 = time.perf_counter()
        try:
            mode = parse_mode(return_mode)
            if format not in ("turtle", "ntriples"):
                raise ValueError("format must be 'turtle' or 'ntriples'")
            if mode == "blob":
                require_blob_access(ctx)
            sg, inp, _ = await _scene(ctx, content, path, blob_id, base_iri=base_iri)
        except (ValueError, sw.SceneError) as e:
            return _err(e)
        text = sg.turtle
        out = {"ok": True, **sg.describe(), "sha256": sg.sha256, "warnings": sg.warnings,
               "convert_ms": sg.convert_ms}
        if include_ontology:
            slice_text, n_classes, n_triples = sw.tbox_slice(sg, sg.ontology_version)
            text = text + "\n" + slice_text
            out["ontology_classes"] = {"classes": n_classes, "triples": n_triples}
        if format == "ntriples":
            text = sg.graph.serialize(format="nt")
            if include_ontology:
                out["warnings"] = out["warnings"] + ["ntriples output has the scene only; the class slice is Turtle"]
        out["format"] = format
        if mode == "full":
            limit = _inline_max()
            out["turtle" if format == "turtle" else "ntriples"] = text[:limit]
            if len(text) > limit:
                out["truncated"] = True
                out["note"] = (f"{len(text)} characters; the first {limit} are shown. Use return_mode='blob', "
                               "or query the graph with sparql_scene(graph_id=...).")
        elif mode == "blob":
            out["turtle_blob"] = document_info(text, TURTLE if format == "turtle" else "application/n-triples", True)
        else:
            out["text_bytes"] = len(text.encode("utf-8"))
        out["input"] = inp
        out["instrument"] = sw.instrument(sg.ontology_version)
        out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return json.dumps(out, indent=1)

    @mcp.tool(structured_output=False)
    @holds_input
    async def sparql_scene(sparql: str, content: str = "", path: str = "", blob_id: str = "", graph_id: str = "",
                           include_ontology: bool = True, ontology_version: str = "", offset: int = 0,
                           limit: int = 50, data_ttl: str = "", result_format: str = "compact",
                           ctx: Context = None) -> str:
        """Run a SPARQL SELECT or ASK over a scene's RDF graph, joined with the X3D Ontology.

        Prefixes are pre-declared: x3do = X3dOntology4.1# (written x3d: instead when the query uses only
        x3d:), rdf, rdfs, owl, xsd, dcterms, and ':' = the scene's base. Ask about individuals and their fields, e.g.
          SELECT ?shape ?g WHERE { ?shape a x3do:Shape ; x3do:hasGeometry ?g }
          SELECT ?n ?t WHERE { ?n a ?t . ?t rdfs:subClassOf* x3do:X3DGeometryNode }   (class hierarchy)
        Colours and vectors are rdf:Lists: ?m x3do:diffuseColor ?c . ?c rdf:first ?r . ROUTE endpoints are
        DEF strings: ?r x3do:fromNode ?def . ?n x3do:DEF ?def . Only SELECT and ASK run; SERVICE and FROM
        are refused; the server's deadline and row cap apply. A query with ORDER BY keeps its order; other
        rows are sorted. Pages carry a stable page_sha256. Scene individuals show as :Name, ontology terms
        as x3do:Name. The default compact rows are short strings and lose information: an unbound variable
        and an empty string both read "", every blank node reads _:blank, and 11 and "11" look alike. Use
        result_format="typed" when the answer depends on types, missing values or which blank is which.

        Args:
            sparql: The query (SELECT or ASK).
            content / path / blob_id: The scene, converted first (as scene_to_rdf); or
            graph_id: A scene converted earlier in this session.
            include_ontology: Join the X3D Ontology (default true; needed for rdfs:subClassOf paths).
            ontology_version: "4.0" or "4.1"; default by the document's X3D version (4.1 for 4.1 and for
                  no version, 4.0 for 4.0 and 3.x).
            offset: First row of the page (0-based).
            limit: Rows per page, 1 to 500 (default 50).
            data_ttl: Domain data in Turtle joined to the scene (asset shells, sensor observations, museum
                  records, FHIR resources...). Link it to scene nodes by their IRIs (the scene's base + DEF,
                  e.g. <https://example.org/cell/scene.ttl#Station3>) so one query can need both graphs.
                  Up to 2,000,000 characters; nothing it names is fetched.
            result_format: "compact" (default) or "typed": each cell a SPARQL JSON term {type: uri |
                  literal | bnode, value, datatype?, xml:lang?, compact?}; unbound variables are left out of
                  the row; blank nodes are labelled b0, b1 ... per answer, keeping co-reference.

        Returns JSON {graph_id, rows, total, offset, next_offset, page_sha256, ontology_version, order (query | sorted),
        result_format, vars (typed only), truncated?, instrument, ms}.
        """
        t0 = time.perf_counter()
        try:
            sg, inp, cached = await _scene(ctx, content, path, blob_id, graph_id)
            ov = ontology_version or sg.ontology_version
            dkey = hashlib.sha256(data_ttl.encode()).hexdigest() if data_ttl else ""
            key = (id(_cache(ctx)), sg.graph_id, sparql, bool(include_ontology), ov, dkey, result_format)
            if key in _PAGES:
                _PAGES.move_to_end(key)
                result = _PAGES[key]
            else:
                result = _remember(_PAGES, key, await run_heavy(sw.sparql, sg, sparql, bool(include_ontology), ov,
                                                                 data_ttl, result_format, size=sg.triples * 40 + len(data_ttl)))
        except QueryRefused as e:
            return _err(f"Query refused: {e}")
        except QueryTimeout as e:
            return _err(str(e))
        except (ValueError, sw.SceneError) as e:
            return _err(e)
        except Exception as e:                      # rdflib evaluation errors
            return _err(f"SPARQL error: {e}")
        rows = result["rows"]
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 500))
        chunk = rows[offset:offset + limit]
        nxt = offset + limit
        out = {
            "ok": True,
            "graph_id": sg.graph_id,
            "graph_sha256": sg.sha256,
            "ontology_version": result["ontology_version"],
            "order": result["order"],
            "result_format": result["result_format"],
            "rows": chunk,
            "total": result["total"],
            "offset": offset,
            "next_offset": nxt if nxt < len(rows) else None,
            "page_sha256": hashlib.sha256(json.dumps({"rows": chunk}, sort_keys=True, ensure_ascii=False)
                                          .encode()).hexdigest(),
        }
        if "vars" in result:
            out["vars"] = result["vars"]
        if result["truncated"]:
            out["truncated"] = True
        if not cached:
            out["converted"] = {"triples": sg.triples, "warnings": sg.warnings, "input": inp}
        out["instrument"] = sw.instrument(result["ontology_version"] or sg.ontology_version)
        out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return json.dumps(out, separators=(",", ":"))

    @mcp.tool(structured_output=False)
    @holds_input
    async def shacl_validate(content: str = "", path: str = "", blob_id: str = "", graph_id: str = "",
                             shapes: str = "x3d-core", extra_shapes_ttl: str = "", inference: str = "none",
                             ontology_version: str = "4.1", return_mode: str = "summary",
                             data_ttl: str = "", ctx: Context = None) -> str:
        """Check a scene's RDF graph against SHACL shapes; each result names the node, its DEF and a fix.

        The bundled x3d-core shapes: S1 colour and scalar ranges in [0,1]; S1b an SFColor has 3
        components; S2 a Shape has geometry (warning); S3 a USE names an existing DEF of the same type;
        S4 the Scene has a Viewpoint (warning); S5 ROUTE endpoints name existing DEFs; S6 each node field
        is one the X3D Ontology gives the parent type; S7 nodes in a children field are X3DChildNodes;
        S8 one DEF names one node. RDF keeps no document order, so USE-before-DEF and ROUTE-before-DEF
        are validate_semantic's job; schema validity is validate_x3d's.

        Args:
            content / path / blob_id: The scene, converted first (as scene_to_rdf); or
            graph_id: A scene converted earlier in this session.
            shapes: "x3d-core" (default), "x3d-core,x3d-cardinality" (adds: an SFNode field such as
                  Appearance.material holds one node, generated from the X3DUOM), or "none" (only
                  extra_shapes_ttl).
            extra_shapes_ttl: Your own shapes in Turtle, added to the bundled ones (x3do: is the
                  X3dOntology4.1# namespace). SPARQL constraints are guarded like sparql_scene.
            inference: "none" (default), "rdfs", "owlrl" or "both". Inference expands the scene with the
                  whole ontology first; see the warning it returns.
            ontology_version: Ontology the shapes read (default "4.1", for every document version).
            return_mode: "summary" (default: counts and the first 25 results) or "full" (all results).
            data_ttl: Domain data in Turtle added to the data graph, as for sparql_scene, so your shapes can
                  hold the scene to a record (a robot's rated reach, a device's location) and the record to
                  the scene.

        Returns JSON {conforms, counts {Violation, Warning, Info}, by_check, results: [{focus, def,
        node_type, path, value, severity, message, shape, check_id, fix_hint}], graph_id, instrument, ms}.
        conforms is false only when there is a Violation (warnings are reported, not failed).
        """
        t0 = time.perf_counter()
        if return_mode not in ("summary", "full"):
            return _err("return_mode must be 'summary' or 'full'")
        try:
            sg, inp, cached = await _scene(ctx, content, path, blob_id, graph_id)
            key = (id(_cache(ctx)), sg.sha256, shapes, hashlib.sha256(extra_shapes_ttl.encode()).hexdigest(),
                   inference, ontology_version, hashlib.sha256(data_ttl.encode()).hexdigest() if data_ttl else "")
            if key in _REPORTS:
                _REPORTS.move_to_end(key)
                res = _REPORTS[key]
            else:
                res = _remember(_REPORTS, key, await run_heavy(sw.shacl, sg, shapes, extra_shapes_ttl, inference,
                                                               ontology_version, None, data_ttl,
                                                               size=sg.triples * 40 + len(data_ttl)))
        except QueryTimeout as e:
            return _err(str(e))
        except (ValueError, sw.SceneError) as e:
            return _err(e)
        except RuntimeError as e:
            return _err(f"SHACL error: {e}")
        results = res["results"]
        out = {"ok": True, "conforms": res["conforms"], "counts": res["counts"], "by_check": res["by_check"],
               "graph_id": sg.graph_id, "graph_sha256": sg.sha256, "ontology_version": res["ontology_version"],
               "inference": inference, "shapes": shapes}
        if return_mode == "summary" and len(results) > SUMMARY_RESULTS:
            out["results"] = results[:SUMMARY_RESULTS]
            out["results_total"] = len(results)
            out["note"] = f"first {SUMMARY_RESULTS} of {len(results)} results; return_mode='full' lists all"
        else:
            out["results"] = results
        if res["warnings"]:
            out["warnings"] = res["warnings"]
        if not cached:
            out["converted"] = {"triples": sg.triples, "warnings": sg.warnings, "input": inp}
        out["instrument"] = sw.instrument(res["ontology_version"],
                                          shapes if sw.bundled_shape_names(shapes) else "x3d-core")
        out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return json.dumps(out, indent=1)

    @mcp.tool(structured_output=False)
    @holds_input
    async def rdf_infer(content: str = "", path: str = "", blob_id: str = "", graph_id: str = "",
                        profile: str = "subclass", budget: float = 60.0, ontology_version: str = "",
                        sample: int = 20, ctx: Context = None) -> str:
        """Show what reasoning over the X3D Ontology adds to a scene's graph, node by node.

        profile="subclass" (default) adds only rdf:type superclasses (a Sphere is also an
        X3DGeometryNode, X3DNode...): small and safe. "rdfs" and "owl-rl" run the full closure of
        scene + ontology (owlrl) and report how many types each node ends up with; the X3D Ontology
        lists several classes as rdfs:domain of many fields, which RDFS reads as all applying at once,
        so the answer carries a warning explaining the counts. The graph kept for the session is not
        changed.

        Args:
            content / path / blob_id: The scene, converted first; or graph_id from this session.
            profile: "subclass" (default), "rdfs" or "owl-rl".
            budget: Seconds the rdfs / owl-rl closure may take (1 to 300; default 60).
            ontology_version: "4.0" or "4.1"; default by the document's X3D version.
            sample: How many added rdf:type triples to list (default 20).

        Returns JSON {profile, added, type_triples_added, sample, per_node_type_counts [{node, explicit,
        after}], types_per_node {min, median, max}, warnings, graph_id, instrument, ms}.
        """
        t0 = time.perf_counter()
        try:
            budget = max(1.0, min(float(budget), 300.0))
            sg, inp, cached = await _scene(ctx, content, path, blob_id, graph_id)
            res = await run_heavy(sw.infer, sg, profile, budget, ontology_version, sample, size=sg.triples * 40)
        except QueryTimeout as e:
            return _err(f"{e}; the closure did not finish within budget={budget:g} s. "
                        "profile='subclass' answers class membership without the closure")
        except (ValueError, sw.SceneError) as e:
            return _err(e)
        except RuntimeError as e:
            return _err(f"inference error: {e}")
        out = {"ok": True, **res, "graph_id": sg.graph_id, "graph_sha256": sg.sha256}
        if not cached:
            out["converted"] = {"triples": sg.triples, "warnings": sg.warnings, "input": inp}
        out["instrument"] = sw.instrument(res["ontology_version"])
        out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return json.dumps(out, indent=1)

    @mcp.tool(structured_output=False)
    async def ontology_vs_gate(child: str, ontology_version: str = "4.1", xsd: bool = True) -> str:
        """Compare three answers to "where may this node go?": the X3D Ontology map, the X3DUOM gate, the XSD.

        map = node_parents (the ontology's hasChild property lattice: which parent types have a field whose
        range admits the child); gate = where_can_place (the X3DUOM's acceptableNodeTypes on each node
        field); XSD = the verified table shipped with the server for gate pairs, and a minimal document
        validated live for pairs only the map proposes. The ontology proposes, the gate and the XSD decide.
        The answer states its counting rules.

        Args:
            child: X3D node type (e.g. "ImageTexture", "Box", "HAnimJoint").
            ontology_version: "4.1" (default) or "4.0".
            xsd: Add XSD verdicts (A accepts, R rejects, I inconclusive); false skips the live checks.

        Returns JSON {child, counts, parents: [{parent, in_map, in_gate, map_fields, gate_fields, xsd}],
        counting_rules, instrument, ms}.
        """
        t0 = time.perf_counter()
        try:
            res = await run_heavy(sw.ontology_vs_gate, child, ontology_version, bool(xsd), size=1 << 30)
        except (ValueError, sw.SceneError) as e:
            return _err(e)
        except KeyError as e:
            return _err(f"Unknown node type: {e}")
        res["instrument"] = sw.instrument(res["ontology_version"])
        res["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return json.dumps(res, indent=1)
