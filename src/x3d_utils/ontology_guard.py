"""Limits for ontology queries: what a SPARQL text may do, how long it may run, how many rows come back.

query_ontology takes SPARQL from a model, and on a hosted server from anyone who can reach /mcp. rdflib
executes SERVICE (an outgoing HTTP request) and FROM <url> (a fetch), runs a pathological property path
until it finishes, and returns every row. The guard parses the text first and refuses what the tools never
need; the runner puts a deadline on the query that no thread can ignore.
"""

import multiprocessing
import os
import re
import threading

from rdflib.plugins.sparql import algebra, parser

MAX_CHARS_ENV = "X3D_MCP_SPARQL_MAX_CHARS"
TIMEOUT_ENV = "X3D_MCP_SPARQL_TIMEOUT"
MAX_ROWS_ENV = "X3D_MCP_ONTOLOGY_MAX_ROWS"
DEFAULT_MAX_CHARS = 20_000
DEFAULT_TIMEOUT = 20.0
DEFAULT_MAX_ROWS = 20_000

_NODE_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class QueryRefused(ValueError):
    """The query text is not allowed (message says why)."""


class QueryTimeout(TimeoutError):
    """The query ran past its deadline and was stopped."""


def _env_number(name: str, default, cast):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def max_chars() -> int:
    return _env_number(MAX_CHARS_ENV, DEFAULT_MAX_CHARS, int)


def timeout_seconds() -> float:
    return _env_number(TIMEOUT_ENV, DEFAULT_TIMEOUT, float)


def max_rows() -> int:
    return _env_number(MAX_ROWS_ENV, DEFAULT_MAX_ROWS, int)


def check_node_type(name: str) -> str:
    """A node or term name that is safe to put into a query text. Raises QueryRefused."""
    if not isinstance(name, str) or not _NODE_TYPE.match(name):
        raise QueryRefused("node type must be a plain name such as Sphere (letters, digits and underscore)")
    return name


def _walk(node, seen=None):
    """Every CompValue name in a parsed query (the algebra is a tree of dicts and lists)."""
    if seen is None:
        seen = set()
    if id(node) in seen:
        return
    seen.add(id(node))
    name = getattr(node, "name", None)
    if isinstance(name, str):
        yield name
    if isinstance(node, dict):
        children = list(node.values())
    elif isinstance(node, (list, tuple, set)):
        children = list(node)
    else:
        return
    for child in children:
        yield from _walk(child, seen)


def guard_sparql(text: str) -> None:
    """Raise QueryRefused unless `text` is a SELECT or ASK query with no SERVICE and no FROM clause."""
    if not isinstance(text, str) or not text.strip():
        raise QueryRefused("empty query")
    if len(text) > max_chars():
        raise QueryRefused(f"query is {len(text)} characters; the limit is {max_chars()} ({MAX_CHARS_ENV})")
    try:
        parsed = parser.parseQuery(text)
        translated = algebra.translateQuery(parsed)
    except Exception as e:                      # rdflib raises several parse and translation types
        raise QueryRefused(f"SPARQL does not parse: {e}") from None
    kind = translated.algebra.name
    if kind not in ("SelectQuery", "AskQuery"):
        raise QueryRefused(f"only SELECT and ASK are allowed, not {kind}")
    names = set(_walk(translated.algebra))
    if names & {"Service", "ServiceGraphPattern"}:
        raise QueryRefused("SERVICE is not allowed (it makes the server send HTTP requests)")
    clauses = parsed[1].get("datasetClause") if hasattr(parsed[1], "get") else None
    if isinstance(clauses, list) and clauses:
        raise QueryRefused("FROM and FROM NAMED are not allowed; the ontology graph is fixed")


def _child(conn, fn, args):
    try:
        conn.send(("ok", fn(*args)))
    except BaseException as e:                  # sent back as an error, never raised in the child
        conn.send(("err", f"{type(e).__name__}: {e}"))
    finally:
        conn.close()


def run_bounded(fn, *args, timeout: float | None = None):
    """Run fn(*args) with a deadline that stops it. The result must be picklable.

    Where fork exists the work runs in a forked child that is killed at the deadline (the ontology graph is
    shared copy-on-write, so there is nothing to load). Without fork it runs in a daemon thread and the
    caller is released at the deadline, but the thread keeps going until it finishes.
    Raises QueryTimeout; any error inside fn is raised as RuntimeError with its message.
    """
    limit = timeout_seconds() if timeout is None else timeout
    if "fork" in multiprocessing.get_all_start_methods():
        ctx = multiprocessing.get_context("fork")
        recv, send = ctx.Pipe(duplex=False)
        proc = ctx.Process(target=_child, args=(send, fn, args), daemon=True)
        proc.start()
        send.close()
        try:
            if not recv.poll(limit):
                raise QueryTimeout(f"query ran past {limit:g} s and was stopped ({TIMEOUT_ENV})")
            try:
                status, value = recv.recv()
            except EOFError:
                raise RuntimeError("query worker ended without an answer (out of memory?)") from None
        finally:
            if proc.is_alive():
                proc.kill()
            proc.join(5)
            recv.close()
    else:
        box: dict = {}

        def target():
            try:
                box["r"] = ("ok", fn(*args))
            except BaseException as e:
                box["r"] = ("err", f"{type(e).__name__}: {e}")

        t = threading.Thread(target=target, daemon=True)
        t.start()
        t.join(limit)
        if "r" not in box:
            raise QueryTimeout(f"query ran past {limit:g} s ({TIMEOUT_ENV}); it cannot be stopped on this platform")
        status, value = box["r"]
    if status == "err":
        raise RuntimeError(value)
    return value
