"""Ask the door station which LAN commands it knows (research aid, admin only).

Two steps, one connection at a time with a pause in between, because the
door station's LAN service stops answering when it gets many connections:

1. Discover: send each command number with a body the device cannot decrypt.
   A known command answers with ``command + 1``; an unknown one closes the
   connection. Nothing can run, because the device cannot read the request.
2. Ask: send each known command once, encrypted with the device key, with an
   empty request (only the placeholder operation code). The decrypted answer
   tells what the command is about.

The key never leaves this module: answers are returned with every value that
looks like a key, password, code or token replaced by ``***``.
"""

from __future__ import annotations

import hashlib
import re
import socket
import struct
import time
from collections.abc import Callable, Iterable

from .lan_client import (
    HEADER_LEN,
    MAGIC,
    PLACEHOLDER_OP_CODE,
    PORT_CTRL,
    _build_packet,
    _parse_response,
    _recv_response,
    check_lan_host,
)

# Commands seen to answer on a CPD5 (0x2011 invite, 0x2013 init, 0x3105 play
# belong to the video; the rest are unknown).
KNOWN_COMMANDS = (0x2007, 0x2009, 0x200B, 0x2011, 0x2013, 0x2015, 0x2017, 0x2023, 0x2025)
MAX_RANGE = 512
DEFAULT_PAUSE = 0.3
MAX_ANSWER_CHARS = 2000

_UNREADABLE_BODY = b"\x00" * 32
_SECRET_NAME = r"[\w:]*(?:key|password|passwd|secret|token|code|ticket|sign)[\w:]*"
_SECRET_ATTR = re.compile(rf'({_SECRET_NAME}\s*=\s*")[^"]*(")', re.IGNORECASE)
_SECRET_TAG = re.compile(rf"(<({_SECRET_NAME})(?:\s[^>]*)?>)[^<]*(</\2>)", re.IGNORECASE)


def redact(text: str) -> str:
    """Replace the values of key-, password-, code- and token-like fields."""
    text = _SECRET_ATTR.sub(r"\1***\2", text)
    return _SECRET_TAG.sub(r"\1***\3", text)


def parse_range(text: str) -> list[int]:
    """``"2000-20ff"`` -> command numbers; at most ``MAX_RANGE`` of them."""
    first, _, last = text.strip().partition("-")
    low = int(first, 16)
    high = int(last or first, 16)
    if not 0 <= low <= high <= 0xFFFF:
        raise ValueError(f"{text!r} is not a range of hex command numbers")
    if high - low + 1 > MAX_RANGE:
        raise ValueError(f"at most {MAX_RANGE} commands per call")
    return list(range(low, high + 1))


def _empty_request() -> bytes:
    return (
        f'<?xml version="1.0" encoding="utf-8"?>\n'
        f"<Request>\n"
        f"\t<OperationCode>{PLACEHOLDER_OP_CODE}</OperationCode>\n"
        f"</Request>\n"
    ).encode()


def _unreadable_packet(cmd: int) -> bytes:
    """The frame of ``_build_packet`` around a body the device cannot decrypt."""
    body = _UNREADABLE_BODY
    header = (
        MAGIC
        + b"\x01\x00\x00\x00"
        + struct.pack(">I", 1)
        + b"\x00\x00\x00\x00"
        + struct.pack(">I", cmd)
        + b"\xff\xff\xff\xff"
        + struct.pack(">I", len(body))
        + b"\x00\x00\x00\x00"
    )
    return header + body + hashlib.md5(body).hexdigest().encode("ascii")  # noqa: S324 - the device's checksum


def _exchange(host: str, packet: bytes, timeout: float) -> bytes | None:
    """One connection, one request. ``None`` when the device closed without answering."""
    with socket.create_connection((host, PORT_CTRL), timeout=timeout) as sock:
        sock.sendall(packet)
        try:
            return _recv_response(sock, timeout=timeout)
        except ConnectionError:
            return None


def discover(
    host: str,
    commands: Iterable[int],
    *,
    pause: float = DEFAULT_PAUSE,
    timeout: float = 3.0,
    exchange: Callable[[str, bytes, float], bytes | None] = _exchange,
    sleep: Callable[[float], None] = time.sleep,
) -> list[int]:
    """The commands of ``commands`` the device answers. Stops when it refuses connections."""
    check_lan_host(host)
    known = []
    for cmd in commands:
        try:
            wire = exchange(host, _unreadable_packet(cmd), timeout)
        except OSError as err:
            raise ConnectionError(f"the door station stopped answering at 0x{cmd:04x} ({err})") from err
        if wire and len(wire) >= HEADER_LEN and struct.unpack(">I", wire[16:20])[0] == cmd + 1:
            known.append(cmd)
        sleep(pause)
    return known


def ask(
    host: str,
    key: bytes,
    commands: Iterable[int],
    *,
    pause: float = DEFAULT_PAUSE,
    timeout: float = 5.0,
    exchange: Callable[[str, bytes, float], bytes | None] = _exchange,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, str]:
    """``{"0x2007": "<redacted answer>", ...}`` for an empty request to each command."""
    check_lan_host(host)
    answers: dict[str, str] = {}
    for cmd in commands:
        name = f"0x{cmd:04x}"
        try:
            wire = exchange(host, _build_packet(1, cmd, _empty_request(), key), timeout)
        except OSError as err:
            answers[name] = f"connection failed: {err}"
            break
        if wire is None:
            answers[name] = "closed without an answer"
        else:
            try:
                meta, plain = _parse_response(wire, key)
            except Exception as err:  # noqa: BLE001 - an unreadable answer is a finding too
                answers[name] = f"unreadable answer: {type(err).__name__}"
            else:
                text = plain.decode("utf-8", errors="replace")
                answers[name] = f"answer 0x{meta['cmd']:04x}: {redact(text)[:MAX_ANSWER_CHARS]}"
        sleep(pause)
    return answers
