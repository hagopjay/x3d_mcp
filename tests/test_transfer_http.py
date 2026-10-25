"""Transport tests: the /blob routes and the blob MCP tools over a real HTTP server.

Each test talks to a uvicorn server running in a thread of the test process (the store
is the process-wide one, so tests can also look inside it), except the last group, which
starts src/server.py as a subprocess to check the wiring under each transport.
"""

import asyncio
import base64
import gzip
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path

import brotli
import httpx
import pytest
import uvicorn
import zstandard
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.fastmcp import FastMCP

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from tools import transfer as blob_tools
from tools import validate_tool
from transfer import blobs, routes
from transfer.blobs import BlobConfig

pytestmark = pytest.mark.filterwarnings("ignore:Use `streamable_http_client` instead:DeprecationWarning")

TOKEN = "t" * 8 + "k" * 40
AUTH = {"Authorization": "Bearer " + TOKEN}
SCENE = ('<?xml version="1.0" encoding="UTF-8"?>\n<X3D profile="Interchange" version="4.0"><Scene>'
         + "".join(f'<Transform DEF="T{i}" translation="{i} 0 0"><Shape><Appearance><Material '
                   f'diffuseColor="0.5 0.2 0.3"/></Appearance><Box size="1 1 1"/></Shape></Transform>\n'
                   for i in range(4000))
         + "</Scene></X3D>\n")
RAW = SCENE.encode()
SHA = hashlib.sha256(RAW).hexdigest()


class Live:
    """A FastMCP app served by uvicorn in a thread."""

    def __init__(self, cfg: BlobConfig, host: str = "127.0.0.1"):
        blobs.configure(cfg)
        self.cfg = cfg
        self.mcp = FastMCP("transfer-test", host=host)
        blob_tools.register(self.mcp)
        validate_tool.register(self.mcp)
        routes.register(self.mcp, cfg)
        self.server = uvicorn.Server(uvicorn.Config(
            self.mcp.streamable_http_app(), host="127.0.0.1", port=0, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.025)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)
        blobs.reset()

    def stats(self) -> dict:
        return blobs.get_store().stats()


def live_cfg(tmp_path=None, **kw) -> BlobConfig:
    base = dict(enabled=True, http=True, token=TOKEN, idle_timeout=1.0, max_bytes=64 << 20,
                mem_bytes=32 << 20, quota_bytes=200 << 20, max_uploads=4)
    if tmp_path is not None:
        base["spill_dir"] = str(tmp_path / "spill")
    base.update(kw)
    return BlobConfig(**base)


@pytest.fixture
def live(tmp_path):
    srv = Live(live_cfg(tmp_path))
    yield srv
    srv.close()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in [n for n in os.environ if n.startswith(("X3D_MCP_BLOB", "X3D_MCP_ALLOWED"))]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    blobs.reset()
    yield
    blobs.reset()


def encode(codec: str, data: bytes) -> bytes:
    return {"identity": data, "gzip": gzip.compress(data, 6, mtime=0),
            "br": brotli.compress(data, quality=5),
            "zstd": zstandard.ZstdCompressor(level=3).compress(data)}[codec]


def raw_request(port: int, head: str, body: bytes = b"", read: float = 5.0) -> bytes:
    s = socket.create_connection(("127.0.0.1", port))
    try:
        s.sendall(head.encode() + body)
        s.settimeout(read)
        return s.recv(4000)
    except socket.timeout:
        return b""
    finally:
        s.close()


# ---------------------------------------------------------------- auth and host/origin

def test_every_route_needs_the_bearer_token(live):
    for method, path in (("POST", "/blob"), ("PUT", "/blob"), ("PUT", f"/blob/{SHA}"),
                         ("GET", f"/blob/{SHA}"), ("HEAD", f"/blob/{SHA}"), ("DELETE", f"/blob/{SHA}")):
        r = httpx.request(method, live.url + path, content=b"x")
        assert r.status_code == 401, (method, path)
        assert "Bearer" in r.headers["www-authenticate"]
    for bad in ("Bearer " + "x" * len(TOKEN), "Bearer", TOKEN, "Basic " + TOKEN,
                "Bearer " + TOKEN[:-1], "Bearer " + TOKEN + "x"):
        r = httpx.post(live.url + "/blob", content=b"x", headers={"Authorization": bad})
        assert r.status_code == 401, bad
    assert httpx.post(live.url + "/blob", content=b"x", headers=AUTH).status_code == 201
    assert live.stats()["blobs"] == 1                    # refused requests stored nothing


def test_bearer_scheme_is_case_insensitive_and_token_is_never_echoed(live):
    r = httpx.post(live.url + "/blob", content=b"x", headers={"Authorization": "bearer " + TOKEN})
    assert r.status_code == 201
    bad = httpx.post(live.url + "/blob", content=b"x", headers={"Authorization": "Bearer wrong"})
    assert TOKEN not in bad.text and TOKEN not in str(bad.headers)


def test_host_and_origin_checks_reuse_the_sdk_rules_on_a_loopback_bind(live):
    assert httpx.post(live.url + "/blob", content=b"x",
                      headers={**AUTH, "Host": "evil.example"}).status_code == 421
    assert httpx.post(live.url + "/blob", content=b"x",
                      headers={**AUTH, "Origin": "https://evil.example"}).status_code == 403
    assert httpx.post(live.url + "/blob", content=b"x",
                      headers={**AUTH, "Origin": f"http://127.0.0.1:{live.port}"}).status_code == 201
    # the check runs before the bearer: a bad Host never learns whether its token was right
    assert httpx.post(live.url + "/blob", headers={"Host": "evil.example"}).status_code == 421


def test_no_cors_headers_are_ever_sent(live):
    r = httpx.options(live.url + "/blob", headers={"Origin": "http://localhost:3000",
                                                   "Access-Control-Request-Method": "PUT"})
    assert not [h for h in r.headers if h.lower().startswith("access-control-")]
    r = httpx.post(live.url + "/blob", content=b"x", headers={**AUTH, "Origin": f"http://127.0.0.1:{live.port}"})
    assert not [h for h in r.headers if h.lower().startswith("access-control-")]


def test_hosted_bind_uses_the_env_allow_lists(tmp_path):
    srv = Live(live_cfg(tmp_path, allowed_hosts=("127.0.0.1:*",), allowed_origins=("http://good.example",)),
               host="0.0.0.0")                      # a hosted bind: the SDK enables nothing by itself
    try:
        assert httpx.post(srv.url + "/blob", content=b"x", headers={**AUTH, "Host": "evil.example"}).status_code == 421
        assert httpx.post(srv.url + "/blob", content=b"x",
                          headers={**AUTH, "Origin": "http://evil.example"}).status_code == 403
        assert httpx.post(srv.url + "/blob", content=b"x",
                          headers={**AUTH, "Origin": "http://good.example"}).status_code == 201
    finally:
        srv.close()


def test_without_allow_lists_a_hosted_bind_relies_on_the_bearer_alone(tmp_path):
    srv = Live(live_cfg(tmp_path), host="0.0.0.0")
    try:
        assert httpx.post(srv.url + "/blob", content=b"x",
                          headers={**AUTH, "Host": "anything.example"}).status_code == 201
    finally:
        srv.close()


# ---------------------------------------------------------------- upload

def test_post_identity_returns_id_and_dedupes(live):
    r = httpx.post(live.url + "/blob", content=RAW, headers={**AUTH, "Content-Type": "model/x3d+xml",
                                                              "X-Blob-Name": "scene.x3d"})
    body = r.json()
    assert r.status_code == 201 and body["blob_id"] == SHA == body["sha256"]
    assert body["size"] == len(RAW) and body["encoding"] == "zstd" and body["created"] is True
    assert body["ratio"] > 10 and body["media_type"] == "model/x3d+xml" and body["name"] == "scene.x3d"
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
    again = httpx.post(live.url + "/blob", content=RAW, headers=AUTH)
    assert again.status_code == 200 and again.json()["created"] is False
    assert live.stats()["blobs"] == 1


def test_put_with_a_known_id_is_answered_without_reading_the_body(live):
    httpx.post(live.url + "/blob", content=RAW, headers=AUTH)
    r = httpx.put(f"{live.url}/blob/{SHA}", content=b"", headers=AUTH)
    assert r.status_code == 200 and r.json()["blob_id"] == SHA and r.json()["created"] is False
    # a raw client announcing a huge body gets the answer before sending any of it
    head = (f"PUT /blob/{SHA} HTTP/1.1\r\nHost: 127.0.0.1:{live.port}\r\nAuthorization: Bearer {TOKEN}\r\n"
            "Content-Length: 5000000000\r\n\r\n")
    assert raw_request(live.port, head).startswith(b"HTTP/1.1 200")


def test_put_checks_the_stated_id_against_the_bytes(live):
    r = httpx.put(f"{live.url}/blob/{'0' * 64}", content=b"abc", headers=AUTH)
    assert r.status_code == 422 and r.json()["actual"] == hashlib.sha256(b"abc").hexdigest()
    assert r.json()["expected"] == "0" * 64
    assert live.stats()["blobs"] == 0
    ok = httpx.put(f"{live.url}/blob/{hashlib.sha256(b'abc').hexdigest()}", content=b"abc", headers=AUTH)
    assert ok.status_code == 201
    r = httpx.post(live.url + "/blob", content=b"xyz", headers={**AUTH, "X-Blob-Sha256": "1" * 64})
    assert r.status_code == 422
    r = httpx.put(f"{live.url}/blob/{'a' * 64}", content=b"xyz", headers={**AUTH, "X-Blob-Sha256": "b" * 64})
    assert r.status_code == 400


def test_bad_ids_are_400(live):
    for bad in ("xyz", "g" * 64, "a" * 63, "a" * 65, "sha256:" + "a" * 10, "%2e%2e", "a" * 63 + "%00"):
        for method in ("PUT", "GET", "HEAD", "DELETE"):
            r = httpx.request(method, f"{live.url}/blob/{bad}", content=b"x" if method == "PUT" else None,
                              headers=AUTH)
            assert r.status_code == 400, (method, bad)
    # an encoded slash is decoded before routing, so the router answers 404; it never reaches the store
    for method in ("PUT", "GET", "DELETE"):
        r = httpx.request(method, f"{live.url}/blob/..%2f..%2fetc%2fpasswd", content=b"x", headers=AUTH)
        assert r.status_code == 404
    assert live.stats()["blobs"] == 0


@pytest.mark.parametrize("codec", ["identity", "gzip", "br", "zstd"])
def test_each_content_encoding_uploads_to_the_same_id(live, codec):
    body = encode(codec, RAW)
    r = httpx.post(live.url + "/blob", content=body, headers={**AUTH, "Content-Encoding": codec})
    assert r.status_code == 201, r.text
    assert r.json()["blob_id"] == SHA and r.json()["size"] == len(RAW)
    assert r.json()["encoding"] == ("br" if codec == "br" else "zstd")
    assert httpx.get(f"{live.url}/blob/{SHA}", headers=AUTH).content == RAW


def test_chunked_upload_without_content_length(live):
    def chunks():
        for i in range(0, len(RAW), 70_000):
            yield RAW[i:i + 70_000]
    r = httpx.post(live.url + "/blob", content=chunks(), headers=AUTH)
    assert r.status_code == 201 and r.json()["blob_id"] == SHA
    assert live.stats()["inflight"] == 0


def test_large_incompressible_upload_spills_to_disk_and_comes_back(live, tmp_path):
    noise = os.urandom(40 << 20)                             # past the 32 MiB memory budget
    r = httpx.post(live.url + "/blob", content=noise, headers=AUTH, timeout=60)
    assert r.status_code == 201 and r.json()["encoding"] == "identity"
    st = live.stats()
    assert st["mem_used"] == 0 and len(os.listdir(tmp_path / "spill")) == 1
    got = httpx.get(f"{live.url}/blob/{r.json()['blob_id']}", headers=AUTH, timeout=60)
    assert got.content == noise


def test_unsupported_and_stacked_content_encodings_are_415(live):
    r = httpx.post(live.url + "/blob", content=b"x", headers={**AUTH, "Content-Encoding": "deflate"})
    assert r.status_code == 415 and "br" in r.json()["supported"] and r.headers["accept-encoding"]
    assert httpx.post(live.url + "/blob", content=b"x",
                      headers={**AUTH, "Content-Encoding": "gzip, br"}).status_code == 415


def test_declared_length_over_the_limit_is_413_before_any_body_byte(live):
    head = (f"POST /blob HTTP/1.1\r\nHost: 127.0.0.1:{live.port}\r\nAuthorization: Bearer {TOKEN}\r\n"
            "Content-Length: 5000000000\r\n\r\n")
    t0 = time.perf_counter()
    reply = raw_request(live.port, head)
    assert reply.startswith(b"HTTP/1.1 413") and time.perf_counter() - t0 < 1.0
    assert live.stats()["inflight"] == 0 and live.stats()["uploads"] == 0


@pytest.mark.parametrize("codec", ["gzip", "br", "zstd"])
def test_decompression_bombs_are_refused_fast_and_leave_nothing_behind(live, codec):
    zeros = b"\0" * (64 << 20)
    if codec == "gzip":
        c = zlib.compressobj(9, zlib.DEFLATED, 31)
        bomb = c.compress(zeros) + c.flush()
    elif codec == "br":
        c = brotli.Compressor(quality=5)
        bomb = c.process(zeros) + c.finish()
    else:
        c = zstandard.ZstdCompressor(level=3).compressobj()
        bomb = c.compress(zeros) + c.flush()
    t0 = time.perf_counter()
    r = httpx.post(live.url + "/blob", content=bomb, headers={**AUTH, "Content-Encoding": codec})
    assert r.status_code == 413 and "ratio" in r.json()["error"]
    assert time.perf_counter() - t0 < 3
    st = live.stats()
    assert st["blobs"] == 0 and st["inflight"] == 0 and st["inflight_mem"] == 0


def test_decoded_size_cap_over_http(tmp_path):
    srv = Live(live_cfg(tmp_path, max_bytes=2 << 20, max_ratio=0))
    try:
        r = httpx.post(srv.url + "/blob", content=encode("gzip", b"A" * (6 << 20)),
                       headers={**AUTH, "Content-Encoding": "gzip"})
        assert r.status_code == 413 and "decoded size" in r.json()["error"]
        r = httpx.post(srv.url + "/blob", content=os.urandom(3 << 20), headers=AUTH)
        assert r.status_code == 413
    finally:
        srv.close()


def test_damaged_streams_are_400(live):
    good = {c: encode(c, RAW) for c in ("gzip", "br", "zstd")}
    for codec, body in good.items():
        h = {**AUTH, "Content-Encoding": codec}
        assert httpx.post(live.url + "/blob", content=body[:-7], headers=h).status_code == 400
        assert httpx.post(live.url + "/blob", content=body + b"junk", headers=h).status_code == 400
        assert httpx.post(live.url + "/blob", content=os.urandom(400), headers=h).status_code == 400
    assert live.stats()["blobs"] == 0


def test_stalled_upload_gets_408_and_releases_everything(live):
    head = (f"POST /blob HTTP/1.1\r\nHost: 127.0.0.1:{live.port}\r\nAuthorization: Bearer {TOKEN}\r\n"
            "Content-Length: 1000000\r\n\r\n")
    t0 = time.perf_counter()
    reply = raw_request(live.port, head, b"a" * 100, read=8)
    assert reply.startswith(b"HTTP/1.1 408") and 0.9 < time.perf_counter() - t0 < 5
    st = live.stats()
    assert st["inflight"] == 0 and st["uploads"] == 0 and st["inflight_mem"] == 0


def test_client_disconnect_mid_upload_leaves_no_reservation_or_temp_file(live, tmp_path):
    s = socket.create_connection(("127.0.0.1", live.port))
    s.sendall((f"POST /blob HTTP/1.1\r\nHost: 127.0.0.1:{live.port}\r\nAuthorization: Bearer {TOKEN}\r\n"
               "Content-Length: 90000000\r\n\r\n").encode() + os.urandom(40 << 20))
    time.sleep(0.5)
    s.close()
    for _ in range(40):
        st = live.stats()
        if st["uploads"] == 0:
            break
        time.sleep(0.1)
    assert st["uploads"] == 0 and st["inflight"] == 0 and st["inflight_mem"] == 0
    assert [p for p in os.listdir(tmp_path / "spill") if p.startswith(".up_")] == []
    assert st["blobs"] == 0


def test_too_many_concurrent_uploads_get_429_then_recover(tmp_path):
    srv = Live(live_cfg(tmp_path, max_uploads=2, idle_timeout=5.0))
    try:
        def slow():
            def gen():
                for _ in range(15):
                    yield os.urandom(10_000)
                    time.sleep(0.1)
            httpx.post(srv.url + "/blob", content=gen(), headers=AUTH, timeout=20)
        ts = [threading.Thread(target=slow) for _ in range(2)]
        for t in ts:
            t.start()
        for _ in range(100):
            if srv.stats()["uploads"] == 2:
                break
            time.sleep(0.02)
        r = httpx.post(srv.url + "/blob", content=b"quick", headers=AUTH)
        assert r.status_code == 429 and r.headers["retry-after"]
        for t in ts:
            t.join()
        assert httpx.post(srv.url + "/blob", content=b"quick", headers=AUTH).status_code == 201
        assert srv.stats()["uploads"] == 0 and srv.stats()["inflight"] == 0
    finally:
        srv.close()


def test_quota_exhaustion_is_507_and_old_blobs_are_evicted_first(tmp_path):
    srv = Live(live_cfg(tmp_path, mem_bytes=1 << 20, quota_bytes=3 << 20))
    try:
        ids = []
        for _ in range(4):
            r = httpx.post(srv.url + "/blob", content=os.urandom(1 << 20), headers=AUTH)
            assert r.status_code == 201
            ids.append(r.json()["blob_id"])
        alive = [httpx.head(f"{srv.url}/blob/{i}", headers=AUTH).status_code for i in ids]
        assert alive == [404, 200, 200, 200]
        assert srv.stats()["total"] <= 3 << 20
        r = httpx.post(srv.url + "/blob", content=os.urandom(4 << 20), headers=AUTH)
        assert r.status_code == 413 or r.status_code == 507
    finally:
        srv.close()


# ---------------------------------------------------------------- download

def _raw_get(live, accept: str | None, method: str = "GET"):
    headers = dict(AUTH)
    if accept is not None:
        headers["Accept-Encoding"] = accept
    with httpx.stream(method, f"{live.url}/blob/{SHA}", headers=headers) as rs:
        return rs.status_code, dict(rs.headers), b"".join(rs.iter_raw())


def _decode(coding: str, body: bytes) -> bytes:
    return {"identity": lambda b: b, "gzip": gzip.decompress, "br": brotli.decompress,
            "zstd": lambda b: zstandard.ZstdDecompressor().decompress(b, max_output_size=len(RAW) + 10)}[coding](body)


@pytest.mark.parametrize("accept,expect", [
    ("zstd", "zstd"), ("br, zstd", "zstd"), ("gzip", "identity"), ("identity", "identity"),
    ("gzip;q=0, zstd", "zstd"), ("*", "zstd"), ("zstd;q=0", "identity"),
])
def test_download_negotiates_encoding(live, accept, expect):
    httpx.post(live.url + "/blob", content=RAW, headers=AUTH)               # stored as zstd
    status, hdr, body = _raw_get(live, accept)
    assert status == 200
    assert hdr.get("content-encoding", "identity") == expect
    assert _decode(expect, body) == RAW
    assert hdr["content-length"] == str(len(body))
    assert hdr["vary"] == "Accept-Encoding" and hdr["x-blob-codec"] == "zstd"
    assert hdr["x-blob-size"] == str(len(RAW))


def test_download_without_accept_encoding_is_identity(live):
    httpx.post(live.url + "/blob", content=RAW, headers=AUTH)
    head = (f"GET /blob/{SHA} HTTP/1.1\r\nHost: 127.0.0.1:{live.port}\r\nAuthorization: Bearer {TOKEN}\r\n"
            "Connection: close\r\n\r\n")
    s = socket.create_connection(("127.0.0.1", live.port))
    s.sendall(head.encode())
    data = b""
    while chunk := s.recv(1 << 20):
        data += chunk
    s.close()
    header, _, body = data.partition(b"\r\n\r\n")
    assert b"content-encoding" not in header.lower() and body == RAW


def test_download_of_a_brotli_blob_decodes_for_clients_that_cannot_read_it(live):
    httpx.post(live.url + "/blob", content=encode("br", RAW), headers={**AUTH, "Content-Encoding": "br"})
    status, hdr, body = _raw_get(live, "br")
    assert hdr.get("content-encoding") == "br" and brotli.decompress(body) == RAW
    status, hdr, body = _raw_get(live, "gzip")
    assert "content-encoding" not in hdr and body == RAW and hdr["content-length"] == str(len(RAW))


def test_identity_stored_blob_is_sent_without_content_encoding(live):
    noise = os.urandom(300_000)
    nid = httpx.post(live.url + "/blob", content=noise, headers=AUTH).json()["blob_id"]
    with httpx.stream("GET", f"{live.url}/blob/{nid}", headers={**AUTH, "Accept-Encoding": "zstd, br, gzip"}) as rs:
        got = b"".join(rs.iter_raw())
        assert "content-encoding" not in rs.headers
    assert got == noise


def test_head_has_the_headers_and_no_body(live):
    httpx.post(live.url + "/blob", content=RAW, headers={**AUTH, "Content-Type": "model/x3d+xml"})
    r = httpx.head(f"{live.url}/blob/{SHA}", headers={**AUTH, "Accept-Encoding": "identity"})
    assert r.status_code == 200 and r.content == b""
    assert r.headers["content-length"] == str(len(RAW)) and r.headers["content-type"] == "model/x3d+xml"
    assert r.headers["etag"] == f'"{SHA}"'
    r = httpx.head(f"{live.url}/blob/{SHA}", headers={**AUTH, "Accept-Encoding": "zstd"})
    assert r.headers["content-encoding"] == "zstd" and r.headers["etag"] == f'"{SHA}-zstd"'


def test_downloads_cannot_run_as_a_page(live):
    httpx.post(live.url + "/blob", content=b"<html><script>alert(1)</script></html>",
               headers={**AUTH, "Content-Type": "text/html"})
    sid = hashlib.sha256(b"<html><script>alert(1)</script></html>").hexdigest()
    r = httpx.get(f"{live.url}/blob/{sid}", headers=AUTH)
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["content-security-policy"] == "sandbox"
    assert r.headers["cache-control"] == "private, no-transform"


def test_unknown_blob_is_404_and_delete_works(live):
    assert httpx.get(f"{live.url}/blob/{'1' * 64}", headers=AUTH).status_code == 404
    assert httpx.head(f"{live.url}/blob/{'1' * 64}", headers=AUTH).status_code == 404
    assert httpx.delete(f"{live.url}/blob/{'1' * 64}", headers=AUTH).status_code == 404
    httpx.post(live.url + "/blob", content=RAW, headers=AUTH)
    assert httpx.delete(f"{live.url}/blob/{SHA}", headers=AUTH).status_code == 204
    assert httpx.get(f"{live.url}/blob/{SHA}", headers=AUTH).status_code == 404


def test_download_lease_is_released_when_the_client_goes_away(live):
    big = os.urandom(8 << 20)
    bid = httpx.post(live.url + "/blob", content=big, headers=AUTH).json()["blob_id"]
    with httpx.stream("GET", f"{live.url}/blob/{bid}", headers=AUTH) as rs:
        next(rs.iter_raw())                                                 # read one piece, then leave
    for _ in range(40):
        if blobs.get_store()._d[bid].refs == 0:
            break
        time.sleep(0.1)
    assert blobs.get_store()._d[bid].refs == 0


# ---------------------------------------------------------------- MCP tools over HTTP

def call(url: str, headers: dict | None, name: str, args: dict) -> str:
    async def go():
        async with streamablehttp_client(url + "/mcp", headers=headers) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool(name, args)
                return res.content[0].text
    return asyncio.run(go())


def test_blob_tools_round_trip_with_the_bearer_on_the_mcp_connection(live):
    put = json.loads(call(live.url, AUTH, "blob_put", {"content": SCENE, "media_type": "model/x3d+xml"}))
    assert put["blob_id"] == SHA and put["created"] is True
    info = json.loads(call(live.url, AUTH, "blob_info", {"blob_id": "sha256:" + SHA}))
    assert info["size"] == len(RAW) and info["preview"].startswith("<?xml")
    page = json.loads(call(live.url, AUTH, "blob_read", {"blob_id": SHA, "offset": 0, "length": 100}))
    assert page["data"] == SCENE[:100] and page["next_offset"] == 100 and not page["eof"]
    # an upload through the route is visible to the tools, and the other way round
    other = httpx.post(live.url + "/blob", content=b"via route", headers=AUTH).json()["blob_id"]
    assert json.loads(call(live.url, AUTH, "blob_read", {"blob_id": other}))["data"] == "via route"
    assert json.loads(call(live.url, AUTH, "blob_delete", {"blob_id": SHA})) == {"deleted": True}
    assert httpx.get(f"{live.url}/blob/{SHA}", headers=AUTH).status_code == 404


def test_tools_refuse_a_connection_without_the_bearer(live):
    httpx.post(live.url + "/blob", content=RAW, headers=AUTH)
    for name, args in (("blob_put", {"content": "x"}), ("blob_info", {"blob_id": SHA}),
                       ("blob_read", {"blob_id": SHA}), ("blob_delete", {"blob_id": SHA})):
        out = json.loads(call(live.url, None, name, args))
        assert "Authorization: Bearer" in out["error"], name
        out = json.loads(call(live.url, {"Authorization": "Bearer nope"}, name, args))
        assert "Authorization: Bearer" in out["error"], name
    assert httpx.head(f"{live.url}/blob/{SHA}", headers=AUTH).status_code == 200      # nothing was deleted
    assert live.stats()["blobs"] == 1


def test_blob_parameters_of_the_other_tools_need_the_bearer_too(live):
    httpx.post(live.url + "/blob", content=RAW, headers=AUTH)
    refused = json.loads(call(live.url, None, "validate_x3d", {"blob_id": SHA}))
    assert refused["valid"] is False and "Authorization: Bearer" in refused["errors"][0]
    # return_mode="blob" would write to the store: also gated, even for inline content
    refused = json.loads(call(live.url, None, "validate_x3d", {"content": SCENE, "return_mode": "blob"}))
    assert "Authorization: Bearer" in refused["errors"][0]
    assert live.stats()["blobs"] == 1
    ok = json.loads(call(live.url, AUTH, "validate_x3d", {"blob_id": SHA}))
    assert ok["valid"] is True
    inline = call(live.url, None, "validate_x3d", {"content": SCENE})              # plain calls stay open
    assert json.loads(inline) == ok


def test_tools_open_lets_headerless_clients_use_the_tools(tmp_path):
    srv = Live(live_cfg(tmp_path, tools_open=True))
    try:
        put = json.loads(call(srv.url, None, "blob_put", {"content": "hello"}))
        assert put["size"] == 5
        assert httpx.get(f"{srv.url}/blob/{put['blob_id']}").status_code == 401         # the routes still need it
    finally:
        srv.close()


def test_blob_put_over_http_takes_base64_and_compressed_payloads(live):
    z = base64.b64encode(zstandard.ZstdCompressor(level=19).compress(RAW)).decode()
    assert len(z) < len(RAW) / 20
    put = json.loads(call(live.url, AUTH, "blob_put", {
        "content": z, "encoding": "base64", "content_encoding": "zstd", "media_type": "model/x3d+xml"}))
    assert put["blob_id"] == SHA and put["encoding"] == "zstd" and put["size"] == len(RAW)
    bomb = base64.b64encode(brotli.compress(b"\0" * (200 << 20), quality=1)).decode()
    out = json.loads(call(live.url, AUTH, "blob_put", {"content": bomb, "encoding": "base64",
                                                      "content_encoding": "br"}))
    assert "error" in out
    assert live.stats()["blobs"] == 1


def test_blob_put_path_is_refused_over_http(live, tmp_path):
    f = tmp_path / "a.x3d"
    f.write_text("<X3D/>")
    out = json.loads(call(live.url, AUTH, "blob_put", {"path": str(f)}))
    assert "disabled over the HTTP transport" in out["error"]


# ---------------------------------------------------------------- server wiring (subprocess)

PROBE = (
    "import asyncio, json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import server\n"
    "print(json.dumps({'tools': sorted(t.name for t in asyncio.run(server.mcp.list_tools()) "
    "if t.name.startswith('blob')), 'routes': sorted(r.path for r in server.mcp._custom_starlette_routes), "
    "'instructions': 'LARGE SCENES' in server.INSTRUCTIONS}))\n"
)


def probe(env: dict) -> dict:
    full = {k: v for k, v in os.environ.items() if not k.startswith(("X3D_MCP", "MCP_TRANSPORT"))}
    full.update(env)
    p = subprocess.run([sys.executable, "-c", PROBE, str(SRC)], env=full, capture_output=True, text=True,
                       timeout=120)
    if p.returncode != 0:
        return {"exit": p.returncode, "stderr": p.stderr}
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_server_wiring_per_transport():
    from concurrent.futures import ThreadPoolExecutor
    cases = {
        "stdio": {},
        "stdio-off": {"X3D_MCP_BLOB": "off"},
        "http": {"MCP_TRANSPORT": "streamable-http"},
        "http-token": {"MCP_TRANSPORT": "streamable-http", "X3D_MCP_BLOB_TOKEN": TOKEN},
        "http-on-without-token": {"MCP_TRANSPORT": "streamable-http", "X3D_MCP_BLOB": "on"},
        "http-short-token": {"MCP_TRANSPORT": "streamable-http", "X3D_MCP_BLOB_TOKEN": "short"},
    }
    with ThreadPoolExecutor(6) as ex:
        got = dict(zip(cases, ex.map(probe, cases.values())))
    four = ["blob_delete", "blob_info", "blob_put", "blob_read"]
    base = ["/", "/pulse"]
    assert got["stdio"] == {"tools": four, "routes": base, "instructions": True}        # no HTTP routes under stdio
    assert got["stdio-off"] == {"tools": [], "routes": base, "instructions": False}
    assert got["http"] == {"tools": [], "routes": base, "instructions": False}          # off by default over HTTP
    assert got["http-token"]["tools"] == four
    assert [r for r in got["http-token"]["routes"] if r.startswith("/blob")] == ["/blob"] + ["/blob/{blob_id}"] * 3
    for key in ("http-on-without-token", "http-short-token"):
        assert got[key]["exit"] != 0 and "X3D_MCP_BLOB_TOKEN" in got[key]["stderr"], key


def test_real_server_process_serves_blob_routes_and_tools():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("X3D_MCP", "MCP_TRANSPORT"))}
    env.update(MCP_TRANSPORT="streamable-http", HOST="127.0.0.1", PORT=str(port), X3D_MCP_BLOB_TOKEN=TOKEN,
               PYTHONUNBUFFERED="1")
    p = subprocess.Popen([sys.executable, str(SRC / "server.py")], env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.PIPE)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(240):
            try:
                if httpx.get(base + "/pulse", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.25)
        else:
            pytest.fail("server did not start")
        assert httpx.post(base + "/blob", content=RAW).status_code == 401
        r = httpx.post(base + "/blob", content=encode("zstd", RAW), headers={**AUTH, "Content-Encoding": "zstd"})
        assert r.status_code == 201 and r.json()["blob_id"] == SHA
        out = json.loads(call(base, AUTH, "validate_semantic", {"blob_id": SHA, "return_mode": "summary"}))
        assert out["input"]["sha256"] == SHA and out["return_mode"] == "summary"
        refused = call(base, None, "validate_semantic", {"blob_id": SHA})
        assert "Authorization: Bearer" in refused
    finally:
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
        assert TOKEN not in (p.stderr.read().decode() if p.stderr else "")           # never logged


# ---------------------------------------------------------------- start-up checks (subprocess)

def _clean_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("X3D_MCP", "MCP_TRANSPORT"))}
    env.update(PYTHONUNBUFFERED="1", **extra)
    return env


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_http_server(**env):
    port = _free_port()
    p = subprocess.Popen(
        [sys.executable, str(SRC / "server.py")],
        env=_clean_env(MCP_TRANSPORT="streamable-http", HOST="127.0.0.1", PORT=str(port),
                       X3D_MCP_BLOB_TOKEN=TOKEN, **env),
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    base = f"http://127.0.0.1:{port}"
    for _ in range(240):
        try:
            if httpx.get(base + "/pulse", timeout=1).status_code == 200:
                return p, base
        except httpx.HTTPError:
            time.sleep(0.25)
    p.kill()
    pytest.fail("server did not start")


def _stop(p):
    p.terminate()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()


HTTP_ENV = {"MCP_TRANSPORT": "streamable-http", "X3D_MCP_BLOB_TOKEN": TOKEN}


def test_unusable_spill_directory_stops_the_server_at_startup(tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir()
    os.chmod(loose, 0o755)
    got = probe({**HTTP_ENV, "X3D_MCP_BLOB_DIR": str(loose)})
    assert got["exit"] != 0 and "must be private" in got["stderr"]
    target = tmp_path / "real"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target)
    got = probe({**HTTP_ENV, "X3D_MCP_BLOB_DIR": str(link)})
    assert got["exit"] != 0 and "not a directory" in got["stderr"]
    ok = probe({**HTTP_ENV, "X3D_MCP_BLOB_DIR": str(tmp_path / "fresh")})
    assert "tools" in ok                                      # a usable directory starts fine


def test_server_on_a_spill_directory_another_process_holds_stops_at_startup(tmp_path):
    from transfer.blobs import BlobStore
    d = tmp_path / "spill"
    owner = BlobStore(BlobConfig(spill_dir=str(d)))
    try:
        got = probe({**HTTP_ENV, "X3D_MCP_BLOB_DIR": str(d)})
        assert got["exit"] != 0 and "in use by another server process" in got["stderr"]
    finally:
        owner.close()
    assert "tools" in probe({**HTTP_ENV, "X3D_MCP_BLOB_DIR": str(d)})


def test_origins_without_hosts_stop_the_server_at_startup():
    got = probe({**HTTP_ENV, "X3D_MCP_ALLOWED_ORIGINS": "https://app.example.com"})
    assert got["exit"] != 0 and "X3D_MCP_ALLOWED_HOSTS" in got["stderr"]
    ok = probe({"X3D_MCP_ALLOWED_ORIGINS": "https://app.example.com"})        # stdio does not use them
    assert "tools" in ok


def test_hosts_with_origins_serve_blob_and_mcp_and_refuse_others():
    p, base = _start_http_server(X3D_MCP_ALLOWED_HOSTS="127.0.0.1:*",
                                 X3D_MCP_ALLOWED_ORIGINS="https://app.example.com")
    try:
        assert httpx.post(base + "/blob", content=b"hello", headers=AUTH).status_code == 201
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "t", "version": "1"}}}
        mcp_h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        assert httpx.post(base + "/mcp", json=init, headers=mcp_h).status_code == 200
        ok_origin = httpx.post(base + "/blob", content=b"hello2", headers={**AUTH, "Origin": "https://app.example.com"})
        assert ok_origin.status_code == 201
        bad_origin = httpx.post(base + "/blob", content=b"x", headers={**AUTH, "Origin": "https://evil.example"})
        assert bad_origin.status_code == 403
        bad_host = httpx.post(base + "/blob", content=b"x", headers={**AUTH, "Host": "evil.example"})
        assert bad_host.status_code == 421
    finally:
        _stop(p)


def test_stdio_sigterm_removes_the_temporary_spill_directory(tmp_path):
    f = tmp_path / "scene.x3d"
    f.write_bytes(os.urandom(5000))
    tmpbase = tmp_path / "tmp"
    tmpbase.mkdir()
    p = subprocess.Popen([sys.executable, str(SRC / "server.py")],
                         env=_clean_env(X3D_MCP_BLOB_MEM_BYTES="1", TMPDIR=str(tmpbase)),
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    def rpc(msg):
        p.stdin.write(json.dumps(msg) + "\n")
        p.stdin.flush()

    def reply():
        return json.loads(p.stdout.readline())

    try:
        rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"}}})
        reply()
        rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "blob_put", "arguments": {"path": str(f)}}})
        out = json.loads(reply()["result"]["content"][0]["text"])
        assert out["size"] == 5000
        (spill,) = list(tmpbase.glob("x3d-blobs-*"))
        assert len(os.listdir(spill)) == 1
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=20)
        assert not spill.exists()
        assert p.returncode == 128 + signal.SIGTERM
    finally:
        if p.poll() is None:
            p.kill()
