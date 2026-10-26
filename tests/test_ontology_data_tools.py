"""Ontology guard, paged query, where_can_place and claim ledger tools."""

import asyncio
import json
import time

import pytest
from mcp.server.fastmcp import FastMCP

from tools import claims as claim_tools
from tools import semantics
from x3d_utils import claims, placement
from x3d_utils.ontology import get_ontology
from x3d_utils.ontology_guard import (
    QueryRefused, QueryTimeout, check_node_type, guard_sparql, run_bounded,
)


@pytest.fixture(scope="module")
def mcp():
    m = FastMCP("t")
    semantics.register(m)
    claim_tools.register(m)
    return m


def call(mcp, name, args):
    result = asyncio.run(mcp.call_tool(name, args))
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


# guard ---------------------------------------------------------------

@pytest.mark.parametrize("q", [
    "SELECT * WHERE { SERVICE <http://127.0.0.1:9/> { ?s ?p ?o } }",
    "SELECT * WHERE { { SELECT * WHERE { SERVICE SILENT <http://127.0.0.1:9/> { ?s ?p ?o } } } }",
    "SELECT * FROM <http://127.0.0.1:9/g> WHERE { ?s ?p ?o }",
    "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }",
    "DESCRIBE <http://x>",
    "",
])
def test_guard_refuses(q):
    with pytest.raises(QueryRefused):
        guard_sparql(q)


@pytest.mark.parametrize("q", [
    "ASK { x3d:Sphere rdfs:subClassOf x3d:X3DGeometryNode }",
    "SELECT ?c WHERE { ?c rdfs:subClassOf x3d:X3DNode } LIMIT 3",
])
def test_guard_allows(q):
    from x3d_utils.ontology import PREFIXES
    guard_sparql(PREFIXES + q)


def test_guard_length_limit(monkeypatch):
    monkeypatch.setenv("X3D_MCP_SPARQL_MAX_CHARS", "50")
    with pytest.raises(QueryRefused, match="characters"):
        guard_sparql("SELECT * WHERE { ?s ?p ?o } " + "#" * 100)


def test_node_type_injection_refused():
    for bad in ["Sphere . } UNION { SERVICE <http://x/> {?s ?p ?o}", "Sphere rdfs:subClassOf* ?r", "", "A B", "1x"]:
        with pytest.raises(QueryRefused):
            check_node_type(bad)
        with pytest.raises(QueryRefused):
            get_ontology().node_parents(bad)
    assert get_ontology().node_parents("Sphere")


def test_node_parents_tool_reports_refusal(mcp):
    out = call(mcp, "node_parents", {"node_type": "Sphere } SERVICE <http://x/> {"})
    assert "refused" in out["error"]


def test_run_bounded_kills_and_returns():
    t = time.time()
    with pytest.raises(QueryTimeout):
        run_bounded(time.sleep, 30, timeout=0.5)
    assert time.time() - t < 5
    assert run_bounded(sum, [1, 2, 3]) == 6
    with pytest.raises(RuntimeError, match="ZeroDivisionError"):
        run_bounded(lambda: 1 / 0)


def test_query_ontology_default_output_unchanged(mcp):
    out = call(mcp, "query_ontology", {"sparql": "SELECT ?c WHERE { ?c rdfs:subClassOf x3d:X3DGeometryNode } LIMIT 5"})
    assert out["count"] == 5 and "truncated" not in out and "rows" in out


def test_query_ontology_refuses_service(mcp):
    out = call(mcp, "query_ontology", {"sparql": "SELECT * WHERE { SERVICE <http://127.0.0.1:9/> { ?s ?p ?o } }"})
    assert "refused" in out["error"] and "SERVICE" in out["error"]


def test_query_ontology_row_cap(mcp, monkeypatch):
    monkeypatch.setenv("X3D_MCP_ONTOLOGY_MAX_ROWS", "10")
    out = call(mcp, "query_ontology", {"sparql": "SELECT ?s ?p ?o WHERE { ?s ?p ?o }"})
    assert out["truncated"] is True and out["count"] == 10 and out["total"] > 10


def test_query_ontology_timeout(mcp, monkeypatch):
    monkeypatch.setenv("X3D_MCP_SPARQL_TIMEOUT", "0.3")
    q = "SELECT * WHERE { ?a ?p1 ?b . ?c ?p2 ?d . ?e ?p3 ?f . ?g ?p4 ?h }"
    out = call(mcp, "query_ontology", {"sparql": q})
    assert "stopped" in out["error"]


# paging ----------------------------------------------------------------

def test_query_page_walks_all_rows_in_order(mcp):
    q = "SELECT ?c WHERE { ?c rdfs:subClassOf x3d:X3DGeometryNode }"
    first = call(mcp, "query_ontology_page", {"sparql": q, "limit": 10})
    assert first["offset"] == 0 and len(first["rows"]) == 10 and first["next_offset"] == 10
    seen, off = [], 0
    while off is not None:
        p = call(mcp, "query_ontology_page", {"sparql": q, "offset": off, "limit": 10})
        seen += [r["c"] for r in p["rows"]]
        off = p["next_offset"]
    assert len(seen) == first["total"] and seen == sorted(seen, key=lambda c: json.dumps({"c": c}))
    again = call(mcp, "query_ontology_page", {"sparql": q, "limit": 10})
    assert again["page_sha256"] == first["page_sha256"]


def test_query_page_folds_blank_nodes(mcp):
    q = "SELECT ?b WHERE { ?s ?p ?b . FILTER(isBlank(?b)) }"
    out = call(mcp, "query_ontology_page", {"sparql": q, "limit": 5})
    assert out["total"] > 0 and all(r["b"] == "_:blank" for r in out["rows"])


def test_query_page_refuses(mcp):
    assert "refused" in call(mcp, "query_ontology_page", {"sparql": "DESCRIBE <http://x>"})["error"]


# placement -------------------------------------------------------------

def test_where_can_place_sphere_and_material(mcp):
    sph = call(mcp, "where_can_place", {"child": "Sphere"})
    assert ("Shape", "geometry") in {(r["parent"], r["field"]) for r in sph["rows"]}
    mat = call(mcp, "where_can_place", {"child": "Material", "limit": 100})
    assert ("Appearance", "material") in {(r["parent"], r["field"]) for r in mat["rows"]}
    assert all(r["fieldType"] in ("SFNode", "MFNode") for r in sph["rows"])
    assert sph["table_sha256"] == placement.get_table()["table_sha256"]


def test_where_can_place_paging_and_errors(mcp):
    p1 = call(mcp, "where_can_place", {"child": "Shape", "limit": 5})
    assert len(p1["rows"]) == 5 and p1["next_offset"] == 5
    p2 = call(mcp, "where_can_place", {"child": "Shape", "offset": 5, "limit": 500})
    assert p1["total"] == 5 + len(p2["rows"]) and p2["next_offset"] is None
    assert "Unknown" in call(mcp, "where_can_place", {"child": "NoSuchNode"})["error"]


def test_placement_excludes_event_fields():
    rows = placement.where_can_place("Shape", 0, 500)["rows"]
    assert not any(r["field"] in ("addChildren", "removeChildren") for r in rows)


def test_placement_table_is_stable():
    assert placement._build()["table_sha256"] == placement.get_table()["table_sha256"]


def test_ontology_resources_registered(mcp):
    assert "x3d-ontology://turtle" in [str(r.uri) for r in asyncio.run(mcp.list_resources())]
    assert any("placement" in str(t.uriTemplate) for t in asyncio.run(mcp.list_resource_templates()))
    turtle = asyncio.run(mcp.read_resource("x3d-ontology://turtle"))
    assert "Sphere" in list(turtle)[0].content[:2_000_000]


# claims ----------------------------------------------------------------

def test_claim_record_is_canonical_and_hashed():
    r = claims.make_record("a claim", "Confirmed", "s", {"validator": "abc"}, "e")
    assert r["sha256"] == claims.record_hash(r)
    assert claims.canonical(r) == claims.canonical(json.loads(claims.canonical(r)))


def test_claim_bad_grade():
    with pytest.raises(claims.ClaimError):
        claims.make_record("x", "Certain")
    with pytest.raises(claims.ClaimError):
        claims.make_record("", "Likely")


def test_chain_detects_tampering():
    text = ""
    for i in range(4):
        text, _ = claims.append(text, {"claim": f"c{i}", "grade": "Likely"})
    rows = claims.parse_ledger(text)
    assert claims.verify(rows, chain=True)["ok"]
    cut = rows[:1] + rows[2:]
    bad = claims.verify(cut, chain=True)
    assert not bad["ok"] and bad["problems"][0]["index"] == 1
    rows[2]["claim"] = "changed"
    assert not claims.verify(rows)["ok"]


def test_page_columns_and_hash_options():
    text = ""
    for i in range(30):
        text, _ = claims.append(text, {"claim": f"claim {i}", "grade": "Confirmed", "subject": "s"})
    rows = claims.parse_ledger(text)
    full = len(json.dumps(claims.page(rows, 0, 20)["rows"]))
    short = len(json.dumps(claims.page(rows, 0, 20, hash_chars=12)["rows"]))
    none = len(json.dumps(claims.page(rows, 0, 20, drop_hashes=True)["rows"]))
    assert none < short < full
    p = claims.page(rows, 0, 5, columns="claim,grade")
    assert p["columns"] == ["claim", "grade"] and p["next_offset"] == 5
    with pytest.raises(claims.ClaimError):
        claims.page(rows, 0, 5, columns="nope")


def test_claim_tools_round_trip(mcp):
    a = call(mcp, "claim_append", {"claim": "first", "grade": "Confirmed", "instrument": '{"validator": "688174f"}'})
    b = call(mcp, "claim_append", {"claim": "second", "grade": "Likely", "ledger": a["ledger"]})
    assert b["records"] == 2 and b["record"]["prev"] == a["head"]
    assert call(mcp, "claims_verify", {"ledger": b["ledger"]})["ok"] is True
    pg = call(mcp, "claims_page", {"ledger": b["ledger"], "drop_hashes": True})
    assert pg["total"] == 2 and "sha256" not in pg["columns"]
    assert "error" in call(mcp, "claim_append", {"claim": "x", "grade": "Sure"})
    assert "error" in call(mcp, "claims_page", {"ledger": "not json"})
