"""Minimal standard-library MCAP reader (https://mcap.dev/spec, format version 0) and ROS 1 and ROS 2 message decoders.

Read-only and defensive: every record length is bounds-checked; a record that does not fit is counted in `problems` and
the walk stops instead of trusting it. Chunks with compression "" are read; zstd / lz4 chunks are decompressed only if
the stdlib can (Python 3.14 compression.zstd) or an installed backports.zstd / zstandard / lz4 module can, otherwise
they are counted as skipped (no external program is run). ROS 1 (ros1msg) and ROS 2 (ros2msg + CDR) payloads decode.

Record layout: opcode u8, length u64 LE, content. Strings: u32 length + UTF-8. Byte arrays: u32 (or u64 for chunk
records) length + bytes. Maps: u32 byte length, then key/value strings.
"""
import struct

MAGIC = b"\x89MCAP0\r\n"
OPS = {1: "Header", 2: "Footer", 3: "Schema", 4: "Channel", 5: "Message", 6: "Chunk", 7: "MessageIndex", 8: "ChunkIndex",
       9: "Attachment", 10: "AttachmentIndex", 11: "Statistics", 12: "Metadata", 13: "MetadataIndex", 14: "SummaryOffset", 15: "DataEnd"}


class R:
    def __init__(self, b, off=0, end=None):
        self.b, self.o, self.e = b, off, len(b) if end is None else end

    def take(self, n):
        if n < 0 or self.o + n > self.e:
            raise ValueError("record field runs past its record")
        v = self.b[self.o:self.o + n]
        self.o += n
        return v

    def u8(self): return self.take(1)[0]
    def u16(self): return struct.unpack("<H", self.take(2))[0]
    def u32(self): return struct.unpack("<I", self.take(4))[0]
    def u64(self): return struct.unpack("<Q", self.take(8))[0]
    def s(self): return self.take(self.u32()).decode("utf-8", "replace")

    def smap(self):
        n = self.u32()
        sub = R(self.b, self.o, self.o + n)
        self.take(n)
        out = {}
        while sub.o < sub.e:
            k = sub.s()
            out[k] = sub.s()
        return out


def _records(buf, off, end, out, problems, depth=0):
    while off + 9 <= end:
        op = buf[off]
        ln = struct.unpack("<Q", buf[off + 1:off + 9])[0]
        if off + 9 + ln > end:
            problems.append("record opcode %d at %d overruns (%d bytes)" % (op, off, ln))
            return
        c = R(buf, off + 9, off + 9 + ln)
        try:
            if op == 1:
                out["header"] = {"profile": c.s(), "library": c.s()}
            elif op == 3:
                sid = c.u16()
                out["schemas"][sid] = {"id": sid, "name": c.s(), "encoding": c.s(), "data": bytes(c.take(c.u32()))}
            elif op == 4:
                cid, sid = c.u16(), c.u16()
                out["channels"][cid] = {"id": cid, "schema_id": sid, "topic": c.s(), "encoding": c.s(), "metadata": c.smap()}
            elif op == 5:
                cid, seq, lt, pt = c.u16(), c.u32(), c.u64(), c.u64()
                out["messages"].append({"channel": cid, "seq": seq, "log_time": lt, "publish_time": pt, "data": bytes(buf[c.o:c.e])})
            elif op == 6 and depth == 0:
                st, et, usz, crc = c.u64(), c.u64(), c.u64(), c.u32()
                comp = c.s()
                n = c.u64()
                start = c.o
                c.take(n)
                out["chunks"].append({"start": st, "end": et, "compression": comp or "none", "bytes": n, "uncompressed": usz})
                if comp == "":
                    _records(buf, start, start + n, out, problems, depth + 1)
                else:
                    data = _decompress(comp, bytes(buf[start:start + n]), usz)
                    if data is None:
                        out["skipped_chunks"] += 1
                    else:
                        _records(data, 0, len(data), out, problems, depth + 1)
            elif op == 9:
                lt, ct = c.u64(), c.u64()
                name, mt = c.s(), c.s()
                out["attachments"].append({"name": name, "media_type": mt, "bytes": c.u64()})
            elif op == 11:
                out["statistics"] = {"message_count": c.u64(), "schema_count": c.u16(), "channel_count": c.u32(), "attachment_count": c.u32(),
                                     "metadata_count": c.u32(), "chunk_count": c.u32(), "start": c.u64(), "end": c.u64()}
            elif op == 12:
                out["metadata"].append({"name": c.s(), "map": c.smap()})
            out["opcounts"][OPS.get(op, "op%d" % op)] = out["opcounts"].get(OPS.get(op, "op%d" % op), 0) + 1
        except ValueError as e:
            problems.append("opcode %d at %d: %s" % (op, off, e))
        off += 9 + ln
        if op == 15 and depth == 0:    # DataEnd: the summary section follows; schemas/channels there repeat the data section
            out["data_end"] = off
    return off


def _decompress(comp, data, usz):
    """zstd through the stdlib (Python 3.14 compression.zstd) or, if installed, the backports.zstd / zstandard /
    lz4 Python modules. ROS 2 recorders write zstd chunks by default, so without one of these a real bag reads as
    skipped chunks, which read_mcap reports. No external program is run."""
    mods = {"zstd": ("compression.zstd", "backports.zstd", "zstandard"), "lz4": ("lz4.frame",)}.get(comp, ())
    for name in mods:
        try:
            mod = __import__(name, fromlist=["decompress"])
        except ImportError:
            continue
        try:
            if name == "zstandard":
                return mod.ZstdDecompressor().decompress(data, max_output_size=usz)
            return mod.decompress(data)
        except Exception:
            return None
    return None


def read_mcap(path):
    buf = open(path, "rb").read()
    if buf[:8] != MAGIC or buf[-8:] != MAGIC:
        raise ValueError("not an MCAP file (magic missing at start or end)")
    out = {"header": None, "schemas": {}, "channels": {}, "messages": [], "chunks": [], "attachments": [], "metadata": [],
           "statistics": None, "opcounts": {}, "skipped_chunks": 0}
    problems = []
    _records(buf, 8, len(buf) - 8, out, problems)
    out["problems"] = problems
    out["bytes"] = len(buf)
    return out


# ---------- ROS 1 message decoding from the ros1msg schema text ----------
PRIM = {"bool": "<?", "int8": "<b", "uint8": "<B", "byte": "<B", "char": "<B", "int16": "<h", "uint16": "<H", "int32": "<i",
        "uint32": "<I", "int64": "<q", "uint64": "<Q", "float32": "<f", "float64": "<d"}


def parse_ros1(schema_name, text):
    """ros1msg schema (main definition, then '=====' separated 'MSG: pkg/Name' dependencies) -> {type: [(ftype, name, array)]}."""
    defs, cur, pkg = {}, schema_name, schema_name.split("/")[0]
    defs[cur] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("==="):
            continue
        if line.startswith("MSG:"):
            cur = line[4:].strip()
            defs[cur] = []
            continue
        parts = line.split()
        if len(parts) < 2 or "=" in parts[1] or (len(parts) > 2 and parts[2].startswith("=")):
            continue            # constant
        t, name = parts[0], parts[1]
        arr = None
        if "[" in t:
            t, a = t.split("[", 1)
            a = a.rstrip("]")
            arr = int(a) if a else -1
        defs[cur].append((t, name, arr))
    return defs


def _resolve(t, scope, defs):
    if t in PRIM or t in ("string", "time", "duration"):
        return t
    if t == "Header":
        return "std_msgs/Header"
    if "/" in t:
        return t
    pkg = scope.split("/")[0]
    return pkg + "/" + t if pkg + "/" + t in defs else next((k for k in defs if k.endswith("/" + t)), t)


def decode_ros1(defs, typ, data, maxlen=4096):
    r = R(data)

    def one(t, scope):
        t = _resolve(t, scope, defs)
        if t in PRIM:
            return struct.unpack(PRIM[t], r.take(struct.calcsize(PRIM[t])))[0]
        if t == "string":
            return r.take(r.u32()).decode("utf-8", "replace")
        if t in ("time", "duration"):
            s, ns = struct.unpack("<ii" if t == "duration" else "<II", r.take(8))
            return s + ns / 1e9
        if t not in defs:
            raise ValueError("unknown type " + t)
        return {n: field(ft, n, a, t) for ft, n, a in defs[t]}

    def field(ft, n, a, scope):
        if a is None:
            return one(ft, scope)
        cnt = r.u32() if a == -1 else a
        rt = _resolve(ft, scope, defs)
        if rt in ("uint8", "byte", "char", "int8"):
            b = r.take(cnt)
            return {"_bytes": len(b)}
        if cnt > maxlen and rt in PRIM:
            r.take(cnt * struct.calcsize(PRIM[rt]))
            return {"_count": cnt}
        return [one(ft, scope) for _ in range(cnt)]
    return one(typ, typ)


# ---------- ROS 2 message decoding: ros2msg schema text, CDR payloads ----------
# CDR (OMG CDR as written by ROS 2 rmw, XCDR1): a 4-byte encapsulation header (0x00 0x01 little endian, 0x00 0x00 big
# endian, then 2 option bytes), then each primitive aligned to its own size (at most 8) counted from the end of the
# header. string: u32 length that includes the NUL, the bytes, the NUL. Sequence: u32 count, then elements. Fixed
# array: elements only. A message with no fields is serialised as one uint8. XCDR2, wstring and ros2idl schemas are
# refused (ValueError), not guessed.
PRIM2 = {"bool": "?", "byte": "B", "char": "B", "int8": "b", "uint8": "B", "int16": "h", "uint16": "H", "int32": "i",
         "uint32": "I", "int64": "q", "uint64": "Q", "float32": "f", "float64": "d"}


def _ros2_name(t):
    """'sensor_msgs/msg/JointState' and 'sensor_msgs/JointState' are the same type."""
    p = t.split("/")
    return p[0] + "/" + p[-1] if len(p) == 3 else t


def parse_ros2(schema_name, text):
    """ros2msg schema (main definition, then '=====' separated 'MSG: pkg/Name' dependencies) ->
    {type: [(ftype, name, array, bound)]}; array is None (scalar), -1 (sequence), N (fixed) or ('<=', N) (bounded)."""
    cur = _ros2_name(schema_name)
    defs = {cur: []}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("==="):
            continue
        if line.startswith("MSG:"):
            cur = _ros2_name(line[4:].strip())
            defs[cur] = []
            continue
        parts = line.split()
        if len(parts) < 2 or "=" in parts[1] or (len(parts) > 2 and parts[2].startswith("=")):
            continue            # constant
        t, name = parts[0], parts[1]      # a third part is a default value: not on the wire
        arr = None
        if "[" in t:
            t, a = t.split("[", 1)
            a = a.rstrip("]")
            arr = -1 if a == "" else (("<=", int(a[2:])) if a.startswith("<=") else int(a))
        if "<=" in t:           # bounded string: string<=N, same wire format as string
            t = t.split("<=")[0]
        defs[cur].append((t, name, arr))
    return defs


def _resolve2(t, scope, defs):
    if t in PRIM2 or t in ("string", "wstring"):
        return t
    t = _ros2_name(t)
    if "/" in t:
        return t
    pkg = scope.split("/")[0]
    return pkg + "/" + t if pkg + "/" + t in defs else next((k for k in defs if k.endswith("/" + t)), t)


def decode_cdr(defs, typ, data, maxlen=4096):
    """One CDR payload -> dict. Byte-like arrays come back as {'_bytes': n}; primitive arrays longer than maxlen as
    {'_count': n}, as in decode_ros1."""
    if len(data) < 4:
        raise ValueError("CDR payload shorter than its 4-byte header")
    if data[0] != 0 or data[1] not in (0, 1):
        raise ValueError(f"CDR encapsulation {data[0]:#04x} {data[1]:#04x} not handled (only CDR_BE/CDR_LE, not XCDR2): refused")
    end = "<" if data[1] == 1 else ">"
    r = R(data, 4)

    def align(n):
        pad = (-(r.o - 4)) % n
        r.take(pad)

    def prim(t):
        f = PRIM2[t]
        n = struct.calcsize(f)
        align(min(n, 8))
        return struct.unpack(end + f, r.take(n))[0]

    def u32():
        align(4)
        return struct.unpack(end + "I", r.take(4))[0]

    def one(t, scope):
        t = _resolve2(t, scope, defs)
        if t in PRIM2:
            return prim(t)
        if t == "string":
            n = u32()
            b = r.take(n)
            return b[:-1].decode("utf-8", "replace") if n else ""
        if t == "wstring":
            raise ValueError("wstring not handled: refused")
        if t not in defs:
            raise ValueError("unknown type " + t)
        if not defs[t]:
            prim("uint8")       # empty message: one placeholder byte
            return {}
        return {n: field(ft, n, a, t) for ft, n, a in defs[t]}

    def field(ft, n, a, scope):
        if a is None:
            return one(ft, scope)
        cnt = a if isinstance(a, int) and a >= 0 else u32()
        if isinstance(a, tuple) and cnt > a[1]:
            raise ValueError(f"{n}: {cnt} elements exceeds bound {a[1]}")
        rt = _resolve2(ft, scope, defs)
        if rt in ("uint8", "byte", "char", "int8"):
            return {"_bytes": len(r.take(cnt))}
        if cnt > maxlen and rt in PRIM2:
            align(min(struct.calcsize(PRIM2[rt]), 8))
            r.take(cnt * struct.calcsize(PRIM2[rt]))
            return {"_count": cnt}
        return [one(ft, scope) for _ in range(cnt)]
    return one(_ros2_name(typ), _ros2_name(typ))


def decode_message(m, msg):
    """Decode one message of read_mcap() output by its channel/schema encoding: ros1 (ros1msg) or cdr (ros2msg)."""
    ch = m["channels"][msg["channel"]]
    sc = m["schemas"].get(ch["schema_id"])
    if sc is None:
        raise ValueError("channel has no schema")
    if ch["encoding"] == "ros1" and sc["encoding"] == "ros1msg":
        return decode_ros1(parse_ros1(sc["name"], sc["data"].decode()), sc["name"], msg["data"])
    if ch["encoding"] == "cdr" and sc["encoding"] == "ros2msg":
        return decode_cdr(parse_ros2(sc["name"], sc["data"].decode()), sc["name"], msg["data"])
    raise ValueError(f"message encoding {ch['encoding']} / schema {sc['encoding']} not handled: refused")
