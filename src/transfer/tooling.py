"""Helpers the tools share: resolving an input (inline, path or blob) and running heavy work off the event loop."""

import threading

import anyio

from .auth import tool_gate
from .blobs import get_config, resolve_text

_HEAVY = threading.Lock()
HEAVY_POLL = 0.02         # seconds between looks at the heavy-job lock while it is taken


async def resolve_input(content: str = "", path: str = "", blob_id: str = "", ctx=None) -> tuple[str, str]:
    """resolve_text for a tool. A blob_id is checked against the caller's bearer first
    (over HTTP) and decoded in a worker thread; inline and path inputs behave exactly as before.
    Raises ValueError with a message the tool can return."""
    if blob_id and blob_id.strip():
        cfg = get_config()
        denied = tool_gate(ctx, cfg)
        if denied:
            raise ValueError(denied)
        # a stored document may be far larger than an inline one could be (X3D_MCP_TOOL_MAX_BYTES):
        # a 34 KB blob can be 250 MiB of text, and a parse costs about 15 times that in memory
        return await anyio.to_thread.run_sync(resolve_text, content or "", path or "", blob_id,
                                              cfg.tool_max_bytes)
    return resolve_text(content or "", path or "", blob_id or "")


def require_blob_access(ctx) -> None:
    """Raise ValueError unless this caller may store blobs (used by return_mode="blob")."""
    denied = tool_gate(ctx, get_config())
    if denied:
        raise ValueError(denied)


async def run_heavy(fn, *args, size: int = 0, **kwargs):
    """Call fn(*args, **kwargs). Work on documents of offload_min bytes or more runs in a
    worker thread, one such job at a time, so the event loop keeps answering (and accepting
    uploads) while a 50 MB scene is parsed. Smaller work runs inline exactly as before."""
    if size < get_config().offload_min:
        return fn(*args, **kwargs)
    # Wait for the lock on the event loop, not in a pool thread: queued jobs would otherwise
    # hold a thread each and, past 40, starve every other caller of the pool (the blob tools).
    while not _HEAVY.acquire(blocking=False):
        await anyio.sleep(HEAVY_POLL)
    try:
        return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))
    finally:
        _HEAVY.release()
