"""tools/lancorrelate.py on a synthetic capture: it must find the one local packet sent at each ring."""

import ipaddress
import struct
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "lancorrelate.py"
DEVICE, DEVICE_MAC = "192.168.1.50", bytes.fromhex("02aabbccdd01")
ROUTER, ROUTER_MAC = "192.168.1.1", bytes.fromhex("02aabbccdd02")
CLOUD = "203.0.113.5"
T0 = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc).timestamp()


def ip4(src, dst, proto, transport) -> bytes:
    hdr = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(transport), 0, 0, 64, proto, 0,
                      ipaddress.IPv4Address(src).packed, ipaddress.IPv4Address(dst).packed)
    return hdr + transport


def eth(src_mac, dst_mac, payload) -> bytes:
    return dst_mac + src_mac + b"\x08\x00" + payload


def udp(src, dst, sport, dport, body, src_mac=DEVICE_MAC, dst_mac=b"\xff" * 6) -> bytes:
    return eth(src_mac, dst_mac, ip4(src, dst, 17, struct.pack(">HHHH", sport, dport, 8 + len(body), 0) + body))


def tcp(src, dst, sport, dport, body, src_mac=DEVICE_MAC, dst_mac=ROUTER_MAC) -> bytes:
    return eth(src_mac, dst_mac, ip4(src, dst, 6, struct.pack(">HHIIBBHHH", sport, dport, 0, 0, 0x50, 0x18, 0, 0, 0) + body))


def client_hello(name: str) -> bytes:
    sni = name.encode()
    ext = struct.pack(">HHHBH", 0, len(sni) + 5, len(sni) + 3, 0, len(sni)) + sni
    hello = b"\x03\x03" + b"\x00" * 32 + b"\x00" + b"\x00\x02\x13\x01" + b"\x01\x00" + struct.pack(">H", len(ext)) + ext
    hs = b"\x01" + len(hello).to_bytes(3, "big") + hello
    return b"\x16\x03\x01" + struct.pack(">H", len(hs)) + hs


def dns_response(name: str, addr: str) -> bytes:
    qname = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
    return (struct.pack(">HHHHHH", 1, 0x8180, 1, 1, 0, 0) + qname + struct.pack(">HH", 1, 1)
            + b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + ipaddress.IPv4Address(addr).packed)


def write_pcap(path: Path, frames) -> None:
    out = [struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)]
    for ts, frame in sorted(frames, key=lambda f: f[0]):
        out.append(struct.pack("<IIII", int(ts), int(ts % 1 * 1e6), len(frame), len(frame)) + frame)
    path.write_bytes(b"".join(out))


def test_the_local_packet_at_each_ring_is_the_candidate(tmp_path: Path) -> None:
    rings = [T0 + 1200, T0 + 3000, T0 + 5400]
    frames = [
        (T0 + 1, udp(ROUTER, DEVICE, 53, 40000, dns_response("dev.example.com", CLOUD),
                     src_mac=ROUTER_MAC, dst_mac=DEVICE_MAC)),
        (T0 + 2, tcp(DEVICE, CLOUD, 50000, 443, client_hello("dev.example.com"))),
        (T0 + 0.6, tcp(DEVICE, CLOUD, 50001, 6800, b"\x9e\xba\xac\xe9" + b"x" * 40)),
    ]
    for t in range(0, 7200, 30):
        frames.append((T0 + t + 0.5, udp(DEVICE, "255.255.255.255", 50162, 50162, b"h" * 100)))
    for t in range(0, 7200, 60):
        frames.append((T0 + t + 0.7, tcp(DEVICE, CLOUD, 50001, 6800, b"k" * 32)))
    for r in rings:
        frames.append((r + 0.3, udp(DEVICE, "255.255.255.255", 6000, 6000, b"r" * 64)))
        frames.append((r + 1.0, tcp(DEVICE, CLOUD, 50001, 6800, b"e" * 300)))
    cap = tmp_path / "day.pcap"
    write_pcap(cap, frames)
    berlin = ZoneInfo("Europe/Berlin")
    notes = tmp_path / "notes.txt"
    notes.write_text("".join(
        datetime.fromtimestamp(r, berlin).strftime("%Y-%m-%d %H:%M:%S") + " ring\n" for r in rings))

    out = subprocess.run([sys.executable, "-I", str(SCRIPT), str(cap), "--device", DEVICE,
                          "--events", str(notes)], capture_output=True, text=True, check=True).stdout

    assert "TLS dev.example.com" in out
    assert "not TLS, starts 9ebaace9" in out
    candidates = [line for line in out.splitlines() if "LOCAL CANDIDATE" in line]
    assert len(candidates) == 1 and "udp/6000 len=72" in candidates[0], out


def test_ethernet_padding_is_not_payload(tmp_path: Path) -> None:
    """A bare ACK in a 60-byte frame carries zero padding; it must not look like a first payload."""
    ack = tcp(DEVICE, CLOUD, 50001, 6800, b"")
    ack += b"\x00" * (60 - len(ack))
    first = tcp(DEVICE, CLOUD, 50001, 6800, b"\x24\x0b\x01\x2e" + b"x" * 30)
    cap = tmp_path / "pad.pcap"
    write_pcap(cap, [(T0, ack), (T0 + 1, first)])
    out = subprocess.run([sys.executable, "-I", str(SCRIPT), str(cap), "--device", DEVICE],
                         capture_output=True, text=True, check=True).stdout
    assert "not TLS, starts 240b012e" in out, out


def test_the_fritzbox_modified_pcap_format_is_read(tmp_path: Path) -> None:
    """The FRITZ!Box writes magic a1b2cd34 (big endian): 8 more bytes in every packet header."""
    frames = [(T0 + i, tcp(DEVICE, CLOUD, 50001, 6800, b"\x24\x0b\x01\x2e" + b"x" * 20)) for i in range(3)]
    out = [struct.pack(">IHHiIII", 0xA1B2CD34, 2, 4, 0, 0, 2048, 1)]
    for ts, frame in frames:
        out.append(struct.pack(">IIIIIHBB", int(ts), 0, len(frame), len(frame), 0, 0x0800, 0, 0) + frame)
    cap = tmp_path / "fritz.eth"
    cap.write_bytes(b"".join(out))
    text = subprocess.run([sys.executable, "-I", str(SCRIPT), str(cap), "--device", DEVICE],
                          capture_output=True, text=True, check=True).stdout
    assert "3 packets of the unit" in text and "not TLS, starts 240b012e" in text, text
    listing = subprocess.run([sys.executable, "-I", str(SCRIPT), str(cap)],
                             capture_output=True, text=True, check=True).stdout
    assert DEVICE in listing and "1 local hosts" in listing, listing
