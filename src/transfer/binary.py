"""Binary twin of the text input resolver: the bytes of a splat file from a stored blob or a local path.

transfer.blobs.resolve_text hands a tool X3D text. Splat files are binary (SPZ is gzip, PLY is
little-endian floats), so the splat tools take exactly one of `path` or `blob_id` and get bytes back.
Content cannot be inline: a 5 MB SPZ file is not text and is not something to paste.
"""

import os

import anyio

from .auth import tool_gate
from .blobs import get_config, get_store, normalize_blob_id, transport_is_http
from .errors import BlobError


def resolve_bytes(path: str = "", blob_id: str = "", max_bytes: int | None = None) -> tuple[bytes, str]:
    """Return (data, label) for exactly one of a file `path` (stdio only) or a stored `blob_id`.

    `label` names the source for messages ("file <path>", "blob <id prefix>"). Errors are ValueError with a
    message a tool can return as is. `max_bytes` caps a file read; a blob is already capped by the store.
    """
    given = [name for name, v in (("path", path), ("blob_id", blob_id)) if v and v.strip()]
    if len(given) != 1:
        raise ValueError("Provide exactly one of `path` (a local file, stdio only) or `blob_id` (a stored blob)"
                         + (f"; got {' and '.join(given)}." if given else "."))
    if given == ["blob_id"]:
        bid = normalize_blob_id(blob_id)
        return get_store().get_bytes(bid), f"blob {bid[:12]}"
    if get_config().http or transport_is_http():
        raise ValueError("Path input is disabled over the HTTP transport. "
                         "Upload the file to POST /blob and pass its blob_id instead.")
    p = os.path.expanduser(path.strip())
    if not os.path.isfile(p):
        raise ValueError(f"file not found: {p}")
    limit = max_bytes if max_bytes is not None else get_config().max_bytes
    try:
        if os.path.getsize(p) > limit:
            raise BlobError(f"file is larger than the limit of {limit:,} bytes (X3D_MCP_BLOB_MAX_BYTES)")
        with open(p, "rb") as f:
            return f.read(), f"file {p}"
    except OSError as exc:
        raise ValueError(f"could not read {p}: {exc}") from exc


async def resolve_bytes_input(path: str = "", blob_id: str = "", ctx=None) -> tuple[bytes, str]:
    """resolve_bytes for a tool: a blob_id is checked against the caller's bearer first (over HTTP), and the
    read or decode runs in a worker thread."""
    if blob_id and blob_id.strip():
        denied = tool_gate(ctx, get_config())
        if denied:
            raise ValueError(denied)
    return await anyio.to_thread.run_sync(resolve_bytes, path or "", blob_id or "")
