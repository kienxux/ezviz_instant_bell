"""Put a day of the door station's network traffic next to its events.

Usage: python tools/lancorrelate.py CAPTURE --device IP --events FILE [--events FILE ...]

CAPTURE  a pcap or pcapng file, e.g. the FRITZ!Box packet capture of its Wi-Fi
         interface (Ethernet framing). Nothing is sent anywhere; the file is
         only read.
--device the IPv4 address of the unit on the network (with an HP7: the indoor
         monitor). Its MAC address is learned from that, so its IPv6 traffic
         counts too.
--events either notes, one event per line in local time:
             2026-11-14 12:03:15 ring
             2026-11-14 12:10 unlock-app   any comment after the kind
         or the CSV download of Home Assistant's History page
         (entity_id,state,last_changed): every change of an event entity
         counts as one event of the kind <entity_id>.

What it prints:
1. who the unit talks to: each peer with its DNS name when the capture shows
   the lookup, ports, packets, bytes; for each TCP connection whether it is
   TLS (with the server name) or something else (its first 4 bytes);
2. per event: what the unit sent and received around it, local and internet;
3. per kind of event: the packet signatures (direction, peer, protocol, port,
   length) seen around most events of that kind and rarely at other times.
   A local signature seen around every event and almost never otherwise is a
   candidate for a local signal.
"""

import argparse
import bisect
import csv
import ipaddress
import struct
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ETH, RAW, SLL, SLL2 = 1, 101, 113, 276
# Home networks only: is_private would also count documentation and other
# reserved ranges as local.
LOCAL_NETS = [ipaddress.ip_network(n) for n in
              ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")]


def read_frames(path: Path):
    """Yield (timestamp, linktype, frame) from a pcap or pcapng file."""
    data = path.read_bytes()
    magic = data[:4]
    if magic == b"\x0a\x0d\x0d\x0a":
        yield from _pcapng(data)
        return
    for endian in "<>":
        m = struct.unpack(endian + "I", magic)[0]
        if m in (0xA1B2C3D4, 0xA1B23C4D):
            break
    else:
        sys.exit(f"{path}: not a pcap or pcapng file")
    scale = 1e-6 if m == 0xA1B2C3D4 else 1e-9
    linktype = struct.unpack(endian + "I", data[20:24])[0]
    off = 24
    while off + 16 <= len(data):
        sec, frac, incl, _ = struct.unpack(endian + "IIII", data[off:off + 16])
        off += 16
        yield sec + frac * scale, linktype, data[off:off + incl]
        off += incl


def _pcapng(data: bytes):
    off, endian, ifaces = 0, "<", []
    while off + 12 <= len(data):
        btype = struct.unpack(endian + "I", data[off:off + 4])[0]
        if btype == 0x0A0D0D0A:
            endian = "<" if data[off + 8:off + 12] == b"\x4d\x3c\x2b\x1a" else ">"
            ifaces = []
        blen = struct.unpack(endian + "I", data[off + 4:off + 8])[0]
        if blen < 12:
            break
        body = data[off + 8:off + blen - 4]
        if btype == 1:
            linktype = struct.unpack(endian + "H", body[:2])[0]
            resol = 1e-6
            o = 8
            while o + 4 <= len(body):
                code, olen = struct.unpack(endian + "HH", body[o:o + 4])
                if code == 0:
                    break
                if code == 9 and olen >= 1:
                    v = body[o + 4]
                    resol = 2.0 ** -(v & 0x7F) if v & 0x80 else 10.0 ** -v
                o += 4 + (olen + 3) // 4 * 4
            ifaces.append((linktype, resol))
        elif btype == 6 and ifaces:
            iface, hi, lo, cap = struct.unpack(endian + "IIII", body[:16])
            linktype, resol = ifaces[iface] if iface < len(ifaces) else ifaces[0]
            yield ((hi << 32) | lo) * resol, linktype, body[20:20 + cap]
        off += blen


def parse(linktype: int, frame: bytes):
    """(src_mac, dst_mac, ethertype, network payload) or None."""
    if linktype == ETH:
        if len(frame) < 14:
            return None
        dst, src, etype = frame[:6], frame[6:12], struct.unpack(">H", frame[12:14])[0]
        payload = frame[14:]
        while etype in (0x8100, 0x88A8) and len(payload) >= 4:
            etype, payload = struct.unpack(">H", payload[2:4])[0], payload[4:]
        return src, dst, etype, payload
    if linktype == RAW and frame:
        return None, None, 0x0800 if frame[0] >> 4 == 4 else 0x86DD, frame
    if linktype == SLL and len(frame) >= 16:
        return frame[6:12], None, struct.unpack(">H", frame[14:16])[0], frame[16:]
    if linktype == SLL2 and len(frame) >= 20:
        return frame[12:18], None, struct.unpack(">H", frame[:2])[0], frame[20:]
    return None


def ip_layer(etype: int, p: bytes):
    """(src, dst, proto, transport payload) for IPv4 or IPv6, else None."""
    if etype == 0x0800 and len(p) >= 20:
        ihl = (p[0] & 0x0F) * 4
        return (str(ipaddress.IPv4Address(p[12:16])), str(ipaddress.IPv4Address(p[16:20])),
                p[9], p[ihl:])
    if etype == 0x86DD and len(p) >= 40:
        return (str(ipaddress.IPv6Address(p[8:24])), str(ipaddress.IPv6Address(p[24:40])),
                p[6], p[40:])
    return None


def ports(proto: int, t: bytes):
    """(sport, dport, payload) for TCP/UDP."""
    if proto == 6 and len(t) >= 20:
        sport, dport = struct.unpack(">HH", t[:4])
        return sport, dport, t[(t[12] >> 4) * 4:]
    if proto == 17 and len(t) >= 8:
        sport, dport = struct.unpack(">HH", t[:4])
        return sport, dport, t[8:]
    return 0, 0, b""


def tls_server_name(p: bytes):
    """The SNI of a TLS ClientHello, '' for a ClientHello without one, None if no ClientHello."""
    if len(p) < 43 or p[0] != 0x16 or p[1] != 3 or p[5] != 1:
        return None
    try:
        o = 43
        o += 1 + p[o]
        o += 2 + struct.unpack(">H", p[o:o + 2])[0]
        o += 1 + p[o]
        end = o + 2 + struct.unpack(">H", p[o:o + 2])[0]
        o += 2
        while o + 4 <= min(end, len(p)):
            etype, elen = struct.unpack(">HH", p[o:o + 4])
            if etype == 0:
                nlen = struct.unpack(">H", p[o + 7:o + 9])[0]
                return p[o + 9:o + 9 + nlen].decode("ascii", "replace")
            o += 4 + elen
    except (IndexError, struct.error):
        pass
    return ""


def dns_answers(p: bytes):
    """(name, address) pairs of the A/AAAA answers of a DNS response."""
    def name(o):
        labels, jumped, end = [], False, o
        for _ in range(64):
            n = p[o]
            if n == 0:
                return ".".join(labels), (end if jumped else o + 1)
            if n & 0xC0 == 0xC0:
                if not jumped:
                    end = o + 2
                jumped, o = True, ((n & 0x3F) << 8) | p[o + 1]
                continue
            labels.append(p[o + 1:o + 1 + n].decode("ascii", "replace"))
            o += 1 + n
        raise ValueError
    out = []
    try:
        if len(p) < 12 or not p[2] & 0x80:
            return out
        qd, an = struct.unpack(">HH", p[4:8])
        o = 12
        for _ in range(qd):
            o = name(o)[1] + 4
        for _ in range(an):
            n, o = name(o)
            rtype, _, _, rdlen = struct.unpack(">HHIH", p[o:o + 10])
            rdata = p[o + 10:o + 10 + rdlen]
            if rtype == 1 and rdlen == 4:
                out.append((n, str(ipaddress.IPv4Address(rdata))))
            elif rtype == 28 and rdlen == 16:
                out.append((n, str(ipaddress.IPv6Address(rdata))))
            o += 10 + rdlen
    except (IndexError, ValueError, struct.error):
        pass
    return out


def peer_class(ip: str) -> str:
    a = ipaddress.ip_address(ip)
    if a.is_multicast:
        return "multicast"
    if ip == "255.255.255.255" or ip.endswith(".255"):
        return "broadcast"
    if a.is_link_local or any(a in n for n in LOCAL_NETS):
        return "local"
    return "internet"


def load_events(paths, tz: ZoneInfo):
    events = []
    for path in paths:
        text = Path(path).read_text(encoding="utf-8-sig")
        if text.lstrip().startswith("entity_id"):
            for row in csv.DictReader(text.splitlines()):
                if row.get("state") in (None, "", "unknown", "unavailable"):
                    continue
                ts = datetime.fromisoformat(row["last_changed"].replace("Z", "+00:00"))
                events.append((ts.timestamp(), row["entity_id"]))
            continue
        for line in text.splitlines():
            parts = line.split()
            if len(parts) < 3 or line.lstrip().startswith("#"):
                continue
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    ts = datetime.strptime(f"{parts[0]} {parts[1]}", fmt).replace(tzinfo=tz)
                    break
                except ValueError:
                    ts = None
            if ts is None:
                sys.exit(f"{path}: cannot read the time in: {line}")
            events.append((ts.timestamp(), parts[2]))
    return sorted(events)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("capture", type=Path)
    ap.add_argument("--device", required=True, help="IPv4 address of the unit")
    ap.add_argument("--events", action="append", default=[], help="notes or HA history CSV")
    ap.add_argument("--tz", default="Europe/Berlin", help="time zone of the notes")
    ap.add_argument("--before", type=float, default=5.0, help="seconds before an event")
    ap.add_argument("--after", type=float, default=20.0, help="seconds after an event")
    args = ap.parse_args()
    tz = ZoneInfo(args.tz)

    frames = list(read_frames(args.capture))
    if not frames:
        sys.exit("no packets in the capture")
    macs = set()
    for _, lt, fr in frames:
        h = parse(lt, fr)
        ipl = h and ip_layer(h[2], h[3])
        if ipl and h[0] and ipl[0] == args.device:
            macs.add(h[0])
    names = {}
    pkts = []  # (ts, dir, peer, class, proto, port, length, payload)
    unsupported = Counter()
    for ts, lt, fr in frames:
        h = parse(lt, fr)
        if h is None:
            unsupported[lt] += 1
            continue
        src_mac, dst_mac, etype, payload = h
        ipl = ip_layer(etype, payload)
        if ipl is None:
            continue
        src, dst, proto, t = ipl
        if src == args.device or (src_mac and src_mac in macs):
            direction, peer = "out", dst
        elif dst == args.device or (dst_mac and dst_mac in macs):
            direction, peer = "in", src
        else:
            continue
        sport, dport, body = ports(proto, t)
        if proto == 17 and sport == 53:
            for n, a in dns_answers(body):
                names[a] = n
        port = dport if direction == "out" else sport
        pkts.append((ts, direction, peer, peer_class(peer), proto, port, len(t), body, sport, dport))
    if unsupported:
        print(f"!! frames of an unknown link type skipped: {dict(unsupported)} (expected Ethernet)")
    if not pkts:
        sys.exit(f"no packets of {args.device} in the capture")

    t0, t1 = pkts[0][0], pkts[-1][0]
    local = lambda ts: datetime.fromtimestamp(ts, timezone.utc).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
    pname = {6: "tcp", 17: "udp", 1: "icmp", 58: "icmp6"}
    label = lambda peer: names.get(peer, peer)  # noqa: E731
    print(f"{len(pkts)} packets of the unit from {local(t0)} to {local(t1)} "
          f"({(t1 - t0) / 3600:.1f} h), MAC addresses {len(macs)}")

    print("\n== Peers ==")
    peers = defaultdict(lambda: [0, 0, set()])
    for ts, d, peer, cls, proto, port, ln, *_ in pkts:
        p = peers[(cls, peer)]
        p[0] += 1
        p[1] += ln
        p[2].add(f"{pname.get(proto, proto)}/{port}")
    for (cls, peer), (n, b, pts) in sorted(peers.items(), key=lambda kv: -kv[1][1]):
        print(f"  {cls:9} {label(peer):45} packets={n:>7} bytes={b:>10}  {' '.join(sorted(pts)[:6])}")

    print("\n== TCP connections opened by the unit: protocol ==")
    flows = {}
    for ts, d, peer, cls, proto, port, ln, body, sport, dport in pkts:
        if proto != 6 or d != "out" or not body:
            continue
        key = (peer, sport, dport)
        if key in flows:
            continue
        sni = tls_server_name(body)
        flows[key] = ("TLS " + (sni or "(no server name)")) if sni is not None else f"not TLS, starts {body[:4].hex()}"
    kinds = Counter((label(peer), dport, what) for (peer, _, dport), what in flows.items())
    for (peer, dport, what), n in kinds.most_common():
        print(f"  {peer:45} port {dport:<5} {n:>4}x  {what}")

    events = load_events(args.events, tz)
    if not events:
        print("\nno events given (--events): only the overview above")
        return
    print(f"\n== Events ({len(events)}) ==")
    win = args.before + args.after

    def sig(p):
        ts, d, peer, cls, proto, port, ln, *_ = p
        who = label(peer) if cls != "internet" or peer in names else "internet"
        return (d, cls, who, pname.get(proto, proto), port, ln)

    times = [p[0] for p in pkts]

    def window(start):
        i, j = bisect.bisect_left(times, start), bisect.bisect_left(times, start + win)
        return pkts[i:j]

    by_kind = defaultdict(list)
    for ets, kind in events:
        if not t0 <= ets <= t1:
            print(f"  {local(ets)} {kind:20} outside the capture")
            continue
        w = window(ets - args.before)
        loc = [p for p in w if p[3] != "internet"]
        net = [p for p in w if p[3] == "internet"]
        first = next((p[0] - ets for p in w if p[0] >= ets), None)
        print(f"  {local(ets)} {kind:20} local={len(loc):>4} internet={len(net):>5}"
              + (f"  first packet after +{first:.1f}s" if first is not None else ""))
        by_kind[kind].append(set(map(sig, w)))

    # Windows of the same length that touch no event: how often each signature shows up anyway.
    quiet, start = [], t0
    starts = [e[0] for e in events]
    while start + win <= t1:
        near = bisect.bisect_left(starts, start - args.after)
        if near >= len(starts) or starts[near] > start + win + args.before:
            quiet.append(set(map(sig, window(start))))
        start += win
    base = Counter(s for w in quiet for s in w)
    print(f"\n== Signatures around events, against {len(quiet)} quiet windows of {win:.0f} s ==")
    for kind, wins in by_kind.items():
        hits = Counter(s for w in wins for s in w)
        scored = sorted(((h / len(wins) - base[s] / max(len(quiet), 1), h, s) for s, h in hits.items()),
                        reverse=True)[:12]
        print(f"  {kind} ({len(wins)} events)")
        for score, h, s in scored:
            d, cls, who, proto, port, ln = s
            mark = "  <== LOCAL CANDIDATE" if cls != "internet" and h == len(wins) and \
                base[s] <= 0.02 * max(len(quiet), 1) else ""
            print(f"    {h}/{len(wins)} events, {base[s]:>5} quiet  {d:3} {cls:9} {who:40} "
                  f"{proto}/{port} len={ln}{mark}")


if __name__ == "__main__":
    main()
