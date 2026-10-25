"""transfer.jobs.run_exclusive: one heavy job at a time, queued without holding worker threads."""

import threading
import time

import anyio
import pytest

from transfer import jobs
from transfer.jobs import Busy, run_exclusive


def test_waiting_calls_do_not_hold_worker_threads():
    async def main():
        results = []

        async def job(i):
            results.append(await run_exclusive(lambda: (time.sleep(0.05), i)[1], max_queue=100))

        async with anyio.create_task_group() as tg:
            for i in range(60):                                 # more than anyio's 40 worker threads
                tg.start_soon(job, i)
            await anyio.sleep(0.2)
            t = time.perf_counter()
            await anyio.to_thread.run_sync(lambda: 1)           # an unrelated caller
            waited = time.perf_counter() - t
        return results, waited

    results, waited = anyio.run(main)
    assert sorted(results) == list(range(60)) and waited < 0.5


def test_jobs_run_one_at_a_time_in_arrival_order():
    order, running = [], []

    def work(i):
        running.append(i)
        assert len(running) == 1
        time.sleep(0.02)
        order.append(i)
        running.pop()

    async def main():
        async with anyio.create_task_group() as tg:
            for i in range(6):
                tg.start_soon(lambda i=i: run_exclusive(work, i, max_queue=10))
                await anyio.sleep(0.005)

    anyio.run(main)
    assert order == list(range(6))


def test_a_full_queue_is_answered_with_busy():
    release = threading.Event()

    async def main():
        errors = []

        async def call():
            try:
                await run_exclusive(lambda: release.wait(5), max_queue=2)
            except Busy as exc:
                errors.append(str(exc))

        async with anyio.create_task_group() as tg:
            for _ in range(5):
                tg.start_soon(call)
                await anyio.sleep(0.05)
            release.set()
        return errors

    errors = anyio.run(main)
    assert len(errors) == 2 and "X3D_MCP_SPLAT_MAX_QUEUE" in errors[0] and not jobs._waiting


def test_a_job_past_its_budget_is_reported_but_keeps_the_lock_until_it_ends():
    release = threading.Event()

    async def main():
        with pytest.raises(Busy, match="budget"):
            await run_exclusive(lambda: release.wait(5), budget=0.1)
        t = time.perf_counter()
        async with anyio.create_task_group() as tg:
            tg.start_soon(lambda: anyio.to_thread.run_sync(lambda: (time.sleep(0.2), release.set())))
            await run_exclusive(lambda: None)                   # waits for the abandoned job
        return time.perf_counter() - t

    assert anyio.run(main) >= 0.15
