"""Helpers the tools share: resolving an input (inline, path or blob) and running heavy work off the event loop."""

import functools
import inspect
import threading
from contextlib import asynccontextmanager
from contextvars import ContextVar

import anyio

from .auth import tool_gate
from .blobs import get_config, get_store, resolve_text

_HEAVY = threading.Lock()
HEAVY_POLL = 0.02         # seconds between looks at the heavy-job lock while it is taken

# Decoded blob text held by tool calls in flight. A call that names a blob decodes the whole
# document before it queues for the heavy-job lock, so without a bound every queued call would
# hold its text. The bound is twice the per-document limit; a lone document is always admitted.
_DOC_LOCK = threading.Lock()
_DOC_BYTES = 0
_held: ContextVar["_Held | None"] = ContextVar("x3d_blob_held", default=None)


class _Held:
    """The decoded bytes one tool call has been admitted for."""

    def __init__(self):
        self.bytes = 0


def _admit(n: int, budget: int) -> bool:
    global _DOC_BYTES
    with _DOC_LOCK:
        if _DOC_BYTES and _DOC_BYTES + n > budget:
            return False
        _DOC_BYTES += n
        return True


def _give_back(n: int) -> None:
    global _DOC_BYTES
    with _DOC_LOCK:
        _DOC_BYTES -= n


def _holds(fn):
    """Decorator for a tool that takes `blob_id`: the decoded document it asks for is counted
    against the in-flight budget from the moment it is admitted until the call ends."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        held = _Held()
        token = _held.set(held)
        try:
            return await fn(*args, **kwargs)
        finally:
            _held.reset(token)
            _give_back(held.bytes)
    return wrapper


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


def holds_input(fn):
    """Decorator for a tool that takes `blob_id`: counts the decoded document against the in-flight
    budget (_holds) and holds one of X3D_MCP_TOOL_CONCURRENCY places for the call (with_blob_slot)."""
    return with_blob_slot(_holds(fn))


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
        held = _held.get()
        if held is not None and not (content or "").strip() and not (path or "").strip():
            size = get_store().stat(blob_id)["size"]
            if size <= cfg.tool_max_bytes:              # a larger one is refused when it is read
                while not _admit(size, 2 * cfg.tool_max_bytes):
                    await anyio.sleep(HEAVY_POLL)       # wait on the loop, holding neither text nor thread
                held.bytes += size
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
