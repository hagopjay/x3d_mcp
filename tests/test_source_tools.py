"""source_witness_audit, patch_x3d_attribute and shacl_counterfactual."""

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tools import source_tools
from x3d_utils import source_witness as srcw

DOCTYPE = ("<?xml version='1.0' encoding='UTF-8'?>\n"
           "<!DOCTYPE X3D PUBLIC 'ISO//Web3D//DTD X3D 4.0//EN' 'https://www.web3d.org/specifications/x3d-4.0.dtd'>\n")
SCENE = (DOCTYPE +
         "<X3D profile='Interchange' version='4.0'>\n"
         "  <head><meta name='title' content='witness fixture'/></head>\n"
         "  <Scene>\n"
         "    <!-- keep this comment -->\n"
         "    <Viewpoint description='front' position='0 1 6'/>\n"
         "    <Shape DEF='Ruby'>\n"
         "      <Appearance><Material DEF='RubyMat' diffuseColor='1.5 0.08 0.16'   shininess='0.6'/></Appearance>\n"
         "      <Box size='1 1 1'/>\n"
         "    </Shape>\n"
         "  </Scene>\n"
         "</X3D>\n")
FORWARD = ("<X3D profile='Interchange' version='4.0'><Scene>\n"
           "<Shape USE='late'/>\n"
           "<Shape DEF='late'><Box/></Shape>\n"
           "</Scene></X3D>")
# A Box placed straight into a Transform's children field (the lab's fault F5): S7 flags it on the
# asserted graph; RDFS entailment from hasChildren's range types it an X3DChildNode and the violation vanishes.
F5 = ("<?xml version='1.0' encoding='UTF-8'?>\n<X3D profile='Interchange' version='4.0'><Scene>\n"
      "<Viewpoint position='0 0 5'/>\n"
      "<Transform DEF='Shelf'>\n"
      "  <Shape DEF='Ok'><Appearance><Material diffuseColor='0.8 0.2 0.2'/></Appearance><Sphere/></Shape>\n"
      "  <Box DEF='Stray' containerField='children' size='0.2 0.4 0.2'/>\n"
      "</Transform>\n</Scene></X3D>")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def mcp():
    m = FastMCP("t")
    source_tools.register(m)
    return m


def call(mcp, name, **args):
    out = asyncio.run(mcp.call_tool(name, args))
    content = out[0] if isinstance(out, tuple) else out
    return json.loads(content[0].text)


# --- source_witness_audit -------------------------------------------------------------------------

def test_audit_locates_forward_use_by_bytes(mcp):
    res = call(mcp, "source_witness_audit", content=FORWARD)
    assert res["ok"] is True and res["clean"] is False
    assert res["source_sha256"] == sha(FORWARD)
    f = [x for x in res["findings"] if x["code"] == "forward-use"]
    assert len(f) == 1
    w = f[0]["witness"]
    assert FORWARD.encode()[w["start_byte"]:w["end_byte"]] == b"<Shape USE='late'/>"
    assert w["line"] == 2
    assert f[0]["related_witness"]["def"] == "late"


def test_audit_clean_scene(mcp):
    res = call(mcp, "source_witness_audit", content=SCENE)
    assert res["ok"] and res["clean"] and res["doctype"]["present"] is True
    assert res["doctype"]["external_dtd_loaded"] is False


def test_audit_refuses_entity_declarations():
    evil = "<!DOCTYPE X3D [<!ENTITY a 'b'>]><X3D><Scene/></X3D>"
    res = srcw.audit_scene(evil.encode())
    assert res["ok"] is False and res["findings"][0]["code"] == "entity-declaration-refused"


# --- patch_x3d_attribute --------------------------------------------------------------------------

def test_patch_changes_only_the_value(mcp):
    res = call(mcp, "patch_x3d_attribute", content=SCENE, def_name="RubyMat", attribute="diffuseColor",
               new_value="0.85 0.08 0.16", expected_value="1.5 0.08 0.16", expected_sha256=sha(SCENE))
    assert res["ok"] is True
    doc = res["document"]
    assert doc == SCENE.replace("1.5 0.08 0.16", "0.85 0.08 0.16")       # every other byte kept
    assert doc.startswith(DOCTYPE) and "<!-- keep this comment -->" in doc and "'   shininess" in doc
    r = res["receipt"]
    assert r["source_sha256"] == sha(SCENE) and r["output_sha256"] == sha(doc)
    assert r["before_value"] == "1.5 0.08 0.16" and r["after_value"] == "0.85 0.08 0.16"
    start, end = r["replaced_byte_range"]
    assert SCENE.encode()[start:end] == b"1.5 0.08 0.16"
    assert res["recheck"]["xsd_valid"] is True
    assert res["recheck"]["semantic_errors"] == 0


def test_patch_refuses_stale_hash(mcp):
    res = call(mcp, "patch_x3d_attribute", content=SCENE, def_name="RubyMat", attribute="diffuseColor",
               new_value="0.85 0.08 0.16", expected_sha256="0" * 64)
    assert res["ok"] is False and res["code"] == "source-conflict"


def test_patch_refuses_unexpected_old_value(mcp):
    res = call(mcp, "patch_x3d_attribute", content=SCENE, def_name="RubyMat", attribute="diffuseColor",
               new_value="0.85 0.08 0.16", expected_value="1 0 0", expected_sha256=sha(SCENE))
    assert res["ok"] is False and res["code"] == "value-conflict"


def test_patch_refuses_missing_attribute_and_identity_fields(mcp):
    res = call(mcp, "patch_x3d_attribute", content=SCENE, def_name="RubyMat", attribute="emissiveColor",
               new_value="1 0 0", expected_sha256=sha(SCENE))
    assert res["code"] == "attribute-missing"
    res = call(mcp, "patch_x3d_attribute", content=SCENE, def_name="RubyMat", attribute="DEF",
               new_value="X", expected_sha256=sha(SCENE))
    assert res["code"] == "attribute-refused"


def test_patch_refuses_ambiguous_def(mcp):
    doc = SCENE.replace("<Box size='1 1 1'/>", "<Box DEF='RubyMat' size='1 1 1'/>")
    res = call(mcp, "patch_x3d_attribute", content=doc, def_name="RubyMat", attribute="size",
               new_value="2 2 2", expected_sha256=sha(doc))
    assert res["ok"] is False and res["code"] == "target-ambiguous"


def test_patch_escapes_quotes(mcp):
    res = call(mcp, "patch_x3d_attribute", content=SCENE, def_name="RubyMat", attribute="shininess",
               new_value="0.5' onload='x", expected_sha256=sha(SCENE), recheck=False, return_mode="full")
    assert "0.5&apos; onload=&apos;x" in res["document"]


def test_patch_recheck_reports_invalid_value(mcp):
    res = call(mcp, "patch_x3d_attribute", content=SCENE, def_name="RubyMat", attribute="diffuseColor",
               new_value="red", expected_sha256=sha(SCENE), return_mode="summary")
    assert res["ok"] is True and "document" not in res
    assert res["recheck"]["xsd_valid"] is False


# --- shacl_counterfactual -------------------------------------------------------------------------

# The RDFS run expands the scene with the whole X3D Ontology, so its cost does not shrink with the scene: this
# 7-node fixture takes 17-20 s on a 4-core container (pyshacl 0.40.2, rdflib 7.6.0, owlrl 7.6.2), against the
# 20 s default deadline (X3D_MCP_SPARQL_TIMEOUT). On GitHub runners it crossed the deadline intermittently and
# the tool answered {"ok": false, "error": "query ran past 20 s ..."}, which read as a logic failure. This test is
# about what inference hides, not about speed, so it gets its own generous deadline; the deadline itself is
# covered by the ontology guard tests.
COUNTERFACTUAL_DEADLINE_S = "180"


def test_counterfactual_shows_inference_hiding_s7(mcp, monkeypatch):
    monkeypatch.setenv("X3D_MCP_SPARQL_TIMEOUT", COUNTERFACTUAL_DEADLINE_S)
    res = call(mcp, "shacl_counterfactual", content=F5, inference="rdfs")
    assert res["ok"] is True, res
    assert res["asserted"]["conforms"] is False
    hidden = res["hidden_by_inference"]
    assert hidden["violations"] >= 1
    assert any(r["check_id"] == "children-not-child-node" and r["focus"].endswith("Stray") for r in hidden["sample"])
    assert "hidden, not repaired" in res["verdict"]


def test_counterfactual_rejects_none(mcp):
    res = call(mcp, "shacl_counterfactual", content=F5, inference="none")
    assert res["ok"] is False


def test_counterfactual_buckets():
    a = {"conforms": False, "counts": {"Violation": 1}, "results": [
        {"check_id": "S7", "focus": "x#A", "path": "p", "value": None, "severity": "Violation"},
        {"check_id": "S1", "focus": "x#B", "path": "q", "value": "2", "severity": "Violation"}]}
    d = {"conforms": False, "counts": {"Violation": 2}, "results": [
        {"check_id": "S1", "focus": "x#B", "path": "q", "value": "2", "severity": "Violation"},
        {"check_id": "S6", "focus": "x#C", "path": "r", "value": None, "severity": "Violation"}]}
    out = source_tools.counterfactual(a, d)
    assert out["hidden_by_inference"]["count"] == 1 and out["hidden_by_inference"]["by_check"] == {"S7": 1}
    assert out["added_by_inference"]["count"] == 1 and out["added_by_inference"]["focus_nodes"] == 1
    assert out["unchanged"]["count"] == 1
