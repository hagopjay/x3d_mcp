"""Tests for the standard-library capture readers: pcap_reader (pcapng / pcap, Ethernet/IP/UDP/TCP, SIP/SDP, RTP/RTCP) and
mcap_reader (MCAP records, uncompressed chunks, ROS 1 message decoding).

Every capture here is built in code, byte by byte, from the published layouts (pcapng draft, libpcap, MCAP spec v0,
RFC 3261/4566/3550), so the suite needs no binary fixture and no download.
Run: python3 -m pytest tests/test_capture_readers.py -q
"""
import importlib.util
import struct
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "x3d_utils"


def _load(name):
    spec = importlib.util.spec_from_file_location(name + "_test", SRC / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


P = _load("pcap_reader")
M = _load("mcap_reader")


# ---------------- builders ----------------
def eth_ipv4_udp(src, dst, sport, dport, payload, ttl=64):
    udp = struct.pack(">HHHH", sport, dport, 8 + len(payload), 0) + payload
    ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 1, 0, ttl, 17, 0, bytes(src), bytes(dst)) + udp
    return b"\x02\x00\x00\x00\x00\x02" + b"\x02\x00\x00\x00\x00\x01" + b"\x08\x00" + ip


def rtp(seq, ts, ssrc=0xD2BD4E3E, pt=8, n=160, marker=False):
    return struct.pack(">BBHII", 0x80, (0x80 if marker else 0) | pt, seq, ts, ssrc) + b"\xd5" * n


def _blk(btype, body):
    pad = (-len(body)) % 4
    ln = 12 + len(body) + pad
    return struct.pack("<II", btype, ln) + body + b"\0" * pad + struct.pack("<I", ln)


def pcapng(frames, tsresol=6):
    shb = _blk(0x0A0D0D0A, struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1))
    opt = struct.pack("<HH", 9, 1) + bytes([tsresol]) + b"\0\0\0" + struct.pack("<HH", 0, 0)
    idb = _blk(1, struct.pack("<HHI", 1, 0, 65535) + opt)
    out = shb + idb
    for t, data in frames:
        ticks = int(round(t * 10 ** tsresol))
        out += _blk(6, struct.pack("<IIIII", 0, ticks >> 32, ticks & 0xFFFFFFFF, len(data), len(data)) + data)
    return out


def pcap(frames, order="<", nano=False):
    magic = 0xA1B23C4D if nano else 0xA1B2C3D4
    out = struct.pack(order + "IHHiIII", magic, 2, 4, 0, 0, 65535, 1)
    for t, data in frames:
        frac = int(round((t % 1) * (1e9 if nano else 1e6)))
        out += struct.pack(order + "IIII", int(t), frac, len(data), len(data)) + data
    return out


INVITE = (b"INVITE sip:bob@example.com SIP/2.0\r\nVia: SIP/2.0/UDP 10.0.0.1:5060\r\ni: call-1@10.0.0.1\r\n"
          b"f: <sip:alice@example.com>;tag=1\r\nt: <sip:bob@example.com>\r\nCSeq: 1 INVITE\r\nc: application/sdp\r\n\r\n"
          b"v=0\r\no=- 1 1 IN IP4 10.0.0.1\r\nc=IN IP4 10.0.0.9\r\nm=audio 40376 RTP/AVP 8 0\r\na=rtpmap:8 PCMA/8000\r\na=rtpmap:0 PCMU/8000\r\n")
A, B, MEDIA = (10, 0, 0, 1), (10, 0, 0, 2), (10, 0, 0, 9)


def call_frames():
    fr = [(1000.0, eth_ipv4_udp(A, B, 5060, 5060, INVITE)),
          (1000.05, eth_ipv4_udp(B, A, 5060, 5060, b"SIP/2.0 180 Ringing\r\ni: call-1@10.0.0.1\r\nCSeq: 1 INVITE\r\n\r\n"))]
    for k in range(5):
        fr.append((1001.0 + 0.02 * k, eth_ipv4_udp(B, MEDIA, 8000, 40376, rtp(100 + k, 160 * k))))
    return fr


# ---------------- pcap_reader ----------------
@pytest.mark.parametrize("fmt", ["pcapng", "pcap-le", "pcap-be", "pcap-ns"])
def test_reads_every_container_variant(tmp_path, fmt):
    fr = call_frames()
    data = {"pcapng": lambda: pcapng(fr), "pcap-le": lambda: pcap(fr), "pcap-be": lambda: pcap(fr, ">"), "pcap-ns": lambda: pcap(fr, nano=True)}[fmt]()
    f = tmp_path / "c.cap"
    f.write_bytes(data)
    cap = P.read_capture(f)
    assert cap["format"] == ("pcapng" if fmt == "pcapng" else "pcap") and cap["malformed"] == 0
    assert len(cap["packets"]) == 7
    assert cap["packets"][1]["t"] - cap["packets"][0]["t"] == pytest.approx(0.05, abs=2e-6)


def test_pcapng_nanosecond_resolution(tmp_path):
    f = tmp_path / "ns.pcapng"
    f.write_bytes(pcapng([(5.000000123, eth_ipv4_udp(A, B, 1, 2, b"x"))], tsresol=9))
    assert P.read_capture(f)["packets"][0]["t"] == pytest.approx(5.000000123, abs=1e-9)


def test_decode_sip_sdp_and_compact_headers(tmp_path):
    f = tmp_path / "c.pcapng"
    f.write_bytes(pcapng(call_frames()))
    p0, p1 = P.read_capture(f)["packets"][:2]
    d = P.decode(p0)
    assert d["layers"] == ["eth", "ipv4", "udp"] and (d["src"], d["dst"], d["sport"], d["dport"]) == ("10.0.0.1", "10.0.0.2", 5060, 5060)
    s = P.sip(d["payload"])
    assert s["method"] == "INVITE" and s["headers"]["call-id"] == "call-1@10.0.0.1" and s["headers"]["cseq"] == "1 INVITE"
    assert s["sdp"]["conn"] == "10.0.0.9" and s["sdp"]["media"][0]["port"] == 40376 and s["sdp"]["media"][0]["rtpmap"]["8"] == "PCMA/8000"
    r = P.sip(P.decode(p1)["payload"])
    assert r["status"] == 180 and r["reason"] == "Ringing"


def test_rtp_and_rtcp_are_told_apart():
    r = P.rtp(rtp(7, 1120, marker=True))
    assert (r["pt"], r["seq"], r["ts"], r["ssrc"], r["marker"], r["size"]) == (8, 7, 1120, 0xD2BD4E3E, True, 172)
    sr = struct.pack(">BBHI", 0x80, 200, 6, 0x1234) + b"\0" * 20
    assert P.rtp(sr) is None and P.rtcp(sr)["type"] == "SR"
    assert P.rtp(b"\x40" + b"\0" * 20) is None              # version 1 is not RTP


def test_truncated_capture_is_counted_not_trusted(tmp_path):
    data = pcapng(call_frames())
    f = tmp_path / "t.pcapng"
    f.write_bytes(data[:-30])
    cap = P.read_capture(f)
    assert cap["malformed"] == 1 and len(cap["packets"]) == 6


def test_not_a_capture_raises(tmp_path):
    f = tmp_path / "x.bin"
    f.write_bytes(b"hello world, not a capture at all")
    with pytest.raises(ValueError):
        P.read_capture(f)


def test_ipv6_text_follows_rfc5952():
    assert P.ip_str(bytes.fromhex("20010db8000000000000000000000001")) == "2001:db8::1"
    assert P.ip_str(bytes.fromhex("20010db8000000010000000000000001")) == "2001:db8:0:1::1"
    assert P.ip_str(bytes(16)) == "::"


# ---------------- mcap_reader ----------------
def _s(x):
    b = x.encode()
    return struct.pack("<I", len(b)) + b


def _rec(op, body):
    return bytes([op]) + struct.pack("<Q", len(body)) + body


VEC_SCHEMA = "float64 x\nfloat64 y\nfloat64 z\n"
POSE_SCHEMA = ("std_msgs/Header header\ngeometry_msgs/Vector3 position\nstring frame\nuint8[] blob\nfloat32[3] fixed\n"
               "================================================================================\n"
               "MSG: std_msgs/Header\nuint32 seq\ntime stamp\nstring frame_id\n"
               "================================================================================\n"
               "MSG: geometry_msgs/Vector3\n" + VEC_SCHEMA)


def pose_msg(seq, x):
    hdr = struct.pack("<III", seq, 100, 5) + _s("map")
    return hdr + struct.pack("<ddd", x, 2.0, 3.0) + _s("base_link") + struct.pack("<I", 3) + b"abc" + struct.pack("<fff", 1, 2, 3)


def mcap(n=3, compression="", corrupt=False):
    head = _rec(1, _s("ros1") + _s("test-writer"))
    schema = _rec(3, struct.pack("<H", 1) + _s("demo_msgs/Pose") + _s("ros1msg") + struct.pack("<I", len(POSE_SCHEMA)) + POSE_SCHEMA.encode())
    chan = _rec(4, struct.pack("<HH", 1, 1) + _s("/pose") + _s("ros1") + struct.pack("<I", 0))
    msgs = b"".join(_rec(5, struct.pack("<HIQQ", 1, i, 10 ** 9 + i * 10 ** 8, 10 ** 9 + i * 10 ** 8) + pose_msg(i, float(i))) for i in range(n))
    inner = schema + chan + msgs
    chunk = _rec(6, struct.pack("<QQQI", 10 ** 9, 10 ** 9 + n * 10 ** 8, len(inner), 0) + _s(compression) + struct.pack("<Q", len(inner)) + inner)
    body = M.MAGIC + head + chunk + _rec(15, struct.pack("<I", 0)) + _rec(2, struct.pack("<QQI", 0, 0, 0)) + M.MAGIC
    if corrupt:                       # claim a record longer than the file
        i = body.index(chunk)
        body = body[:i + 1] + struct.pack("<Q", 10 ** 9) + body[i + 9:]
    return body


def test_mcap_records_and_ros1_decoding(tmp_path):
    f = tmp_path / "a.mcap"
    f.write_bytes(mcap(3))
    m = M.read_mcap(f)
    assert m["header"] == {"profile": "ros1", "library": "test-writer"} and m["problems"] == [] and m["skipped_chunks"] == 0
    assert m["channels"][1]["topic"] == "/pose" and m["schemas"][1]["name"] == "demo_msgs/Pose"
    assert [x["log_time"] for x in m["messages"]] == [10 ** 9, 10 ** 9 + 10 ** 8, 10 ** 9 + 2 * 10 ** 8]
    defs = M.parse_ros1("demo_msgs/Pose", m["schemas"][1]["data"].decode())
    v = M.decode_ros1(defs, "demo_msgs/Pose", m["messages"][2]["data"])
    assert v["header"]["seq"] == 2 and v["header"]["frame_id"] == "map" and v["header"]["stamp"] == pytest.approx(100 + 5e-9)
    assert v["position"] == {"x": 2.0, "y": 2.0, "z": 3.0} and v["frame"] == "base_link"
    assert v["blob"] == {"_bytes": 3} and v["fixed"] == [1.0, 2.0, 3.0]


def test_mcap_overrun_is_a_problem_not_a_crash(tmp_path):
    f = tmp_path / "bad.mcap"
    f.write_bytes(mcap(2, corrupt=True))
    m = M.read_mcap(f)
    assert m["problems"] and m["messages"] == []


def test_mcap_compressed_chunk_is_skipped_without_external_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "_decompress", lambda comp, data, usz: None)
    f = tmp_path / "z.mcap"
    f.write_bytes(mcap(2, compression="zstd"))
    m = M.read_mcap(f)
    assert m["skipped_chunks"] == 1 and m["messages"] == [] and m["chunks"][0]["compression"] == "zstd"


def test_mcap_magic_is_required(tmp_path):
    f = tmp_path / "n.mcap"
    f.write_bytes(b"\x89MCAP0\r\n" + b"\0" * 20)
    with pytest.raises(ValueError):
        M.read_mcap(f)


# ---------------- ROS 2: ros2msg schemas, CDR payloads ----------------
JS2 = ("std_msgs/Header header\nstring[] name\nfloat64[] position\nfloat64[] velocity\nfloat64[] effort\n"
       "================================================================================\n"
       "MSG: std_msgs/Header\nbuiltin_interfaces/Time stamp\nstring frame_id\n"
       "================================================================================\n"
       "MSG: builtin_interfaces/Time\nint32 sec\nuint32 nanosec\n")


def joint_state_cdr(little=True):
    """sensor_msgs/JointState with two joints, padding written out by hand (offsets counted after the 4-byte header)."""
    e = "<" if little else ">"
    b = bytes([0, 1 if little else 0, 0, 0])
    b += struct.pack(e + "iI", 5, 7)                                   # 0..8   stamp
    b += struct.pack(e + "I", 5) + b"base\0"                           # 8..17  frame_id
    b += b"\0" * 3                                                     # 17..20 pad to 4
    b += struct.pack(e + "I", 2)                                       # 20..24 name count
    b += struct.pack(e + "I", 3) + b"j0\0" + b"\0"                     # 24..32 "j0" + pad
    b += struct.pack(e + "I", 3) + b"j1\0" + b"\0"                     # 32..40 "j1" + pad
    b += struct.pack(e + "I", 2) + b"\0" * 4                           # 40..48 position count, pad to 8
    b += struct.pack(e + "dd", 0.25, -1.5)                             # 48..64
    b += struct.pack(e + "II", 0, 0)                                   # velocity, effort empty
    return b


def mcap2(payloads):
    head = _rec(1, _s("ros2") + _s("test-writer"))
    schema = _rec(3, struct.pack("<H", 1) + _s("sensor_msgs/msg/JointState") + _s("ros2msg") + struct.pack("<I", len(JS2)) + JS2.encode())
    chan = _rec(4, struct.pack("<HH", 1, 1) + _s("/joint_states") + _s("cdr") + struct.pack("<I", 0))
    msgs = b"".join(_rec(5, struct.pack("<HIQQ", 1, i, i, i) + p) for i, p in enumerate(payloads))
    return M.MAGIC + head + schema + chan + msgs + _rec(15, struct.pack("<I", 0)) + _rec(2, struct.pack("<QQI", 0, 0, 0)) + M.MAGIC


@pytest.mark.parametrize("little", [True, False])
def test_ros2_joint_state_cdr_both_byte_orders(tmp_path, little):
    f = tmp_path / "js.mcap"
    f.write_bytes(mcap2([joint_state_cdr(little)]))
    m = M.read_mcap(f)
    v = M.decode_message(m, m["messages"][0])
    assert v == {"header": {"stamp": {"sec": 5, "nanosec": 7}, "frame_id": "base"}, "name": ["j0", "j1"],
                 "position": [0.25, -1.5], "velocity": [], "effort": []}


def test_ros2msg_parse_bounds_defaults_constants_and_empty():
    d = M.parse_ros2("p/msg/T", "int32 K=3\nstring<=4 s\nfloat32[<=2] b\nuint8 x 5\nInner[3] f\n===\nMSG: p/Inner\n===\nMSG: p/Empty\n")
    assert d["p/T"] == [("string", "s", None), ("float32", "b", ("<=", 2)), ("uint8", "x", None), ("Inner", "f", 3)]
    assert d["p/Inner"] == [] and d["p/Empty"] == []
    # an empty message is one placeholder byte on the wire; three of them in a fixed array
    assert M.decode_cdr(d, "p/msg/T", bytes([0, 1, 0, 0]) + struct.pack("<I", 3) + b"ab\0" + b"\0" + struct.pack("<I", 1)
                        + struct.pack("<f", 0.5) + b"\x05" + b"\0\0\0") == {"s": "ab", "b": [0.5], "x": 5, "f": [{}, {}, {}]}


def test_ros2_cdr_refusals():
    d = M.parse_ros2("p/T", "float32[<=1] b\n")
    with pytest.raises(ValueError, match="exceeds bound"):
        M.decode_cdr(d, "p/T", bytes([0, 1, 0, 0]) + struct.pack("<Iff", 2, 1, 2))
    with pytest.raises(ValueError, match="XCDR2"):
        M.decode_cdr(d, "p/T", bytes([0, 7, 0, 0]) + struct.pack("<I", 0))
    with pytest.raises(ValueError, match="wstring"):
        M.decode_cdr(M.parse_ros2("p/W", "wstring w\n"), "p/W", bytes([0, 1, 0, 0, 1, 0, 0, 0, 65, 0, 0, 0]))
    with pytest.raises(ValueError, match="past its record"):
        M.decode_cdr(M.parse_ros2("p/D", "float64 x\n"), "p/D", bytes([0, 1, 0, 0]) + b"\0" * 4)
