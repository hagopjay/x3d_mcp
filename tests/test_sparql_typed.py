"""sparql_scene result_format="typed" and ORDER BY (Evidence Edition findings F03 and F04)."""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tools import semantic_web as sw_tools

SCENE = ("<X3D profile='Interchange' version='4.0'><Scene>\n"
         "<Shape DEF='Pearl'><Appearance><Material DEF='Nacre' diffuseColor='1 1 1'/></Appearance><Sphere/></Shape>\n"
         "</Scene></X3D>")
BIG = "123456789012345678901234567890"
DATA = f"""
@prefix ex: <https://example.org/ns#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
ex:a ex:n 2 ; ex:label "" .
ex:b ex:n 11 ; ex:label "eleven"@en .
ex:c ex:n "11" .
ex:d ex:n "{BIG}"^^xsd:integer .
ex:e ex:part [ ex:k 1 ] , [ ex:k 2 ] .
"""
EX = "PREFIX ex: <https://example.org/ns#> "


def run(q, fmt="typed"):
    m = FastMCP("t")
    sw_tools.register(m)
    out = asyncio.run(m.call_tool("sparql_scene", {"content": SCENE, "sparql": EX + q, "data_ttl": DATA,
                                                   "result_format": fmt, "limit": 500}))
    content = out[0] if isinstance(out, tuple) else out
    return json.loads(content[0].text)


def test_order_by_is_kept_and_numbers_sort_as_numbers():
    res = run("SELECT ?s ?n WHERE { ?s ex:n ?n . FILTER(isNumeric(?n)) } ORDER BY ?n")
    assert res["ok"] and res["order"] == "query"
    assert [r["n"]["value"] for r in res["rows"]] == ["2", "11", BIG]
    compact = run("SELECT ?s ?n WHERE { ?s ex:n ?n . FILTER(isNumeric(?n)) } ORDER BY DESC(?n)", "compact")
    assert [r["n"] for r in compact["rows"]] == [BIG, "11", "2"]      # the query's order, not string order


def test_unordered_rows_are_sorted():
    assert run("SELECT ?n WHERE { ?s ex:n ?n }")["order"] == "sorted"


def test_typed_keeps_datatype_lexical_form_and_language():
    rows = run("SELECT ?s ?n WHERE { ?s ex:n ?n }")["rows"]
    by = {r["s"]["compact"] if "compact" in r["s"] else r["s"]["value"]: r["n"] for r in rows}
    eleven = sorted((r["n"].get("datatype", "") for r in rows if r["n"]["value"] == "11"))
    assert eleven == ["", "http://www.w3.org/2001/XMLSchema#integer"]     # 11 and "11" stay apart
    assert any(t["value"] == BIG and t["datatype"].endswith("#integer") for t in by.values())
    lab = run("SELECT ?l WHERE { ex:b ex:label ?l }")["rows"][0]["l"]
    assert lab == {"type": "literal", "value": "eleven", "xml:lang": "en"}


def test_unbound_is_not_empty_text():
    q = "SELECT ?s ?l WHERE { ?s ex:n ?n . FILTER(?s IN (ex:a, ex:c)) OPTIONAL { ?s ex:label ?l } }"
    typed = {r["s"]["value"].rsplit("#", 1)[1]: r for r in run(q)["rows"]}
    assert typed["a"]["l"] == {"type": "literal", "value": ""}           # bound to the empty string
    assert "l" not in typed["c"]                                          # unbound: left out
    compact = run(q, "compact")["rows"]
    assert sorted(r["l"] for r in compact) == ["", ""]                    # the compact form cannot tell them apart


def test_blank_nodes_keep_coreference():
    rows = run("SELECT ?b ?k ?b2 WHERE { ex:e ex:part ?b . ?b ex:k ?k . BIND(?b AS ?b2) }")["rows"]
    labels = [r["b"]["value"] for r in rows]
    assert sorted(labels) == ["b0", "b1"]
    assert all(r["b"] == r["b2"] for r in rows)
    assert run("SELECT ?b WHERE { ex:e ex:part ?b }", "compact")["rows"] == [{"b": "_:blank"}, {"b": "_:blank"}]


def test_typed_scene_terms_and_vars():
    res = run("SELECT ?m ?c WHERE { ?m a x3do:Material ; x3do:diffuseColor ?c }")
    assert res["vars"] == ["m", "c"] and res["result_format"] == "typed"
    m = res["rows"][0]["m"]
    assert m["type"] == "uri" and m["compact"] == ":Nacre"


def test_typed_ask_and_bad_format():
    assert run("ASK { ?s ex:n 2 }")["rows"] == [{"ask": True}]
    bad = run("ASK { ?s ex:n 2 }", "xml")
    assert bad["ok"] is False and "result_format" in bad["error"]


@pytest.mark.xfail(strict=True, reason="rdflib 7.6.0 drops the row of a VALUES binding whose OPTIONAL "
                   "does not match; SPARQL 1.1 keeps it with ?l unbound. Strict, so a fixed rdflib shows up here.")
def test_engine_values_then_optional_keeps_unmatched_row():
    rows = run("SELECT ?s ?l WHERE { VALUES ?s { ex:a ex:c } OPTIONAL { ?s ex:label ?l } }")["rows"]
    assert len(rows) == 2
