"""The splat tools over a real HTTP server: upload an SPZ to /blob, inspect and chunk it by blob_id, download the
chunks from /blob, validate them with the existing tools. Nothing but ids and summaries crosses /mcp."""

import asyncio
import hashlib
import json
import threading
import time

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.fastmcp import FastMCP

from .splat_fixtures import make_cloud, write_spz

from tools import splats, validate_tool
from tools import transfer as blob_tools
from transfer import blobs, routes
from transfer.blobs import BlobConfig

pytestmark = pytest.mark.filterwarnings("ignore:Use `streamable_http_client` instead:DeprecationWarning")

TOKEN = "t" * 8 + "k" * 40
AUTH = {"Authorization": "Bearer " + TOKEN}
DATA = write_spz(make_cloud(6000, sh_degree=1), fractional_bits=10, antialiased=True)
SHA = hashlib.sha256(DATA).hexdigest()


class Live:
    def __init__(self, cfg: BlobConfig):
        blobs.configure(cfg)
        self.mcp = FastMCP("splat-http-test")
        splats.register(self.mcp)
        validate_tool.register(self.mcp)
        blob_tools.register(self.mcp)
        routes.register(self.mcp, cfg)
        self.server = uvicorn.Server(uvicorn.Config(self.mcp.streamable_http_app(), host="127.0.0.1", port=0, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.025)
        self.url = f"http://127.0.0.1:{self.server.servers[0].sockets[0].getsockname()[1]}"

    def close(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)
        blobs.reset()


@pytest.fixture
def live(monkeypatch):
    for name in ("MCP_TRANSPORT",):
        monkeypatch.delenv(name, raising=False)
    blobs.reset()
    srv = Live(BlobConfig(enabled=True, http=True, token=TOKEN, idle_timeout=1.0, max_bytes=64 << 20,
                          mem_bytes=32 << 20, quota_bytes=200 << 20))
    yield srv
    srv.close()


def call(url: str, headers, name: str, args: dict) -> dict | str:
    async def go():
        async with streamablehttp_client(url + "/mcp", headers=headers) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return (await s.call_tool(name, args)).content[0].text
    text = asyncio.run(go())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def upload(live) -> str:
    r = httpx.post(live.url + "/blob", content=DATA, headers={**AUTH, "Content-Type": "application/octet-stream"})
    assert r.status_code == 201 and r.json()["blob_id"] == SHA
    return SHA


def test_upload_inspect_chunk_download_validate(live):
    bid = upload(live)
    rep = call(live.url, AUTH, "splat_inspect", {"blob_id": bid})
    assert rep["count"] == 6000 and rep["sh_degree"] == 1 and rep["format"] == "spz" and rep["verdict"]["level"] == "ok"

    res = call(live.url, AUTH, "splat_chunk", {"blob_id": bid, "target": 1500, "name": "http-demo"})
    assert res["stored"] == "blobs" and res["partition"]["chunks"] == 4 and "out_dir" not in res
    master = httpx.get(f"{live.url}/blob/{res['master']['blob_id']}", headers=AUTH)
    assert master.status_code == 200 and hashlib.sha256(master.content).hexdigest() == res["master"]["sha256"]
    assert master.content.count(b"<Inline ") == 4
    manifest = json.loads(httpx.get(f"{live.url}/blob/{res['manifest']['blob_id']}", headers=AUTH).content)
    assert [c["sha256"] for c in manifest["chunks"]] == [e["sha256"] for e in res["chunk_files"]]
    for e in res["chunk_files"]:
        body = httpx.get(f"{live.url}/blob/{e['blob_id']}", headers=AUTH).content
        assert hashlib.sha256(body).hexdigest() == e["sha256"] and len(body) == e["bytes"]
        assert call(live.url, AUTH, "validate_x3d", {"blob_id": e["blob_id"], "return_mode": "summary"})["valid"] is True
        sem = call(live.url, AUTH, "validate_semantic", {"blob_id": e["blob_id"], "return_mode": "summary"})
        assert sem["errors"] == 0 and sem["warnings"] == 0


def test_the_bearer_and_the_transport_rules_hold_over_http(live, tmp_path):
    bid = upload(live)
    for tool in ("splat_inspect", "splat_chunk"):
        assert "Authorization: Bearer" in call(live.url, None, tool, {"blob_id": bid})["error"]
        f = tmp_path / "a.spz"
        f.write_bytes(DATA)
        assert "disabled over the HTTP transport" in call(live.url, AUTH, tool, {"path": str(f)})["error"]
    out = call(live.url, AUTH, "splat_chunk", {"blob_id": bid, "out_dir": str(tmp_path / "o")})
    assert "out_dir is disabled over the HTTP transport" in out["error"] and not (tmp_path / "o").exists()


def test_a_wrong_kind_of_blob_is_an_error_not_a_crash(live):
    r = httpx.post(live.url + "/blob", content=b"this is not a splat file", headers=AUTH)
    junk = r.json()["blob_id"]
    for tool in ("splat_inspect", "splat_chunk"):
        assert "unrecognised input" in call(live.url, AUTH, tool, {"blob_id": junk})["error"]
