"""Errors raised by the transfer layer.

All of them subclass ValueError so the existing tool code, which turns a
ValueError from the input resolver into a plain error message, handles them
without change. ``status`` is the HTTP status the blob routes answer with.
"""


class BlobError(ValueError):
    status = 400


class BlobIdError(BlobError):
    status = 400


class BlobCorrupt(BlobError):
    """A compressed stream is truncated, damaged, or followed by extra data."""
    status = 400


class BlobMismatch(BlobError):
    """The sha256 of the decoded bytes is not the expected one."""
    status = 422

    def __init__(self, expected: str, actual: str):
        super().__init__(
            "sha256 of the decoded bytes does not match the expected id "
            f"(expected {expected}, actual {actual})"
        )
        self.expected = expected
        self.actual = actual


class BlobNotFound(BlobError):
    status = 404


class BlobTooLarge(BlobError):
    """Decoded size, wire size or expansion ratio over a limit."""
    status = 413


class UnsupportedEncoding(BlobError):
    status = 415

    def __init__(self, message: str, supported: tuple[str, ...] = ()):
        super().__init__(message)
        self.supported = supported


class StoreFull(BlobError):
    """Quota or memory budget exhausted and nothing can be evicted."""
    status = 507


class TooManyUploads(BlobError):
    status = 429


class BlobUnavailable(BlobError):
    """The blob store is switched off on this server."""
    status = 404


class BlobTimeout(BlobError):
    status = 408


class ConfigError(ValueError):
    """An environment variable of the transfer layer is invalid (a startup error)."""
