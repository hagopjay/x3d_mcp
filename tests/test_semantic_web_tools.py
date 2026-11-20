"""Semantic-web tools: scene_to_rdf fidelity, sparql_scene guard and paging, shacl_validate on clean and
faulted scenes, rdf_infer, ontology_vs_gate, the instrument object, the prompt and INSTRUCTIONS.

Fixtures and goldens: tests/fixtures/scene_rdf (README.md there says where each comes from).
"""

import asyncio
import hashlib
import json
import os
import shutil
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import pytest
import rdflib
from mcp.server.fastmcp import FastMCP
from rdflib.compare import isomorphic

from tools import semantic_web as sw_tools
from x3d_utils import semantic_web as sw
from x3d_utils.ontology import (
    ONTOLOGY_41_PATH, ONTOLOGY_PATH, get_ontology, ontology_sha256, ontology_version_for,
)
from x3d_utils.ontology_guard import QueryRefused
from x3d_utils.scene_rdf import UNNAMED_BASE, scene_to_rdf

FIX = Path(__file__).resolve().parent / "fixtures" / "scene_rdf"
CONSORTIUM = ["Altar", "HelloWorld", "HelloX3dAuthorsAnimationChain", "MaterialModulator",
              "MetadataNodeExamplesX3D4"]
CLEAN = CONSORTIUM + ["LabScene41", "SwitchOrder"]
FAULTED = ["faulted/HelloWorldFaulted", "faulted/HelloWorldBoxChild", "faulted/LabScene41Faulted"]
ONTOLOGY_41_SHA256 = "057f1ba083a636aa50332f87fda0bfa76793876c2b14c86f51dd6db55639f2d8"

GEOMETRY_QUERY = "SELECT ?n ?t WHERE { ?n a ?t . ?t rdfs:subClassOf* x3do:X3DGeometryNode }"


def read(stem: str) -> str:
    return (FIX / f"{stem}.x3d").read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def converted(stem: str) -> sw.SceneGraph:
    return sw.convert(read(stem))


@lru_cache(maxsize=None)
def validated(stem: str) -> dict:
    return sw.shacl(converted(stem))


@pytest.fixture(scope="module")
def mcp():
    m = FastMCP("t")
    sw_tools.register(m)
    return m


def call(mcp, name, args):
    result = asyncio.run(mcp.call_tool(name, args))
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


# scene_to_rdf: the same graph as X3dToTurtle.xslt -------------------------------------------------

def test_fixtures_match_manifest():
    manifest = json.loads((FIX / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["files"]) == len(CLEAN) + len(FAULTED)
    for rel, entry in manifest["files"].items():
        x3d = FIX / rel
        assert hashlib.sha256(x3d.read_bytes()).hexdigest() == entry["x3d_sha256"], rel
        golden = x3d.with_name(entry["golden"])
        assert hashlib.sha256(golden.read_bytes()).hexdigest() == entry["golden_sha256"], rel
    assert "Redistribution and use" in (FIX / "LICENSE.txt").read_text(encoding="utf-8")


def list_canonical(g: rdflib.Graph):
    """The graph with each rdf:List written as a tuple of its items, or None when it has a blank node
    that is not a list cell. For such graphs equal canonical sets (and equal sizes) mean isomorphic."""
    cells = {s for s in g.subjects() if isinstance(s, rdflib.BNode)}
    for s in cells:
        if {p for p in g.predicates(s)} != {rdflib.RDF.first, rdflib.RDF.rest}:
            return None

    def items(node):
        out, seen = [], set()
        while node != rdflib.RDF.nil:
            if node in seen or node not in cells:
                return ("broken list",)
            seen.add(node)
            out.append(g.value(node, rdflib.RDF.first))
            node = g.value(node, rdflib.RDF.rest)
        return tuple(out)

    return {(s, p, items(o) if isinstance(o, rdflib.BNode) else o) for s, p, o in g if not isinstance(s, rdflib.BNode)}


def same_graph(a: rdflib.Graph, b: rdflib.Graph) -> bool:
    """Isomorphism: exact list-canonical comparison when the only blank nodes are list cells (linear time;
    rdflib's general algorithm takes minutes on Altar's 3992 triples), rdflib isomorphic() otherwise."""
    if len(a) != len(b):
        return False
    ca, cb = list_canonical(a), list_canonical(b)
    if ca is not None and cb is not None:
        return ca == cb
    return isomorphic(a, b)


@pytest.mark.parametrize("stem", CLEAN + FAULTED)
def test_isomorphic_to_stylesheet_output(stem):
    res = scene_to_rdf(read(stem))
    golden = rdflib.Graph().parse(FIX / f"{stem}.golden.ttl", format="turtle")
    assert len(res.graph) == len(golden)
    assert same_graph(res.graph, golden)
    if len(golden) < 1000:              # rdflib's own algorithm agrees where it is quick
        assert isomorphic(res.graph, golden)
    # the Turtle text the tool returns is the same graph again
    assert same_graph(rdflib.Graph().parse(data=res.turtle, format="turtle"), golden)


def test_comparison_detects_a_changed_list_item():
    golden = rdflib.Graph().parse(FIX / "LabScene41.golden.ttl", format="turtle")
    changed = rdflib.Graph().parse(FIX / "faulted" / "LabScene41Faulted.golden.ttl", format="turtle")
    assert list_canonical(golden) is not None and not same_graph(golden, changed)
    edited = rdflib.Graph()
    edited += golden
    cell = next(s for s, o in golden.subject_objects(rdflib.RDF.first) if o == rdflib.Literal("0.6", datatype=rdflib.XSD.decimal))
    edited.set((cell, rdflib.RDF.first, rdflib.Literal("0.7", datatype=rdflib.XSD.decimal)))
    assert len(edited) == len(golden) and not same_graph(golden, edited)


def test_names_and_base():
    hello = converted("HelloWorld")
    assert hello.base == "https://X3dGraphics.com/examples/X3dForWebAuthors/Chapter01TechnicalOverview/HelloWorld.ttl#"
    g, x3do = hello.graph, rdflib.Namespace(sw.X3D_41_NS)
    use = rdflib.URIRef(hello.base + "MaterialLightBlue-USE-1")
    assert (use, rdflib.OWL.sameAs, rdflib.URIRef(hello.base + "MaterialLightBlue")) in g
    assert (use, rdflib.RDF.type, x3do.Material) in g
    assert converted("SwitchOrder").base == UNNAMED_BASE
    assert hello.graph_id == hello.sha256[:16]
    assert hello.sha256 == hashlib.sha256(hello.turtle.encode("utf-8")).hexdigest()


def test_dtd_defaults_by_version():
    # 3.3 documents take the 3.3 DTD defaults; ontology joins pick 4.0 for 3.x/4.0 and 4.1 for 4.1 / none
    assert converted("HelloWorld").dtd == "3.3"
    assert converted("LabScene41").dtd == "4.1"
    assert [ontology_version_for(v) for v in ("3.3", "4.0", "4.1", "", "4.2")] == ["4.0", "4.0", "4.1", "4.1", "4.1"]


EDGE = """<X3D profile='Full' version='4.0'><head><meta name='identifier' content='https://example.org/e.x3d'/></head>
<Scene><RigidBodyCollection>
<SingleAxisHingeJoint DEF='H1' containerField='joints' maxAngle='0.3'/>
<SingleAxisHingeJoint DEF='H2' containerField='joints' maxAngle='3.141593'/></RigidBodyCollection>
<Shape DEF='A'><Box/></Shape><Shape DEF='B' xmlns:q='urn:q'><q:Box/></Shape>
<GaussianSplats DEF='G'/><Group DEF='N' xmlns:q='urn:q' q:USE='A'/></Scene></X3D>"""


def test_converter_edge_rules():
    """Rules of the stylesheet that the Consortium fixtures do not reach (each checked against Saxon in the
    corpus sweep): a default-value test compares the attribute's string value; the DTD declares elements by
    qualified name; an element the version's DTD lacks is named by its tag; @USE has no namespace."""
    res = scene_to_rdf(EDGE)
    g, x3do, base = res.graph, rdflib.Namespace(sw.X3D_41_NS), "https://example.org/e.ttl#"
    node = lambda n: rdflib.URIRef(base + n)
    assert {str(o) for o in g.objects(node("H1"), x3do.maxAngle)} == {"0.3"}
    assert not list(g.objects(node("H2"), x3do.maxAngle))          # the default (pi) is not written
    assert list(g.objects(node("A"), x3do.hasGeometry)) and list(g.objects(node("B"), x3do.hasBox))
    assert (node("Scene"), x3do.hasGaussianSplats, node("G")) in g
    assert (node("N"), x3do.USE, rdflib.Literal("A")) in g and not list(g.objects(node("N"), rdflib.OWL.sameAs))
    assert any("q:Box is not declared" in w for w in res.warnings)
    assert any("GaussianSplats is not in the X3D 4.0 DTD" in w and "'children'" in w for w in res.warnings)


@pytest.mark.skipif(not (os.environ.get("X3D_MCP_X3DTOTURTLE_XSLT") and os.environ.get("X3D_MCP_X3D_DTD_DIR")),
                    reason="set X3D_MCP_X3DTOTURTLE_XSLT and X3D_MCP_X3D_DTD_DIR to regenerate the goldens")
def test_goldens_regenerate_identically(tmp_path):
    pytest.importorskip("saxonche")
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "make_scene_rdf_goldens", Path(__file__).resolve().parent.parent / "evaluation" / "make_scene_rdf_goldens.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    work = tmp_path / "scene_rdf"
    shutil.copytree(FIX, work, ignore=shutil.ignore_patterns("*.golden.ttl", "manifest.json"))
    assert gen.main([os.environ["X3D_MCP_X3DTOTURTLE_XSLT"], os.environ["X3D_MCP_X3D_DTD_DIR"], "--out", str(work)]) == 0
    for stem in CLEAN + FAULTED:
        assert (work / f"{stem}.golden.ttl").read_bytes() == (FIX / f"{stem}.golden.ttl").read_bytes(), stem


def test_scene_to_rdf_tool(mcp):
    out = call(mcp, "scene_to_rdf", {"content": read("LabScene41"), "include_ontology": True})
    assert out["ok"] and out["triples"] == 230 and out["graph_id"] == converted("LabScene41").graph_id
    assert out["turtle"].startswith("@prefix")
    assert out["ontology_classes"]["classes"] > 0
    summary = call(mcp, "scene_to_rdf", {"content": read("LabScene41"), "return_mode": "summary"})
    assert "turtle" not in summary and summary["text_bytes"] > 0
    nt = call(mcp, "scene_to_rdf", {"content": read("SwitchOrder"), "format": "ntriples"})
    assert same_graph(rdflib.Graph().parse(data=nt["ntriples"], format="nt"), converted("SwitchOrder").graph)


def test_scene_to_rdf_refuses_non_x3d_and_caps_triples(mcp, monkeypatch):
    assert call(mcp, "scene_to_rdf", {"content": "<html/>"})["ok"] is False
    monkeypatch.setenv(sw.MAX_TRIPLES_ENV, "100")
    out = call(mcp, "scene_to_rdf", {"content": read("HelloWorld")})
    assert out["ok"] is False and sw.MAX_TRIPLES_ENV in out["error"]


def test_session_cache():
    class Session:
        pass

    sessions = [Session(), Session()]
    a, b = SimpleNamespace(session=sessions[0]), SimpleNamespace(session=sessions[1])
    assert sw_tools._cache(a) is sw_tools._cache(a)
    assert sw_tools._cache(a) is not sw_tools._cache(b)
    assert sw_tools._cache(None) is sw_tools._fallback
    cache = sw.GraphCache(keep=2)
    for stem in ("HelloWorld", "LabScene41", "SwitchOrder"):
        cache.put(converted(stem))
    assert len(cache) == 2 and cache.get(converted("HelloWorld").graph_id) is None
    assert cache.get("sha256:" + converted("SwitchOrder").sha256) is converted("SwitchOrder")
    del sessions


# sparql_scene ------------------------------------------------------------------------------------

def test_sparql_rows_and_graph_id(mcp):
    first = call(mcp, "sparql_scene", {"content": read("LabScene41"), "sparql": GEOMETRY_QUERY})
    assert first["ok"] and first["total"] == 3
    assert sorted(r["t"] for r in first["rows"]) == ["x3do:Box", "x3do:Cone", "x3do:Sphere"]
    again = call(mcp, "sparql_scene", {"graph_id": first["graph_id"], "sparql": GEOMETRY_QUERY.replace("x3do:", "x3d:")})
    assert again["rows"] == first["rows"] and "converted" not in again
    missing = call(mcp, "sparql_scene", {"graph_id": "0" * 16, "sparql": GEOMETRY_QUERY})
    assert missing["ok"] is False and "no scene graph" in missing["error"]


def test_sparql_ask_and_ontology_join():
    sg = converted("LabScene41")
    assert sw.sparql(sg, "ASK { :Orb a x3do:Transform }")["rows"] == [{"ask": True}]
    no_onto = sw.sparql(sg, GEOMETRY_QUERY, include_ontology=False)
    assert no_onto["total"] == 0          # without the ontology there is no subClassOf to follow


@pytest.mark.parametrize("q", [
    "INSERT DATA { :a :b :c }",
    "DELETE WHERE { ?s ?p ?o }",
    "LOAD <http://127.0.0.1:9/x.ttl>",
    "CLEAR ALL",
    "SELECT * WHERE { SERVICE <http://127.0.0.1:9/> { ?s ?p ?o } }",
    "SELECT * FROM <http://127.0.0.1:9/g> WHERE { ?s ?p ?o }",
    "SELECT * FROM NAMED <http://127.0.0.1:9/g> WHERE { GRAPH ?g { ?s ?p ?o } }",
    "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }",
    "DESCRIBE :Orb",
])
def test_sparql_guard_refuses(mcp, q):
    out = call(mcp, "sparql_scene", {"content": read("SwitchOrder"), "sparql": q})
    assert out["ok"] is False and out["error"].startswith("Query refused")
    with pytest.raises(QueryRefused):
        sw.sparql(converted("SwitchOrder"), q)


def test_sparql_paging(mcp):
    q = "SELECT ?s ?p ?o WHERE { ?s ?p ?o }"
    gid = converted("LabScene41").graph_id
    call(mcp, "scene_to_rdf", {"content": read("LabScene41"), "return_mode": "summary"})
    full = call(mcp, "sparql_scene", {"graph_id": gid, "sparql": q, "include_ontology": False, "limit": 500})
    assert full["total"] == 230 and len(full["rows"]) == 230 and full["next_offset"] is None
    rows, offset, hashes = [], 0, []
    while offset is not None:
        page = call(mcp, "sparql_scene", {"graph_id": gid, "sparql": q, "include_ontology": False,
                                          "offset": offset, "limit": 50})
        rows += page["rows"]
        hashes.append(page["page_sha256"])
        offset = page["next_offset"]
    assert rows == full["rows"] and len(hashes) == 5
    repeat = call(mcp, "sparql_scene", {"graph_id": gid, "sparql": q, "include_ontology": False, "offset": 50,
                                        "limit": 50})
    assert repeat["page_sha256"] == hashes[1]
    past = call(mcp, "sparql_scene", {"graph_id": gid, "sparql": q, "include_ontology": False, "offset": 1000})
    assert past["rows"] == [] and past["next_offset"] is None


# shacl_validate ----------------------------------------------------------------------------------

@pytest.mark.parametrize("stem", CLEAN)
def test_shacl_no_false_positives(stem):
    res = validated(stem)
    assert res["counts"]["Violation"] == 0 and res["conforms"]
    # the only warning a clean scene may get is S4, and only when it truly has no Viewpoint
    has_viewpoint = "<Viewpoint" in read(stem)
    assert res["by_check"] == ({} if has_viewpoint else {"no-viewpoint": 1})


EXPECTED_FAULTS = {
    "faulted/HelloWorldFaulted": {
        "colour-out-of-range": ("MaterialLightBlue", "Violation"),
        "field-not-in-domain": (None, "Violation"),
        "no-viewpoint": (None, "Warning"),
        "route-missing-from-node": (None, "Violation"),
        "shape-no-geometry": (None, "Warning"),
        "use-type-mismatch": ("ImageCloudlessEarth", "Violation"),
    },
    "faulted/HelloWorldBoxChild": {"children-not-child-node": (None, "Violation")},
    "faulted/LabScene41Faulted": {
        "colour-out-of-range": ("Brass", "Violation"),
        "tuple-count": ("Blue", "Violation"),
        "value-out-of-range": ("Blue", "Violation"),
    },
}


@pytest.mark.parametrize("stem", FAULTED)
def test_shacl_one_result_per_planted_fault(stem):
    res = validated(stem)
    expected = EXPECTED_FAULTS[stem]
    assert res["by_check"] == {check: 1 for check in expected}
    for r in res["results"]:
        node_def, severity = expected[r["check_id"]]
        assert r["severity"] == severity
        assert r["def"] == node_def
        assert r["message"] and r["fix_hint"]
    assert res["conforms"] is False


def test_shacl_fault_details():
    by = {r["check_id"]: r for r in validated("faulted/HelloWorldFaulted")["results"]}
    assert by["colour-out-of-range"]["value"] == "1.5"
    assert "Ghost" in by["route-missing-from-node"]["message"]
    assert "geometry" in by["field-not-in-domain"]["message"]
    assert "ImageTexture" in by["use-type-mismatch"]["message"]
    by41 = {r["check_id"]: r for r in validated("faulted/LabScene41Faulted")["results"]}
    assert by41["value-out-of-range"]["path"] == "x3do:transparency" and by41["value-out-of-range"]["value"] == "1.2"
    assert "2 components" in by41["tuple-count"]["message"]


def test_shacl_tool_and_extra_shapes(mcp):
    extra = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix x3do: <https://www.web3d.org/specifications/X3dOntology4.1#> .
@prefix ex: <urn:example:> .
ex:TransformNamed a sh:NodeShape ; sh:targetClass x3do:Transform ;
  sh:property [ sh:path x3do:DEF ; sh:minCount 1 ; sh:message "Transform without a DEF" ] .
"""
    out = call(mcp, "shacl_validate", {"content": read("LabScene41"), "extra_shapes_ttl": extra})
    assert out["ok"] and out["counts"] == {"Violation": 1, "Warning": 0, "Info": 0}
    assert out["results"][0]["message"] == "Transform without a DEF"
    only = call(mcp, "shacl_validate", {"graph_id": out["graph_id"], "shapes": "none", "extra_shapes_ttl": extra})
    assert only["by_check"] == {"TransformNamed": 1} and only["results"][0]["focus"] == ":Transform_2_5"
    assert out["instrument"]["shapes"]["sha256"] == hashlib.sha256(sw.SHAPES["x3d-core"].read_bytes()).hexdigest()


@pytest.mark.parametrize("ttl, needle", [
    ("@prefix owl: <http://www.w3.org/2002/07/owl#> . <urn:s> owl:imports <http://127.0.0.1:9/x.ttl> .", "imports"),
    ("""@prefix sh: <http://www.w3.org/ns/shacl#> .
<urn:s> a sh:NodeShape ; sh:targetNode <urn:n> ;
  sh:sparql [ sh:select "SELECT $this WHERE { SERVICE <http://127.0.0.1:9/> { $this ?p ?o } }" ] .""", "SERVICE"),
])
def test_shacl_extra_shapes_guarded(mcp, ttl, needle):
    out = call(mcp, "shacl_validate", {"content": read("SwitchOrder"), "extra_shapes_ttl": ttl})
    assert out["ok"] is False and needle in out["error"]


# rdf_infer ---------------------------------------------------------------------------------------

def test_rdf_infer_subclass_default(mcp):
    out = call(mcp, "rdf_infer", {"content": read("LabScene41")})
    assert out["ok"] and out["profile"] == "subclass" and out["ontology_version"] == "4.1"
    assert out["added"] == 72 and out["type_triples_added"] == 72
    assert out["types_per_node"] == {"min": 3, "median": 4.0, "max": 6}
    assert out["warnings"] == []
    assert converted("LabScene41").triples == 230 and len(converted("LabScene41").graph) == 230


def test_rdf_infer_rdfs_reports_type_counts():
    res = sw.infer(converted("LabScene41"), profile="rdfs", budget=120)
    worst = res["per_node_type_counts"][0]
    assert worst["after"] > 100 > worst["explicit"]
    text = res["warnings"][0]
    assert "rdfs:domain" in text and "profile='subclass'" in text
    for word in ("bug", "wrong", "broken", "flaw", "error"):
        assert word not in text.lower()


def test_rdf_infer_refuses_unknown_profile(mcp):
    out = call(mcp, "rdf_infer", {"content": read("SwitchOrder"), "profile": "owl-full"})
    assert out["ok"] is False


# ontology_vs_gate --------------------------------------------------------------------------------

def test_ontology_vs_gate_counts(mcp):
    out = call(mcp, "ontology_vs_gate", {"child": "ImageTexture", "xsd": False})
    c = out["counts"]
    assert (c["map_rows"], c["map_parents"], c["gate_parent_field_pairs"], c["gate_parents"]) == (85, 62, 36, 14)
    assert c["both"] + c["map_only"] == c["map_parents"] and c["both"] + c["gate_only"] == c["gate_parents"]
    assert len(out["counting_rules"]) >= 4 and any("de-duplication" in r for r in out["counting_rules"])
    assert "xsd" not in c


def test_ontology_vs_gate_xsd_verdicts(mcp):
    out = call(mcp, "ontology_vs_gate", {"child": "Box"})
    xsd = out["counts"]["xsd"]
    assert set(k.split(":")[1] for k in xsd) <= {"A", "R", "I", "not-run"}
    shape = next(p for p in out["parents"] if p["parent"] == "Shape")
    assert shape["in_map"] and shape["in_gate"] and shape["xsd"]["geometry"]["verdict"] == "A"


def test_ontology_vs_gate_refuses_bad_names(mcp):
    assert call(mcp, "ontology_vs_gate", {"child": "NotANode"})["ok"] is False
    assert call(mcp, "ontology_vs_gate", {"child": "Box } UNION { ?s ?p ?o"})["ok"] is False


# ontology releases: defaults unchanged ---------------------------------------------------------

def test_bundled_ontologies():
    assert ontology_sha256("4.1") == ONTOLOGY_41_SHA256
    assert get_ontology().path == ONTOLOGY_PATH and get_ontology("4.1").path == ONTOLOGY_41_PATH
    default = get_ontology().node_parents("ImageTexture")
    assert len(default) == 62 and len({r["parent"] for r in default}) == 40
    v41 = get_ontology("4.1").node_parents("ImageTexture")
    assert len(v41) == 85 and len({r["parent"] for r in v41}) == 62


# instrument --------------------------------------------------------------------------------------

def test_instrument_on_every_answer(mcp):
    gid = call(mcp, "scene_to_rdf", {"content": read("SwitchOrder"), "return_mode": "summary"})["graph_id"]
    answers = [
        call(mcp, "scene_to_rdf", {"content": read("SwitchOrder"), "return_mode": "summary"}),
        call(mcp, "sparql_scene", {"graph_id": gid, "sparql": "ASK { ?s ?p ?o }"}),
        call(mcp, "shacl_validate", {"graph_id": gid}),
        call(mcp, "rdf_infer", {"graph_id": gid}),
        call(mcp, "ontology_vs_gate", {"child": "Sphere", "xsd": False}),
    ]
    import pyshacl
    for out in answers:
        ins = out["instrument"]
        assert ins["x3d_mcp_commit"] and "x3d_mcp_dirty" in ins
        assert ins["ontology"]["sha256"] == ontology_sha256(ins["ontology"]["version"])
        assert ins["x3duom_sha256"] == hashlib.sha256(sw.X3DUOM_PATH.read_bytes()).hexdigest()
        assert ins["converter"]["tables_sha256"] and ins["converter"]["stylesheet_sha256"]
        assert ins["rdflib"] == rdflib.__version__ and ins["pyshacl"] == pyshacl.__version__
        assert len(ins["shapes"]["sha256"]) == 64


def test_instrument_commit_pin(monkeypatch):
    monkeypatch.setenv("X3D_MCP_COMMIT", "abc123")
    sw._commit.cache_clear()
    try:
        assert sw.instrument("4.1")["x3d_mcp_commit"] == "abc123"
    finally:
        monkeypatch.delenv("X3D_MCP_COMMIT")
        sw._commit.cache_clear()
    ins = sw.instrument("4.1")
    assert ins["x3d_mcp_commit"] != "abc123"
    assert ins["ontology"] == {"version": "4.1", "file": "X3dOntology4.1.ttl", "sha256": ONTOLOGY_41_SHA256}


# prompt and INSTRUCTIONS -------------------------------------------------------------------------

def test_server_registers_tools_and_steers_to_them():
    import server
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    tools = ["scene_to_rdf", "sparql_scene", "shacl_validate", "rdf_infer", "ontology_vs_gate"]
    assert set(tools) <= names
    for name in tools + ["semantic_audit", "validate_x3d", "validate_semantic", "claim_append"]:
        assert name in server.INSTRUCTIONS, name
    prompts = {p.name for p in asyncio.run(server.mcp.list_prompts())}
    assert "semantic_audit" in prompts
    body = asyncio.run(server.mcp.get_prompt("semantic_audit", {}))
    text = "\n".join(m.content.text for m in body.messages)
    for name in ["scene_to_rdf", "sparql_scene", "shacl_validate", "validate_x3d", "validate_semantic", "claim_append"]:
        assert name in text, name


def test_semantic_audit_queries_run():
    """The example queries the prompt gives the model parse and run on a real scene."""
    from tools.prompts import register
    m = FastMCP("p")
    register(m)
    body = m._prompt_manager._prompts["semantic_audit"].fn()
    queries = [chunk.split("`")[0] for chunk in body.split("`SELECT")[1:]]
    assert len(queries) >= 4
    sg = converted("HelloWorld")
    for q in queries:
        out = sw.sparql(sg, "SELECT" + q)
        assert out["total"] >= 0


# data_ttl: domain records joined to the scene --------------------------------------------------------

CELL = """<?xml version="1.0" encoding="UTF-8"?>
<X3D profile='Interchange' version='4.0'>
 <head><meta name='identifier' content='https://example.org/cell/scene.x3d'/></head>
 <Scene>
  <Transform DEF='RobotBase'><Shape><Box size='0.5 0.5 0.5'/></Shape></Transform>
  <Transform DEF='Station3' translation='2.5 0 0.4'><Shape><Box/></Shape></Transform>
 </Scene>
</X3D>"""
CELL_DATA = """@prefix ex: <https://example.org/cell/record#> .
@prefix sc: <https://example.org/cell/scene.ttl#> .
ex:Robot ex:depictedBy sc:RobotBase ; ex:maxReach 2.1 ; ex:servesStation ex:S3 .
ex:S3 ex:depictedBy sc:Station3 .
"""
REACH_SHAPE = """@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <https://example.org/cell/record#> .
ex:ReachShape a sh:NodeShape ; sh:targetSubjectsOf ex:servesStation ;
  sh:message "station beyond the robot's rated reach" ;
  sh:sparql [ sh:select \"\"\"
    PREFIX ex: <https://example.org/cell/record#>
    PREFIX x3do: <https://www.web3d.org/specifications/X3dOntology4.1#>
    PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
    SELECT $this ?value WHERE {
      $this ex:maxReach ?r ; ex:servesStation/ex:depictedBy ?value .
      ?value x3do:translation ?L . ?L rdf:first ?x ; rdf:rest/rdf:rest/rdf:first ?z .
      FILTER(?x * ?x + ?z * ?z > ?r * ?r)
    }\"\"\" ] .
"""


def test_sparql_scene_joins_data_ttl(mcp):
    q = ("PREFIX ex: <https://example.org/cell/record#> SELECT ?n ?x WHERE { ?a ex:depictedBy ?n . "
         "?n x3do:translation ?L . ?L rdf:first ?x }")
    alone = call(mcp, "sparql_scene", {"sparql": q, "content": CELL})
    assert alone["ok"] and alone["total"] == 0
    joined = call(mcp, "sparql_scene", {"sparql": q, "content": CELL, "data_ttl": CELL_DATA})
    assert joined["ok"] and joined["total"] == 1 and joined["rows"][0]["n"] == ":Station3"
    again = call(mcp, "sparql_scene", {"sparql": q, "graph_id": joined["graph_id"]})
    assert again["total"] == 0                     # the cache key includes the data, so no stale join


def test_shacl_validate_holds_scene_to_record(mcp):
    out = call(mcp, "shacl_validate", {"content": CELL, "shapes": "none", "extra_shapes_ttl": REACH_SHAPE,
                                       "data_ttl": CELL_DATA})
    assert out["ok"] and out["conforms"] is False and out["counts"]["Violation"] == 1
    assert out["results"][0]["value"] == ":Station3"
    near = CELL_DATA.replace("2.1", "3.0")
    assert call(mcp, "shacl_validate", {"content": CELL, "shapes": "none", "extra_shapes_ttl": REACH_SHAPE,
                                        "data_ttl": near})["conforms"] is True


def test_data_ttl_refused_when_not_turtle(mcp):
    out = call(mcp, "sparql_scene", {"sparql": "SELECT * WHERE { ?s ?p ?o } LIMIT 1", "content": CELL,
                                     "data_ttl": "this is not turtle"})
    assert out["ok"] is False and "data_ttl is not Turtle" in out["error"]


def test_sequence_path_rows_not_doubled_by_ontology_join():
    """rdflib's ReadOnlyGraphAggregate returned sequence-path rows twice; the joined graph must not."""
    q = "SELECT ?n ?m WHERE { ?n a x3do:Shape ; x3do:hasAppearance/x3do:hasMaterial ?m }"
    sg = converted("HelloWorld")
    with_ont = sw.sparql(sg, q, include_ontology=True)
    without = sw.sparql(sg, q, include_ontology=False)
    assert with_ont["total"] == without["total"] == 2


# x3d-cardinality: generated shapes, an SFNode field holds one node -------------------------------

def _doc41(body: str) -> str:
    return f"<?xml version='1.0' encoding='UTF-8'?>\n<X3D profile='Full' version='4.1'><Scene>{body}</Scene></X3D>"


def test_cardinality_shapes_file_is_what_the_generator_writes():
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "evaluation" / "build_cardinality_shapes.py"
    spec = importlib.util.spec_from_file_location("build_cardinality_shapes", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert sw.SHAPES["x3d-cardinality"].read_text(encoding="utf-8") == mod.build()


@pytest.mark.parametrize("body, node_type, path", [
    ("<Shape><Appearance><Material/><Material diffuseColor='1 0 0'/></Appearance><Box/></Shape>",
     "x3do:Appearance", "x3do:hasMaterial"),
    ("<Shape><Appearance><Material DEF='M'/><Material USE='M'/></Appearance><Box/></Shape>",
     "x3do:Appearance", "x3do:hasMaterial"),
    ("<Shape><IndexedFaceSet coordIndex='0 1 2 -1'><Coordinate point='0 0 0 1 0 0 0 1 0'/>"
     "<Coordinate point='0 0 0 1 0 0 0 1 1'/></IndexedFaceSet></Shape>", "x3do:IndexedFaceSet", "x3do:hasCoord"),
])
def test_cardinality_shapes_catch_what_core_passes(body, node_type, path):
    sg = sw.convert(_doc41(body))
    assert "sfnode-multiple" not in sw.shacl(sg)["by_check"]              # x3d-core alone: silent
    res = sw.shacl(sg, shapes="x3d-core,x3d-cardinality")
    hits = [r for r in res["results"] if r["check_id"] == "sfnode-multiple"]
    assert len(hits) == 1 and hits[0]["severity"] == "Violation"
    assert node_type in hits[0]["node_type"] and hits[0]["path"] == path and hits[0]["fix_hint"]


@pytest.mark.parametrize("stem", CLEAN)
def test_cardinality_shapes_no_false_positives(stem):
    res = sw.shacl(sw.convert(read(stem)), shapes="x3d-cardinality")
    assert res["results"] == []


def test_cardinality_shapes_through_the_tool(mcp):
    doc = _doc41("<Viewpoint/><Shape><Appearance><Material/><Material/></Appearance><Box/></Shape>")
    out = call(mcp, "shacl_validate", {"content": doc, "shapes": "x3d-core,x3d-cardinality"})
    assert out["ok"] and out["by_check"] == {"sfnode-multiple": 1}
    inst = out["instrument"]["shapes"]
    assert inst["name"] == "x3d-core,x3d-cardinality" and [s["name"] for s in inst["sets"]] == ["x3d-core", "x3d-cardinality"]
    default = call(mcp, "shacl_validate", {"content": doc})
    assert default["by_check"] == {} and default["instrument"]["shapes"]["name"] == "x3d-core"
