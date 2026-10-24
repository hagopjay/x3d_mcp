"""Transfer layer: move large documents to and from the tools without passing them through the model.

blobs     content-addressed blob store, the `resolve_text` input resolver, configuration
capped    bounded decoders (decompression-bomb guard) for gzip, brotli and zstd
errors    the exceptions the layer raises (all ValueError subclasses)
"""

from .blobs import (  # noqa: F401
    BlobConfig, BlobStore, blob_enabled, configure, get_config, get_store, normalize_blob_id,
    reset, resolve_text,
)
from .errors import BlobError  # noqa: F401
