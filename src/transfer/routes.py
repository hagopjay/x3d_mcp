"""HTTP routes for the blob store, mounted on the FastMCP app under the HTTP transport only.

  POST|PUT /blob            upload, id unknown to the client
  PUT      /blob/{sha256}   upload, the client states the id (422 if the bytes differ)
  GET      /blob/{sha256}   the bytes (Accept-Encoding honoured)
  HEAD     /blob/{sha256}   headers only
  DELETE   /blob/{sha256}

Every route needs `Authorization: Bearer <X3D_MCP_BLOB_TOKEN>` (constant-time compare) and
passes the SDK's Host/Origin check. No CORS headers are ever sent. An upload body is
streamed (never read whole), may be gzip, br or zstd (Content-Encoding), is decoded with a
size cap and a ratio limit, and is refused on headers alone where it can be: bad id, known
id, unsupported encoding, declared length over the limit.

register() is only called when the store is enabled, so without X3D_MCP_BLOB_TOKEN these
paths do not exist.
"""

import time

import anyio
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from .auth import bearer_ok, transport_security
from .blobs import clean_media_type, get_config, get_store, normalize_blob_id
from .capped import available_codecs, normalize_encoding
from .errors import BlobError, BlobIdError, BlobTimeout, UnsupportedEncoding

_SECURITY = {"X-Content-Type-Options": "nosniff"}


def _json(status: int, obj: dict, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(obj, status_code=status,
                        headers={"Cache-Control": "no-store", **_SECURITY, **(headers or {})})


def _error(e: BlobError) -> JSONResponse:
    body = {"error": str(e)}
    headers = {}
    if isinstance(e, UnsupportedEncoding):
        body["supported"] = list(e.supported or available_codecs())
        headers["Accept-Encoding"] = ", ".join(body["supported"])
    if e.status == 429:
        headers["Retry-After"] = "2"
    if hasattr(e, "expected"):
        body["expected"], body["actual"] = e.expected, e.actual
    return _json(e.status, body, headers)


def _with_ratio(meta: dict, created: bool | None = None) -> dict:
    out = dict(meta, ratio=round(meta["size"] / max(meta["stored_size"], 1), 2))
    if created is not None:
        out["created"] = created
    return out


def _accepted(request: Request) -> set[str]:
    """Content codings the client accepts (q=0 excluded; * stands for gzip, br, zstd)."""
    out, star = set(), None
    for part in request.headers.get("accept-encoding", "").split(","):
        tok, _, q = part.strip().partition(";")
        tok = tok.strip().lower()
        if not tok:
            continue
        qv = 1.0
        if q.strip().startswith("q="):
            try:
                qv = float(q.strip()[2:])
            except ValueError:
                qv = 1.0
        if tok == "*":
            star = qv > 0
        elif qv > 0:
            out.add(tok)
    if star:
        out |= {"gzip", "br", "zstd"}
    return out


class _Leased(StreamingResponse):
    """A streaming response that releases its lease whether it completes or is cancelled."""

    def __init__(self, lease, body, **kw):
        super().__init__(body, **kw)
        self._lease = lease

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._lease.close()


def register(mcp, cfg=None) -> None:
    from mcp.server.transport_security import TransportSecurityMiddleware

    cfg = cfg or get_config()
    guard = TransportSecurityMiddleware(transport_security(mcp, cfg))

    async def check(request: Request):
        err = await guard.validate_request(request, is_post=False)
        if err is not None:
            return err
        if not bearer_ok(request.headers.get("authorization"), cfg.token):
            return _json(401, {"error": "missing or invalid bearer token"},
                         {"WWW-Authenticate": 'Bearer realm="x3d-blob"'})
        return None

    async def upload(request: Request):
        bad = await check(request)
        if bad is not None:
            return bad
        store = get_store()
        try:
            expect = None
            if request.path_params.get("blob_id") is not None:
                expect = normalize_blob_id(request.path_params["blob_id"])
            stated = request.headers.get("x-blob-sha256")
            if stated:
                stated = normalize_blob_id(stated)
                if expect and stated != expect:
                    raise BlobIdError("X-Blob-Sha256 differs from the id in the URL")
                expect = stated
            if expect:
                known = store.peek(expect)
                if known is not None:         # already stored: answered before a body byte is read
                    return _json(200, _with_ratio(known, False))
            coding = request.headers.get("content-encoding", "identity")
            if "," in coding:
                raise UnsupportedEncoding("stacked Content-Encoding values are not supported",
                                          available_codecs())
            enc = normalize_encoding(coding)
            cl = request.headers.get("content-length")
            declared = int(cl) if cl and cl.isdigit() else None
            up = store.upload(
                enc, clean_media_type(request.headers.get("content-type")),
                name=request.headers.get("x-blob-name", ""), expect_id=expect, declared=declared)
        except BlobError as e:
            return _error(e)
        t0 = time.monotonic()
        try:
            it = request.stream().__aiter__()
            while True:
                try:
                    with anyio.fail_after(cfg.idle_timeout):
                        chunk = await it.__anext__()
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    raise BlobTimeout(f"no data for {cfg.idle_timeout:g}s") from None
                if time.monotonic() - t0 > cfg.total_timeout:
                    raise BlobTimeout(f"upload took longer than {cfg.total_timeout:g}s")
                up.feed(chunk)
            meta, created = up.finish()
            return _json(201 if created else 200, _with_ratio(meta, created))
        except BlobError as e:
            return _error(e)
        except ClientDisconnect:
            return Response(status_code=499)
        finally:
            up.abort()

    async def download(request: Request):
        bad = await check(request)
        if bad is not None:
            return bad
        store = get_store()
        try:
            lease = store.acquire(request.path_params["blob_id"])
        except BlobError as e:
            return _error(e)
        stored_ok = lease.encoding != "identity" and lease.encoding in _accepted(request)
        headers = {
            "ETag": f'"{lease.blob_id}{"-" + lease.encoding if stored_ok else ""}"',
            "Vary": "Accept-Encoding",
            "Cache-Control": "private, no-transform",
            "Content-Disposition": f'attachment; filename="blob-{lease.blob_id[:12]}"',
            "Content-Security-Policy": "sandbox",
            "X-Blob-Size": str(lease.size),
            "X-Blob-Stored-Size": str(lease.stored_size),
            "X-Blob-Codec": lease.encoding,
            **_SECURITY,
        }
        if stored_ok:
            headers["Content-Encoding"] = lease.encoding
        headers["Content-Length"] = str(lease.stored_size if stored_ok else lease.size)
        if request.method == "HEAD":
            lease.close()
            return Response(status_code=200, headers={**headers, "Content-Type": lease.media_type})
        pieces = lease.iter_stored() if (stored_ok or lease.encoding == "identity") else lease.iter_decoded()

        async def body():
            for piece in pieces:
                yield piece

        return _Leased(lease, body(), media_type=lease.media_type, headers=headers)

    async def delete(request: Request):
        bad = await check(request)
        if bad is not None:
            return bad
        try:
            ok = get_store().delete(request.path_params["blob_id"])
        except BlobError as e:
            return _error(e)
        return Response(status_code=204 if ok else 404, headers=_SECURITY)

    mcp.custom_route("/blob", methods=["POST", "PUT"])(upload)
    mcp.custom_route("/blob/{blob_id}", methods=["PUT"])(upload)
    mcp.custom_route("/blob/{blob_id}", methods=["GET", "HEAD"])(download)
    mcp.custom_route("/blob/{blob_id}", methods=["DELETE"])(delete)
