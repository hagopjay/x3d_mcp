"""Server limits for the splat tools, from the environment.

  X3D_MCP_SPLAT_MAX_POINTS        most splats a file may declare before it is refused. Default 10,000,000
                                  (about 200 MB of SPZ streams and 560 MB of float32 arrays at SH degree 0).
  X3D_MCP_SPLAT_MAX_RATIO         refuse an SPZ file whose streams would expand to more than this many times its
                                  size; real files measure 1.2 to 3. Default 250 (the blob store's own ratio limit),
                                  which leaves wide headroom. 0 switches the policy off (the header is still checked
                                  against what deflate and zstd can produce).
  X3D_MCP_SPLAT_MAX_PAYLOAD_BYTES most decoded SPZ stream bytes a file may declare before it is refused. Default 512M
                                  (the decoder holds the streams and float32 arrays about four times that).
  X3D_MCP_SPLAT_MAX_TEXT_BYTES    largest X3D text scene splat_inspect and splat_chunk will read. Default 256M.
                                  Text is capped by bytes because it parses to arrays larger than itself. Measured
                                  peaks above the input: X3D text 4 times (many small nodes, the worst case), ASCII
                                  PLY 5.6 times (one-digit numbers, the worst case); plan for that at the cap.
  X3D_MCP_SPLAT_MAX_OUTPUT_BYTES  largest X3D text a single splat_chunk call may produce, estimated from a sample
                                  before anything is cut. Default 512M. The files are held in memory until stored.

  X3D_MCP_SPLAT_MAX_QUEUE         calls that may wait for the one running heavy job (default 8); more get a busy error.
  X3D_MCP_SPLAT_JOB_SECONDS       how long a call waits for its own job once it has started (default 600; 0: no limit).

Sizes accept K, M, G suffixes (binary units). A bad value is a startup error (the server checks them once).
"""

import os
from dataclasses import dataclass

from .blobs import _env_float, _env_size


@dataclass(frozen=True)
class SplatLimits:
    max_points: int = 10_000_000
    max_ratio: float | None = 250.0
    max_payload_bytes: int = 512 << 20
    max_text_bytes: int = 256 << 20
    max_output_bytes: int = 512 << 20
    max_queue: int = 8
    job_seconds: float = 600.0

    @classmethod
    def from_env(cls, environ=None) -> "SplatLimits":
        """Read the limits. Raises transfer.errors.ConfigError for a value that is not a number or size."""
        env = os.environ if environ is None else environ
        ratio = _env_float(env, "X3D_MCP_SPLAT_MAX_RATIO", cls.max_ratio, 0.0)
        return cls(
            max_points=_env_size(env, "X3D_MCP_SPLAT_MAX_POINTS", cls.max_points, 1),
            max_ratio=ratio or None,
            max_payload_bytes=_env_size(env, "X3D_MCP_SPLAT_MAX_PAYLOAD_BYTES", cls.max_payload_bytes, 1),
            max_text_bytes=_env_size(env, "X3D_MCP_SPLAT_MAX_TEXT_BYTES", cls.max_text_bytes, 1),
            max_output_bytes=_env_size(env, "X3D_MCP_SPLAT_MAX_OUTPUT_BYTES", cls.max_output_bytes, 1),
            max_queue=_env_size(env, "X3D_MCP_SPLAT_MAX_QUEUE", cls.max_queue, 1),
            job_seconds=_env_float(env, "X3D_MCP_SPLAT_JOB_SECONDS", cls.job_seconds, 0.0),
        )
