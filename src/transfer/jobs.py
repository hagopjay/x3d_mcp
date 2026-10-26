"""Run one heavy job at a time without parking a worker thread per waiting call.

`tooling.run_heavy` takes a threading.Lock inside the worker thread, so every queued call holds one of anyio's 40 worker
threads while it waits, and 41 queued calls starve every other `to_thread` user (input resolution, blob reads, other
tools). `run_exclusive` waits on the event loop instead, in first-in first-out order, and takes a thread only once the
lock is free. The queue is bounded (a full queue is answered at once with `Busy`) and a job has a wall-clock budget.

The lock is the one `run_heavy` uses, so splat jobs and run_heavy jobs still run one at a time between them.
"""

import collections

import anyio

from .errors import BlobError
from .tooling import _HEAVY

_POLL = 0.02
_waiting: collections.deque = collections.deque()     # touched only from the event loop thread


class Busy(BlobError):
    """Too many heavy jobs are queued, or one ran past its time budget."""
    status = 503


async def run_exclusive(fn, *args, max_queue: int = 8, budget: float | None = None):
    """Call fn(*args) in a worker thread once every earlier caller is done. Raises Busy when `max_queue` calls are already
    waiting, or when the job has not finished within `budget` seconds of starting (the job itself cannot be stopped; the
    lock stays held until it ends, so the next caller still waits for it)."""
    if len(_waiting) >= max_queue:
        raise Busy(f"{len(_waiting)} heavy jobs are already queued (X3D_MCP_SPLAT_MAX_QUEUE); retry shortly")
    ticket = object()
    _waiting.append(ticket)
    try:
        while not (_waiting[0] is ticket and _HEAVY.acquire(blocking=False)):
            await anyio.sleep(_POLL)
    finally:
        _waiting.remove(ticket)

    def locked():
        try:
            return fn(*args)
        finally:
            _HEAVY.release()

    with anyio.move_on_after(budget or None) as scope:
        return await anyio.to_thread.run_sync(locked, abandon_on_cancel=True)
    if scope.cancelled_caught:
        raise Busy(f"the job ran past its {budget:g} s budget (X3D_MCP_SPLAT_JOB_SECONDS); it is still running on the server")
