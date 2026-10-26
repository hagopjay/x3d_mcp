"""Tests for the blob store (src/transfer): round trip, codecs, bomb guard, quota, TTL,
eviction, spill, id validation, configuration, the input resolver, and the in-process
signatures the playground depends on."""

import gzip
import hashlib
import inspect
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import brotli
import zstandard

from transfer import blobs
from transfer.blobs import BlobConfig, BlobStore, normalize_blob_id, resolve_text
from transfer.capped import Capped, available_codecs, decode_all
from transfer.errors import (
    BlobCorrupt, BlobError, BlobIdError, BlobMismatch, BlobNotFound, BlobTooLarge, BlobUnavailable,
    ConfigError, StoreFull, StoreUnavailable, TooManyUploads, UnsupportedEncoding,
)

SCENE = ('<?xml version="1.0" encoding="UTF-8"?>\n<X3D profile="Interchange" version="4.0"><Scene>'
         + "".join(f'<Transform DEF="T{i}" translation="{i} 0 0"><Shape><Appearance><Material '
                   f'diffuseColor="{i % 10 / 10:.1f} 0.2 0.3"/></Appearance><Box size="1 1 1"/></Shape>'
                   f'</Transform>\n' for i in range(3000))
         + "</Scene></X3D>\n")
RAW = SCENE.encode()


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def cfg(**kw) -> BlobConfig:
    return BlobConfig(**kw)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def spilled(d) -> list[str]:
    """The blob ids that have a spill file in `d` (file names are <id>.<random suffix>)."""
    return sorted(n.split(".")[0] for n in os.listdir(d) if not n.startswith(".up_"))


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    for name in [n for n in os.environ if n.startswith(("X3D_MCP_BLOB", "X3D_MCP_ALLOWED", "X3D_MCP_SUMMARY",
                                                        "X3D_MCP_OFFLOAD"))]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    blobs.reset()
    yield
    blobs.reset()


# ---------------------------------------------------------------- round trip

def test_round_trip_and_metadata():
    s = BlobStore(cfg())
    m = s.put(RAW, media_type="model/x3d+xml", name="scene.x3d")
    assert m["blob_id"] == m["sha256"] == sha(RAW)
    assert m["size"] == len(RAW) and m["created"] is True
    assert m["encoding"] == "zstd" and m["stored_size"] < len(RAW) / 10
    assert m["media_type"] == "model/x3d+xml" and m["name"] == "scene.x3d"
    assert m["expires"] > time.time()
    assert s.get_bytes(m["blob_id"]) == RAW
    assert s.get_text(m["blob_id"]) == SCENE
    assert s.stat(m["blob_id"])["size"] == len(RAW)
    assert s.read(m["blob_id"], 5, 20) == RAW[5:25]
    assert s.read(m["blob_id"], len(RAW) - 3) == RAW[-3:]
    assert s.read(m["blob_id"], len(RAW), 10) == b""
    assert s.delete(m["blob_id"]) is True
    assert s.delete(m["blob_id"]) is False
    with pytest.raises(BlobNotFound):
        s.get_bytes(m["blob_id"])


def test_same_content_same_id_and_dedupe():
    s = BlobStore(cfg())
    a = s.put(RAW)
    b = s.put(RAW)
    assert a["blob_id"] == b["blob_id"] and a["created"] and not b["created"]
    assert s.stats()["blobs"] == 1
    assert s.stats()["total"] == a["stored_size"]


def test_text_helpers_and_unicode_and_bad_utf8():
    s = BlobStore(cfg())
    text = "é€𝄞 <X3D/>\n"
    m = s.put(text.encode())
    assert s.get_text(m["blob_id"]) == text
    bad = s.put(b"\xff\xfe\x00binary")
    with pytest.raises(BlobError, match="not valid UTF-8"):
        s.get_text(bad["blob_id"])


def test_empty_blob():
    s = BlobStore(cfg())
    m = s.put(b"")
    assert m["blob_id"] == sha(b"") and m["size"] == 0
    assert s.get_bytes(m["blob_id"]) == b""
    assert s.read(m["blob_id"], 0, 10) == b""


def test_media_type_and_name_are_sanitised():
    s = BlobStore(cfg())
    m = s.put(b"x", media_type="TEXT/Plain; charset=utf-8", name="a\x00b\nc" + "z" * 300)
    assert m["media_type"] == "text/plain"
    assert "\x00" not in m["name"] and "\n" not in m["name"] and len(m["name"]) <= 200
    assert s.put(b"y", media_type="not a type!")["media_type"] == "application/octet-stream"


# ---------------------------------------------------------------- codecs

ENCODERS = {
    "identity": lambda b: b,
    "gzip": lambda b: gzip.compress(b, 6),
    "br": lambda b: brotli.compress(b, quality=5),
    "zstd": lambda b: zstandard.ZstdCompressor(level=3).compress(b),
}


@pytest.mark.parametrize("codec", ["identity", "gzip", "br", "zstd"])
def test_each_codec_round_trips_to_the_same_id(codec):
    s = BlobStore(cfg())
    m = s.put(ENCODERS[codec](RAW), content_encoding=codec)
    assert m["blob_id"] == sha(RAW) and m["size"] == len(RAW)
    assert s.get_bytes(m["blob_id"]) == RAW
    # br and zstd bodies are kept as sent; identity and gzip are re-encoded to zstd
    assert m["encoding"] == {"identity": "zstd", "gzip": "zstd", "br": "br", "zstd": "zstd"}[codec]
    if codec in ("br", "zstd"):
        assert m["stored_size"] == len(ENCODERS[codec](RAW))


@pytest.mark.parametrize("codec", ["gzip", "br", "zstd"])
@pytest.mark.parametrize("step", [1, 7, 4096, 1 << 20])
def test_streaming_upload_is_independent_of_chunking(codec, step):
    data = ENCODERS[codec](RAW[:40_000])
    if step == 1 and len(data) > 6000:
        data = ENCODERS[codec](RAW[:6000])
        want = RAW[:6000]
    else:
        want = RAW[:40_000]
    s = BlobStore(cfg())
    up = s.upload(codec, "text/plain")
    try:
        for i in range(0, len(data), step):
            up.feed(data[i:i + step])
        meta, created = up.finish()
    finally:
        up.abort()
    assert meta["blob_id"] == sha(want) and created
    assert s.get_bytes(meta["blob_id"]) == want
    assert s.stats()["inflight"] == 0 and s.stats()["uploads"] == 0


def test_incompressible_data_is_stored_as_is():
    s = BlobStore(cfg())
    noise = os.urandom(400_000)
    m = s.put(noise)
    assert m["encoding"] == "identity" and m["stored_size"] == len(noise)
    assert s.get_bytes(m["blob_id"]) == noise
    # an already-gzipped file (an .spz) is also kept as is, whatever codec it arrived in
    m2 = s.put(gzip.compress(noise), content_encoding="identity")
    assert m2["encoding"] == "identity"


def test_without_zstandard_falls_back_to_gzip_and_refuses_zstd(monkeypatch):
    from transfer import capped
    monkeypatch.setattr(capped, "_zstd_mod", None)
    monkeypatch.setattr(capped, "_zstd_checked", True)
    assert "zstd" not in available_codecs()
    s = BlobStore(cfg())
    m = s.put(RAW)
    assert m["encoding"] == "gzip" and m["stored_size"] < len(RAW) / 5
    assert s.get_bytes(m["blob_id"]) == RAW
    m2 = s.put(gzip.compress(RAW), content_encoding="gzip")      # kept as sent
    assert m2["blob_id"] == m["blob_id"] and not m2["created"]
    with pytest.raises(UnsupportedEncoding, match="zstandard"):
        s.put(b"x", content_encoding="zstd")


def test_unsupported_encodings():
    s = BlobStore(cfg())
    for bad in ("deflate", "compress", "gzip, br", "lz4"):
        with pytest.raises(UnsupportedEncoding):
            s.upload(bad)
    assert s.stats()["uploads"] == 0                  # refused before anything was reserved
    for given, want in (("X-GZIP", "gzip"), (None, "identity"), ("", "identity"), ("BR", "br")):
        up = s.upload(given)
        assert up.encoding == want
        up.abort()


# ---------------------------------------------------------------- bomb guard

ZEROS = b"\0" * (64 << 20)


def _zero_bomb(codec: str) -> bytes:
    if codec == "gzip":
        c = zlib.compressobj(9, zlib.DEFLATED, 31)
        return b"".join([c.compress(ZEROS), c.flush()])
    if codec == "br":
        c = brotli.Compressor(quality=5)
        return c.process(ZEROS) + c.finish()
    c = zstandard.ZstdCompressor(level=3).compressobj()
    return c.compress(ZEROS) + c.flush()


@pytest.mark.parametrize("codec", ["gzip", "br", "zstd"])
def test_ratio_limit_stops_a_bomb_early(codec):
    bomb = _zero_bomb(codec)
    s = BlobStore(cfg())
    t0 = time.perf_counter()
    with pytest.raises(BlobTooLarge, match="ratio"):
        s.put(bomb, content_encoding=codec)
    assert time.perf_counter() - t0 < 3
    st = s.stats()
    assert st["blobs"] == 0 and st["inflight"] == 0 and st["inflight_mem"] == 0 and st["uploads"] == 0


@pytest.mark.parametrize("codec", ["gzip", "br", "zstd"])
def test_decoded_size_cap_stops_a_bomb_even_with_the_ratio_limit_off(codec):
    bomb = _zero_bomb(codec)
    s = BlobStore(cfg(max_ratio=0, max_bytes=8 << 20))
    t0 = time.perf_counter()
    with pytest.raises(BlobTooLarge, match="decoded size"):
        s.put(bomb, content_encoding=codec)
    assert time.perf_counter() - t0 < 3
    assert s.stats()["blobs"] == 0


def test_decoder_pieces_are_bounded():
    bomb = _zero_bomb("br")
    d = Capped("br", 1 << 30)
    biggest = 0
    with pytest.raises(BlobTooLarge):
        d.max_ratio = 250
        for piece in d.feed(bomb):
            biggest = max(biggest, len(piece))
    assert biggest <= 1 << 20 and d.out < 8 << 20            # stopped after a few MiB, not 64


def test_legit_high_ratio_small_file_passes():
    tiny = b"a" * 500_000                       # 500000:1 but under the 1 MiB the ratio check waits for
    s = BlobStore(cfg())
    for codec in ("gzip", "br", "zstd"):
        assert s.get_bytes(s.put(ENCODERS[codec](tiny), content_encoding=codec)["blob_id"]) == tiny


@pytest.mark.parametrize("codec", ["gzip", "br", "zstd"])
def test_truncated_trailing_and_garbage_streams_are_rejected(codec):
    s = BlobStore(cfg())
    good = ENCODERS[codec](RAW)
    with pytest.raises(BlobCorrupt, match="truncated|corrupt"):
        s.put(good[:-5], content_encoding=codec)
    with pytest.raises(BlobCorrupt):
        s.put(good + b"junk", content_encoding=codec)
    with pytest.raises(BlobCorrupt):
        s.put(os.urandom(2000), content_encoding=codec)
    with pytest.raises(BlobCorrupt):
        s.put(b"", content_encoding=codec)
    assert s.stats()["blobs"] == 0 and s.stats()["inflight"] == 0


def test_expected_id_mismatch_is_rejected_and_nothing_is_stored():
    s = BlobStore(cfg())
    with pytest.raises(BlobMismatch) as ei:
        s.put(b"abc", expect_id="0" * 64)
    assert ei.value.actual == sha(b"abc") and ei.value.expected == "0" * 64 and ei.value.status == 422
    assert s.stats()["blobs"] == 0
    assert s.put(b"abc", expect_id="sha256:" + sha(b"abc").upper())["blob_id"] == sha(b"abc")


def test_size_limits():
    s = BlobStore(cfg(max_bytes=1000))
    with pytest.raises(BlobTooLarge, match="limit"):
        s.upload(declared=5000)                                   # refused before any byte is read
    with pytest.raises(BlobTooLarge):
        s.put(os.urandom(2000))
    s2 = BlobStore(cfg())
    with pytest.raises(BlobTooLarge):
        s2.put(os.urandom(5000), max_bytes=4000)                  # blob_put style per-call cap
    assert s2.stats()["inflight"] == 0


def test_too_many_concurrent_uploads():
    s = BlobStore(cfg(max_uploads=2))
    a, b = s.upload(), s.upload()
    with pytest.raises(TooManyUploads):
        s.upload()
    a.abort()
    c = s.upload()
    for u in (b, c):
        u.abort()
    assert s.stats()["uploads"] == 0


def test_abort_releases_reservation_memory_and_temp_files(tmp_path):
    d = tmp_path / "spill"
    s = BlobStore(cfg(mem_bytes=100_000, spill_dir=str(d)))
    up = s.upload(declared=500_000)
    up.feed(os.urandom(300_000))                     # past the RAM budget: now in a temp file
    assert [p for p in os.listdir(d) if p.startswith(".up_")]
    up.abort()
    up.abort()                                       # idempotent
    assert os.listdir(d) == []
    st = s.stats()
    assert st["inflight"] == 0 and st["inflight_mem"] == 0 and st["uploads"] == 0


# ---------------------------------------------------------------- quota, eviction, TTL

def test_quota_evicts_least_recently_used_first():
    s = BlobStore(cfg(mem_bytes=1_000_000, quota_bytes=1_000_000))
    ids = [s.put(os.urandom(300_000))["blob_id"] for _ in range(3)]
    s.stat(ids[0])                                   # touch: ids[1] is now the oldest
    ids.append(s.put(os.urandom(300_000))["blob_id"])
    alive = [s.peek(i) is not None for i in ids]
    assert alive == [True, False, True, True]
    assert s.stats()["total"] <= 1_000_000


def test_blob_in_use_is_never_evicted_and_a_full_store_says_so():
    s = BlobStore(cfg(mem_bytes=700_000, quota_bytes=700_000))
    a = s.put(os.urandom(300_000))["blob_id"]
    b = s.put(os.urandom(300_000))["blob_id"]
    la, lb = s.acquire(a), s.acquire(b)
    with pytest.raises(StoreFull, match="in use"):
        s.put(os.urandom(300_000))                   # nothing may be evicted: both are being read
    la.close()
    s.put(os.urandom(300_000))                       # now the idle one makes room
    assert s.peek(a) is None and s.peek(b) is not None
    lb.close()
    with pytest.raises(StoreFull, match="quota"):
        s.put(os.urandom(800_000))                   # larger than the whole quota
    assert s.stats()["inflight"] == 0 and s.stats()["inflight_mem"] == 0


def test_ttl_idle_refresh_and_hard_max_age():
    clock = Clock()
    s = BlobStore(cfg(ttl=100, max_age=250), clock=clock)
    m = s.put(b"hello")
    assert m["expires"] == clock.t + 100
    clock.t += 90
    assert s.get_bytes(m["blob_id"]) == b"hello"             # use renews the idle TTL
    clock.t += 90
    assert s.peek(m["blob_id"]) is not None
    clock.t += 90                                             # 270 s after creation: past max_age
    assert s.peek(m["blob_id"]) is None
    with pytest.raises(BlobNotFound, match="expired"):
        s.get_bytes(m["blob_id"])
    m2 = s.put(b"again")
    clock.t += 99
    assert s.peek(m2["blob_id"]) is not None                  # still alive, and renewed
    clock.t += 101
    assert s.peek(m2["blob_id"]) is None                      # idle for 101 s


def test_per_blob_ttl_is_clamped_to_max_age():
    clock = Clock()
    s = BlobStore(cfg(ttl=100, max_age=500), clock=clock)
    m = s.put(b"a", ttl=10_000)
    assert m["expires"] == clock.t + 500
    short = s.put(b"b", ttl=5)
    assert short["expires"] == clock.t + 5


def test_reupload_renews_the_idle_ttl_but_not_the_hard_lifetime():
    clock = Clock()
    s = BlobStore(cfg(ttl=100, max_age=150), clock=clock)
    m = s.put(b"x")
    clock.t += 90
    again = s.put(b"x")                                       # same bytes: idle TTL renewed
    assert again["created"] is False and again["created_at"] == m["created_at"]
    assert again["expires"] == m["created_at"] + 150          # ... up to the hard maximum
    clock.t += 90
    assert s.peek(m["blob_id"]) is None                       # 180 s after the first store


def test_frequent_reuploads_do_not_keep_a_blob_past_max_age():
    clock = Clock()
    s = BlobStore(cfg(ttl=3600, max_age=86400), clock=clock)
    first = s.put(b"scene")
    for _ in range(24):                                                  # 23 h 20 min of re-uploads
        clock.t += 3500
        assert s.put(b"scene")["created_at"] == first["created_at"]      # kept alive, same life
    clock.t += 3500                                                      # 24 h 18 min after the first store
    assert s.peek(first["blob_id"]) is None


def test_reupload_cannot_shorten_the_ttl_somebody_else_asked_for():
    clock = Clock()
    s = BlobStore(cfg(ttl=3600, max_age=86400), clock=clock)
    a = s.put(b"shared", ttl=80000)
    s.put(b"shared", ttl=1)
    clock.t += 5
    assert s.peek(a["blob_id"]) is not None
    longer = s.put(b"shared", ttl=85000)                      # a longer ask is honoured
    assert longer["expires"] == clock.t + 85000


def test_expired_blobs_free_their_quota_and_spill_files(tmp_path):
    clock = Clock()
    d = tmp_path / "spill"
    s = BlobStore(cfg(mem_bytes=1000, quota_bytes=2_000_000, spill_dir=str(d), ttl=10), clock=clock)
    s.put(os.urandom(100_000))
    assert len(os.listdir(d)) == 1
    clock.t += 11
    s.stats()
    assert os.listdir(d) == [] and s.stats()["total"] == 0


def test_delete_while_a_reader_holds_a_lease_keeps_the_file_until_released(tmp_path):
    d = tmp_path / "spill"
    s = BlobStore(cfg(mem_bytes=1000, quota_bytes=2_000_000, spill_dir=str(d)))
    data = os.urandom(50_000)
    m = s.put(data)
    lease = s.acquire(m["blob_id"])
    assert s.delete(m["blob_id"]) is True
    with pytest.raises(BlobNotFound):
        s.stat(m["blob_id"])
    assert b"".join(lease.iter_stored()) == data              # the reader still gets its bytes
    lease.close()
    assert os.listdir(d) == []


# ---------------------------------------------------------------- spill directory

def test_spill_to_disk_past_the_memory_budget(tmp_path):
    d = tmp_path / "spill"
    s = BlobStore(cfg(mem_bytes=100_000, quota_bytes=5_000_000, spill_dir=str(d)))
    small = s.put(os.urandom(50_000))
    big = os.urandom(400_000)
    m = s.put(big)
    st = s.stats()
    assert st["mem_used"] == small["stored_size"]             # the big one is on disk
    assert spilled(d) == [m["blob_id"]]
    assert stat.S_IMODE(os.stat(d / os.listdir(d)[0]).st_mode) == 0o600
    assert s.get_bytes(m["blob_id"]) == big
    assert s.read(m["blob_id"], 1000, 50) == big[1000:1050]
    s.delete(m["blob_id"])
    assert os.listdir(d) == []


def test_compressed_blob_on_disk_reads_back(tmp_path):
    d = tmp_path / "spill"
    s = BlobStore(cfg(mem_bytes=1000, quota_bytes=5_000_000, spill_dir=str(d), hot_bytes=0))
    m = s.put(RAW)
    assert m["encoding"] == "zstd" and spilled(d) == [m["blob_id"]]
    assert s.get_bytes(m["blob_id"]) == RAW
    assert s.read(m["blob_id"], 777, 100) == RAW[777:877]


def test_memory_only_store_evicts_or_refuses():
    s = BlobStore(cfg(mem_bytes=500_000, quota_bytes=500_000))
    s.put(os.urandom(300_000))
    s.put(os.urandom(300_000))                                # evicts the first
    assert s.stats()["blobs"] == 1
    with pytest.raises(StoreFull):
        s.put(os.urandom(600_000))


def test_spill_dir_must_be_private_and_ours(tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    os.chmod(loose, 0o755)
    with pytest.raises(ConfigError, match="0700"):
        BlobStore(cfg(spill_dir=str(loose)))
    target = tmp_path / "real"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ConfigError, match="not a directory"):
        BlobStore(cfg(spill_dir=str(link)))
    fresh = tmp_path / "new" / "deep"
    BlobStore(cfg(spill_dir=str(fresh)))
    assert stat.S_IMODE(os.stat(fresh).st_mode) == 0o700


def test_startup_cleaning_removes_only_our_own_files(tmp_path):
    d = tmp_path / "spill"
    d.mkdir(mode=0o700)
    ours = [d / ("ab" * 32 + "." + "ef" * 8), d / (".up_" + "cd" * 16)]
    foreign = [d / "notes.txt", d / ("AB" * 32), d / ("ab" * 31), d / "..x",
               d / ("ab" * 32),                                  # a bare id: some other store's file
               d / ("ab" * 32 + "." + "ef" * 7), d / ("ab" * 32 + ".ef" * 8)]
    for p in ours + foreign:
        p.write_bytes(b"x")
    BlobStore(cfg(spill_dir=str(d)))
    assert all(not p.exists() for p in ours)
    assert all(p.exists() for p in foreign)


def test_stdio_auto_spill_makes_a_private_temp_dir_lazily_and_removes_it():
    s = BlobStore(cfg(mem_bytes=10_000, quota_bytes=5_000_000, auto_spill=True))
    assert s.stats()["spill_dir"] is None
    s.put(os.urandom(50_000))
    d = s.stats()["spill_dir"]
    assert d and stat.S_IMODE(os.stat(d).st_mode) == 0o700 and len(os.listdir(d)) == 1
    s.close()
    assert not os.path.exists(d)


# ---------------------------------------------------------------- ids

def test_id_syntax():
    h = sha(b"x")
    assert normalize_blob_id(h) == h
    assert normalize_blob_id("sha256:" + h) == h
    assert normalize_blob_id("  SHA256:" + h.upper() + "  ") == h
    for bad in ("", "abc", h[:-1], h + "0", "../" + h[3:], "a/b", "..\\x", h[:-1] + "g", "\x00" * 64,
                h[:60] + "\n\n\n\n", ("٠" * 64), "sha256:", "sha1:" + h, "%2e%2e/" + h[7:]):
        with pytest.raises(BlobIdError):
            normalize_blob_id(bad)


def test_traversal_attempts_never_touch_the_filesystem(tmp_path):
    d = tmp_path / "spill"
    s = BlobStore(cfg(mem_bytes=1000, quota_bytes=5_000_000, spill_dir=str(d)))
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read")
    s.put(os.urandom(5000))
    for evil in ("../secret.txt", "..%2fsecret.txt", str(secret), "/etc/passwd", "....//secret.txt",
                 "sha256:../secret.txt", "a" * 64 + "/../../secret.txt"):
        for op in (s.get_bytes, s.get_text, s.stat, s.delete, s.peek, s.acquire):
            with pytest.raises(BlobIdError):
                op(evil)
        with pytest.raises(BlobIdError):
            s.read(evil, 0, 10)
        with pytest.raises(BlobIdError):
            s.upload(expect_id=evil)
    assert secret.read_text() == "do not read"
    assert len(os.listdir(d)) == 1
    assert all(re.fullmatch(r"[0-9a-f]{64}\.[0-9a-f]{16}", n) for n in os.listdir(d))


# ---------------------------------------------------------------- reading

def test_read_windows_on_each_storage_form(tmp_path):
    big = (b"0123456789" * 50_000)
    forms = {
        "zstd-mem": BlobStore(cfg(hot_bytes=0)),
        "zstd-hot": BlobStore(cfg()),
        "identity-mem": BlobStore(cfg()),
        "zstd-disk": BlobStore(cfg(mem_bytes=1000, quota_bytes=5_000_000, spill_dir=str(tmp_path / "s1"), hot_bytes=0)),
    }
    for name, s in forms.items():
        data = os.urandom(200_000) if name == "identity-mem" else big
        m = s.put(data)
        assert m["encoding"] == ("identity" if name == "identity-mem" else "zstd")
        for off, n in ((0, 16), (1, 1), (99_999, 5000), (len(data) - 10, 100), (0, len(data))):
            assert s.read(m["blob_id"], off, n) == data[off:off + n], (name, off, n)
        with pytest.raises(BlobError, match="beyond"):
            s.read(m["blob_id"], len(data) + 1, 1)
        with pytest.raises(BlobError):
            s.read(m["blob_id"], -1, 1)


def test_large_blob_pages_are_stream_decoded_not_cached():
    big = b"abcdefghij" * 100_000
    s = BlobStore(cfg(hot_bytes=100))
    m = s.put(big)
    assert s.read(m["blob_id"], 500_000, 10) == big[500_000:500_010]
    assert s._hot is None
    s2 = BlobStore(cfg())
    m2 = s2.put(big)
    s2.read(m2["blob_id"], 10, 10)
    assert s2._hot is not None and s2._hot[0] == m2["blob_id"]


def test_lease_streams_stored_and_decoded_bytes():
    s = BlobStore(cfg())
    m = s.put(RAW)
    lease = s.acquire(m["blob_id"])
    try:
        assert lease.encoding == "zstd" and lease.size == len(RAW)
        assert decode_all(b"".join(lease.iter_stored()), "zstd", len(RAW)) == RAW
        assert b"".join(lease.iter_decoded()) == RAW
    finally:
        lease.close()
    lease.close()                                              # idempotent
    assert s._d[m["blob_id"]].refs == 0


# ---------------------------------------------------------------- configuration

def test_config_defaults_and_size_suffixes():
    c = BlobConfig.from_env({})
    assert c.enabled and not c.http and c.max_bytes == 256 << 20 and c.mem_bytes == 128 << 20
    assert c.quota == 1 << 30 and c.ttl == 3600 and c.max_age == 86400 and c.max_ratio == 250
    assert c.inline_max == 3_000_000 and c.max_uploads == 4 and c.auto_spill is True
    c = BlobConfig.from_env({"X3D_MCP_BLOB_MAX_BYTES": "64M", "X3D_MCP_BLOB_MEM_BYTES": "1G",
                             "X3D_MCP_BLOB_DIR": "/tmp/x", "X3D_MCP_BLOB_TTL": "60"})
    assert c.max_bytes == 64 << 20 and c.mem_bytes == 1 << 30 and c.quota == 1 << 30
    assert c.ttl == 60 and c.auto_spill is False
    h = BlobConfig.from_env({"MCP_TRANSPORT": "streamable-http", "X3D_MCP_BLOB_TOKEN": "k" * 40})
    assert h.http and h.enabled and not h.auto_spill and h.quota == 128 << 20      # RAM only over HTTP


def test_enablement_matrix():
    tok = "k" * 40
    http = {"MCP_TRANSPORT": "streamable-http"}
    assert BlobConfig.from_env({}).enabled                                   # stdio: on
    assert not BlobConfig.from_env({"X3D_MCP_BLOB": "off"}).enabled
    assert not BlobConfig.from_env(http).enabled                             # HTTP without token: off
    assert BlobConfig.from_env({**http, "X3D_MCP_BLOB_TOKEN": tok}).enabled
    assert not BlobConfig.from_env({**http, "X3D_MCP_BLOB_TOKEN": tok, "X3D_MCP_BLOB": "off"}).enabled
    with pytest.raises(ConfigError, match="X3D_MCP_BLOB_TOKEN"):
        BlobConfig.from_env({**http, "X3D_MCP_BLOB": "on"})                  # no unauthenticated routes
    with pytest.raises(ConfigError, match="32"):
        BlobConfig.from_env({**http, "X3D_MCP_BLOB_TOKEN": "short"})
    assert BlobConfig.from_env({"X3D_MCP_BLOB_TOKEN": "short"}).enabled      # stdio ignores the token


def test_invalid_config_values_are_startup_errors():
    for env in ({"X3D_MCP_BLOB_MAX_BYTES": "lots"}, {"X3D_MCP_BLOB_TTL": "soon"},
                {"X3D_MCP_BLOB": "maybe"}, {"X3D_MCP_BLOB_MAX_BYTES": "0"}, {"X3D_MCP_BLOB_TTL": "0"}):
        with pytest.raises(ConfigError):
            BlobConfig.from_env(env)


def test_process_wide_store_follows_configure():
    assert blobs.blob_enabled()
    s1 = blobs.get_store()
    assert blobs.get_store() is s1
    blobs.configure(BlobConfig(enabled=False))
    with pytest.raises(BlobUnavailable, match="not enabled"):
        blobs.get_store()
    blobs.configure(BlobConfig())
    assert blobs.get_store() is not s1


# ---------------------------------------------------------------- the input resolver

def test_resolve_text_picks_the_one_input_given(tmp_path):
    f = tmp_path / "s.x3d"
    f.write_text("<X3D>file</X3D>", encoding="utf-8")
    meta = blobs.get_store().put(b"<X3D>blob</X3D>")
    assert resolve_text(content="<X3D>inline</X3D>") == ("<X3D>inline</X3D>", "inline content")
    assert resolve_text(path=str(f)) == ("<X3D>file</X3D>", f"file {f}")
    text, label = resolve_text(blob_id=meta["blob_id"])
    assert text == "<X3D>blob</X3D>" and label == "blob " + meta["blob_id"][:12]
    assert resolve_text(blob_id="sha256:" + meta["blob_id"].upper())[0] == "<X3D>blob</X3D>"


def test_resolve_text_conflicts_and_nothing_are_errors_never_a_silent_winner(tmp_path):
    meta = blobs.get_store().put(b"<X3D/>")
    bid = meta["blob_id"]
    with pytest.raises(ValueError, match="not both"):                        # the original wording
        resolve_text(content="<X3D/>", path="/tmp/x.x3d")
    with pytest.raises(ValueError, match="Provide either `content`.*or `path`"):
        resolve_text()
    with pytest.raises(ValueError, match="exactly one of.*got: content, blob_id"):
        resolve_text(content="<X3D/>", blob_id=bid)
    with pytest.raises(ValueError, match="got: path, blob_id"):
        resolve_text(path="/tmp/x.x3d", blob_id=bid)
    with pytest.raises(ValueError, match="got: content, path, blob_id"):
        resolve_text("a", "b", bid)
    assert resolve_text(content="  ", path="", blob_id=bid)[0] == "<X3D/>"   # blank means not given


def test_resolve_text_errors_are_clear():
    with pytest.raises(BlobNotFound, match="never stored, expired"):
        resolve_text(blob_id="0" * 64)
    with pytest.raises(BlobIdError, match="64 hexadecimal"):
        resolve_text(blob_id="../etc/passwd")
    blobs.configure(BlobConfig(enabled=False))
    with pytest.raises(BlobUnavailable, match="X3D_MCP_BLOB_TOKEN"):
        resolve_text(blob_id="0" * 64)


def test_resolve_text_path_rules_are_unchanged(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="not found"):
        resolve_text(path=str(tmp_path / "nope.x3d"))
    f = tmp_path / "s.x3d"
    f.write_text("<X3D/>")
    monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
    with pytest.raises(ValueError, match="disabled over the HTTP transport"):
        resolve_text(path=str(f))


def test_load_x3d_source_accepts_a_blob_id_and_keeps_its_old_behaviour():
    from x3d_utils.source import load_x3d_source
    meta = blobs.get_store().put(b"<X3D>b</X3D>")
    assert load_x3d_source(blob_id=meta["blob_id"]) == "<X3D>b</X3D>"
    assert load_x3d_source(content="<X3D/>") == "<X3D/>"
    assert load_x3d_source("<X3D/>", None, None) == "<X3D/>"
    with pytest.raises(ValueError, match="not both"):
        load_x3d_source(content="<X3D/>", path="/tmp/x.x3d")


# ---------------------------------------------------------------- in-process callers (playground)

def test_in_process_validation_signatures_are_unchanged():
    """playground/report.py calls these directly with the scene string. They may only
    gain optional, appended parameters; today none has gained any."""
    from validation import autofix, repair, semantic, validate
    expected = {
        (validate, "validate_xml"): "(xml_string: str, version: str | None = None) -> dict",
        (validate, "validate_json"): "(json_string: str) -> dict",
        (validate, "validate_scene"): "(scene_manager) -> dict",
        (semantic, "semantic_diagnostics"): "(xml_string: str) -> list[validation.semantic.Diagnostic]",
        (semantic, "validate_semantic"): "(xml_string: str) -> str",
        (repair, "repair_x3d"): "(xml_string: str) -> dict",
        (autofix, "autofix_containerfields"): "(xml_string: str) -> dict",
    }
    for (mod, name), want in expected.items():
        assert str(inspect.signature(getattr(mod, name))) == want, f"{mod.__name__}.{name}"


# ---------------------------------------------------------------- review fixes: spill files and leases

def test_reupload_after_delete_while_leased_keeps_the_new_blob(tmp_path):
    """A deleted blob that a reader still holds must not share a file with an identical new upload."""
    d = tmp_path / "spill"
    s = BlobStore(cfg(mem_bytes=1000, quota_bytes=20_000_000, spill_dir=str(d)))
    data = os.urandom(50_000)
    old = s.put(data)
    assert old["encoding"] == "identity"
    lease = s.acquire(old["blob_id"])
    assert s.delete(old["blob_id"]) is True
    again = s.put(data)
    assert again["created"] is True
    assert len(os.listdir(d)) == 2                            # the reader's file and the new one
    assert b"".join(lease.iter_stored()) == data
    lease.close()
    assert len(os.listdir(d)) == 1
    assert s.get_bytes(old["blob_id"]) == data                # the new blob survived the old reader
    assert s.read(old["blob_id"], 10, 5) == data[10:15]


def test_old_reader_keeps_its_own_codec_when_the_same_content_is_stored_differently(tmp_path):
    d = tmp_path / "spill"
    s = BlobStore(cfg(mem_bytes=1000, quota_bytes=20_000_000, spill_dir=str(d)))
    data = os.urandom(50_000)
    old = s.put(data)                                         # kept as is: incompressible
    lease = s.acquire(old["blob_id"])
    s.delete(old["blob_id"])
    new = s.put(zstandard.ZstdCompressor(level=3).compress(data), "zstd")      # same id, other codec
    assert new["blob_id"] == old["blob_id"] and new["encoding"] == "zstd"
    assert lease.encoding == "identity"
    assert b"".join(lease.iter_decoded()) == data
    lease.close()
    assert s.get_bytes(new["blob_id"]) == data


def test_dropped_blob_with_a_reader_still_counts_against_the_quota_and_ram(tmp_path):
    s = BlobStore(cfg(mem_bytes=1_000_000, quota_bytes=1_000_000))
    a = s.put(os.urandom(600_000))
    lease = s.acquire(a["blob_id"])
    s.delete(a["blob_id"])
    st = s.stats()
    assert st["blobs"] == 0 and st["total"] == 600_000 and st["mem_used"] == 600_000
    with pytest.raises(StoreFull):                            # the bytes are still pinned in memory
        s.put(os.urandom(600_000))
    lease.close()
    st = s.stats()
    assert st["total"] == 0 and st["mem_used"] == 0
    s.put(os.urandom(600_000))
    d = tmp_path / "spill"
    s = BlobStore(cfg(mem_bytes=1000, quota_bytes=1_000_000, spill_dir=str(d)))
    a = s.put(os.urandom(600_000))
    lease = s.acquire(a["blob_id"])
    s.delete(a["blob_id"])
    with pytest.raises(StoreFull):                            # ... and the file is still on disk
        s.put(os.urandom(600_000))
    lease.close()
    assert s.stats()["total"] == 0 and os.listdir(d) == []
    s.put(os.urandom(600_000))


def test_entry_whose_spill_file_vanished_is_forgotten_and_can_be_stored_again(tmp_path):
    d = tmp_path / "spill"
    s = BlobStore(cfg(mem_bytes=1000, quota_bytes=20_000_000, spill_dir=str(d)))
    data = os.urandom(50_000)
    m = s.put(data)
    for name in os.listdir(d):
        os.unlink(d / name)                                   # a tmp cleaner, a disk fault
    assert s.peek(m["blob_id"]) is None                       # not a ghost: stat says so
    assert s.stats()["blobs"] == 0 and s.stats()["total"] == 0
    again = s.put(data)
    assert again["created"] is True and s.get_bytes(m["blob_id"]) == data
    for name in os.listdir(d):
        os.unlink(d / name)
    assert s.put(data)["created"] is True                     # the dedupe path checks the file too
    assert s.get_bytes(m["blob_id"]) == data


def test_spill_errors_do_not_carry_server_paths(tmp_path):
    d = tmp_path / "spill"
    s = BlobStore(cfg(mem_bytes=1000, quota_bytes=20_000_000, spill_dir=str(d), hot_bytes=0))
    for call in (lambda i: s.read(i, 0, 10), lambda i: b"".join(s.acquire(i).iter_stored())):
        m = s.put(os.urandom(50_000))
        (path,) = [d / n for n in os.listdir(d)]
        path.unlink()
        path.mkdir()                                          # exists, but cannot be read as a file
        with pytest.raises(BlobNotFound) as ei:
            call(m["blob_id"])
        assert str(tmp_path) not in str(ei.value) and "no longer available" in str(ei.value)
        path.rmdir()
    shutil.rmtree(d)                                          # the whole directory goes
    with pytest.raises(StoreFull) as ei:
        s.put(os.urandom(50_000))
    assert str(tmp_path) not in str(ei.value) and "spill file" in str(ei.value)


def test_a_failed_move_into_place_leaves_the_accounting_clean(tmp_path, monkeypatch):
    d = tmp_path / "spill"
    s = BlobStore(cfg(mem_bytes=1000, quota_bytes=20_000_000, spill_dir=str(d)))
    real = os.replace

    def broken(src, dst):
        raise OSError(28, "No space left on device", str(dst))

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(StoreFull, match="No space left on device") as ei:
        s.put(os.urandom(50_000))
    assert str(tmp_path) not in str(ei.value)
    monkeypatch.setattr(os, "replace", real)
    st = s.stats()
    assert st["inflight"] == 0 and st["inflight_mem"] == 0 and st["uploads"] == 0 and st["blobs"] == 0
    assert os.listdir(d) == []                                # the temp file is gone too


# ---------------------------------------------------------------- review fixes: sharing a spill directory

def test_a_second_store_on_the_same_spill_directory_is_refused_not_wiped(tmp_path):
    d = tmp_path / "spill"
    a = BlobStore(cfg(mem_bytes=1000, quota_bytes=20_000_000, spill_dir=str(d)))
    data = os.urandom(50_000)
    m = a.put(data)
    with pytest.raises(ConfigError, match="in use by another server process"):
        BlobStore(cfg(spill_dir=str(d)))
    assert len(os.listdir(d)) == 1 and a.get_bytes(m["blob_id"]) == data
    a.close()
    BlobStore(cfg(spill_dir=str(d)))                          # free again once the owner is gone


def test_a_second_process_on_the_same_spill_directory_is_refused(tmp_path):
    d = tmp_path / "spill"
    a = BlobStore(cfg(mem_bytes=1000, quota_bytes=20_000_000, spill_dir=str(d)))
    m = a.put(os.urandom(50_000))
    code = ("import sys\nsys.path.insert(0, sys.argv[1])\n"
            "from transfer.blobs import BlobConfig, BlobStore\nfrom transfer.errors import ConfigError\n"
            "try:\n    BlobStore(BlobConfig(spill_dir=sys.argv[2]))\nexcept ConfigError as e:\n"
            "    print('refused:', e)\n")
    src = str(Path(__file__).resolve().parent.parent / "src")
    out = subprocess.run([sys.executable, "-c", code, src, str(d)], capture_output=True, text=True, timeout=60)
    assert "refused:" in out.stdout and "another server process" in out.stdout, out
    assert len(os.listdir(d)) == 1 and a.peek(m["blob_id"]) is not None


def test_a_crashed_servers_files_are_cleaned_by_the_next_one(tmp_path):
    d = tmp_path / "spill"
    d.mkdir(mode=0o700)
    leftovers = [d / ("ab" * 32 + ".0123456789abcdef"), d / (".up_" + "cd" * 16)]
    for p in leftovers:
        p.write_bytes(b"x")
    BlobStore(cfg(spill_dir=str(d)))                          # the lock died with the old process
    assert not any(p.exists() for p in leftovers)


# ---------------------------------------------------------------- review fixes: table size and sweeping

def test_blob_count_cap_evicts_the_least_recently_used():
    s = BlobStore(cfg(max_blobs=3))
    ids = [s.put(bytes([i]) * 10)["blob_id"] for i in range(3)]
    s.stat(ids[0])                                            # ids[1] is now the oldest
    ids.append(s.put(b"new")["blob_id"])
    assert [s.peek(i) is not None for i in ids] == [True, False, True, True]
    assert s.stats()["blobs"] == 3


def test_blob_count_cap_refuses_when_every_blob_is_in_use():
    s = BlobStore(cfg(max_blobs=2))
    leases = [s.acquire(s.put(bytes([i]) * 10)["blob_id"]) for i in range(2)]
    with pytest.raises(StoreFull, match="X3D_MCP_BLOB_MAX_COUNT"):
        s.put(b"third")
    with pytest.raises(StoreFull):
        s.upload()
    assert s.stats()["uploads"] == 0 and s.stats()["inflight"] == 0
    leases[0].close()
    s.put(b"third")                                           # room again
    for lease in leases:
        lease.close()


def test_lookups_do_not_scan_the_whole_table():
    class Counting(dict):
        scans = 0

        def values(self):
            Counting.scans += 1
            return super().values()

    from collections import OrderedDict

    class CountingOD(OrderedDict):
        def values(self):
            Counting.scans += 1
            return super().values()

    clock = Clock()
    s = BlobStore(cfg(), clock=clock)
    ids = [s.put(b"blob %d" % i)["blob_id"] for i in range(50)]
    s._d = CountingOD(s._d)
    Counting.scans = 0
    for _ in range(20):
        for i in ids[:5]:
            s.stat(i)                                         # 100 lookups inside one second
    assert Counting.scans <= 1


def test_an_expired_blob_is_refused_even_before_the_next_sweep():
    clock = Clock()
    s = BlobStore(cfg(ttl=10), clock=clock)
    a = s.put(b"a")["blob_id"]
    s.stat(a)                                                 # a sweep has just run
    clock.t += 11
    s._swept = clock.t - 0.1                                  # ... and another is not due yet
    assert s.peek(a) is None
    with pytest.raises(BlobNotFound):
        s.get_bytes(a)


def test_expired_blobs_are_dropped_by_the_throttled_sweep():
    clock = Clock()
    s = BlobStore(cfg(ttl=10), clock=clock)
    s.put(b"a")
    s.put(b"b")
    clock.t += 11
    s.put(b"c")                                               # a put sweeps
    assert s.stats()["blobs"] == 1


def test_in_process_puts_do_not_take_streaming_upload_slots():
    s = BlobStore(cfg(max_uploads=2))
    a, b = s.upload(), s.upload()                             # both slots busy with streaming uploads
    with pytest.raises(TooManyUploads):
        s.upload()
    m = s.put(b"small document")                              # blob_put and result storage still work
    assert m["created"] is True and s.stats()["uploads"] == 2
    a.abort()
    b.abort()


# ---------------------------------------------------------------- review fixes: configuration

def test_config_repr_does_not_show_the_token():
    c = BlobConfig(token="SECRET-TOKEN-VALUE" * 3)
    assert "SECRET" not in repr(c) and "SECRET" not in str(c)
    assert c.token.startswith("SECRET")


def test_new_limits_have_defaults_and_env_names():
    c = BlobConfig.from_env({})
    assert c.max_blobs == 10000 and c.tool_max_bytes == 96 << 20 and c.min_rate == 4096
    assert c.path_roots == ()
    c = BlobConfig.from_env({"X3D_MCP_BLOB_MAX_COUNT": "50", "X3D_MCP_TOOL_MAX_BYTES": "8M",
                             "X3D_MCP_BLOB_MIN_RATE": "0",
                             "X3D_MCP_BLOB_PATH_ROOTS": os.pathsep.join(["/a", "/b"])})
    assert c.max_blobs == 50 and c.tool_max_bytes == 8 << 20 and c.min_rate == 0
    assert c.path_roots == ("/a", "/b")
    with pytest.raises(ConfigError):
        BlobConfig.from_env({"X3D_MCP_BLOB_MAX_COUNT": "0"})


def test_origins_without_hosts_is_a_startup_error_over_http():
    http = {"MCP_TRANSPORT": "streamable-http", "X3D_MCP_BLOB_TOKEN": "k" * 40}
    with pytest.raises(ConfigError, match="ALLOWED_ORIGINS needs X3D_MCP_ALLOWED_HOSTS"):
        BlobConfig.from_env({**http, "X3D_MCP_ALLOWED_ORIGINS": "https://app.example.com"})
    ok = BlobConfig.from_env({**http, "X3D_MCP_ALLOWED_ORIGINS": "https://app.example.com",
                              "X3D_MCP_ALLOWED_HOSTS": "x3d.example.com"})
    assert ok.allowed_hosts == ("x3d.example.com",)
    BlobConfig.from_env({"X3D_MCP_ALLOWED_ORIGINS": "https://app.example.com"})        # stdio: unused


def test_get_store_reports_an_unusable_spill_directory_without_the_path(tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir()
    os.chmod(loose, 0o755)
    blobs.configure(BlobConfig(spill_dir=str(loose)))
    with pytest.raises(StoreUnavailable) as ei:
        blobs.get_store()
    assert str(tmp_path) not in str(ei.value) and ei.value.status == 503
    with pytest.raises(ConfigError, match="0700"):
        blobs.start()                                         # the server's start-up check says why


# ---------------------------------------------------------------- review fixes: multi-member streams

def _feed_all(enc, data, size=1000):
    d = Capped(enc, 1 << 30)
    out = []
    for i in range(0, max(len(data), 1), size):
        out.extend(d.feed(data[i:i + size]))
    out.extend(d.finish())
    return b"".join(out)


def test_multi_member_gzip_and_multi_frame_zstd_streams_are_decoded():
    a, b = os.urandom(3000) + b"A" * 50_000, b"B" * 70_000
    gz = gzip.compress(a, 6, mtime=0) + gzip.compress(b, 6, mtime=0) + gzip.compress(b"", 6, mtime=0)
    comp = zstandard.ZstdCompressor(level=3)
    z = comp.compress(a) + comp.compress(b)
    with_skippable = comp.compress(a) + b"\x50\x2a\x4d\x18" + (4).to_bytes(4, "little") + b"meta" + comp.compress(b)
    for size in (1, 7, 1000, 1 << 20):
        assert _feed_all("gzip", gz, size) == a + b
        assert _feed_all("zstd", z, size) == a + b
        assert _feed_all("zstd", with_skippable, size) == a + b
    s = BlobStore(cfg())
    for enc, body in (("gzip", gz), ("zstd", z)):
        m = s.put(body, enc)
        assert m["blob_id"] == sha(a + b) and s.get_bytes(m["blob_id"]) == a + b


def test_garbage_or_a_cut_second_member_is_still_refused():
    a = b"x" * 5000
    for enc, comp in (("gzip", lambda b: gzip.compress(b, 6, mtime=0)),
                      ("zstd", zstandard.ZstdCompressor(level=3).compress)):
        good = comp(a)
        for bad in (good + b"garbage", good + b"\0" * 20, good + comp(a)[:-3]):
            with pytest.raises(BlobCorrupt):
                _feed_all(enc, bad, 100)
        with pytest.raises(BlobCorrupt, match="trailing data"):
            _feed_all(enc, good + b"garbage trailing bytes", 1 << 20)
    bomb = gzip.compress(b"\0" * (30 << 20), 9) * 3
    with pytest.raises(BlobTooLarge):
        d = Capped("gzip", 50 << 20)
        list(d.feed(bomb))                                    # members add up against the cap
