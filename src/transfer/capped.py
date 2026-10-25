"""Incremental decoders with a hard cap on decoded bytes and bounded output per library call.

A decompression bomb is a tiny input that expands without bound: 3 KB of brotli
decodes to 2 GB in one library call. Every decoder here is driven in steps that
cannot expand past a fixed piece size, and the decoded size and the expansion
ratio are checked between steps, so a bomb is refused after a few megabytes.

feed() is a generator: each decoded piece goes to the caller (hash, write to
store) before the next decode step runs, so nothing accumulates. gzip and
brotli emit at most OUT_PIECE bytes per step; python-zstandard's
decompressobj has no output bound, so the input is fed in ZSTD_SLICE-byte
slices instead (a 256-byte slice expands to at most a few MiB).

brotli needs brotli>=1.2.0 for output_buffer_limit; older wheels cannot be
bounded, so brotli is refused rather than decoded unbounded. zstd needs the
zstandard package. gzip and identity use only the standard library.
"""

import zlib

from .errors import BlobCorrupt, BlobTooLarge, UnsupportedEncoding

OUT_PIECE = 1 << 20
ZSTD_SLICE = 256
COALESCE = 256 << 10          # zstd pieces are joined up to this size before they are yielded

CODECS = ("identity", "gzip", "br", "zstd")

_br_ok: bool | None = None
_zstd_mod = None
_zstd_checked = False


def zstd_module():
    """The zstandard module, or None when the optional dependency is absent."""
    global _zstd_mod, _zstd_checked
    if not _zstd_checked:
        _zstd_checked = True
        try:
            import zstandard
            _zstd_mod = zstandard
        except ImportError:
            _zstd_mod = None
    return _zstd_mod


def brotli_bounded() -> bool:
    """Whether brotli is installed and supports a bounded Decompressor.process()."""
    global _br_ok
    if _br_ok is None:
        try:
            import brotli
            brotli.Decompressor().process(b"", output_buffer_limit=1)
            _br_ok = True
        except Exception:                # ImportError, or TypeError on brotli < 1.2.0
            _br_ok = False
    return _br_ok


def available_codecs() -> tuple[str, ...]:
    out = ["identity", "gzip"]
    if brotli_bounded():
        out.append("br")
    if zstd_module() is not None:
        out.append("zstd")
    return tuple(out)


def normalize_encoding(name: str | None) -> str:
    enc = (name or "identity").strip().lower()
    if enc in ("", "none"):
        enc = "identity"
    if enc == "x-gzip":
        enc = "gzip"
    if enc not in CODECS:
        raise UnsupportedEncoding(
            f"unsupported content encoding {name!r}; supported: {', '.join(available_codecs())}",
            available_codecs(),
        )
    if enc not in available_codecs():
        extra = "brotli>=1.2.0" if enc == "br" else "zstandard"
        raise UnsupportedEncoding(
            f"content encoding {enc!r} needs the {extra} package (pip install 'x3d-mcp[blob]'); "
            f"supported here: {', '.join(available_codecs())}",
            available_codecs(),
        )
    return enc


class Capped:
    """Decode a compressed stream with a size cap and an expansion-ratio limit.

    max_decoded: refuse once more than this many decoded bytes were produced.
    max_ratio:   refuse once decoded > max_ratio x wire bytes, checked only after
                 ratio_after decoded bytes (a small, highly repetitive file is fine).
    """

    def __init__(self, encoding: str, max_decoded: int, max_ratio: float | None = None,
                 ratio_after: int = 1 << 20):
        self.encoding = normalize_encoding(encoding)
        self.max_decoded = max_decoded
        self.max_ratio = max_ratio
        self.ratio_after = ratio_after
        self.wire = 0
        self.out = 0
        if self.encoding == "identity":
            self._d = None
        elif self.encoding == "gzip":
            self._d = zlib.decompressobj(31)
        elif self.encoding == "br":
            import brotli
            self._d = brotli.Decompressor()
        else:
            self._d = zstd_module().ZstdDecompressor().decompressobj()

    def _account(self, n: int) -> None:
        self.out += n
        if self.out > self.max_decoded:
            raise BlobTooLarge(
                f"decoded size exceeds {self.max_decoded} bytes (after {self.wire} wire bytes)")
        if (self.max_ratio and self.out > self.ratio_after
                and self.out > self.max_ratio * max(self.wire, 1)):
            raise BlobTooLarge(
                f"expansion ratio above {self.max_ratio:g}:1 (decoded {self.out}, "
                f"wire {self.wire}); set X3D_MCP_BLOB_MAX_RATIO to raise or disable (0) the limit")

    def feed(self, chunk: bytes):
        self.wire += len(chunk)
        enc = self.encoding
        if enc == "identity":
            self._account(len(chunk))
            yield chunk
            return
        try:
            if enc == "gzip":
                buf = chunk
                while buf:
                    if self._d.eof:
                        raise BlobCorrupt("trailing data after the compressed stream")
                    o = self._d.decompress(buf, OUT_PIECE)
                    buf = self._d.unconsumed_tail
                    if o:
                        self._account(len(o))
                        yield o
                    if self._d.eof and (buf or self._d.unused_data):
                        raise BlobCorrupt("trailing data after the compressed stream")
            elif enc == "br":
                o = self._d.process(chunk, output_buffer_limit=OUT_PIECE)
                while True:
                    if o:
                        self._account(len(o))
                        yield o
                    if self._d.can_accept_more_data():
                        break
                    o = self._d.process(b"", output_buffer_limit=OUT_PIECE)
            else:  # zstd
                # slices of already-compressed data decode to ~256 B each; join them
                # or the caller sees thousands of tiny pieces
                acc, n = [], 0
                for i in range(0, len(chunk), ZSTD_SLICE):
                    if self._d.eof:
                        raise BlobCorrupt("trailing data after the compressed stream")
                    o = self._d.decompress(chunk[i:i + ZSTD_SLICE])
                    if o:
                        self._account(len(o))
                        acc.append(o)
                        n += len(o)
                        if n >= COALESCE:
                            yield b"".join(acc)
                            acc, n = [], 0
                    if self._d.eof and self._d.unused_data:
                        raise BlobCorrupt("trailing data after the compressed stream")
                if acc:
                    yield b"".join(acc)
        except (BlobCorrupt, BlobTooLarge):
            raise
        except Exception as e:           # zlib.error, brotli.error, zstandard.ZstdError
            raise BlobCorrupt(f"corrupt {enc} stream: {e}") from e

    def finish(self):
        """Call once after the last chunk. Yields output the decoder still holds, then
        checks the stream ended cleanly.

        brotli-python reports can_accept_more_data() == True while the tail of the
        output is still buffered, and zlib can hold output after its input is used
        up, so both are drained here with empty input."""
        enc = self.encoding
        try:
            if enc == "gzip":
                while not self._d.eof:
                    o = self._d.decompress(b"", OUT_PIECE)
                    if not o:
                        break
                    self._account(len(o))
                    yield o
            elif enc == "br":
                while not self._d.is_finished():
                    o = self._d.process(b"", output_buffer_limit=OUT_PIECE)
                    if not o:
                        break
                    self._account(len(o))
                    yield o
        except (BlobCorrupt, BlobTooLarge):
            raise
        except Exception as e:
            raise BlobCorrupt(f"corrupt {enc} stream: {e}") from e
        if enc == "gzip" and not self._d.eof:
            raise BlobCorrupt("truncated gzip stream")
        if enc == "br" and not self._d.is_finished():
            raise BlobCorrupt("truncated br stream")
        if enc == "zstd" and not self._d.eof:
            raise BlobCorrupt("truncated zstd stream")


def decode_all(data: bytes, encoding: str, max_decoded: int, max_ratio: float | None = None) -> bytes:
    """Decode a whole compressed buffer with the same guards as a stream."""
    d = Capped(encoding, max_decoded, max_ratio)
    parts: list[bytes] = []
    for i in range(0, max(len(data), 1), 1 << 20):
        parts.extend(d.feed(data[i:i + (1 << 20)]))
    parts.extend(d.finish())
    return b"".join(parts)
