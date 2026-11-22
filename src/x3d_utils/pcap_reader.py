"""Minimal, standard-library reader for pcapng and classic pcap, plus Ethernet / IPv4 / IPv6 / UDP / TCP / SIP / SDP / RTP / RTCP.

Read-only and defensive: a capture is untrusted input, so every length is bounds-checked and a malformed block or packet is
skipped and counted, never executed or trusted. Formats followed: pcapng (IETF draft-ietf-opsawg-pcapng: SHB 0x0A0D0D0A,
IDB 1, SPB 3, EPB 6), classic pcap (magic a1b2c3d4 / a1b23c4d, either byte order), RFC 3550 for RTP/RTCP, RFC 3261 for SIP,
RFC 4566 for SDP.
"""
import struct

LINK_ETHERNET, LINK_RAW, LINK_LINUX_SLL = 1, 101, 113


def read_capture(path):
    data = open(path, "rb").read()
    if data[:4] == b"\x0a\x0d\x0d\x0a":
        return _pcapng(data)
    return _pcap(data)


def _pcap(data):
    magic = data[:4]
    order = {b"\xd4\xc3\xb2\xa1": "<", b"\xa1\xb2\xc3\xd4": ">", b"\x4d\x3c\xb2\xa1": "<", b"\xa1\xb2\x3c\x4d": ">"}.get(magic)
    if not order:
        raise ValueError("not a pcap or pcapng file")
    nano = magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
    link = struct.unpack(order + "I", data[20:24])[0] & 0x0FFFFFFF
    pkts, off, bad = [], 24, 0
    while off + 16 <= len(data):
        ts, frac, cap, orig = struct.unpack(order + "IIII", data[off:off + 16])
        off += 16
        if cap > len(data) - off:
            bad += 1
            break
        pkts.append({"t": ts + frac / (1e9 if nano else 1e6), "link": link, "data": data[off:off + cap], "len": orig})
        off += cap
    return {"format": "pcap", "packets": pkts, "malformed": bad, "interfaces": [{"link": link}], "app": None}


def _opts(buf, order):
    out, off = {}, 0
    while off + 4 <= len(buf):
        code, ln = struct.unpack(order + "HH", buf[off:off + 4])
        if code == 0:
            break
        out.setdefault(code, []).append(buf[off + 4:off + 4 + ln])
        off += 4 + ((ln + 3) & ~3)
    return out


def _pcapng(data):
    off, order, ifaces, pkts, bad, app, comment = 0, "<", [], [], 0, None, None
    while off + 12 <= len(data):
        btype = struct.unpack(order + "I", data[off:off + 4])[0]
        if btype == 0x0A0D0D0A:
            bom = data[off + 8:off + 12]
            order = "<" if bom == b"\x4d\x3c\x2b\x1a" else ">"
            ifaces = []
        blen = struct.unpack(order + "I", data[off + 4:off + 8])[0]
        if blen < 12 or off + blen > len(data):
            bad += 1
            break
        body = data[off + 8:off + blen - 4]
        if btype == 0x0A0D0D0A:
            o = _opts(body[16:], order)
            app = o.get(4, [b""])[0].decode("utf-8", "replace") or None
            comment = o.get(1, [b""])[0].decode("utf-8", "replace") or None
        elif btype == 1 and len(body) >= 8:
            link = struct.unpack(order + "H", body[0:2])[0]
            o = _opts(body[8:], order)
            res = 6
            if 9 in o and o[9][0]:
                v = o[9][0][0]
                res = (v & 0x7F) if not v & 0x80 else -(v & 0x7F)
            ifaces.append({"link": link, "tsres": res, "name": o.get(2, [b""])[0].decode("utf-8", "replace")})
        elif btype == 6 and len(body) >= 20:
            iid, th, tl, cap, orig = struct.unpack(order + "IIIII", body[:20])
            if iid >= len(ifaces) or cap > len(body) - 20:
                bad += 1
            else:
                r = ifaces[iid]["tsres"]
                div = 10 ** r if r >= 0 else 2 ** (-r)
                pkts.append({"t": ((th << 32) | tl) / div, "link": ifaces[iid]["link"], "data": body[20:20 + cap], "len": orig, "iface": iid})
        elif btype == 3 and len(body) >= 4 and ifaces:
            orig = struct.unpack(order + "I", body[:4])[0]
            pkts.append({"t": None, "link": ifaces[0]["link"], "data": body[4:4 + orig], "len": orig, "iface": 0})
        off += blen
    return {"format": "pcapng", "packets": pkts, "malformed": bad, "interfaces": ifaces, "app": app, "comment": comment}


def ip_str(b):
    if len(b) == 4:
        return ".".join(str(x) for x in b)
    h = [b[i] << 8 | b[i + 1] for i in range(0, 16, 2)]
    best = (0, 0)
    i = 0
    while i < 8:            # RFC 5952 zero compression
        if h[i] == 0:
            j = i
            while j < 8 and h[j] == 0:
                j += 1
            if j - i > best[1] - best[0] and j - i > 1:
                best = (i, j)
            i = j
        else:
            i += 1
    if best[1]:
        return ":".join("%x" % x for x in h[:best[0]]) + "::" + ":".join("%x" % x for x in h[best[1]:])
    return ":".join("%x" % x for x in h)


def decode(p):
    """Return a dict of the layers this reader understands; unknown layers stop the walk (recorded as `stop`)."""
    d, out = p["data"], {"layers": []}
    off, et = 0, None
    if p["link"] == LINK_ETHERNET and len(d) >= 14:
        out["eth_src"], out["eth_dst"] = d[6:12].hex(":"), d[0:6].hex(":")
        et, off = struct.unpack(">H", d[12:14])[0], 14
        while et in (0x8100, 0x88A8) and len(d) >= off + 4:
            et, off = struct.unpack(">H", d[off + 2:off + 4])[0], off + 4
        out["layers"].append("eth")
    elif p["link"] == LINK_LINUX_SLL and len(d) >= 16:
        et, off = struct.unpack(">H", d[14:16])[0], 16
        out["layers"].append("sll")
    elif p["link"] == LINK_RAW and d:
        et = 0x0800 if d[0] >> 4 == 4 else 0x86DD
    else:
        out["stop"] = "link %s" % p["link"]
        return out
    proto, pay = None, b""
    if et == 0x0800 and len(d) >= off + 20:
        ihl = (d[off] & 0x0F) * 4
        tot = struct.unpack(">H", d[off + 2:off + 4])[0]
        proto = d[off + 9]
        out["src"], out["dst"], out["ttl"] = ip_str(d[off + 12:off + 16]), ip_str(d[off + 16:off + 20]), d[off + 8]
        pay = d[off + ihl:off + max(tot, ihl)]
        out["layers"].append("ipv4")
    elif et == 0x86DD and len(d) >= off + 40:
        proto = d[off + 6]
        out["src"], out["dst"] = ip_str(d[off + 8:off + 24]), ip_str(d[off + 24:off + 40])
        pay = d[off + 40:]
        out["layers"].append("ipv6")
    elif et == 0x0806:
        out["layers"].append("arp")
        return out
    else:
        out["stop"] = "ethertype 0x%04x" % (et or 0)
        return out
    out["proto"] = proto
    if proto == 17 and len(pay) >= 8:
        out["sport"], out["dport"] = struct.unpack(">HH", pay[:4])
        out["payload"] = pay[8:]
        out["layers"].append("udp")
    elif proto == 6 and len(pay) >= 20:
        out["sport"], out["dport"], seq, ack = struct.unpack(">HHII", pay[:12])
        hl = (pay[12] >> 4) * 4
        fl = pay[13]
        out["tcp"] = {"seq": seq, "ack": ack, "flags": "".join(n for b, n in ((0x02, "S"), (0x10, "A"), (0x01, "F"), (0x04, "R"), (0x08, "P")) if fl & b)}
        out["payload"] = pay[hl:]
        out["layers"].append("tcp")
    elif proto in (1, 58):
        out["layers"].append("icmp" if proto == 1 else "icmpv6")
    return out


SIP_METHODS = (b"INVITE", b"ACK", b"BYE", b"CANCEL", b"OPTIONS", b"REGISTER", b"PRACK", b"SUBSCRIBE", b"NOTIFY", b"PUBLISH", b"INFO", b"REFER", b"MESSAGE", b"UPDATE")


def sip(payload):
    if not payload or not (payload.startswith(b"SIP/2.0 ") or payload.split(b" ", 1)[0] in SIP_METHODS):
        return None
    head, _, body = payload.partition(b"\r\n\r\n")
    lines = head.decode("utf-8", "replace").split("\r\n")
    first = lines[0]
    hdr = {}
    short = {"i": "call-id", "f": "from", "t": "to", "v": "via", "m": "contact", "l": "content-length", "c": "content-type"}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            k = k.strip().lower()
            hdr.setdefault(short.get(k, k), v.strip())
    r = {"first": first, "headers": hdr, "body": body.decode("utf-8", "replace")}
    if first.startswith("SIP/2.0"):
        parts = first.split(" ", 2)
        r["status"], r["reason"] = int(parts[1]), parts[2] if len(parts) > 2 else ""
    else:
        r["method"] = first.split(" ", 1)[0]
    if "application/sdp" in hdr.get("content-type", "").lower():
        r["sdp"] = sdp(r["body"])
    return r


def sdp(text):
    s = {"media": []}
    for ln in text.splitlines():
        if ln.startswith("c=IN "):
            s["conn"] = ln.split()[-1]
        elif ln.startswith("m="):
            p = ln[2:].split()
            s["media"].append({"type": p[0], "port": int(p[1]), "proto": p[2], "fmts": p[3:], "rtpmap": {}})
        elif ln.startswith("a=rtpmap:") and s["media"]:
            k, v = ln[9:].split(" ", 1)
            s["media"][-1]["rtpmap"][k] = v
        elif ln.startswith("o="):
            s["origin"] = ln[2:]
    return s


def rtp(payload):
    if len(payload) < 12 or payload[0] >> 6 != 2:
        return None
    pt = payload[1] & 0x7F
    if 72 <= pt <= 76:          # RTCP packet types 200-204 fall here when masked
        return None
    seq, ts, ssrc = struct.unpack(">HII", payload[2:12])
    return {"pt": pt, "marker": bool(payload[1] & 0x80), "seq": seq, "ts": ts, "ssrc": ssrc, "cc": payload[0] & 0x0F, "size": len(payload)}


def rtcp(payload):
    if len(payload) < 8 or payload[0] >> 6 != 2 or not 200 <= payload[1] <= 204:
        return None
    return {"pt": payload[1], "ssrc": struct.unpack(">I", payload[4:8])[0], "type": {200: "SR", 201: "RR", 202: "SDES", 203: "BYE", 204: "APP"}[payload[1]]}
