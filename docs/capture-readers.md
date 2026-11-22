# Capture readers: pcapng / pcap and MCAP (standard library)

Two small, read-only readers under `src/x3d_utils/` that turn a packet capture or a robotics log into plain Python data, ready for a generator that writes X3D. No dependency, no external program, nothing executed from the file.

| module | reads | decodes |
|---|---|---|
| `pcap_reader.py` | pcapng (SHB, IDB with `if_tsresol`, EPB, SPB) and classic pcap (µs or ns, either byte order) | Ethernet (VLAN), Linux SLL, raw IP; IPv4 / IPv6 (RFC 5952 text); UDP / TCP; SIP (RFC 3261, compact headers), SDP (RFC 4566), RTP / RTCP (RFC 3550) |
| `mcap_reader.py` | MCAP format version 0 (Header, Schema, Channel, Message, Chunk, Statistics, Metadata, Attachment, DataEnd) | ROS 1 messages from the `ros1msg` schema text: primitives, strings, time/duration, fixed and variable arrays, nested types, `Header`; byte arrays summarised |

Both treat the file as untrusted. Every length is checked against its record or block, and a truncated or overrunning record is counted (`malformed`, `problems`) and stops the walk instead of being trusted. zstd chunks are decompressed only if Python's own `compression.zstd` exists (3.14+); otherwise they are counted in `skipped_chunks`. lz4, ROS 2 CDR, protobuf and JSON payloads are not decoded yet.

```python
from x3d_utils import pcap_reader as P, mcap_reader as M
cap = P.read_capture("call.pcapng")
for p in cap["packets"]:
    d = P.decode(p)
    s = P.sip(d.get("payload", b"")) if "sport" in d else None

log = M.read_mcap("run.mcap")
defs = {sid: M.parse_ros1(s["name"], s["data"].decode()) for sid, s in log["schemas"].items() if s["encoding"] == "ros1msg"}
```

Used by the x3d.demo `capture-3d` scenes, where a Wireshark VoIP capture becomes a 3D ladder diagram and a Foxglove MCAP log becomes a GPS path, TF frames and topic lanes (hagopjay/x3d.demo#143). Tests: `tests/test_capture_readers.py`, which builds every capture byte by byte in code.
