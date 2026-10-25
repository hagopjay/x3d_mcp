"""Server limits for the splat tools, from the environment.

  X3D_MCP_SPLAT_MAX_POINTS        most splats a file may declare before it is refused. Default 10,000,000
                                  (about 200 MB of SPZ streams and 560 MB of float32 arrays at SH degree 0).
  X3D_MCP_SPLAT_MAX_RATIO         refuse an SPZ file whose streams would expand to more than this many times its
                                  size; real files measure 1.2 to 3. Unset: no policy (the header is still checked
                                  against what deflate and zstd can produce). A value of 100 leaves wide headroom.
  X3D_MCP_SPLAT_MAX_TEXT_BYTES    largest X3D text scene splat_inspect and splat_chunk will read. Default 256M.
                                  Text parses to float64, 1.5 to 4 times its size, so it is capped by bytes.
  X3D_MCP_SPLAT_MAX_OUTPUT_BYTES  largest X3D text a single splat_chunk call may produce, estimated from a sample
                                  before anything is cut. Default 512M. The files are held in memory until stored.

Sizes accept K, M, G suffixes (binary units). A bad value is a startup error (the server checks them once).
"""

import os
from dataclasses import dataclass

from .blobs import _env_float, _env_size


@dataclass(frozen=True)
class SplatLimits:
    max_points: int = 10_000_000
    max_ratio: float | None = None
    max_text_bytes: int = 256 << 20
    max_output_bytes: int = 512 << 20

    @classmethod
    def from_env(cls, environ=None) -> "SplatLimits":
        """Read the limits. Raises transfer.errors.ConfigError for a value that is not a number or size."""
        env = os.environ if environ is None else environ
        ratio = _env_float(env, "X3D_MCP_SPLAT_MAX_RATIO", 0.0, 0.0)
        return cls(
            max_points=_env_size(env, "X3D_MCP_SPLAT_MAX_POINTS", cls.max_points, 1),
            max_ratio=ratio or None,
            max_text_bytes=_env_size(env, "X3D_MCP_SPLAT_MAX_TEXT_BYTES", cls.max_text_bytes, 1),
            max_output_bytes=_env_size(env, "X3D_MCP_SPLAT_MAX_OUTPUT_BYTES", cls.max_output_bytes, 1),
        )
