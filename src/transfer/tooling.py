"""Helpers the tools share: resolving an input (inline, path or blob) and running heavy work off the event loop."""

import functools
import inspect
import threading
from contextlib import asynccontextmanager

import anyio

from .auth import tool_gate
from .blobs import get_config, resolve_text

_HEAVY = threading.Lock()
_slots_lock = threading.Lock()
_slots_busy = 0


@asynccontextmanager
async def blob_slot(blob_id: str = ""):
    """Hold one of X3D_MCP_TOOL_CONCURRENCY places while a tool works on a blob.

    A blob decodes to bytes, then text, then a parse tree: about 12 times its decoded size
    in memory, held for the whole call. Without a bound, a few calls at once multiply that
    (the store counts only stored bytes). Calls without a blob_id never wait here.
    """
    global _slots_busy
    if not (blob_id or "").strip():
        yield
        return
    while True:
        with _slots_lock:
            if _slots_busy < get_config().tool_concurrency:
                _slots_busy += 1
                break
        await anyio.sleep(0.05)
    try:
        yield
    finally:
        with _slots_lock:
            _slots_busy -= 1


def with_blob_slot(fn):
    """Decorate an async tool with a blob_id parameter so blob_slot covers the whole call.
    The signature is kept (functools.wraps), so the tool schema is unchanged."""
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        blob_id = sig.bind_partial(*args, **kwargs).arguments.get("blob_id") or ""
        async with blob_slot(blob_id):
            return await fn(*args, **kwargs)

    return wrapper


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
