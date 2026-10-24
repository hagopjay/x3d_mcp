"""Helpers the tools share: resolving an input (inline, path or blob) and running heavy work off the event loop."""

import threading

import anyio

from .auth import tool_gate
from .blobs import get_config, resolve_text

_HEAVY = threading.Lock()


async def resolve_input(content: str = "", path: str = "", blob_id: str = "", ctx=None) -> tuple[str, str]:
    """resolve_text for a tool. A blob_id is checked against the caller's bearer first
    (over HTTP) and decoded in a worker thread; inline and path inputs behave exactly as before.
    Raises ValueError with a message the tool can return."""
    if blob_id and blob_id.strip():
        denied = tool_gate(ctx, get_config())
        if denied:
            raise ValueError(denied)
        return await anyio.to_thread.run_sync(resolve_text, content or "", path or "", blob_id)
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

    def locked():
        with _HEAVY:
            return fn(*args, **kwargs)

    return await anyio.to_thread.run_sync(locked)
