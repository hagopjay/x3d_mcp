"""Blob store MCP tools: blob_put, blob_info, blob_read, blob_delete.

The model-facing side of the transfer layer (src/transfer). A document of a few
megabytes is uploaded once, out of band (HTTP route, or `path` under stdio) or in
small pieces with blob_put, and every other tool then names it with `blob_id`.
Registered only when the blob store is enabled; over HTTP each call must also carry
the blob bearer token on the MCP connection (unless X3D_MCP_BLOB_TOOLS_OPEN=1).
"""

import base64
import binascii
import json
import os

import anyio
from mcp.server.fastmcp import Context, FastMCP

from transfer.auth import tool_gate
from transfer.blobs import clean_media_type, get_config, get_store, transport_is_http
from transfer.errors import BlobError

_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/ld+json",
               "application/n-triples", "application/x-ndjson")


def _is_text(media_type: str) -> bool:
    return media_type.startswith(_TEXT_TYPES) or media_type.endswith(("+xml", "+json", "+vrml"))


def _err(message) -> str:
    return json.dumps({"error": str(message)})


def _utf8_cut(win: bytes, at_eof: bool) -> int:
    """Length of the longest prefix of `win` that does not end inside a multibyte character."""
    n = len(win)
    if at_eof or n == 0:
        return n
    for back in range(1, min(4, n) + 1):
        b = win[n - back]
        if b & 0xC0 == 0x80:                    # continuation byte: look further back
            continue
        need = 1 if b < 0xC0 else 2 if b < 0xE0 else 3 if b < 0xF0 else 4
        cut = n if back >= need else n - back
        return cut or n                         # never return an empty page for a non-empty window
    return n


def register(mcp: FastMCP):

    @mcp.tool(structured_output=False)
    async def blob_put(content: str = "", encoding: str = "utf-8", media_type: str = "",
                       content_encoding: str = "", name: str = "", path: str = "",
                       ttl: int = 0, ctx: Context = None) -> str:
        """Store a small document and return its blob_id; other tools then take blob_id instead of content.

        For anything over a few hundred KB do not paste it: upload it out of band
        (PUT/POST /blob with the bearer token, or `path` under stdio). Here the payload
        is limited to X3D_MCP_BLOB_INLINE_MAX decoded bytes (3 MB by default).

        Args:
            content: The text, or base64 when encoding is "base64".
            encoding: "utf-8" (default) or "base64" (for binary or compressed data).
            media_type: e.g. model/x3d+xml, application/json, text/turtle. Default text/plain
                for text, application/octet-stream for base64.
            content_encoding: "", "gzip", "br" or "zstd": the base64 payload is a compressed
                stream of the real content, which shrinks token cost 10 to 40 times for scenes.
            name: A label for display only.
            path: A local file to store instead of content (stdio transport only).
            ttl: Idle lifetime in seconds (0 = the server default). Each use renews it.

        Returns JSON {blob_id, size, stored_size, encoding, media_type, sha256, expires, created}.
        The blob_id is the sha256 of the decoded bytes: the same content always has the same id.
        """
        cfg = get_config()
        denied = tool_gate(ctx, cfg)
        if denied:
            return _err(denied)
        if encoding not in ("utf-8", "base64"):
            return _err(f"encoding must be 'utf-8' or 'base64', not {encoding!r}")
        try:
            store = get_store()
            if path.strip():
                if content.strip():
                    return _err("Provide either `content` or `path`, not both.")
                if cfg.http or transport_is_http():
                    return _err("Path input is disabled over the HTTP transport. "
                                "Upload the file to POST /blob instead.")
                raw = await anyio.to_thread.run_sync(_read_file, path.strip())
                limit, cenc = cfg.max_bytes, ""
                media = clean_media_type(media_type)
            else:
                if encoding == "base64":
                    try:
                        raw = base64.b64decode("".join(content.split()), validate=True)
                    except (binascii.Error, ValueError):
                        return _err("content is not valid base64")
                else:
                    raw = content.encode("utf-8")
                limit, cenc = cfg.inline_max, content_encoding
                media = clean_media_type(media_type) if media_type else (
                    "text/plain" if encoding == "utf-8" else "application/octet-stream")
            meta = await anyio.to_thread.run_sync(
                lambda: store.put(raw, cenc or None, media, ttl or None,
                                  name=name, max_bytes=limit))
        except (BlobError, OSError) as e:
            return _err(e)
        return json.dumps(meta)

    @mcp.tool(structured_output=False)
    async def blob_info(blob_id: str, ctx: Context = None) -> str:
        """Describe a stored blob: size, how it is stored, media type, expiry, and a 200-character preview for text.

        Args:
            blob_id: The id (64 hex characters, or sha256:<hex>).
        """
        denied = tool_gate(ctx, get_config())
        if denied:
            return _err(denied)
        try:
            store = get_store()
            meta = store.stat(blob_id)
            if _is_text(meta["media_type"]) and meta["size"]:
                head = await anyio.to_thread.run_sync(store.read, blob_id, 0, 800)
                meta["preview"] = head.decode("utf-8", "ignore")[:200]
        except BlobError as e:
            return _err(e)
        return json.dumps(meta)

    @mcp.tool(structured_output=False)
    async def blob_read(blob_id: str, offset: int = 0, length: int = 0, encoding: str = "utf-8",
                        ctx: Context = None) -> str:
        """Read a window of a stored blob: `length` bytes from `offset` (default 16 KiB, at most 256 KiB).

        Page through with next_offset until eof is true. Text pages never split a multibyte
        character, so next_offset is what to pass next. Reading whole documents this way
        costs context; prefer passing the blob_id to a tool.

        Args:
            blob_id: The id (64 hex characters, or sha256:<hex>).
            offset: Byte offset of the window.
            length: Window size in bytes (0 = the default page).
            encoding: "utf-8" returns text; "base64" returns the raw bytes.
        """
        cfg = get_config()
        denied = tool_gate(ctx, cfg)
        if denied:
            return _err(denied)
        if encoding not in ("utf-8", "base64"):
            return _err(f"encoding must be 'utf-8' or 'base64', not {encoding!r}")
        try:
            store = get_store()
            meta = store.stat(blob_id)
            total = meta["size"]
            n = min(length if length > 0 else cfg.page_default, cfg.page_max)
            win = await anyio.to_thread.run_sync(store.read, blob_id, offset, n)
        except BlobError as e:
            return _err(e)
        if encoding == "utf-8":
            win = win[:_utf8_cut(win, offset + len(win) >= total)]
            data = win.decode("utf-8", "replace")
        else:
            data = base64.b64encode(win).decode("ascii")
        nxt = offset + len(win)
        return json.dumps({"blob_id": meta["blob_id"], "offset": offset, "length": len(win),
                           "total": total, "next_offset": nxt, "eof": nxt >= total,
                           "encoding": encoding, "data": data})

    @mcp.tool(structured_output=False)
    async def blob_delete(blob_id: str, ctx: Context = None) -> str:
        """Delete a stored blob now (blobs also expire on their own).

        Args:
            blob_id: The id (64 hex characters, or sha256:<hex>).
        """
        denied = tool_gate(ctx, get_config())
        if denied:
            return _err(denied)
        try:
            return json.dumps({"deleted": get_store().delete(blob_id)})
        except BlobError as e:
            return _err(e)


def _read_file(path: str) -> bytes:
    """Read a local file for blob_put(path=...); the store enforces the size limit."""
    p = os.path.expanduser(path)
    if not os.path.isfile(p):
        raise BlobError(f"file not found: {p}")
    limit = get_config().max_bytes
    if os.path.getsize(p) > limit:
        raise BlobError(f"file is larger than X3D_MCP_BLOB_MAX_BYTES ({limit})")
    with open(p, "rb") as f:
        return f.read()
