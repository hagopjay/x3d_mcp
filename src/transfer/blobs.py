"""Content-addressed blob store: large documents travel out of band and tools name them by id.

A tool call that carries a 10 MB scene (or returns one) pushes every byte through
the model's context. The blob store breaks that: a program uploads the bytes once
(HTTP route, or the small `blob_put` tool), the tools take `blob_id=<id>` instead
of `content=`, and mutating tools can hand back an id plus a summary instead of
the document. Ids are the lowercase hex SHA-256 of the decoded bytes, so equal
content has one id whatever codec it travelled in.

Bytes are kept compressed: zstd level 3 by default (25x on a 10 MB scene text in
15 ms), brotli and zstd uploads are kept exactly as sent, and data that does not
compress (an .spz file) is kept as is. Blobs live in memory up to a budget and
spill to a private directory past it, if one is configured. Every blob has an idle
TTL (refreshed on use) and a hard maximum age; the least recently used blobs are
evicted when the quota is reached. Nothing is persisted across restarts.

Environment variables (all optional; sizes accept K/M/G suffixes, binary units):

  X3D_MCP_BLOB                 auto (default) | on | off. auto: on under stdio, on under
                               HTTP only when a token is set. off: never. on over HTTP
                               without a token is a startup error.
  X3D_MCP_BLOB_TOKEN           bearer token (>= 32 characters) for the HTTP routes and,
                               unless TOOLS_OPEN, for the blob parameters of MCP tools.
  X3D_MCP_BLOB_TOOLS_OPEN      1: MCP tools do not need the bearer (clients that cannot set
                               headers on /mcp). Default 0.
  X3D_MCP_BLOB_MAX_BYTES       largest blob, decoded and on the wire. Default 256M.
  X3D_MCP_BLOB_MEM_BYTES       stored bytes kept in RAM. Default 128M.
  X3D_MCP_BLOB_QUOTA_BYTES     total stored bytes. Default 1G with a spill directory,
                               otherwise the RAM budget.
  X3D_MCP_BLOB_DIR             spill directory (private, mode 0700, owned by the server user;
                               created if missing; locked, one process only). Unset: RAM only
                               over HTTP; under stdio a temporary directory is created on first
                               spill and removed at exit and on SIGTERM.
  X3D_MCP_BLOB_HOT_BYTES       decoded-blob cache for paged reads. Default 64M, 0 disables.
  X3D_MCP_BLOB_TTL             idle seconds before a blob expires (refreshed on use). 3600.
  X3D_MCP_BLOB_MAX_AGE         hard lifetime in seconds, from the first store. 86400.
  X3D_MCP_BLOB_MAX_COUNT       most blobs in the table. 10000.
  X3D_MCP_BLOB_MAX_RATIO       largest decoded:wire ratio once 1 MiB was decoded; 0 disables. 250.
  X3D_MCP_BLOB_MAX_UPLOADS     concurrent streaming uploads before 429. 4.
  X3D_MCP_BLOB_IDLE_TIMEOUT    seconds without upload data before 408; also the write timeout of
                               a download. 30.
  X3D_MCP_BLOB_TOTAL_TIMEOUT   seconds for one whole upload. 600.
  X3D_MCP_BLOB_MIN_RATE        bytes per second an upload must average after 10 s; 0 disables. 4096.
  X3D_MCP_BLOB_INLINE_MAX      largest decoded blob_put payload. 3000000 (its base64 form and
                               the JSON-RPC envelope must stay under the SDK's 4 MiB body cap).
  X3D_MCP_BLOB_PAGE_DEFAULT    blob_read page size. 16384.
  X3D_MCP_BLOB_PAGE_MAX        largest blob_read page. 262144.
  X3D_MCP_ALLOWED_HOSTS        comma list of Host values (SDK grammar, host:*) for /mcp and /blob.
  X3D_MCP_ALLOWED_ORIGINS      comma list of Origin values, same grammar; needs ALLOWED_HOSTS too.
  X3D_MCP_SUMMARY_ITEMS        most list items a summary return carries. 200.
  X3D_MCP_OFFLOAD_MIN          documents of at least this many bytes are processed in a worker
                               thread so the event loop keeps serving. Default 1M.
  X3D_MCP_TOOL_MAX_BYTES       largest decoded blob a tool will take as blob_id. Default 64M.
  X3D_MCP_BLOB_PATH_ROOTS      directories blob_put(path=) may read under (os.pathsep-separated);
                               unset: scene, data and splat extensions only.
"""

import atexit
import errno
import hashlib
import logging
import os
import re
import secrets
import shutil
import stat as _stat
import tempfile
import threading
import time
import weakref
import zlib
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Iterator

try:
    import fcntl
except ImportError:                       # Windows: no directory lock
    fcntl = None

from .capped import Capped, available_codecs, normalize_encoding, zstd_module
from .errors import (
    BlobCorrupt, BlobError, BlobIdError, BlobMismatch, BlobNotFound, BlobTooLarge,
    BlobUnavailable, ConfigError, StoreFull, StoreUnavailable, TooManyUploads,
)

_ID_RE = re.compile(r"[0-9a-f]{64}")
_MEDIA_RE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,95}")
_SPILL_FINAL = re.compile(r"[0-9a-f]{64}\.[0-9a-f]{16}")     # <id>.<random>: one file per entry
_SPILL_TEMP = re.compile(r"\.up_[0-9a-f]{32}")

DEFAULT_MEDIA_TYPE = "application/octet-stream"
PROBE_BYTES = 256 << 10          # decided on this much: compress, or store as is
PROBE_MIN_GAIN = 0.05            # a probe that shrinks by less than this is stored as is
RESERVE_FIRST = 1 << 20          # unknown-length uploads reserve this much, then grow by GROW
GROW = 4 << 20
PIECE = 1 << 20
SWEEP_EVERY = 1.0                # seconds between full scans for expired blobs

log = logging.getLogger("x3d_mcp.blob")
_AUTO_DIRS: list[str] = []       # temporary spill directories this process made


def _oserr(ex: OSError) -> str:
    """An OSError's reason without the file name: server paths do not go to clients (the
    detail is logged)."""
    return ex.strerror or type(ex).__name__


# --------------------------------------------------------------------- configuration

_SIZE_RE = re.compile(r"(\d+)\s*([kmg]i?b?)?", re.I)
_SUFFIX = {"": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30}


def _env_size(env, name: str, default: int, minimum: int = 0) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    m = _SIZE_RE.fullmatch(raw.strip())
    if not m:
        raise ConfigError(f"{name}={raw!r} is not a size (use digits with an optional K, M or G)")
    value = int(m.group(1)) * _SUFFIX[(m.group(2) or "")[:1].lower()]
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    return value


def _env_float(env, name: str, default: float, minimum: float = 0.0) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a number") from None
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum:g}")
    return value


def _env_bool(env, name: str, default: bool = False) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_list(env, name: str) -> tuple[str, ...]:
    return tuple(s.strip() for s in (env.get(name) or "").split(",") if s.strip())


def transport_is_http(environ=None) -> bool:
    transport = (environ if environ is not None else os.environ).get("MCP_TRANSPORT", "stdio")
    return transport.lower() in ("http", "streamable-http")


@dataclass(frozen=True)
class BlobConfig:
    enabled: bool = True
    http: bool = False
    token: str = field(default="", repr=False, compare=False)     # a secret: never in a repr
    tools_open: bool = False
    max_bytes: int = 256 << 20
    mem_bytes: int = 128 << 20
    quota_bytes: int = 0                  # 0: derive (see quota)
    spill_dir: str = ""
    auto_spill: bool = False              # stdio: create a private temp dir on first spill
    hot_bytes: int = 64 << 20
    ttl: float = 3600.0
    max_age: float = 86400.0
    max_ratio: float = 250.0
    max_uploads: int = 4
    idle_timeout: float = 30.0
    total_timeout: float = 600.0
    inline_max: int = 3_000_000
    page_default: int = 16384
    page_max: int = 262144
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    summary_items: int = 200
    offload_min: int = 1 << 20
    max_blobs: int = 10000
    tool_max_bytes: int = 96 << 20        # largest decoded blob a tool will read (the store itself takes max_bytes)
    min_rate: int = 4096
    path_roots: tuple[str, ...] = ()
    tool_concurrency: int = 2             # tool calls on blob inputs running at once; each holds several copies

    @property
    def quota(self) -> int:
        if self.quota_bytes:
            return self.quota_bytes
        return (1 << 30) if (self.spill_dir or self.auto_spill) else self.mem_bytes

    @classmethod
    def from_env(cls, environ=None, http: bool | None = None) -> "BlobConfig":
        """Build the configuration from the environment. Raises ConfigError (a startup
        error) for an invalid value, a token under 32 characters, or X3D_MCP_BLOB=on over
        HTTP without a token."""
        env = os.environ if environ is None else environ
        if http is None:
            http = transport_is_http(env)
        mode = (env.get("X3D_MCP_BLOB") or "auto").strip().lower()
        if mode not in ("auto", "on", "off"):
            raise ConfigError(f"X3D_MCP_BLOB={mode!r}; use auto, on or off")
        token = (env.get("X3D_MCP_BLOB_TOKEN") or "").strip()
        if mode == "off":
            enabled = False
        elif not http:
            enabled = True
        elif token:
            enabled = True
        elif mode == "on":
            raise ConfigError(
                "X3D_MCP_BLOB=on over HTTP needs X3D_MCP_BLOB_TOKEN (at least 32 characters); "
                "there is no way to switch on unauthenticated blob routes")
        else:
            enabled = False
        if http and token and len(token) < 32 and mode != "off":
            raise ConfigError("X3D_MCP_BLOB_TOKEN must be at least 32 characters")
        origins = _env_list(env, "X3D_MCP_ALLOWED_ORIGINS")
        if http and origins and not _env_list(env, "X3D_MCP_ALLOWED_HOSTS"):
            raise ConfigError("X3D_MCP_ALLOWED_ORIGINS needs X3D_MCP_ALLOWED_HOSTS as well: the "
                              "SDK's check refuses every request whose Host is not listed")
        spill = (env.get("X3D_MCP_BLOB_DIR") or "").strip()
        cfg = cls(
            enabled=enabled,
            http=http,
            token=token,
            tools_open=_env_bool(env, "X3D_MCP_BLOB_TOOLS_OPEN"),
            max_bytes=_env_size(env, "X3D_MCP_BLOB_MAX_BYTES", 256 << 20, 1),
            mem_bytes=_env_size(env, "X3D_MCP_BLOB_MEM_BYTES", 128 << 20, 1),
            quota_bytes=_env_size(env, "X3D_MCP_BLOB_QUOTA_BYTES", 0),
            spill_dir=spill,
            auto_spill=(not http) and not spill,
            hot_bytes=_env_size(env, "X3D_MCP_BLOB_HOT_BYTES", 64 << 20),
            ttl=_env_float(env, "X3D_MCP_BLOB_TTL", 3600.0, 1.0),
            max_age=_env_float(env, "X3D_MCP_BLOB_MAX_AGE", 86400.0, 1.0),
            max_ratio=_env_float(env, "X3D_MCP_BLOB_MAX_RATIO", 250.0, 0.0),
            max_uploads=int(_env_size(env, "X3D_MCP_BLOB_MAX_UPLOADS", 4, 1)),
            idle_timeout=_env_float(env, "X3D_MCP_BLOB_IDLE_TIMEOUT", 30.0, 0.1),
            total_timeout=_env_float(env, "X3D_MCP_BLOB_TOTAL_TIMEOUT", 600.0, 0.1),
            inline_max=_env_size(env, "X3D_MCP_BLOB_INLINE_MAX", 3_000_000, 1),
            page_default=_env_size(env, "X3D_MCP_BLOB_PAGE_DEFAULT", 16384, 1),
            page_max=_env_size(env, "X3D_MCP_BLOB_PAGE_MAX", 262144, 1),
            allowed_hosts=_env_list(env, "X3D_MCP_ALLOWED_HOSTS"),
            allowed_origins=origins,
            summary_items=int(_env_size(env, "X3D_MCP_SUMMARY_ITEMS", 200, 1)),
            offload_min=_env_size(env, "X3D_MCP_OFFLOAD_MIN", 1 << 20),
            max_blobs=int(_env_size(env, "X3D_MCP_BLOB_MAX_COUNT", 10000, 1)),
            tool_max_bytes=_env_size(env, "X3D_MCP_TOOL_MAX_BYTES", 96 << 20, 1),
            min_rate=_env_size(env, "X3D_MCP_BLOB_MIN_RATE", 4096),
            path_roots=tuple(r for r in (env.get("X3D_MCP_BLOB_PATH_ROOTS") or "").split(os.pathsep)
                             if r.strip()),
            tool_concurrency=int(_env_size(env, "X3D_MCP_TOOL_CONCURRENCY", 2, 1)),
        )
        if cfg.page_default > cfg.page_max:
            cfg = replace(cfg, page_default=cfg.page_max)
        return cfg


# --------------------------------------------------------------------- ids and metadata

def normalize_blob_id(value: str) -> str:
    """Return the 64-character lowercase hex id for `value` (hex, or `sha256:<hex>`).

    This is the only way a client-supplied id reaches the table or the filesystem, so a
    traversal such as `../x` or `a/b` can never name a file."""
    s = (value or "").strip()
    if s.lower().startswith("sha256:"):
        s = s[7:]
    s = s.lower() if s.isascii() else s
    if not _ID_RE.fullmatch(s):
        raise BlobIdError("blob id must be 64 hexadecimal characters (the sha256 of the content), "
                          "optionally prefixed with 'sha256:'")
    return s


def clean_media_type(value: str | None) -> str:
    s = (value or "").split(";")[0].strip().lower()
    return s if _MEDIA_RE.fullmatch(s) else DEFAULT_MEDIA_TYPE


def clean_name(value: str | None) -> str:
    """A display hint only: control characters dropped, 200 characters at most, never a path."""
    return "".join(ch for ch in (value or "") if ch.isprintable())[:200]


@dataclass
class Entry:
    id: str
    size: int
    encoding: str                         # stored codec: identity | zstd | br | gzip
    stored: int
    media_type: str
    name: str
    created: float
    last: float
    expires: float
    ttl: float
    mem: bytes | None = None
    path: str | None = None
    refs: int = 0
    dropped: bool = False


def _meta(e: Entry) -> dict:
    m = {
        "blob_id": e.id,
        "sha256": e.id,
        "size": e.size,
        "stored_size": e.stored,
        "encoding": e.encoding,
        "media_type": e.media_type,
        "expires": round(e.expires, 3),
        "created_at": round(e.created, 3),
    }
    if e.name:
        m["name"] = e.name
    return m


# --------------------------------------------------------------------- upload pipeline

def _zstd_level(level: int):
    return zstd_module().ZstdCompressor(level=level)


class Upload:
    """One streaming ingest: feed() wire chunks, then finish().

    Per chunk it decodes with the bounded decoder (size cap, ratio limit), hashes the
    decoded bytes, and stores: br and zstd bodies exactly as sent, identity and gzip
    bodies re-encoded to zstd level 3 (gzip level 6 when zstandard is absent) unless a
    256 KiB probe shows they do not compress. Always call abort() in a finally; it is a
    no-op after finish() and releases the reservation, the memory held and any temp file.
    """

    def __init__(self, store: "BlobStore", encoding: str, media_type: str, name: str,
                 expect_id: str | None, declared: int | None, ttl: float | None,
                 limit: int | None = None, slot: bool = True):
        cfg = store.cfg
        self.limit = min(limit, cfg.max_bytes) if limit else cfg.max_bytes
        self.store = store
        self.encoding = encoding
        self.media_type = media_type
        self.name = name
        self.expect_id = expect_id
        self.ttl = ttl
        self.wire = 0
        self.stored = 0
        self._dec = Capped(encoding, self.limit, cfg.max_ratio or None)
        self._hash = hashlib.sha256()
        self._keep_wire = encoding in ("br", "zstd") or (encoding == "gzip" and zstd_module() is None)
        self._codec = encoding if self._keep_wire else "identity"
        self._comp = None
        self._probe: bytearray | None = None if self._keep_wire else bytearray()
        self._buf = bytearray()
        self._mem_held = 0
        self._file = None
        self._tmp: str | None = None
        self._reserved = 0
        self._declared = declared
        self._active = False
        self._closed = False
        # cheapest checks first, nothing allocated until all pass
        if declared is not None and declared > self.limit:
            raise BlobTooLarge(f"body of {declared} bytes exceeds the limit of {self.limit} "
                               "(X3D_MCP_BLOB_MAX_BYTES, or X3D_MCP_BLOB_INLINE_MAX for blob_put)")
        if slot:                              # in-process puts hold no streaming slot
            store._begin_upload()
            self._active = True
        try:
            store._room_for_entry()
            # br/zstd bodies are stored as sent, so their size is known; identity and gzip
            # bodies shrink when stored, so reserve a first slice and grow as bytes land
            if declared is None:
                first = RESERVE_FIRST
            elif self._keep_wire:
                first = declared
            else:
                first = min(declared, RESERVE_FIRST)
            first = max(first, 1)
            store._reserve(first)
            self._reserved = first
        except BaseException:
            self.abort()
            raise

    # -- compression policy
    def _decide_codec(self) -> None:
        """First PROBE_BYTES decided: compress with zstd-3 (gzip-6 fallback), or store as is."""
        probe = bytes(self._probe)
        self._probe = None
        if probe:
            if zstd_module() is not None:
                z = _zstd_level(1).compress(probe)
            else:
                z = zlib.compress(probe, 1)
            if len(z) > (1.0 - PROBE_MIN_GAIN) * len(probe):
                self._codec = "identity"
            elif zstd_module() is not None:
                self._codec = "zstd"
                self._comp = _zstd_level(3).compressobj()
            else:
                self._codec = "gzip"
                self._comp = zlib.compressobj(6, zlib.DEFLATED, 31)
        self._emit(probe)

    def _emit(self, piece: bytes) -> None:
        if self._comp is None:
            if piece:
                self._write(piece)
        else:
            out = self._comp.compress(piece)
            if out:
                self._write(out)

    def _sink(self, piece: bytes) -> None:
        if self._probe is not None:
            self._probe += piece
            if len(self._probe) >= PROBE_BYTES:
                self._decide_codec()
        else:
            self._emit(piece)

    # -- storage
    def _write(self, b) -> None:
        n = len(b)
        self.stored += n
        if self._file is None:
            if self.store._mem_acquire(n):
                self._buf += b
                self._mem_held += n
                return
            self._spill()
        try:
            self._file.write(b)
        except OSError as e:
            log.warning("spill write failed: %s", e)
            raise StoreFull(f"could not write the spill file: {_oserr(e)}") from e

    def _spill(self) -> None:
        store = self.store
        if not store._can_spill():
            raise StoreFull("memory budget exhausted and no spill directory is configured "
                            "(X3D_MCP_BLOB_MEM_BYTES, X3D_MCP_BLOB_DIR)")
        try:
            self._file, self._tmp = store._new_temp()
            if self._buf:
                self._file.write(self._buf)
        except OSError as e:
            log.warning("spill file could not be created: %s", e)
            raise StoreFull(f"could not create a spill file: {_oserr(e)}") from e
        store._mem_release(self._mem_held)
        self._mem_held = 0
        self._buf = bytearray()

    def _grow(self) -> None:
        # the reservation counts stored bytes; keep it ahead of what has been stored
        if self.stored > self._reserved:
            step = self.stored - self._reserved + (GROW if self._declared is None else 64 << 10)
            self.store._reserve(step)
            self._reserved += step

    # -- public
    def feed(self, chunk: bytes) -> None:
        if self._closed:
            raise BlobError("upload already finished")
        if not chunk:
            return
        self.wire += len(chunk)
        if self.wire > self.limit:
            raise BlobTooLarge(f"upload exceeds the limit of {self.limit} bytes "
                               "(X3D_MCP_BLOB_MAX_BYTES, or X3D_MCP_BLOB_INLINE_MAX for blob_put)")
        if self._keep_wire:
            self._write(chunk)
        for piece in self._dec.feed(chunk):
            self._hash.update(piece)
            if not self._keep_wire:
                self._sink(piece)
        self._grow()

    def finish(self) -> tuple[dict, bool]:
        """Complete the upload. Returns (meta, created); created is False when the same
        content was already stored. Raises BlobCorrupt for a truncated or damaged stream,
        BlobMismatch when the digest differs from the expected id."""
        if self._closed:
            raise BlobError("upload already finished")
        for piece in self._dec.finish():
            self._hash.update(piece)
            if not self._keep_wire:
                self._sink(piece)
        if not self._keep_wire:
            if self._probe is not None:
                self._decide_codec()
            if self._comp is not None:
                tail = self._comp.flush()
                if tail:
                    self._write(tail)
        digest = self._hash.hexdigest()
        if self.expect_id and digest != self.expect_id:
            raise BlobMismatch(self.expect_id, digest)
        now = self.store._now()
        ttl = self.ttl or self.store.cfg.ttl
        e = Entry(id=digest, size=self._dec.out, encoding=self._codec, stored=self.stored,
                  media_type=self.media_type, name=self.name, created=now, last=now,
                  expires=min(now + ttl, now + self.store.cfg.max_age), ttl=ttl)
        if self._file is not None:
            self._file.close()
            self._file = None
        data = None if self._tmp else bytes(self._buf)
        meta, created = self.store._commit(e, data, self._tmp, self._mem_held)
        self._tmp = None
        self._mem_held = 0
        self._buf = bytearray()
        self._closed = True
        self.abort()
        return meta, created

    def abort(self) -> None:
        """Release everything this upload holds. Safe to call more than once."""
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
        if self._tmp:
            try:
                os.unlink(self._tmp)
            except OSError:
                pass
            self._tmp = None
        if self._mem_held:
            self.store._mem_release(self._mem_held)
            self._mem_held = 0
        self._buf = bytearray()
        if self._reserved:
            self.store._unreserve(self._reserved)
            self._reserved = 0
        if self._active:
            self.store._end_upload()
            self._active = False
        self._closed = True


# --------------------------------------------------------------------- readers

class Lease:
    """A pinned entry for streaming a download: it cannot be evicted until close()."""

    def __init__(self, store: "BlobStore", entry: Entry):
        self._store = store
        self._e = entry
        self._open = True
        self.blob_id = entry.id
        self.size = entry.size
        self.stored_size = entry.stored
        self.encoding = entry.encoding
        self.media_type = entry.media_type
        self.name = entry.name

    def iter_stored(self) -> Iterator[bytes]:
        yield from self._store._iter_stored(self._e)

    def iter_decoded(self) -> Iterator[bytes]:
        yield from self._store._iter_decoded(self._e)

    def close(self) -> None:
        if self._open:
            self._open = False
            self._store._release(self._e)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


# --------------------------------------------------------------------- the store

class BlobStore:
    def __init__(self, cfg: BlobConfig, clock=time.time):
        self.cfg = cfg
        self._now = clock
        self._lock = threading.RLock()
        self._d: "OrderedDict[str, Entry]" = OrderedDict()
        self.total = 0
        self.mem_used = 0
        self.inflight = 0
        self.inflight_mem = 0
        self.uploads = 0
        self._hot: tuple[str, bytes] | None = None
        self._spill: str | None = None
        self._spill_auto = False
        self._swept = float("-inf")
        self._unlock = None
        if cfg.spill_dir:
            self._spill = self._prepare_spill_dir(cfg.spill_dir)
            self._lock_spill()
            self._clean_spill()

    # -- spill directory
    @staticmethod
    def _prepare_spill_dir(path: str) -> str:
        p = os.path.abspath(os.path.expanduser(path))
        if os.path.lexists(p):
            st = os.lstat(p)
            if not _stat.S_ISDIR(st.st_mode):
                raise ConfigError(f"X3D_MCP_BLOB_DIR {p} is not a directory (symlinks are refused)")
            if hasattr(os, "getuid") and st.st_uid != os.getuid():
                raise ConfigError(f"X3D_MCP_BLOB_DIR {p} is not owned by the server user")
            if st.st_mode & 0o077:
                raise ConfigError(f"X3D_MCP_BLOB_DIR {p} must be private (mode 0700); "
                                  f"it is {_stat.S_IMODE(st.st_mode):04o}")
        else:
            os.makedirs(p, mode=0o700, exist_ok=True)
            os.chmod(p, 0o700)
        return p

    def _lock_spill(self) -> None:
        """Take an exclusive lock on the spill directory for the life of this store (released
        by close(), or when the process ends). A second process on the same directory would
        delete the first one's files at start-up, so it is refused instead."""
        if fcntl is None:
            return
        fd = os.open(self._spill, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise ConfigError(
                    f"X3D_MCP_BLOB_DIR {self._spill} is in use by another server process; "
                    "give each process its own directory") from None
            log.warning("X3D_MCP_BLOB_DIR cannot be locked (%s); make sure only one process uses it", e)
            return
        self._unlock = weakref.finalize(self, os.close, fd)

    def _clean_spill(self) -> None:
        """Empty our own files from the spill directory; anything else in it is left alone."""
        try:
            with os.scandir(self._spill) as it:
                for de in it:
                    if (_SPILL_FINAL.fullmatch(de.name) or _SPILL_TEMP.fullmatch(de.name)) \
                            and de.is_file(follow_symlinks=False):
                        try:
                            os.unlink(de.path)
                        except OSError:
                            pass
        except OSError:
            pass

    def _can_spill(self) -> bool:
        with self._lock:
            if self._spill:
                return True
            if self.cfg.auto_spill:
                d = tempfile.mkdtemp(prefix="x3d-blobs-")        # 0700
                self._spill = d
                self._spill_auto = True
                _AUTO_DIRS.append(d)
                atexit.register(shutil.rmtree, d, True)
                return True
            return False

    def _new_temp(self):
        name = ".up_" + secrets.token_hex(16)
        path = os.path.join(self._spill, name)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        return os.fdopen(fd, "wb"), path

    def _final_path(self, blob_id: str) -> str:
        """A fresh file name for one entry: the id plus a random suffix, hex only. Two entries
        with the same content (one deleted while a reader still holds it, one uploaded
        again) never share a file."""
        assert _ID_RE.fullmatch(blob_id), "spill names are hex ids only"
        return os.path.join(self._spill, f"{blob_id}.{secrets.token_hex(8)}")

    def close(self) -> None:
        """Drop everything and remove the spill files this store owns."""
        with self._lock:
            for e in list(self._d.values()):
                self._drop(e)
            self._hot = None
            if self._spill_auto and self._spill:
                shutil.rmtree(self._spill, ignore_errors=True)
                self._spill = None
            if self._unlock is not None:
                self._unlock()
                self._unlock = None

    # -- accounting
    def _begin_upload(self) -> None:
        with self._lock:
            if self.uploads >= self.cfg.max_uploads:
                raise TooManyUploads(f"{self.uploads} uploads are already running "
                                     "(X3D_MCP_BLOB_MAX_UPLOADS); retry shortly")
            self.uploads += 1

    def _end_upload(self) -> None:
        with self._lock:
            self.uploads -= 1

    def _reserve(self, n: int) -> None:
        with self._lock:
            if n > self.cfg.quota:
                raise StoreFull("blob larger than the store quota (X3D_MCP_BLOB_QUOTA_BYTES)")
            self._evict_for(n)
            if self.total + self.inflight + n > self.cfg.quota:
                raise StoreFull("store quota exhausted and every remaining blob is in use "
                                "(X3D_MCP_BLOB_QUOTA_BYTES)")
            self.inflight += n

    def _unreserve(self, n: int) -> None:
        with self._lock:
            self.inflight -= n

    def _lru_victim(self, only_mem: bool = False) -> Entry | None:
        """The least recently used entry nobody is reading (the table is in LRU order)."""
        return next((e for e in self._d.values()
                     if e.refs == 0 and (e.mem is not None or not only_mem)), None)

    def _evict_for(self, need: int) -> None:
        self._sweep()
        if self.total + self.inflight + need <= self.cfg.quota:
            return
        self._sweep(force=True)                   # expired blobs go before live ones
        while self.total + self.inflight + need > self.cfg.quota:
            victim = self._lru_victim()
            if victim is None:
                return
            self._drop(victim)

    def _room_for_entry(self) -> None:
        """Make room for one more table entry (X3D_MCP_BLOB_MAX_COUNT) or raise StoreFull.
        The quota counts payload bytes, so without this cap a stream of tiny blobs costs
        memory and scan time that the quota never sees."""
        with self._lock:
            while len(self._d) >= self.cfg.max_blobs:
                victim = self._lru_victim()
                if victim is None:
                    raise StoreFull(f"{len(self._d)} blobs are stored and all are in use "
                                    "(X3D_MCP_BLOB_MAX_COUNT)")
                self._drop(victim)

    def _mem_acquire(self, n: int) -> bool:
        """Room in the RAM budget for n more buffered bytes? Evicts unused in-memory blobs
        to make it; False means the caller should spill (or fail)."""
        with self._lock:
            if self.mem_used + self.inflight_mem + n > self.cfg.mem_bytes and not self._spill \
                    and not self.cfg.auto_spill:
                while self.mem_used + self.inflight_mem + n > self.cfg.mem_bytes:
                    victim = self._lru_victim(only_mem=True)
                    if victim is None:
                        break
                    self._drop(victim)
            if self.mem_used + self.inflight_mem + n > self.cfg.mem_bytes:
                return False
            self.inflight_mem += n
            return True

    def _mem_release(self, n: int) -> None:
        with self._lock:
            self.inflight_mem -= n

    # -- table
    def _sweep(self, force: bool = False) -> None:
        """Drop expired, unused entries. The scan is O(table), so it runs at most once a
        second unless forced; _lookup checks the one entry it is asked for on its own."""
        now = self._now()
        if not force and self._swept <= now < self._swept + SWEEP_EVERY:
            return
        self._swept = now
        for e in list(self._d.values()):
            if e.expires <= now and e.refs == 0:
                self._drop(e)

    def _drop(self, e: Entry) -> None:
        """Take an entry out of the table. Its bytes and its share of the quota and the RAM
        budget are released now when nobody is reading it, else when the last reader lets go
        (a blob that is deleted but still downloading is still on disk or in memory)."""
        with self._lock:
            if self._d.get(e.id) is not e:
                return
            del self._d[e.id]
            if self._hot and self._hot[0] == e.id:
                self._hot = None
            if e.refs == 0:
                self._free(e)
            else:
                e.dropped = True

    def _free(self, e: Entry) -> None:
        self.total -= e.stored
        if e.mem is not None:
            self.mem_used -= e.stored
            e.mem = None
        if e.path:
            self._unlink(e.path)
            e.path = None

    @staticmethod
    def _unlink(path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    def _touch(self, e: Entry) -> None:
        now = self._now()
        e.last = now
        e.expires = min(now + e.ttl, e.created + self.cfg.max_age)
        self._d.move_to_end(e.id)

    def _lookup(self, blob_id: str) -> Entry:
        """Entry for an id (or raise BlobNotFound); refreshes its idle TTL and LRU rank."""
        bid = normalize_blob_id(blob_id)
        with self._lock:
            self._sweep()
            e = self._d.get(bid)
            if e is not None and e.refs == 0 and e.expires <= self._now():
                self._drop(e)
                e = None
            if e is not None and e.path and not os.path.exists(e.path):
                log.warning("blob %s: its spill file is gone", bid[:12])
                self._drop(e)                     # the file was removed behind our back
                e = None
            if e is None:
                raise BlobNotFound(f"no blob {bid[:12]} (never stored, expired, evicted or deleted); "
                                   "upload it again")
            self._touch(e)
            return e

    @contextmanager
    def _use(self, blob_id: str):
        with self._lock:
            e = self._lookup(blob_id)
            e.refs += 1
        try:
            yield e
        finally:
            self._release(e)

    def _release(self, e: Entry) -> None:
        with self._lock:
            e.refs -= 1
            if e.refs == 0 and e.dropped:
                self._free(e)

    def _commit(self, e: Entry, data: bytes | None, tmp: str | None, mem_held: int) -> tuple[dict, bool]:
        with self._lock:
            self._sweep()
            old = self._d.get(e.id)
            if old is not None and old.path and not os.path.exists(old.path):
                self._drop(old)                       # a ghost whose file is gone: store the new copy
                old = None
            if old is not None:                       # same content already stored
                # renew the idle TTL, but not the hard lifetime, and never shorten what
                # another holder of the same bytes asked for
                old.ttl = max(old.ttl, e.ttl)
                if old.media_type == DEFAULT_MEDIA_TYPE and e.media_type != DEFAULT_MEDIA_TYPE:
                    old.media_type = e.media_type
                self._touch(old)
                self.inflight_mem -= mem_held         # the buffer is discarded
                if tmp:
                    self._unlink(tmp)
                return _meta(old), False
            self._room_for_entry()                    # may raise; nothing has changed yet
            if data is None:
                final = self._final_path(e.id)
                try:
                    os.replace(tmp, final)
                except OSError as ex:
                    log.warning("could not move the spill file into place: %s", ex)
                    raise StoreFull(f"could not store the spill file: {_oserr(ex)}") from ex
                e.path = final
            else:
                self.mem_used += e.stored
                e.mem = data
            self.inflight_mem -= mem_held             # the buffer is now the entry
            self._d[e.id] = e
            self.total += e.stored
            return _meta(e), True

    # -- reading stored bytes
    def _iter_stored(self, e: Entry) -> Iterator[bytes]:
        if e.mem is not None:
            mv = memoryview(e.mem)
            for i in range(0, len(mv), PIECE):
                yield bytes(mv[i:i + PIECE])
            return
        try:
            with open(e.path, "rb") as f:
                while True:
                    b = f.read(PIECE)
                    if not b:
                        return
                    yield b
        except OSError as ex:
            log.warning("blob %s: spill file unreadable: %s", e.id[:12], ex)
            with self._lock:
                self._drop(e)
            raise BlobNotFound(f"blob {e.id[:12]} data is no longer available: {_oserr(ex)}") from ex

    def _iter_decoded(self, e: Entry) -> Iterator[bytes]:
        if e.encoding == "identity":
            yield from self._iter_stored(e)
            return
        d = Capped(e.encoding, e.size)
        buf, n = [], 0
        for piece in self._iter_stored(e):
            for out in d.feed(piece):
                buf.append(out)
                n += len(out)
                if n >= PIECE:
                    yield b"".join(buf)
                    buf, n = [], 0
        for out in d.finish():
            buf.append(out)
        if buf:
            yield b"".join(buf)
        if d.out != e.size:
            raise BlobCorrupt(f"stored blob {e.id[:12]} decoded to {d.out} bytes, expected {e.size}")

    def _hot_get(self, blob_id: str) -> bytes | None:
        with self._lock:
            h = self._hot
            return h[1] if h and h[0] == blob_id else None

    def _hot_set(self, blob_id: str, data: bytes) -> None:
        if self.cfg.hot_bytes and len(data) <= self.cfg.hot_bytes:
            with self._lock:
                if blob_id in self._d:
                    self._hot = (blob_id, data)

    # -- public API
    def upload(self, content_encoding: str | None = None, media_type: str | None = None, *,
               name: str = "", expect_id: str | None = None, declared: int | None = None,
               ttl: float | None = None, max_bytes: int | None = None, slot: bool = True) -> Upload:
        """Start a streaming ingest (see Upload). Raises UnsupportedEncoding (415),
        BlobTooLarge (413), TooManyUploads (429) or StoreFull (507) before any byte is read.
        slot=False skips the X3D_MCP_BLOB_MAX_UPLOADS count (put() uses it: the bytes are
        already in memory, so the limit on streaming uploads has nothing to protect)."""
        enc = normalize_encoding(content_encoding)
        if ttl is not None and ttl <= 0:
            ttl = None
        if ttl is not None:
            ttl = min(float(ttl), self.cfg.max_age)
        return Upload(self, enc, clean_media_type(media_type) if media_type else DEFAULT_MEDIA_TYPE,
                      clean_name(name), normalize_blob_id(expect_id) if expect_id else None, declared, ttl,
                      max_bytes, slot)

    def put(self, data: bytes, content_encoding: str | None = None, media_type: str | None = None,
            ttl: float | None = None, *, name: str = "", expect_id: str | None = None,
            max_bytes: int | None = None) -> dict:
        """Store `data` (raw bytes, or a compressed stream when content_encoding is gzip, br
        or zstd) and return its metadata: blob_id (= sha256 of the decoded bytes), size,
        stored_size, encoding (how it is kept), media_type, sha256, expires, created."""
        up = self.upload(content_encoding, media_type, name=name, expect_id=expect_id,
                         declared=len(data), ttl=ttl, max_bytes=max_bytes, slot=False)
        try:
            for i in range(0, max(len(data), 1), PIECE):
                up.feed(data[i:i + PIECE])
            meta, created = up.finish()
        finally:
            up.abort()
        return dict(meta, created=created)

    def peek(self, blob_id: str) -> dict | None:
        """Metadata when the id is stored (refreshing its TTL), else None. For dedupe checks."""
        try:
            return self.stat(blob_id)
        except BlobNotFound:
            return None

    def stat(self, blob_id: str) -> dict:
        with self._lock:
            return _meta(self._lookup(blob_id))

    def acquire(self, blob_id: str) -> Lease:
        """Pin an entry for streaming; the caller must close() the lease."""
        with self._lock:
            e = self._lookup(blob_id)
            e.refs += 1
            return Lease(self, e)

    def get_bytes(self, blob_id: str, max_bytes: int | None = None) -> bytes:
        """The decoded bytes. Raises BlobNotFound for an unknown, expired or evicted id, and
        BlobTooLarge (before decoding anything) when max_bytes is given and the blob is bigger."""
        with self._use(blob_id) as e:
            if max_bytes is not None and e.size > max_bytes:
                raise BlobTooLarge(f"blob {e.id[:12]} is {e.size} bytes decoded; the limit here is "
                                   f"{max_bytes} (X3D_MCP_TOOL_MAX_BYTES)")
            hot = self._hot_get(e.id)
            if hot is not None:
                return hot
            if e.encoding == "identity" and e.mem is not None:
                data = e.mem
            else:
                data = b"".join(self._iter_decoded(e))
            self._hot_set(e.id, data)
            return data

    def get_text(self, blob_id: str, max_bytes: int | None = None) -> str:
        """The decoded bytes as strict UTF-8 text (see get_bytes for max_bytes)."""
        data = self.get_bytes(blob_id, max_bytes)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as ex:
            raise BlobError(f"blob {blob_id.strip()[:19]} is not valid UTF-8 text ({ex.reason} at byte "
                            f"{ex.start}); it is probably binary data") from ex

    def read(self, blob_id: str, offset: int = 0, length: int | None = None) -> bytes:
        """decoded[offset:offset+length] (to the end when length is None).

        Small blobs are decoded once and kept in a one-entry cache for paging; a blob over
        the cache size is stream-decoded per call, keeping only the window in memory."""
        if offset < 0:
            raise BlobError("offset must be zero or positive")
        with self._use(blob_id) as e:
            if offset > e.size:
                raise BlobError(f"offset {offset} is beyond the end of the blob ({e.size} bytes)")
            end = e.size if length is None else min(e.size, offset + max(length, 0))
            if offset >= end:
                return b""
            hot = self._hot_get(e.id)
            if hot is not None:
                return hot[offset:end]
            if e.encoding == "identity":
                if e.mem is not None:
                    return e.mem[offset:end]
                try:
                    with open(e.path, "rb") as f:
                        f.seek(offset)
                        return f.read(end - offset)
                except OSError as ex:
                    log.warning("blob %s: spill file unreadable: %s", e.id[:12], ex)
                    self._drop(e)
                    raise BlobNotFound(f"blob {e.id[:12]} data is no longer available: {_oserr(ex)}") from ex
            n = end - offset
            cacheable = 0 < e.size <= self.cfg.hot_bytes
            if cacheable and not (offset == 0 and n <= 65536):
                data = b"".join(self._iter_decoded(e))
                self._hot_set(e.id, data)
                return data[offset:end]
            out, pos = bytearray(), 0
            for piece in self._iter_decoded(e):
                lo, hi = max(offset - pos, 0), min(end - pos, len(piece))
                if lo < hi:
                    out += piece[lo:hi]
                pos += len(piece)
                if pos >= end:
                    break
            return bytes(out)

    def delete(self, blob_id: str) -> bool:
        """Remove a blob now (a download in progress finishes). True if it was stored."""
        bid = normalize_blob_id(blob_id)
        with self._lock:
            e = self._d.get(bid)
            if e is None:
                return False
            self._drop(e)
            return True

    def stats(self) -> dict:
        with self._lock:
            self._sweep(force=True)
            files = 0
            if self._spill:
                try:
                    files = sum(1 for de in os.scandir(self._spill)
                                if _SPILL_FINAL.fullmatch(de.name) or _SPILL_TEMP.fullmatch(de.name))
                except OSError:
                    pass
            return {
                "blobs": len(self._d), "total": self.total, "mem_used": self.mem_used,
                "inflight": self.inflight, "inflight_mem": self.inflight_mem,
                "uploads": self.uploads, "quota": self.cfg.quota, "spill_files": files,
                "spill_dir": self._spill,
            }


# --------------------------------------------------------------------- process-wide state

_state_lock = threading.Lock()
_cfg: BlobConfig | None = None
_store: BlobStore | None = None


def configure(cfg: BlobConfig | None = None) -> BlobConfig:
    """Install the process-wide configuration (default: from the environment) and drop any
    existing store. The server calls this once at start-up; tests call it with their own."""
    global _cfg, _store
    with _state_lock:
        if _store is not None:
            _store.close()
        _store = None
        _cfg = cfg if cfg is not None else BlobConfig.from_env()
        return _cfg


def get_config() -> BlobConfig:
    global _cfg
    with _state_lock:
        if _cfg is None:
            _cfg = BlobConfig.from_env()
        return _cfg


def reset() -> None:
    """Forget the configuration and the store (tests)."""
    global _cfg, _store
    with _state_lock:
        if _store is not None:
            _store.close()
        _cfg = None
        _store = None


def blob_enabled() -> bool:
    return get_config().enabled


def get_store() -> BlobStore:
    """The process-wide store. Raises BlobUnavailable when the feature is switched off
    (X3D_MCP_BLOB=off, or HTTP transport without X3D_MCP_BLOB_TOKEN), and StoreUnavailable
    when it cannot start (the reason is logged; it names server paths, so clients do not get it)."""
    global _store
    cfg = get_config()
    if not cfg.enabled:
        raise BlobUnavailable(
            "blob store is not enabled on this server (over HTTP it needs X3D_MCP_BLOB_TOKEN; "
            "X3D_MCP_BLOB=off switches it off)")
    with _state_lock:
        if _store is None:
            try:
                _store = BlobStore(cfg)
            except ConfigError as e:
                log.error("blob store cannot start: %s", e)
                raise StoreUnavailable("the blob store cannot start on this server; "
                                       "see the server log") from e
        return _store


def start() -> None:
    """Build the store now. The server calls this at start-up so that an unusable
    X3D_MCP_BLOB_DIR stops it there (ConfigError) instead of failing every later request."""
    cfg = get_config()
    if not cfg.enabled:
        return
    global _store
    with _state_lock:
        if _store is None:
            _store = BlobStore(cfg)


def exit_on_sigterm() -> None:
    """Remove the temporary spill directories when SIGTERM arrives. Stdio hosts stop a server
    with SIGTERM, which skips atexit; and a normal exit would wait for the thread that is
    blocked reading stdin. So the handler removes the directories itself and exits at once
    (status 128 + signal). Does nothing off the main thread or when something else already
    handles SIGTERM."""
    import signal
    import sys

    def handler(signum, frame):
        for d in _AUTO_DIRS:
            shutil.rmtree(d, ignore_errors=True)
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        os._exit(128 + signum)

    try:
        if signal.getsignal(signal.SIGTERM) is signal.SIG_DFL:
            signal.signal(signal.SIGTERM, handler)
    except (ValueError, OSError):
        pass


# --------------------------------------------------------------------- the input resolver

def resolve_text(content: str = "", path: str = "", blob_id: str = "",
                 max_bytes: int | None = None) -> tuple[str, str]:
    """Return (text, label) for exactly one of inline `content`, a file `path`, or a stored `blob_id`.

    The one place that decides which input is used: giving none, or more than one, is an
    error (nothing "wins" silently). `label` names the source for messages and summaries
    ("inline content", "file <path>", "blob <id prefix>"). Errors are ValueError with a
    message a tool can return as is. Without a blob_id the wording is exactly that of
    x3d_utils.source.load_x3d_source before blobs existed.
    """
    given = [name for name, v in (("content", content), ("path", path), ("blob_id", blob_id))
             if v is not None and v.strip() != ""]
    if not blob_id or not blob_id.strip():
        if len(given) > 1:
            raise ValueError("Provide either `content` (inline X3D) or `path` (a file), not both.")
        if not given:
            raise ValueError("Provide either `content` (inline X3D) or `path` (a file).")
    elif len(given) > 1:
        raise ValueError("Provide exactly one of `content`, `path` or `blob_id`, not several "
                         f"(got: {', '.join(given)}). With blob_id, pass content as an empty string.")
    if given == ["blob_id"]:
        bid = normalize_blob_id(blob_id)
        return get_store().get_text(bid, max_bytes), f"blob {bid[:12]}"
    if given == ["path"]:
        from x3d_utils.source import read_path_text
        return read_path_text(path), f"file {path.strip()}"
    return content, "inline content"
