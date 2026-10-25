"""splat_inspect and splat_chunk as MCP tools: inputs by blob_id and path, the blob store and out_dir outputs,
limits, the transport rules, worker-thread offload, server wiring, and (when the files exist) the acceptance
numbers on the real 500,000-splat files."""

import asyncio
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from mcp.server.fastmcp import FastMCP

from . import splat_fixtures as fx
from .splat_fixtures import SPLAT_DIR, make_cloud, needs_real, real, write_spz

SRC = Path(__file__).resolve().parent.parent / "src"

from tools import splats, validate_tool
from tools import transfer as blob_tools
from transfer import blobs, chunk
from transfer.blobs import BlobConfig
from transfer.inspect_splats import inspect_bytes
from transfer.limits import SplatLimits
from validation.semantic import validate_semantic
from validation.validate import validate_xml

TOKEN = "t" * 8 + "k" * 40


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in [n for n in os.environ if n.startswith(("X3D_MCP_BLOB", "X3D_MCP_ALLOWED", "X3D_MCP_SPLAT",
                                                        "X3D_MCP_OFFLOAD", "MCP_TRANSPORT"))]:
        monkeypatch.delenv(name, raising=False)
    blobs.reset()
    yield
    blobs.reset()


@pytest.fixture
def mcp():
    m = FastMCP("splat-tools-test")
    splats.register(m)
    validate_tool.register(m)
    blob_tools.register(m)
    return m


def run(mcp, tool: str, /, **args) -> dict | str:
    out = asyncio.run(mcp.call_tool(tool, args))
    if isinstance(out, tuple):
        out = out[0]
    text = out[0].text
    try:
        return json.loads(text, parse_constant=lambda c: pytest.fail(f"{c} is not strict JSON"))
    except json.JSONDecodeError:
        return text


def put(data: bytes, media: str = "application/octet-stream") -> str:
    return blobs.get_store().put(data, media_type=media)["blob_id"]


def spz_bytes(n=3000, sh_degree=1, seed=3, **kw) -> bytes:
    return write_spz(make_cloud(n, sh_degree=sh_degree, seed=seed), fractional_bits=kw.pop("fractional_bits", 10), **kw)


@pytest.fixture(scope="module")
def spz_file(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("splat") / "demo.spz"
    p.write_bytes(spz_bytes())
    return p


def stored_files(result: dict) -> dict[str, bytes]:
    """The envelope's files as the blob store holds them, keyed by their path in the envelope."""
    store = blobs.get_store()
    entries = [result["manifest"], result["master"], *result["chunk_files"]]
    return {e["file"]: store.get_bytes(e["blob_id"]) for e in entries}


# ---------------------------------------------------------------- registration and input

def test_both_tools_are_registered_with_optional_path_and_blob_id(mcp):
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert {"splat_inspect", "splat_chunk"} <= set(tools)
    for name in ("splat_inspect", "splat_chunk"):
        props = tools[name].inputSchema["properties"]
        assert props["path"]["default"] == "" and props["blob_id"]["default"] == ""
        assert "ctx" not in props and not tools[name].inputSchema.get("required")
        assert tools[name].outputSchema is None            # structured_output=False: the report is not sent twice


def test_inspect_by_blob_id_equals_the_function_and_by_path_agrees(mcp, spz_file):
    data = spz_file.read_bytes()
    want = inspect_bytes(data)
    by_blob = run(mcp, "splat_inspect", blob_id=put(data))
    by_path = run(mcp, "splat_inspect", path=str(spz_file))
    assert by_blob.pop("source").startswith("blob ") and by_path.pop("source") == f"file {spz_file}"
    assert by_blob == by_path == json.loads(json.dumps(want))
    assert by_blob["count"] == 3000 and by_blob["sh_degree"] == 1 and by_blob["verdict"]["level"] == "ok"


def test_inspect_accepts_ply_and_x3d_text(mcp):
    cloud = make_cloud(500)
    assert run(mcp, "splat_inspect", blob_id=put(fx.write_ply(cloud)))["format"] == "ply"
    from transfer.x3dtext import digit_specs, splat_node, x3d_document
    text = x3d_document(splat_node(cloud, digit_specs("bridge", cloud))).encode()
    assert run(mcp, "splat_inspect", blob_id=put(text))["format"] == "x3d-xml"


def test_exactly_one_input_is_required(mcp, spz_file):
    bid = put(spz_bytes())
    for tool in ("splat_inspect", "splat_chunk"):
        assert "exactly one of" in run(mcp, tool)["error"]
        assert "exactly one of" in run(mcp, tool, path=str(spz_file), blob_id=bid)["error"]
        assert "64 hexadecimal" in run(mcp, tool, blob_id="../x")["error"]
        assert "no blob" in run(mcp, tool, blob_id="0" * 64)["error"].lower()
        assert "file not found" in run(mcp, tool, path="/no/such/file.spz")["error"]


def test_unreadable_input_is_an_error_object_not_an_exception(mcp):
    for junk in (b"", b"PK\x03\x04 not a splat", b"{}", os.urandom(200)):
        assert "error" in run(mcp, "splat_inspect", blob_id=put(junk)), junk[:8]
        assert "error" in run(mcp, "splat_chunk", blob_id=put(junk), name="x"), junk[:8]


def test_report_is_strict_json_even_for_overflowing_values(mcp):
    c = make_cloud(400)
    c.scales[:5] = 1e30
    c.orientations[:5] = 1e30
    c.opacities[5] = 1e30
    out = run(mcp, "splat_inspect", blob_id=put(fx.write_ply(c)))       # run() fails the test on NaN or Infinity
    assert out["verdict"]["level"] in ("suspect", "unusable")


# ---------------------------------------------------------------- splat_chunk into the blob store

def test_chunk_stores_every_file_by_its_sha256_and_returns_ids_not_contents(mcp):
    data = spz_bytes(6000, sh_degree=1)
    res = run(mcp, "splat_chunk", blob_id=put(data), name="demo", target=900)
    assert res["stored"] == "blobs" and res["partition"]["chunks"] == 7 and res["chunk_files_total"] == 7
    assert res["source"] == {"format": "spz-3", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                             "splats": 6000, "sh_degree": 1}
    assert len(json.dumps(res)) < 6000                                  # a summary, never the files
    files = stored_files(res)
    for entry in (res["manifest"], res["master"], *res["chunk_files"]):
        assert entry["blob_id"] == entry["sha256"] == hashlib.sha256(files[entry["file"]]).hexdigest()
        assert entry["bytes"] == len(files[entry["file"]])
    manifest = json.loads(files["manifest.json"])
    assert chunk.verify_manifest(files, manifest) == []
    assert manifest["totals"] == res["totals"] and sum(c["splats"] for c in manifest["chunks"]) == 6000
    assert [e["splats"] for e in res["chunk_files"]] == [c["splats"] for c in manifest["chunks"]]
    assert res["blob_expires"] > time.time() and "bundle" not in res


def test_every_emitted_file_validates_with_validate_x3d_and_validate_semantic(mcp):
    res = run(mcp, "splat_chunk", blob_id=put(spz_bytes(6000, sh_degree=2)), target=1500)
    entries = [res["master"], *res["chunk_files"]]
    assert len(entries) == 1 + res["partition"]["chunks"] > 3
    for e in entries:
        text = blobs.get_store().get_text(e["blob_id"])
        assert validate_xml(text) == {"valid": True, "errors": [], "schema_version": "4.1"}, e["file"]
        by_tool = run(mcp, "validate_x3d", blob_id=e["blob_id"], return_mode="summary")
        assert by_tool["valid"] is True and by_tool["error_count"] == 0 and "verdict" not in by_tool
        report = validate_semantic(text)
        assert "0 error(s), 0 warning(s)" in report or report.startswith("# Semantic Check: All Clear"), (e["file"], report[:400])
        sem = run(mcp, "validate_semantic", blob_id=e["blob_id"], return_mode="summary")
        assert sem["ok"] and sem["errors"] == 0 and sem["warnings"] == 0


def test_validate_true_reports_xsd_and_semantic_verdicts(mcp):
    res = run(mcp, "splat_chunk", blob_id=put(spz_bytes(4000)), target=1000, validate=True)
    assert res["validation"] == {"files": 5, "xsd_valid": 5, "semantic_errors": 0, "semantic_warnings": 0,
                                 "ok": True, "failures": []}
    assert "validation" not in run(mcp, "splat_chunk", blob_id=put(spz_bytes(4000)), target=1000)


def test_bundle_is_a_deterministic_tar_of_the_same_files(mcp):
    bid = put(spz_bytes(3000, sh_degree=0))
    a = run(mcp, "splat_chunk", blob_id=bid, target=800, bundle=True)
    b = run(mcp, "splat_chunk", blob_id=bid, target=800, bundle=True)
    assert a["bundle"]["blob_id"] == b["bundle"]["blob_id"] and a["manifest"] == b["manifest"]      # same input, same ids
    with tarfile.open(fileobj=io.BytesIO(blobs.get_store().get_bytes(a["bundle"]["blob_id"]))) as tar:
        in_tar = {m.name: tar.extractfile(m).read() for m in tar.getmembers()}
    assert in_tar == stored_files(a)
    assert blobs.get_store().stat(a["bundle"]["blob_id"])["media_type"] == "application/x-tar"


def test_chunk_media_types_and_names_are_recorded(mcp):
    res = run(mcp, "splat_chunk", blob_id=put(spz_bytes(1500)), target=600)
    st = blobs.get_store()
    assert st.stat(res["master"]["blob_id"])["media_type"] == "model/x3d+xml"
    assert st.stat(res["chunk_files"][0]["blob_id"])["media_type"] == "model/x3d+xml"
    assert st.stat(res["manifest"]["blob_id"])["media_type"] == "application/json"
    assert st.stat(res["chunk_files"][0]["blob_id"])["name"] == "chunks/c000.x3d"


def test_options_reach_the_envelope(mcp):
    res = run(mcp, "splat_chunk", blob_id=put(spz_bytes(3000, sh_degree=0)), target=700, method="grid", precision="bridge",
              coordinate_system="rdf", bbox_mode="tight", name="my-scene_1", url_prefix="https://h.example/s/")
    files = stored_files(res)
    man = json.loads(files["manifest.json"])
    assert (man["partition"]["method"], man["coordinate_system"], man["bbox_mode"], man["name"]) == ("grid", "rdf", "tight", "my-scene_1")
    assert man["digits"]["positions"] == "%.6f"
    master = files["scene.x3d"].decode()
    assert 'rotation="1 0 0 3.14159265"' in master and "https://h.example/s/chunks/c000.x3d" in master and "my-scene_1" in master


def test_ply_source_chunks_and_x3d_text_source_says_what_was_dropped(mcp):
    ply = fx.write_ply(make_cloud(2000, sh_degree=1))
    res = run(mcp, "splat_chunk", blob_id=put(ply), target=700)
    assert res["source"]["format"] == "ply" and res["source"]["splats"] == 2000 and res["partition"]["chunks"] == 3
    text = stored_files(res)["chunks/c000.x3d"]
    again = run(mcp, "splat_chunk", blob_id=put(text), target=100)
    assert again["source"]["format"] == "x3d-xml" and again["source"]["splats"] == res["chunk_files"][0]["splats"]
    assert any("Only the GaussianSplats nodes" in n for n in again["notes"])
    assert not any("Only the GaussianSplats" in n for n in res["notes"])


def test_bad_arguments_are_errors_that_say_which(mcp):
    bid = put(spz_bytes(500, sh_degree=0))
    cases = [(dict(method="bsp"), "unknown method"), (dict(target=0), "at least 1"), (dict(name="a b"), "name"),
             (dict(name="../x"), "name"), (dict(precision="fine"), "precision"), (dict(coordinate_system="ruf"), "coordinate_system"),
             (dict(bbox_mode="loose"), "bbox_mode"), (dict(url_prefix='a"b'), "url_prefix")]
    for kw, want in cases:
        assert want in run(mcp, "splat_chunk", blob_id=bid, **kw)["error"], kw
    assert "error" not in run(mcp, "splat_chunk", blob_id=bid, precision="lattice")             # an SPZ source is fine
    assert "lattice" in run(mcp, "splat_chunk", blob_id=put(fx.write_ply(make_cloud(100))), precision="lattice")["error"]


def test_non_finite_splats_fail_unless_dropped(mcp):
    c = make_cloud(600, sh_degree=0)
    c.positions[3, 0] = np.nan
    bid = put(fx.write_ply(c))
    assert "1 splats hold NaN or infinity" in run(mcp, "splat_chunk", blob_id=bid)["error"]
    res = run(mcp, "splat_chunk", blob_id=bid, drop_invalid=True, target=300)
    assert res["source"]["splats"] == 599 and res["source"]["dropped_non_finite"] == 1


# ---------------------------------------------------------------- out_dir

def test_out_dir_writes_the_same_files_the_blob_store_holds(mcp, spz_file, tmp_path):
    out = tmp_path / "env"
    res = run(mcp, "splat_chunk", path=str(spz_file), target=900, name="demo", out_dir=str(out), bundle=True)
    assert res["stored"] == "directory" and res["out_dir"] == str(out.resolve()) and "blob_id" not in res["master"]
    on_disk = {str(p.relative_to(out)): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    assert sorted(on_disk) == sorted(["manifest.json", "scene.x3d", "bundle.tar"] + [e["file"] for e in res["chunk_files"]])
    assert Path(res["master"]["path"]) == out.resolve() / "scene.x3d"
    for entry in (res["manifest"], res["master"], res["bundle"], *res["chunk_files"]):
        assert hashlib.sha256(on_disk[entry["file"]]).hexdigest() == entry["sha256"] and len(on_disk[entry["file"]]) == entry["bytes"]
    assert chunk.verify_manifest(on_disk, json.loads(on_disk["manifest.json"])) == []
    assert "blob_expires" not in res and blobs.get_store().stats()["blobs"] == 0                  # nothing went to the store
    via_blob = run(mcp, "splat_chunk", blob_id=put(spz_file.read_bytes()), target=900, name="demo")
    assert {k: v for k, v in stored_files(via_blob).items()} == {k: v for k, v in on_disk.items() if k != "bundle.tar"}


def test_out_dir_never_overwrites_or_follows_links(mcp, spz_file, tmp_path, monkeypatch):
    busy = tmp_path / "busy"
    busy.mkdir()
    keep = busy / "scene.x3d"
    keep.write_text("mine")
    assert "not an empty folder" in run(mcp, "splat_chunk", path=str(spz_file), out_dir=str(busy))["error"]
    assert keep.read_text() == "mine"
    file_dir = tmp_path / "afile"
    file_dir.write_text("x")
    assert "not an empty folder" in run(mcp, "splat_chunk", path=str(spz_file), out_dir=str(file_dir))["error"]
    empty = tmp_path / "empty"
    empty.mkdir()
    assert run(mcp, "splat_chunk", path=str(spz_file), out_dir=str(empty), target=2000)["stored"] == "directory"
    assert run(mcp, "splat_chunk", path=str(spz_file), out_dir=str(empty), target=2000)["error"].endswith("pass a new or empty one")
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    planted = tmp_path / "planted"
    (planted / "chunks").mkdir(parents=True)
    # a link planted after the emptiness check cannot be followed or overwritten (O_EXCL | O_NOFOLLOW)
    (planted / "scene.x3d").symlink_to(victim)
    monkeypatch.setattr(splats, "_check_out_dir", lambda d: Path(d))
    with pytest.raises(ValueError, match="could not write"):
        splats._write_dir({"scene.x3d": b"overwritten"}, str(planted))
    assert victim.read_text() == "precious"


def test_a_failed_chunk_leaves_no_out_dir_behind(mcp, tmp_path):
    out = tmp_path / "nothing"
    assert "error" in run(mcp, "splat_chunk", blob_id=put(b"not a splat file"), out_dir=str(out))
    assert not out.exists()


def test_an_envelope_that_does_not_fit_the_store_is_an_error_and_leaves_nothing_behind(mcp, monkeypatch):
    src = spz_bytes(6000, sh_degree=0)
    bid = put(src)
    ok = run(mcp, "splat_chunk", blob_id=bid, target=1000)
    assert "error" not in ok
    total = blobs.get_store().stats()["total"]
    blobs.reset()
    quota = (total - len(src)) * 3 // 4                    # room for any one file, not for all of them
    monkeypatch.setenv("X3D_MCP_BLOB_QUOTA_BYTES", str(quota))
    monkeypatch.setenv("X3D_MCP_BLOB_MEM_BYTES", str(quota))
    bid = put(src)
    err = run(mcp, "splat_chunk", blob_id=bid, target=1000)
    assert isinstance(err, dict) and "do not fit in the blob store" in err["error"] and "X3D_MCP_BLOB_QUOTA_BYTES" in err["error"]
    assert blobs.get_store().stats()["blobs"] <= 1                     # at most the input; no part of the envelope stays
    monkeypatch.setenv("X3D_MCP_BLOB_QUOTA_BYTES", str(total * 2))      # a store that fits gives ids that all resolve
    monkeypatch.setenv("X3D_MCP_BLOB_MEM_BYTES", str(total * 2))
    blobs.reset()
    good = run(mcp, "splat_chunk", blob_id=put(src), target=1000)
    assert len(stored_files(good)) == 2 + len(good["chunk_files"])


# ---------------------------------------------------------------- limits

def test_server_limits_apply_to_both_tools(mcp, monkeypatch):
    bid = put(spz_bytes(3000, sh_degree=0))
    monkeypatch.setenv("X3D_MCP_SPLAT_MAX_POINTS", "2000")
    assert "above the limit of 2,000" in run(mcp, "splat_chunk", blob_id=bid)["error"]
    rep = run(mcp, "splat_inspect", blob_id=bid)                 # inspect still reports the header facts
    assert rep["count"] == 3000 and rep["verdict"]["level"] == "unusable" and "too-large" in {r["code"] for r in rep["verdict"]["reasons"]}
    monkeypatch.delenv("X3D_MCP_SPLAT_MAX_POINTS")
    monkeypatch.setenv("X3D_MCP_SPLAT_MAX_RATIO", "0.5")
    assert "ratio limit" in run(mcp, "splat_chunk", blob_id=bid)["error"]
    assert "ratio limit" in run(mcp, "splat_inspect", blob_id=bid)["error"]
    monkeypatch.setenv("X3D_MCP_SPLAT_MAX_RATIO", "100")
    assert "error" not in run(mcp, "splat_chunk", blob_id=bid)
    monkeypatch.delenv("X3D_MCP_SPLAT_MAX_RATIO")
    monkeypatch.setenv("X3D_MCP_SPLAT_MAX_OUTPUT_BYTES", "100K")
    err = run(mcp, "splat_chunk", blob_id=bid)["error"]
    assert "X3D_MCP_SPLAT_MAX_OUTPUT_BYTES" in err and "above the limit of 102,400" in err
    monkeypatch.delenv("X3D_MCP_SPLAT_MAX_OUTPUT_BYTES")
    from transfer.x3dtext import digit_specs, splat_node, x3d_document
    cloud = make_cloud(500)
    text = x3d_document(splat_node(cloud, digit_specs("bridge", cloud))).encode()
    monkeypatch.setenv("X3D_MCP_SPLAT_MAX_TEXT_BYTES", str(len(text) - 1))
    assert "above the limit" in run(mcp, "splat_chunk", blob_id=put(text))["error"]
    assert "above the limit" in run(mcp, "splat_inspect", blob_id=put(text))["error"]


def test_limits_come_from_the_environment_and_bad_values_are_named():
    from transfer.errors import ConfigError
    d = SplatLimits.from_env({})
    assert (d.max_points, d.max_ratio, d.max_text_bytes, d.max_output_bytes) == (10_000_000, None, 256 << 20, 512 << 20)
    got = SplatLimits.from_env({"X3D_MCP_SPLAT_MAX_POINTS": "5000", "X3D_MCP_SPLAT_MAX_RATIO": "100",
                                "X3D_MCP_SPLAT_MAX_TEXT_BYTES": "1M", "X3D_MCP_SPLAT_MAX_OUTPUT_BYTES": "2G"})
    assert (got.max_points, got.max_ratio, got.max_text_bytes, got.max_output_bytes) == (5000, 100.0, 1 << 20, 2 << 30)
    for name, value in (("X3D_MCP_SPLAT_MAX_POINTS", "lots"), ("X3D_MCP_SPLAT_MAX_POINTS", "0"), ("X3D_MCP_SPLAT_MAX_RATIO", "x"),
                        ("X3D_MCP_SPLAT_MAX_TEXT_BYTES", "-1"), ("X3D_MCP_SPLAT_MAX_OUTPUT_BYTES", "1.5G")):
        with pytest.raises(ConfigError, match=name):
            SplatLimits.from_env({name: value})


# ---------------------------------------------------------------- transport rules

class _Req:
    def __init__(self, auth=""):
        self.headers = {"authorization": auth} if auth else {}


def _ctx(auth=""):
    return SimpleNamespace(request_context=SimpleNamespace(request=_Req(auth)))


def splats_tools() -> dict:
    """The two tool functions, registered on a throwaway server so they can be called with a stand-in Context."""
    m = FastMCP("direct")
    splats.register(m)
    return {t.name: t.fn for t in m._tool_manager.list_tools()}


def call_with(ctx, tool: str, **args) -> dict:
    return json.loads(asyncio.run(splats_tools()[tool](ctx=ctx, **args)))


def http_config(**kw) -> BlobConfig:
    return BlobConfig(enabled=True, http=True, token=TOKEN, **kw)


def test_over_http_path_and_out_dir_are_refused_and_the_blob_needs_the_bearer(spz_file, tmp_path):
    blobs.configure(http_config())
    bid = put(spz_bytes())
    for tool in ("splat_inspect", "splat_chunk"):
        assert "disabled over the HTTP transport" in call_with(_ctx("Bearer " + TOKEN), tool, path=str(spz_file))["error"]
        assert "Authorization: Bearer" in call_with(_ctx(), tool, blob_id=bid)["error"]
        assert "Authorization: Bearer" in call_with(_ctx("Bearer wrong" + TOKEN), tool, blob_id=bid)["error"]
    assert "out_dir is disabled over the HTTP transport" in call_with(_ctx("Bearer " + TOKEN), "splat_chunk", blob_id=bid,
                                                                      out_dir=str(tmp_path / "x"))["error"]
    assert not (tmp_path / "x").exists()
    good = _ctx("Bearer " + TOKEN)
    assert call_with(good, "splat_inspect", blob_id=bid)["count"] == 3000
    assert call_with(good, "splat_chunk", blob_id=bid, target=1000)["stored"] == "blobs"


def test_path_is_refused_when_the_transport_env_says_http(spz_file, monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
    blobs.reset()
    assert "disabled over the HTTP transport" in call_with(None, "splat_inspect", path=str(spz_file))["error"]
    assert "blob store is not enabled" in call_with(None, "splat_chunk", path=str(spz_file))["error"]      # no token: no store


def test_with_the_blob_store_off_path_and_out_dir_still_work_and_the_error_says_so(monkeypatch, spz_file, tmp_path):
    monkeypatch.setenv("X3D_MCP_BLOB", "off")
    blobs.reset()
    assert call_with(None, "splat_inspect", path=str(spz_file))["count"] == 3000
    err = call_with(None, "splat_chunk", path=str(spz_file))["error"]
    assert "blob store is not enabled" in err and "out_dir" in err
    assert call_with(None, "splat_chunk", path=str(spz_file), out_dir=str(tmp_path / "o"), target=1000)["stored"] == "directory"
    assert "blob store is not enabled" in call_with(None, "splat_inspect", blob_id="a" * 64)["error"]


def test_jobs_run_in_a_worker_thread_not_on_the_event_loop(monkeypatch):
    seen = []
    real_inspect, real_chunk = splats._inspect_job, splats._chunk_job
    monkeypatch.setattr(splats, "_inspect_job", lambda *a: (seen.append(threading.current_thread()), real_inspect(*a))[1])
    monkeypatch.setattr(splats, "_chunk_job", lambda *a: (seen.append(threading.current_thread()), real_chunk(*a))[1])
    bid = put(spz_bytes(300, sh_degree=0))
    fns = splats_tools()

    async def both():
        loop_thread = threading.current_thread()
        await fns["splat_inspect"](blob_id=bid)
        await fns["splat_chunk"](blob_id=bid)
        return loop_thread

    loop_thread = asyncio.run(both())
    assert len(seen) == 2 and all(t is not loop_thread for t in seen)


def test_the_event_loop_keeps_answering_while_a_chunk_runs(monkeypatch):
    gate, started = threading.Event(), threading.Event()
    real_chunk = splats._chunk_job

    def slow(*a):
        started.set()
        assert gate.wait(10)
        return real_chunk(*a)

    monkeypatch.setattr(splats, "_chunk_job", slow)
    bid = put(spz_bytes(300, sh_degree=0))
    fn = splats_tools()["splat_chunk"]

    async def go():
        task = asyncio.create_task(fn(blob_id=bid))
        for _ in range(200):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        ticks = 0
        for _ in range(5):                              # the loop is free: these sleeps return while the job is parked
            await asyncio.sleep(0.01)
            ticks += 1
        gate.set()
        return ticks, json.loads(await task)

    ticks, res = asyncio.run(go())
    assert ticks == 5 and res["stored"] == "blobs"


# ---------------------------------------------------------------- numpy is optional

def _probe(code: str, env=None, *args: str) -> subprocess.CompletedProcess:
    full = {k: v for k, v in os.environ.items() if not k.startswith(("X3D_MCP", "MCP_TRANSPORT"))}
    full.update(env or {})
    return subprocess.run([sys.executable, "-c", code, str(SRC), *args], env=full, capture_output=True, text=True, timeout=120)


def test_without_numpy_the_tools_say_how_to_install_it_and_the_server_skips_them(spz_file):
    out = _probe(
        "import asyncio, json, sys\n"
        "sys.modules['numpy'] = None\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import server\n"
        "from tools import splats\n"
        "names = sorted(t.name for t in asyncio.run(server.mcp.list_tools()) if t.name.startswith('splat'))\n"
        "from mcp.server.fastmcp import FastMCP\n"
        "m = FastMCP('x'); splats.register(m)\n"
        "res = asyncio.run(m.call_tool('splat_inspect', {'path': sys.argv[2]}))\n"
        "text = (res[0] if isinstance(res, tuple) else res)[0].text\n"
        "print(json.dumps({'available': splats.numpy_available(), 'names': names, 'text': text, "
        "'instructions': 'GAUSSIAN SPLATS' in server.INSTRUCTIONS}))\n", None, str(spz_file))
    assert out.returncode == 0, out.stderr[-600:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["available"] is False and got["names"] == [] and got["instructions"] is False
    assert "pip install 'x3d-mcp[splat]'" in json.loads(got["text"])["error"]


PROBE = (
    "import asyncio, json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import server\n"
    "print(json.dumps({'tools': sorted(t.name for t in asyncio.run(server.mcp.list_tools()) if t.name.startswith('splat')), "
    "'instructions': 'GAUSSIAN SPLATS' in server.INSTRUCTIONS}))\n"
)


def test_server_registers_the_splat_tools_where_a_client_can_use_them():
    both = ["splat_chunk", "splat_inspect"]
    cases = {
        "stdio": ({}, {"tools": both, "instructions": True}),
        "stdio-blob-off": ({"X3D_MCP_BLOB": "off"}, {"tools": both, "instructions": True}),           # path still works
        "http-no-token": ({"MCP_TRANSPORT": "streamable-http"}, {"tools": [], "instructions": False}),  # no path, no blobs
        "http-token": ({"MCP_TRANSPORT": "streamable-http", "X3D_MCP_BLOB_TOKEN": TOKEN}, {"tools": both, "instructions": True}),
    }
    for label, (env, want) in cases.items():
        p = _probe(PROBE, env)
        assert p.returncode == 0, (label, p.stderr[-600:])
        assert json.loads(p.stdout.strip().splitlines()[-1]) == want, label


def test_server_refuses_to_start_on_a_bad_splat_limit_and_names_the_variable():
    p = _probe(PROBE, {"X3D_MCP_SPLAT_MAX_POINTS": "lots"})
    assert p.returncode != 0 and "X3D_MCP_SPLAT_MAX_POINTS" in p.stderr


# ---------------------------------------------------------------- the real files (X3D_SPLAT_FIXTURES)

@needs_real
@pytest.mark.parametrize("name,count", [("pantheon.spz", 500000), ("athens.spz", 497556)])
def test_real_spz_inspect_then_chunk_meets_the_acceptance_numbers(mcp, name, count):
    bid = put(real(name))
    t = time.perf_counter()
    rep = run(mcp, "splat_inspect", blob_id=bid)
    assert time.perf_counter() - t < 5                              # measured 0.4 to 0.6 s; the bound is loose on purpose
    assert rep["count"] == count and rep["verdict"]["level"] == "ok" and rep["x3d_text"]["chunks_needed"] == 7

    t = time.perf_counter()
    res = run(mcp, "splat_chunk", blob_id=bid, name=name.split(".")[0])
    assert time.perf_counter() - t < 20                             # measured 1.5 to 1.8 s to build, plus storing the files
    part = res["partition"]
    assert part["method"] == "kd" and part["chunks"] == 10 and part["estimate_corrected"] is False
    assert part["max_splats"] <= 50000 and part["min_splats"] >= part["max_splats"] - 1            # kd: near-equal counts
    assert res["source"]["splats"] == count and sum(e["splats"] for e in res["chunk_files"]) == count
    assert max(e["bytes"] for e in res["chunk_files"]) < 5_300_000                      # largest start tag 5.0 MB at 50,000 splats
    assert 48_000_000 < res["totals"]["chunk_bytes"] < 52_000_000                       # 49.6 MB (pantheon), about 50 MB (athens)
    files = stored_files(res)
    assert chunk.verify_manifest(files, json.loads(files["manifest.json"])) == []
    assert all(len(files[e["file"]].split(b"<GaussianSplats", 1)[1].split(b"/>", 1)[0]) < chunk.START_TAG_BUDGET for e in res["chunk_files"])


@needs_real
def test_real_pantheon_envelope_validates_in_full(mcp):
    res = run(mcp, "splat_chunk", blob_id=put(real("pantheon.spz")), name="pantheon", validate=True)
    assert res["validation"] == {"files": 11, "xsd_valid": 11, "semantic_errors": 0, "semantic_warnings": 0,
                                 "ok": True, "failures": []}
    for e in (res["master"], res["chunk_files"][0], res["chunk_files"][-1]):
        assert run(mcp, "validate_x3d", blob_id=e["blob_id"], return_mode="summary")["valid"] is True


@needs_real
def test_real_pantheon_text_survives_the_round_trip_to_spz_integers():
    """Lattice digits recover the SPZ integers: positions, alphas, colours and scales exactly, rotation words nearly."""
    from transfer import spz
    from transfer.inspect_splats import cloud_from_x3d
    data = real("pantheon.spz")
    packed = spz.unpack(data)
    files, man = chunk.build_envelope(spz.decode(packed), name="p", target=50000)
    parts = [cloud_from_x3d(files[c["file"]].decode(), max_points=10 ** 7)[0] for c in man["chunks"]]
    text = fx.SplatCloud(*[np.concatenate([getattr(p, f) for p in parts]) for f in ("positions", "scales", "orientations", "opacities", "dc")])
    streams, _ = fx.pack_streams(text, fractional_bits=10)

    def rows(pos, alpha, color, scale):
        return np.concatenate([pos.reshape(-1, 9), alpha.reshape(-1, 1), color.reshape(-1, 3), scale.reshape(-1, 3)], axis=1)

    a = rows(streams[0], streams[1], streams[2], streams[3])
    b = rows(packed.positions, packed.alphas, packed.colors, packed.scales)
    key = lambda m: np.lexsort(m.T[::-1])
    assert np.array_equal(a[key(a)], b[key(b)])                      # 100% of the integer streams, as a multiset of splats


@needs_real
def test_real_text_scene_gets_a_verdict_and_can_be_chunked(mcp):
    path = Path(SPLAT_DIR) / "txt" / "pantheon_lattice.x3d"
    if not path.exists():
        pytest.skip("text scene not available")
    text = path.read_text()
    t = time.perf_counter()
    r = validate_xml(text)
    assert time.perf_counter() - t < 30                              # measured 3.7 to 3.9 s
    assert r["valid"] is False and r["checked"] is False and r["verdict"] == "not-checkable-at-this-size"
    assert not any("XML parse error" in e for e in r["errors"])
    tag = r["oversize"]["tags"][0]
    assert tag["element"] == "GaussianSplats" and tag["splats"] == 500000 and tag["array_check"]["errors"] == []
    assert r["oversize"]["structure_check"]["ran"] is True and r["oversize"]["structure_check"]["valid"] is True
    summary = run(mcp, "validate_x3d", blob_id=put(text.encode()), return_mode="summary")
    assert summary["valid"] is False and summary["verdict"] == "not-checkable-at-this-size" and summary["checked"] is False
    res = run(mcp, "splat_chunk", blob_id=put(text.encode()), name="from-text")
    assert res["source"]["format"] == "x3d-xml" and res["partition"]["chunks"] >= 7
    assert sum(e["splats"] for e in res["chunk_files"]) == 500000
