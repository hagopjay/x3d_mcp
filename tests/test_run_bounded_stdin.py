"""run_bounded must not hang when another thread is blocked reading stdin (the MCP stdio transport)."""

import multiprocessing
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

pytestmark = pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(), reason="the deadlock needs fork"
)

SCRIPT = textwrap.dedent(
    """
    import os, sys, threading, time
    from x3d_utils.ontology_guard import run_bounded, QueryTimeout

    def add(a, b):
        return a + b

    # Like the stdio transport: a thread parked in a read holds the stdin buffer lock.
    threading.Thread(target=sys.stdin.buffer.read, args=(1,), daemon=True).start()
    time.sleep(0.3)
    try:
        for _ in range(3):
            assert run_bounded(add, 2, 3, timeout=5) == 5
    except QueryTimeout as e:
        print("TIMEOUT", e, flush=True)
        os._exit(1)
    print("OK", flush=True)
    os._exit(0)  # skip interpreter shutdown, which would wait on the reader thread's stdin lock
    """
)


def test_run_bounded_with_stdin_reader_thread():
    r, w = os.pipe()  # stdin that never delivers data, so the reader thread stays blocked
    try:
        env = dict(os.environ, PYTHONPATH=str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""))
        proc = subprocess.run(
            [sys.executable, "-c", SCRIPT], stdin=r, capture_output=True, text=True, timeout=60, env=env
        )
    finally:
        os.close(r)
        os.close(w)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK" in proc.stdout
