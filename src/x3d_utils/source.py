"""Resolve X3D input from either inline content or a filesystem path.

Generated X3D scenes are routinely hundreds of KB -- forcing the model to inline
the whole document just to validate or render it is wasteful and error-prone.
Every tool that consumes a scene should accept a `path` as an alternative to
`content`; this is the single shared resolver they call.
"""

import os
from pathlib import Path


def _http_transport_active() -> bool:
    """Whether the server is running over the Streamable HTTP transport.

    Path input is a local-use affordance; over HTTP it would let remote
    callers read files from the server's filesystem (same policy as
    tools.scene_ops).
    """
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    return transport in ("http", "streamable-http")


def read_path_text(path: str) -> str:
    """Read an X3D file as UTF-8 text. Raises ValueError for the HTTP transport, a missing file, or a read error."""
    if _http_transport_active():
        raise ValueError(
            "Path input is disabled over the HTTP transport. "
            "Provide inline X3D content instead."
        )
    p = Path(path).expanduser()
    if not p.is_file():
        raise ValueError(f"X3D file not found: {p}")
    try:
        return p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Could not read X3D file {p}: {exc}") from exc


def load_x3d_source(content: str | None = None, path: str | None = None,
                    blob_id: str | None = None) -> str:
    """Return X3D text from exactly one of `content` (inline), `path` (a file) or `blob_id` (a stored blob).

    Raises ValueError if none or several are given, or if the file or blob is
    missing -- callers should surface the message to the user. The choice of
    input is made in transfer.blobs.resolve_text, the one place that owns it.
    """
    from transfer.blobs import resolve_text
    return resolve_text(content or "", path or "", blob_id or "")[0]
