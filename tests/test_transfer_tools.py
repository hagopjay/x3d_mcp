"""Tool-level tests for the transfer layer: inputs by blob_id equal inline results, the
return modes, the blob_* tools, the caller gate, offloading, and stdio behaviour."""

import asyncio
import base64
import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import zstandard
from lxml import etree
from mcp.server.fastmcp import FastMCP

from tools import convert, scene_ops, validate_tool
from tools import transfer as blob_tools
from transfer import blobs, outputs, tooling
from transfer.auth import bearer_ok, request_of, tool_gate
from transfer.blobs import BlobConfig
from validation.autofix import autofix_containerfields
from validation.repair import repair_x3d
from validation.semantic import validate_semantic as semantic_report
from validation.validate import validate_json, validate_xml

TOKEN = "t" * 8 + "k" * 40

SAMPLE = '''<?xml version="1.0" encoding="UTF-8"?>
<X3D profile="Interchange" version="4.1">
  <Scene>
    <Viewpoint DEF="MainView" description="Default View" position="0 0 10"/>
    <DirectionalLight DEF="Sun" direction="0 -1 -1" intensity="0.8"/>
    <Transform DEF="RedGroup" translation="2 0 0">
      <Shape DEF="RedShape">
        <Appearance>
          <Material DEF="RedMat" diffuseColor="1 0 0"/>
        </Appearance>
        <Sphere radius="1.5"/>
      </Shape>
    </Transform>
    <Transform DEF="BlueGroup" translation="-2 0 0">
      <Shape DEF="BlueShape">
        <Appearance>
          <Material DEF="BlueMat" diffuseColor="0 0 1" transparency="0.3"/>
        </Appearance>
        <Box size="2 2 2"/>
      </Shape>
    </Transform>
  </Scene>
</X3D>'''

# containerField wrong (autofix has work to do), plus a schema error
NEEDS_FIX = ('<?xml version="1.0"?><X3D profile="Full" version="4.1"><Scene>'
             "<Shape><Appearance><PhysicalMaterial><ImageTexture url='&quot;t.png&quot;'/>"
             "</PhysicalMaterial></Appearance><Box/></Shape></Scene></X3D>")

NEEDS_REPAIR = ('<?xml version="1.0" encoding="UTF-8"?>\n<X3D profile="Immersive" version="4.0"><Scene>'
                "<EnvironmentLight/><Shape><Appearance><Material/></Appearance><Box/></Shape>"
                "</Scene></X3D>")

BROKEN_SCHEMA = ('<?xml version="1.0" encoding="UTF-8"?>\n<X3D profile="Interchange" version="4.1"><Scene>'
                 '<Transform translation="1 2 x"><Shape><Box size="oops"/></Shape></Transform>'
                 "</Scene></X3D>")

COMMENT_FIRST = "<!-- exported by a tool -->\n" + SAMPLE.split("?>\n", 1)[1]    # no declaration, so valid XML


def big_scene(n: int = 6000) -> str:
    body = "".join(f'<Transform DEF="T{i}" translation="{i} 0 0"><Shape DEF="S{i}"><Appearance>'
                   f'<Material DEF="M{i}" diffuseColor="0.5 0.2 0.3"/></Appearance>'
                   f'<Box size="1 1 1"/></Shape></Transform>\n' for i in range(n))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<X3D profile="Interchange" version="4.1"><Scene>'
            + body + "</Scene></X3D>\n")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in [n for n in os.environ if n.startswith(("X3D_MCP_BLOB", "X3D_MCP_ALLOWED", "X3D_MCP_SUMMARY",
                                                        "X3D_MCP_OFFLOAD"))]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    blobs.reset()
    yield
    blobs.reset()


@pytest.fixture
def mcp():
    m = FastMCP("tools-test")
    validate_tool.register(m)
    scene_ops.register(m)
    convert.register(m)
    blob_tools.register(m)
    return m


def run(mcp, tool: str, /, **args) -> str:
    out = asyncio.run(mcp.call_tool(tool, args))
    if isinstance(out, tuple):
        out = out[0]
    return out[0].text


def store_doc(text: str, media: str = "model/x3d+xml") -> str:
    return blobs.get_store().put(text.encode("utf-8"), media_type=media)["blob_id"]


def element_count(xml: str) -> int:
    return sum(1 for e in etree.fromstring(xml.encode()).iter() if isinstance(e.tag, str))


# ---------------------------------------------------------------- blob_id equals inline, byte for byte

@pytest.mark.parametrize("doc", [SAMPLE, NEEDS_FIX, NEEDS_REPAIR, BROKEN_SCHEMA], ids=["sample", "fix", "repair", "broken"])
def test_validate_and_repair_tools_give_identical_output_for_blob_and_inline(mcp, doc):
    bid = store_doc(doc)
    for tool in ("validate_x3d", "validate_semantic", "autofix_x3d", "repair_x3d"):
        inline = run(mcp, tool, content=doc)
        by_blob = run(mcp, tool, blob_id=bid)
        assert by_blob == inline, tool


def test_default_outputs_are_exactly_the_original_functions_output(mcp):
    """Without blob_id and with return_mode "full" nothing about the output changed."""
    for doc in (SAMPLE, NEEDS_FIX, NEEDS_REPAIR, BROKEN_SCHEMA):
        assert run(mcp, "validate_x3d", content=doc) == json.dumps(validate_xml(doc), indent=2)
        assert run(mcp, "validate_x3d", content=doc, return_mode="full") == json.dumps(validate_xml(doc), indent=2)
        assert run(mcp, "validate_semantic", content=doc) == semantic_report(doc)
        assert run(mcp, "autofix_x3d", content=doc) == json.dumps(autofix_containerfields(doc), indent=2)
        assert run(mcp, "repair_x3d", content=doc) == json.dumps(repair_x3d(doc), indent=2)
    j = '{"X3D": {"@version": "4.0"}}'
    assert run(mcp, "validate_x3d", content=j, encoding="json") == json.dumps(validate_json(j), indent=2)
    assert run(mcp, "validate_x3d", blob_id=store_doc(j, "application/json"), encoding="json") \
        == json.dumps(validate_json(j), indent=2)
    assert "Unsupported encoding" in run(mcp, "validate_x3d", content=SAMPLE, encoding="yaml")


@pytest.mark.parametrize("to", ["xml", "json", "vrml"])
def test_convert_gives_identical_output_for_blob_and_inline(mcp, to):
    inline = run(mcp, "convert_x3d", content=SAMPLE, from_encoding="xml", to_encoding=to)
    by_blob = run(mcp, "convert_x3d", content="", blob_id=store_doc(SAMPLE), from_encoding="xml", to_encoding=to)
    assert by_blob == inline and len(inline) > 100
    assert run(mcp, "convert_x3d", content=SAMPLE, from_encoding="xml", to_encoding="yaml") \
        == "Unknown target encoding: yaml"
    assert run(mcp, "convert_x3d", content=SAMPLE, from_encoding="json", to_encoding="xml").startswith("Parsing from")


def test_scene_edit_tools_give_identical_output_for_blob_and_inline(mcp):
    bid = store_doc(SAMPLE)
    cases = [
        ("modify_x3d_node", dict(def_name="RedMat", field_changes='{"diffuseColor": "0 1 0"}')),
        ("modify_x3d_node", dict(def_name="Nope", field_changes='{"a": "b"}')),
        ("remove_x3d_node", dict(def_name="BlueGroup")),
        ("remove_x3d_node", dict(node_type="Transform", index=1)),
        ("remove_x3d_node", dict(node_type="Transform", index=9)),
        ("move_x3d_node", dict(def_name="RedShape", new_parent_def="BlueGroup")),
        ("move_x3d_node", dict(def_name="RedShape")),
        ("move_x3d_node", dict(def_name="BlueGroup", new_parent_def="BlueGroup")),
    ]
    for tool, kw in cases:
        inline = run(mcp, tool, content=SAMPLE, **kw)
        by_blob = run(mcp, tool, content="", blob_id=bid, **kw)
        assert by_blob == inline, (tool, kw)


def test_a_blob_that_opens_with_a_comment_is_a_document_not_a_path(mcp):
    # inline content like this is mistaken for a file path (an existing quirk, kept as is) ...
    assert run(mcp, "modify_x3d_node", content=COMMENT_FIRST, def_name="RedMat",
               field_changes='{"diffuseColor": "0 1 0"}').startswith("File not found")
    # ... but a stored blob is always a document
    out = run(mcp, "modify_x3d_node", content="", blob_id=store_doc(COMMENT_FIRST), def_name="RedMat",
              field_changes='{"diffuseColor": "0 1 0"}')
    assert out.startswith("<?xml") and 'diffuseColor="0 1 0"' in out
    bom = "﻿" + SAMPLE
    out = run(mcp, "remove_x3d_node", content="", blob_id=store_doc(bom), def_name="Sun")
    assert out.startswith("<?xml") and "Sun" not in out


def test_blob_edit_equals_inline_edit_on_a_large_scene(mcp):
    doc = big_scene()
    bid = store_doc(doc)
    kw = dict(def_name="M42", field_changes='{"diffuseColor": "1 1 1"}')
    assert run(mcp, "modify_x3d_node", content="", blob_id=bid, **kw) == run(mcp, "modify_x3d_node", content=doc, **kw)


# ---------------------------------------------------------------- return modes

def test_modify_summary_and_blob_modes(mcp):
    bid = store_doc(SAMPLE)
    kw = dict(def_name="RedMat", field_changes='{"diffuseColor": "0 1 0", "transparency": "0.5"}')
    full = run(mcp, "modify_x3d_node", content="", blob_id=bid, **kw)

    s = json.loads(run(mcp, "modify_x3d_node", content="", blob_id=bid, return_mode="summary", **kw))
    assert s["return_mode"] == "summary" and s["ok"] is True and s["source"] == "blob " + bid[:12]
    assert s["input"] == {"sha256": bid, "bytes": len(SAMPLE.encode())}
    doc = s["document"]
    assert doc["sha256"] == hashlib.sha256(full.encode()).hexdigest() and doc["bytes"] == len(full.encode())
    assert doc["nodes"] == element_count(full) and doc["node_types"]["Material"] == 2
    assert "blob_id" not in doc
    assert s["changes"] == [
        {"op": "modify", "def": "RedMat", "node": "Material", "field": "diffuseColor", "before": "1 0 0", "after": "0 1 0"},
        {"op": "modify", "def": "RedMat", "node": "Material", "field": "transparency", "before": None, "after": "0.5"},
    ] and s["changes_total"] == 2
    assert blobs.get_store().stats()["blobs"] == 1                  # summary stored nothing

    b = json.loads(run(mcp, "modify_x3d_node", content="", blob_id=bid, return_mode="blob", **kw))
    assert b["return_mode"] == "blob" and b["document"]["blob_id"] == b["document"]["sha256"] == doc["sha256"]
    assert blobs.get_store().get_text(b["document"]["blob_id"]) == full
    assert blobs.get_store().stat(b["document"]["blob_id"])["media_type"] == "model/x3d+xml"


def test_remove_and_move_summaries(mcp):
    bid = store_doc(SAMPLE)
    r = json.loads(run(mcp, "remove_x3d_node", content="", blob_id=bid, def_name="BlueGroup", return_mode="summary"))
    assert r["changes"] == [{"op": "remove", "node": "Transform", "def": "BlueGroup", "parent": "Scene",
                             "removed_elements": 5}]
    mv = json.loads(run(mcp, "move_x3d_node", content="", blob_id=bid, def_name="RedShape",
                        new_parent_def="BlueGroup", return_mode="summary"))
    assert mv["changes"] == [{"op": "move", "def": "RedShape", "node": "Shape", "from_parent": "Transform",
                              "from_parent_def": "RedGroup", "to_parent": "Transform", "to_parent_def": "BlueGroup"}]
    assert mv["document"]["nodes"] == element_count(run(mcp, "move_x3d_node", content="", blob_id=bid,
                                                        def_name="RedShape", new_parent_def="BlueGroup"))


def test_edit_failures_come_back_as_json_in_the_new_modes_and_text_in_full(mcp):
    bid = store_doc(SAMPLE)
    plain = run(mcp, "modify_x3d_node", content="", blob_id=bid, def_name="Nope", field_changes='{"a":"b"}')
    assert plain.startswith("No node with DEF='Nope' found")
    for mode in ("summary", "blob"):
        out = json.loads(run(mcp, "modify_x3d_node", content="", blob_id=bid, def_name="Nope",
                             field_changes='{"a":"b"}', return_mode=mode))
        assert out["ok"] is False and out["error"] == plain and out["return_mode"] == mode
    assert blobs.get_store().stats()["blobs"] == 1


def test_summary_mode_also_works_for_inline_content(mcp):
    out = json.loads(run(mcp, "modify_x3d_node", content=SAMPLE, def_name="Sun",
                         field_changes='{"intensity": "0.1"}', return_mode="summary"))
    assert out["source"] == "inline content" and out["input"]["sha256"] == hashlib.sha256(SAMPLE.encode()).hexdigest()
    assert out["changes"][0]["before"] == "0.8"


def test_content_that_names_a_file_still_works_and_is_summarised_by_path(mcp, tmp_path):
    f = tmp_path / "s.x3d"
    f.write_text(SAMPLE)
    out = json.loads(run(mcp, "remove_x3d_node", content=str(f), def_name="Sun", return_mode="summary"))
    assert out["source"] == f"file {f}" and out["input"] == {"path": str(f)}
    assert out["document"]["nodes"] == element_count(SAMPLE) - 1


def test_autofix_and_repair_modes(mcp):
    bid = store_doc(NEEDS_REPAIR)
    full = json.loads(run(mcp, "repair_x3d", blob_id=bid))
    s = json.loads(run(mcp, "repair_x3d", blob_id=bid, return_mode="summary"))
    assert "fixed" not in s and s["changes"] == full["changes"] and s["changes_total"] == len(full["changes"])
    assert s["unfixable"] == full["unfixable"]
    assert s["document"]["sha256"] == hashlib.sha256(full["fixed"].encode()).hexdigest()
    assert s["document"]["nodes"] == element_count(full["fixed"])
    assert s["remaining"]["schema_valid"] == full["remaining"]["schema_valid"] is True
    assert s["remaining"]["profile_ok"] == full["remaining"]["profile_ok"]
    b = json.loads(run(mcp, "repair_x3d", blob_id=bid, return_mode="blob"))
    assert blobs.get_store().get_text(b["document"]["blob_id"]) == full["fixed"]

    fix_id = store_doc(NEEDS_FIX)
    af = json.loads(run(mcp, "autofix_x3d", blob_id=fix_id))
    s = json.loads(run(mcp, "autofix_x3d", blob_id=fix_id, return_mode="summary"))
    assert s["changes"] == af["changes"] and s["changes"][0]["to"] == "baseTexture"
    assert s["document"]["sha256"] == hashlib.sha256(af["fixed"].encode()).hexdigest()
    assert "fixed" not in s


def test_autofix_and_repair_report_parse_errors_in_every_mode(mcp):
    bid = store_doc("<X3D><Scene></X3D>")
    for tool in ("autofix_x3d", "repair_x3d"):
        full = json.loads(run(mcp, tool, blob_id=bid))
        assert "error" in full
        for mode in ("summary", "blob"):
            out = json.loads(run(mcp, tool, blob_id=bid, return_mode=mode))
            assert out["ok"] is False and out["error"] == full["error"]


def test_validate_x3d_modes(mcp):
    bid = store_doc(BROKEN_SCHEMA)
    full = json.loads(run(mcp, "validate_x3d", blob_id=bid))
    assert full["valid"] is False and len(full["errors"]) >= 2
    s = json.loads(run(mcp, "validate_x3d", blob_id=bid, return_mode="summary"))
    assert s["valid"] is False and s["error_count"] == len(full["errors"]) and s["errors"] == full["errors"]
    assert s["schema_version"] == full["schema_version"] and s["input"]["sha256"] == bid
    b = json.loads(run(mcp, "validate_x3d", blob_id=bid, return_mode="blob"))
    assert json.loads(blobs.get_store().get_text(b["report"]["blob_id"])) == full
    assert b["report"]["media_type"] == "application/json"
    ok = json.loads(run(mcp, "validate_x3d", blob_id=store_doc(SAMPLE), return_mode="summary"))
    assert ok["valid"] is True and ok["error_count"] == 0


def test_summary_lists_are_capped_and_long_strings_clipped(mcp, monkeypatch):
    monkeypatch.setenv("X3D_MCP_SUMMARY_ITEMS", "3")
    blobs.reset()
    many = ('<?xml version="1.0" encoding="UTF-8"?>\n<X3D profile="Interchange" version="4.1"><Scene>'
            + "".join(f'<Transform translation="1 2 x{i}"/>' for i in range(10)) + "</Scene></X3D>")
    full = json.loads(run(mcp, "validate_x3d", content=many))
    assert len(full["errors"]) == 10
    s = json.loads(run(mcp, "validate_x3d", content=many, return_mode="summary"))
    assert s["error_count"] == 10 and len(s["errors"]) == 3 and s["errors_total"] == 10 and s["errors_truncated"] is True
    assert outputs.clip("x" * 400).endswith("[100 more characters]")
    assert outputs.clip("short") == "short" and outputs.clip(5) == 5


def test_validate_semantic_modes(mcp):
    bid = store_doc(NEEDS_FIX)
    report = run(mcp, "validate_semantic", blob_id=bid)
    s = json.loads(run(mcp, "validate_semantic", blob_id=bid, return_mode="summary"))
    import re
    m = re.search(r"Found (\d+) error\(s\), (\d+) warning\(s\), (\d+) info\(s\)", report)
    assert (s["errors"], s["warnings"], s["infos"]) == tuple(int(x) for x in m.groups())
    assert s["ok"] is True and sum(s["by_check"].values()) == s["findings_total"]
    assert all(f["message"] and f["check"] and f["level"] for f in s["findings"])
    assert any("containerfield" in f["check"] for f in s["findings"])
    b = json.loads(run(mcp, "validate_semantic", blob_id=bid, return_mode="blob"))
    assert blobs.get_store().get_text(b["report"]["blob_id"]) == report
    clear = json.loads(run(mcp, "validate_semantic", blob_id=store_doc(SAMPLE + "\n"), return_mode="summary"))
    assert clear["ok"] is True and clear["errors"] == 0
    parse_err = json.loads(run(mcp, "validate_semantic", blob_id=store_doc("<X3D><Scene></X3D>"), return_mode="summary"))
    assert parse_err["ok"] is False and parse_err["error"]


def test_convert_modes(mcp):
    bid = store_doc(SAMPLE)
    full = run(mcp, "convert_x3d", content="", blob_id=bid, from_encoding="xml", to_encoding="json")
    s = json.loads(run(mcp, "convert_x3d", content="", blob_id=bid, from_encoding="xml", to_encoding="json",
                       return_mode="summary"))
    assert s["document"]["sha256"] == hashlib.sha256(full.encode()).hexdigest()
    assert s["document"]["media_type"] == "model/x3d+json" and "nodes" not in s["document"]
    b = json.loads(run(mcp, "convert_x3d", content="", blob_id=bid, from_encoding="xml", to_encoding="vrml",
                       return_mode="blob"))
    assert blobs.get_store().stat(b["document"]["blob_id"])["media_type"] == "model/x3d+vrml"
    x = json.loads(run(mcp, "convert_x3d", content="", blob_id=bid, from_encoding="xml", to_encoding="xml",
                       return_mode="summary"))
    assert x["document"]["nodes"] > 5
    bad = json.loads(run(mcp, "convert_x3d", content="", blob_id=bid, from_encoding="xml", to_encoding="yaml",
                         return_mode="summary"))
    assert bad["ok"] is False and "Unknown target encoding" in bad["error"]


def test_invalid_return_mode_is_an_error_in_each_tools_own_style(mcp):
    bid = store_doc(SAMPLE)
    assert json.loads(run(mcp, "validate_x3d", blob_id=bid, return_mode="tiny"))["errors"][0].startswith("return_mode must be")
    assert run(mcp, "validate_semantic", blob_id=bid, return_mode="tiny").startswith("# Semantic Check: Input Error")
    assert "return_mode must be" in json.loads(run(mcp, "autofix_x3d", blob_id=bid, return_mode="tiny"))["error"]
    assert "return_mode must be" in json.loads(run(mcp, "repair_x3d", blob_id=bid, return_mode="tiny"))["error"]
    assert run(mcp, "modify_x3d_node", content="", blob_id=bid, def_name="Sun", field_changes="{}",
               return_mode="tiny").startswith("return_mode must be")
    assert run(mcp, "convert_x3d", content=SAMPLE, from_encoding="xml", to_encoding="xml",
               return_mode="tiny").startswith("return_mode must be")
    assert outputs.parse_mode(None) == "full" and outputs.parse_mode(" Summary ") == "summary"


def test_the_edit_loop_keeps_the_document_server_side(mcp):
    """Ten successive edits, each naming the previous result by id, moving only ids and summaries."""
    doc = big_scene(8000)
    bid = store_doc(doc)
    cur_inline = doc
    moved = 0
    for i in range(10):
        change = json.dumps({"diffuseColor": f"0.{i} 0.5 0.5"})
        req = dict(content="", blob_id=bid, def_name="M7", field_changes=change, return_mode="blob")
        moved += len(json.dumps(req))
        out = run(mcp, "modify_x3d_node", **req)
        moved += len(out)
        bid = json.loads(out)["document"]["blob_id"]
        cur_inline = run(mcp, "modify_x3d_node", content=cur_inline, def_name="M7", field_changes=change)
    assert blobs.get_store().get_text(bid) == cur_inline
    assert len(doc) > 1_000_000 and moved < 15_000                    # versus 10 x 2 x 1.4 MB inline


def test_a_summary_is_thousands_of_times_smaller_than_the_document(mcp):
    doc = big_scene(8000)
    bid = store_doc(doc)
    kw = dict(content="", blob_id=bid, def_name="M7", field_changes='{"diffuseColor": "1 1 1"}')
    assert len(run(mcp, "modify_x3d_node", **kw)) > 800_000
    assert len(run(mcp, "modify_x3d_node", return_mode="summary", **kw)) < 1500
    assert len(run(mcp, "validate_x3d", blob_id=bid, return_mode="summary")) < 600


# ---------------------------------------------------------------- inputs and errors

def test_one_input_only_across_the_tools(mcp, tmp_path):
    bid = store_doc(SAMPLE)
    f = tmp_path / "s.x3d"
    f.write_text(SAMPLE)
    out = json.loads(run(mcp, "validate_x3d", content=SAMPLE, blob_id=bid))
    assert out["valid"] is False and "exactly one of" in out["errors"][0]
    out = json.loads(run(mcp, "validate_x3d", path=str(f), blob_id=bid))
    assert "got: path, blob_id" in out["errors"][0]
    assert "exactly one of" in run(mcp, "validate_semantic", content=SAMPLE, blob_id=bid)
    assert "exactly one of" in json.loads(run(mcp, "autofix_x3d", content=SAMPLE, blob_id=bid))["error"]
    assert "exactly one of" in json.loads(run(mcp, "repair_x3d", content=SAMPLE, blob_id=bid))["error"]
    assert "exactly one of" in run(mcp, "modify_x3d_node", content=SAMPLE, blob_id=bid, def_name="Sun", field_changes="{}")
    assert "exactly one of" in run(mcp, "remove_x3d_node", content=SAMPLE, blob_id=bid, def_name="Sun")
    assert "exactly one of" in run(mcp, "move_x3d_node", content=SAMPLE, blob_id=bid, def_name="Sun")
    assert "exactly one of" in run(mcp, "convert_x3d", content=SAMPLE, blob_id=bid, from_encoding="xml", to_encoding="xml")
    # the original errors are unchanged when no blob_id is given
    assert json.loads(run(mcp, "validate_x3d"))["errors"] == ["Provide either `content` (inline X3D) or `path` (a file)."]
    assert json.loads(run(mcp, "validate_x3d", content=SAMPLE, path=str(f)))["errors"] == [
        "Provide either `content` (inline X3D) or `path` (a file), not both."]


def test_missing_and_malformed_blob_ids_are_reported_clearly(mcp):
    out = json.loads(run(mcp, "validate_x3d", blob_id="0" * 64))
    assert "never stored, expired" in out["errors"][0]
    out = json.loads(run(mcp, "validate_x3d", blob_id="../../etc/passwd"))
    assert "64 hexadecimal" in out["errors"][0]
    assert "never stored" in run(mcp, "modify_x3d_node", content="", blob_id="1" * 64, def_name="a", field_changes="{}")
    assert "64 hexadecimal" in run(mcp, "validate_semantic", blob_id="nope")


def test_binary_blob_is_refused_as_a_document(mcp):
    bid = blobs.get_store().put(b"\xff\xfe\x00\x01 not text")["blob_id"]
    out = json.loads(run(mcp, "validate_x3d", blob_id=bid))
    assert "not valid UTF-8" in out["errors"][0]


def test_blob_inputs_and_blob_mode_say_so_when_the_store_is_off(mcp):
    blobs.configure(BlobConfig(enabled=False))
    out = json.loads(run(mcp, "validate_x3d", blob_id="0" * 64))
    assert "not enabled" in out["errors"][0]
    out = json.loads(run(mcp, "validate_x3d", content=SAMPLE, return_mode="blob"))
    assert "not enabled" in out["errors"][0]
    # everything that does not need the store still works: defaults and summaries
    assert json.loads(run(mcp, "validate_x3d", content=SAMPLE))["valid"] is True
    s = json.loads(run(mcp, "modify_x3d_node", content=SAMPLE, def_name="Sun", field_changes='{"intensity":"1"}',
                       return_mode="summary"))
    assert s["ok"] is True
    assert json.loads(run(mcp, "repair_x3d", content=NEEDS_REPAIR, return_mode="summary"))["document"]["nodes"] > 3


def test_a_full_store_reports_instead_of_raising(mcp):
    blobs.configure(BlobConfig(mem_bytes=3000, quota_bytes=3000))
    out = json.loads(run(mcp, "repair_x3d", content=big_scene(300), return_mode="blob"))
    assert out["ok"] is False and "quota" in out["error"]


# ---------------------------------------------------------------- the blob tools

def test_blob_put_info_read_delete_in_process(mcp):
    put = json.loads(run(mcp, "blob_put", content=SAMPLE, media_type="model/x3d+xml", name="s.x3d"))
    bid = put["blob_id"]
    assert bid == hashlib.sha256(SAMPLE.encode()).hexdigest() and put["created"] is True and put["name"] == "s.x3d"
    assert json.loads(run(mcp, "blob_put", content=SAMPLE))["created"] is False
    info = json.loads(run(mcp, "blob_info", blob_id=bid))
    assert info["size"] == len(SAMPLE.encode()) and info["preview"] == SAMPLE[:200]
    page = json.loads(run(mcp, "blob_read", blob_id=bid, offset=0, length=50))
    assert page == {"blob_id": bid, "offset": 0, "length": 50, "total": len(SAMPLE.encode()), "next_offset": 50,
                    "eof": False, "encoding": "utf-8", "data": SAMPLE[:50]}
    last = json.loads(run(mcp, "blob_read", blob_id=bid, offset=len(SAMPLE.encode()) - 5, length=100))
    assert last["eof"] is True and last["data"] == SAMPLE[-5:]
    assert json.loads(run(mcp, "blob_delete", blob_id=bid)) == {"deleted": True}
    assert json.loads(run(mcp, "blob_delete", blob_id=bid)) == {"deleted": False}
    assert "never stored" in json.loads(run(mcp, "blob_info", blob_id=bid))["error"]


def test_blob_put_base64_and_compressed(mcp):
    raw = os.urandom(1000) + b"binary\xff"
    out = json.loads(run(mcp, "blob_put", content=base64.b64encode(raw).decode(), encoding="base64"))
    assert out["media_type"] == "application/octet-stream" and out["size"] == len(raw)
    spaced = base64.encodebytes(raw).decode()                       # newlines inside are tolerated
    assert json.loads(run(mcp, "blob_put", content=spaced, encoding="base64"))["blob_id"] == out["blob_id"]
    z = base64.b64encode(zstandard.ZstdCompressor(level=19).compress(SAMPLE.encode())).decode()
    out = json.loads(run(mcp, "blob_put", content=z, encoding="base64", content_encoding="zstd"))
    assert out["blob_id"] == hashlib.sha256(SAMPLE.encode()).hexdigest()
    assert "not valid base64" in json.loads(run(mcp, "blob_put", content="@@@", encoding="base64"))["error"]
    assert "encoding must be" in json.loads(run(mcp, "blob_put", content="x", encoding="latin-1"))["error"]
    assert "corrupt" in json.loads(run(mcp, "blob_put", content=base64.b64encode(b"junk").decode(),
                                       encoding="base64", content_encoding="zstd"))["error"]


def test_blob_put_enforces_the_inline_limit_and_the_bomb_guard(mcp):
    blobs.configure(BlobConfig(inline_max=10_000))
    assert "limit" in json.loads(run(mcp, "blob_put", content="a" * 20_000))["error"]
    ok = json.loads(run(mcp, "blob_put", content="a" * 9_000))
    assert ok["size"] == 9_000
    bomb = base64.b64encode(zstandard.ZstdCompressor(level=3).compress(b"\0" * (50 << 20))).decode()
    assert "error" in json.loads(run(mcp, "blob_put", content=bomb, encoding="base64", content_encoding="zstd"))
    assert blobs.get_store().stats()["blobs"] == 1


def test_blob_put_path_under_stdio(mcp, tmp_path):
    f = tmp_path / "scene.x3d"
    f.write_text(SAMPLE)
    out = json.loads(run(mcp, "blob_put", path=str(f), media_type="model/x3d+xml"))
    assert out["blob_id"] == hashlib.sha256(SAMPLE.encode()).hexdigest()
    assert "not both" in json.loads(run(mcp, "blob_put", path=str(f), content="x"))["error"]
    assert "not found" in json.loads(run(mcp, "blob_put", path=str(tmp_path / "nope")))["error"]


def test_blob_put_path_is_refused_when_http_transport_is_configured(mcp, tmp_path, monkeypatch):
    f = tmp_path / "scene.x3d"
    f.write_text(SAMPLE)
    monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("X3D_MCP_BLOB_TOKEN", TOKEN)
    monkeypatch.setattr(blob_tools, "tool_gate", lambda ctx, cfg: None)   # the gate has its own tests
    blobs.reset()
    out = json.loads(run(mcp, "blob_put", path=str(f)))
    assert "disabled over the HTTP transport" in out["error"]


def test_blob_read_never_splits_a_multibyte_character(mcp):
    text = "é€𝄞" * 700
    bid = json.loads(run(mcp, "blob_put", content=text))["blob_id"]
    off, parts = 0, []
    while True:
        page = json.loads(run(mcp, "blob_read", blob_id=bid, offset=off, length=7))
        parts.append(page["data"])
        assert page["length"] > 0 and "�" not in page["data"]
        off = page["next_offset"]
        if page["eof"]:
            break
    assert "".join(parts) == text
    raw = json.loads(run(mcp, "blob_read", blob_id=bid, offset=1, length=6, encoding="base64"))
    assert base64.b64decode(raw["data"]) == text.encode()[1:7]
    assert "beyond" in json.loads(run(mcp, "blob_read", blob_id=bid, offset=10**9))["error"]
    assert "encoding must be" in json.loads(run(mcp, "blob_read", blob_id=bid, encoding="hex"))["error"]


def test_blob_read_page_limits(mcp):
    blobs.configure(BlobConfig(page_default=100, page_max=500))
    bid = json.loads(run(mcp, "blob_put", content="x" * 5000))["blob_id"]
    assert json.loads(run(mcp, "blob_read", blob_id=bid))["length"] == 100
    assert json.loads(run(mcp, "blob_read", blob_id=bid, length=100_000))["length"] == 500


def test_utf8_cut_helper():
    from tools.transfer import _utf8_cut
    e = "é".encode()                                   # c3 a9
    assert _utf8_cut(b"ab" + e[:1], False) == 2         # lead byte without its continuation
    assert _utf8_cut(b"ab" + e, False) == 4             # complete character kept
    assert _utf8_cut(b"ab" + e[:1], True) == 3          # at end of blob, nothing to wait for
    four = "𝄞".encode()
    for k in range(1, 4):
        assert _utf8_cut(b"x" + four[:k], False) == 1
    assert _utf8_cut(four, False) == 4
    assert _utf8_cut(four[:1], False) == 1              # never an empty page
    assert _utf8_cut(b"", False) == 0


def test_blob_tool_listing_has_no_context_parameter(mcp):
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    for name in ("blob_put", "blob_info", "blob_read", "blob_delete"):
        assert "ctx" not in tools[name].inputSchema["properties"]
    assert tools["blob_read"].outputSchema is None        # structured_output=False: no doubled payload


# ---------------------------------------------------------------- caller gate

def fake_ctx(headers: dict | None):
    if headers is None:
        return SimpleNamespace(request_context=SimpleNamespace(request=None))
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers=headers)))


def test_gate_stdio_and_in_process_callers_are_trusted():
    c = BlobConfig(http=False)
    assert tool_gate(None, c) is None and tool_gate(fake_ctx(None), c) is None
    assert request_of(None) is None

    class Raising:
        @property
        def request_context(self):
            raise ValueError("Context is not available outside of a request")
    assert request_of(Raising()) is None and tool_gate(Raising(), c) is None


def test_gate_over_http_needs_the_bearer_unless_tools_are_open():
    c = BlobConfig(http=True, token=TOKEN)
    assert "Bearer" in tool_gate(fake_ctx({}), c)
    assert "Bearer" in tool_gate(fake_ctx({"authorization": "Bearer wrong"}), c)
    assert "Bearer" in tool_gate(fake_ctx({"authorization": "Basic " + TOKEN}), c)
    assert tool_gate(fake_ctx({"authorization": "Bearer " + TOKEN}), c) is None
    assert "HTTP request" in tool_gate(None, c) and "HTTP request" in tool_gate(fake_ctx(None), c)
    assert tool_gate(fake_ctx({}), BlobConfig(http=True, token=TOKEN, tools_open=True)) is None
    assert "not enabled" in tool_gate(None, BlobConfig(enabled=False))


def test_bearer_ok_is_strict():
    assert bearer_ok("Bearer " + TOKEN, TOKEN) and bearer_ok("bearer " + TOKEN, TOKEN)
    assert not bearer_ok(None, TOKEN) and not bearer_ok("", TOKEN) and not bearer_ok("Bearer", TOKEN)
    assert not bearer_ok("Bearer " + TOKEN, "") and not bearer_ok(TOKEN, TOKEN)
    assert not bearer_ok("Bearer " + TOKEN + " ", TOKEN + "x")


def test_tools_over_http_config_without_a_request_are_refused(mcp):
    blobs.configure(BlobConfig(http=True, token=TOKEN))
    out = json.loads(run(mcp, "validate_x3d", blob_id="0" * 64))
    assert "HTTP request" in out["errors"][0]
    assert "HTTP request" in json.loads(run(mcp, "blob_put", content="x"))["error"]
    # plain inline calls need no blob access and stay open
    assert json.loads(run(mcp, "validate_x3d", content=SAMPLE))["valid"] is True


# ---------------------------------------------------------------- offloading

def test_heavy_work_runs_in_a_worker_thread_one_job_at_a_time():
    blobs.configure(BlobConfig(offload_min=1000))
    main = threading.get_ident()
    seen, active, peak = [], [0], [0]
    lock = threading.Lock()

    def work(tag):
        seen.append((tag, threading.get_ident()))
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        import time
        time.sleep(0.05)
        with lock:
            active[0] -= 1
        return tag

    async def go():
        small = await tooling.run_heavy(work, "small", size=10)
        big = await asyncio.gather(*[tooling.run_heavy(work, f"big{i}", size=5000) for i in range(4)])
        return small, big

    small, big = asyncio.run(go())
    assert small == "small" and big == [f"big{i}" for i in range(4)]
    ids = dict(seen)
    assert ids["small"] == main                                       # small work stays inline
    assert all(ids[f"big{i}"] != main for i in range(4)) and peak[0] == 1


def test_offloaded_tools_return_the_same_text(mcp, monkeypatch):
    monkeypatch.setenv("X3D_MCP_OFFLOAD_MIN", "100")
    blobs.reset()
    inline = {t: run(mcp, t, content=NEEDS_REPAIR) for t in ("validate_x3d", "validate_semantic", "repair_x3d")}
    monkeypatch.setenv("X3D_MCP_OFFLOAD_MIN", "100000000")
    blobs.reset()
    assert inline == {t: run(mcp, t, content=NEEDS_REPAIR) for t in ("validate_x3d", "validate_semantic", "repair_x3d")}


# ---------------------------------------------------------------- output helpers

def test_count_nodes_matches_lxml_and_ignores_non_xml():
    data = big_scene(50).encode()
    counted = outputs.count_nodes(data)
    assert counted["nodes"] == element_count(data.decode()) and counted["node_types"]["Transform"] == 50
    assert list(counted["node_types"])[:1] == ["Transform"] or counted["node_types"]["Transform"] == 50
    assert outputs.count_nodes(b'{"X3D": {}}') is None and outputs.count_nodes(b"VRML") is None
    assert outputs.count_nodes(b"<a><b></a>") is None
    ns = b'<a xmlns="urn:x"><b/><b/><!-- c --></a>'
    assert outputs.count_nodes(ns) == {"nodes": 3, "node_types": {"b": 2, "a": 1}}


def test_cap_items_and_clip_deep():
    assert outputs.cap_items([1, 2, 3], "xs", 5) == {"xs": [1, 2, 3], "xs_total": 3}
    assert outputs.cap_items([1, 2, 3], "xs", 2) == {"xs": [1, 2], "xs_total": 3, "xs_truncated": True}
    deep = outputs.clip_deep({"a": ["y" * 500, {"b": "z" * 400}], "n": 3})
    assert deep["a"][0].endswith("[200 more characters]") and deep["a"][1]["b"].endswith("[100 more characters]")
    assert deep["n"] == 3


# ---------------------------------------------------------------- compatibility

def test_tool_signatures_only_gained_optional_appended_parameters():
    m = FastMCP("sig")
    validate_tool.register(m)
    scene_ops.register(m)
    convert.register(m)
    props = {t.name: t.inputSchema for t in asyncio.run(m.list_tools())}
    before = {                                  # parameter order and which were required, at 8dd0d3e
        "validate_x3d": (["content", "encoding", "path"], []),
        "validate_semantic": (["content", "path"], []),
        "autofix_x3d": (["content", "path"], []),
        "repair_x3d": (["content", "path"], []),
        "convert_x3d": (["content", "from_encoding", "to_encoding"], ["content", "from_encoding", "to_encoding"]),
        "modify_x3d_node": (["content", "def_name", "field_changes"], ["content", "def_name", "field_changes"]),
        "remove_x3d_node": (["content", "def_name", "node_type", "index"], ["content"]),
        "move_x3d_node": (["content", "def_name", "new_parent_def"], ["content", "def_name"]),
    }
    for tool, (old_params, old_required) in before.items():
        names = list(props[tool]["properties"])
        assert names[:len(old_params)] == old_params, tool
        assert names[len(old_params):] == ["blob_id", "return_mode"], tool
        assert props[tool].get("required", []) == old_required, tool       # nothing became required
        assert props[tool]["properties"]["blob_id"]["default"] == ""
        assert props[tool]["properties"]["return_mode"]["default"] == "full"
        assert "ctx" not in props[tool]["properties"]



def test_stdio_default_is_unchanged_for_callers_that_never_heard_of_blobs(mcp):
    """No env at all (stdio): the store is on, but a plain inline call is answered exactly as before
    and creates nothing."""
    assert blobs.get_config().enabled and not blobs.get_config().http
    before = blobs.get_store().stats()["blobs"]
    out = json.loads(run(mcp, "validate_x3d", content=SAMPLE))
    assert out == validate_xml(SAMPLE)
    assert run(mcp, "remove_x3d_node", content=SAMPLE, def_name="Sun").startswith("<?xml")
    assert blobs.get_store().stats()["blobs"] == before == 0
