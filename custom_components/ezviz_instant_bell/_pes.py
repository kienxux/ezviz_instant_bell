"""Incremental MPEG-PS PES splitter.

Used to split the VTM stream's MPEG-PS bytestream into per-stream_id payloads
so the video (H.264, stream_id 0xE0) and audio (AAC ADTS, stream_id 0xC0)
can be fed separately to ffmpeg — the standard ffmpeg mpeg demuxer
mis-identifies the audio PID as MP2 and rejects every packet.

Usage:

    parser = PesParser()
    while True:
        chunk = source.read()
        for stream_id, payload in parser.feed(chunk):
            ...

The parser keeps an internal byte buffer and yields exactly one
(stream_id, payload) tuple per complete PES packet. Pack headers (0xBA),
system headers (0xBB) and padding (0xBE) are silently consumed.
"""
# Adapted for EZVIZ HP7 / CP7 Video Intercom from the ezviz_hp7 integration by Bobsilvio
# (MIT, https://github.com/Bobsilvio/ezviz_hp7); see THIRD_PARTY_LICENSES.md.
from __future__ import annotations

from typing import List, Tuple


# stream_ids that carry an optional PES header (PTS/DTS/etc) before the
# payload. Outside this range the payload starts immediately after the 6-byte
# length field.
def _has_optional_header(stream_id: int) -> bool:
    if 0xC0 <= stream_id <= 0xEF:
        return True
    return stream_id in (0xBD, 0xFD)


# Headers we recognise as "container" framing rather than actual PES packets —
# we want to consume them and move on without yielding.
def _is_container_marker(stream_id: int) -> bool:
    # 0xBE = padding stream; 0xBF = private_stream_2;
    # 0xF0..0xF2, 0xF8 = ECM / EMM / DSMCC; 0xFF = program_stream_directory.
    return stream_id in (0xBE, 0xBF, 0xF0, 0xF1, 0xF2, 0xF8, 0xFF)


class PesParser:
    """Stateful PES splitter; call feed() with each new chunk of bytes."""

    def __init__(self) -> None:
        self._buf = bytearray()
        # diagnostics
        self.packets_yielded = 0
        # Count video PES packets whose PES_scrambling_control is non-zero.
        # The device sets it when Image/Video Encryption is enabled: the
        # container and NAL framing stay readable but the payloads are
        # scrambled, so ffmpeg spins on garbage and it looks exactly like a
        # demux bug (#41 — three weeks to identify). Detecting it lets us
        # tell the user plainly instead.
        self.scrambled_packets = 0
        self.resync_drops = 0

    def feed(self, data: bytes) -> List[Tuple[int, bytes]]:
        """Append data and return every complete (stream_id, payload) tuple."""
        if data:
            self._buf += data
        out: List[Tuple[int, bytes]] = []
        while True:
            consumed = self._step(out)
            if consumed == 0:
                break
        return out

    def _step(self, out: List[Tuple[int, bytes]]) -> int:
        """Try to advance one packet. Returns bytes consumed (0 = need more)."""
        buf = self._buf
        if len(buf) < 6:
            return 0

        # Resync to a start-code prefix.
        if not (buf[0] == 0 and buf[1] == 0 and buf[2] == 1):
            idx = buf.find(b"\x00\x00\x01")
            if idx < 0:
                # Keep the last 2 bytes in case a start code straddles the
                # boundary; drop the rest.
                drop = len(buf) - 2
                if drop > 0:
                    self.resync_drops += drop
                    del buf[:drop]
                return 0
            self.resync_drops += idx
            del buf[:idx]
            return idx

        stream_id = buf[3]

        # MPEG-PS pack header (0xBA): 14 bytes + (last 3 bits of byte 13) of
        # stuffing bytes.
        if stream_id == 0xBA:
            if len(buf) < 14:
                return 0
            stuffing = buf[13] & 0x07
            total = 14 + stuffing
            if len(buf) < total:
                return 0
            del buf[:total]
            return total

        # MPEG-PS system header (0xBB): 16-bit length follows.
        if stream_id == 0xBB:
            length = (buf[4] << 8) | buf[5]
            total = 6 + length
            if len(buf) < total:
                return 0
            del buf[:total]
            return total

        # Other container markers — consume and ignore.
        if _is_container_marker(stream_id):
            length = (buf[4] << 8) | buf[5]
            total = 6 + length
            if len(buf) < total:
                return 0
            del buf[:total]
            return total

        # MPEG-PS program_end_code (0xB9): exactly 4 bytes total.
        if stream_id == 0xB9:
            del buf[:4]
            return 4

        # Real PES packet.
        pes_length = (buf[4] << 8) | buf[5]

        if pes_length == 0:
            # Unbounded packet (only video). Find the next start code; it
            # marks the end of this packet's payload.
            idx = buf.find(b"\x00\x00\x01", 6)
            if idx < 0:
                # Need more data before we can close the packet.
                return 0
            payload_offset = 6
            if _has_optional_header(stream_id):
                if len(buf) > 6 and (buf[6] >> 4) & 0x03:
                    self.scrambled_packets += 1
                if idx < 9:
                    del buf[:idx]
                    return idx
                header_data_len = buf[8]
                payload_offset = 9 + header_data_len
                if payload_offset > idx:
                    payload_offset = idx
            payload = bytes(buf[payload_offset:idx])
            out.append((stream_id, payload))
            self.packets_yielded += 1
            del buf[:idx]
            return idx

        total = 6 + pes_length
        if len(buf) < total:
            return 0

        payload_offset = 6
        if _has_optional_header(stream_id):
            if len(buf) > 6 and (buf[6] >> 4) & 0x03:
                self.scrambled_packets += 1
            if total < 9:
                del buf[:total]
                return total
            header_data_len = buf[8]
            payload_offset = 9 + header_data_len
            if payload_offset > total:
                payload_offset = total
        payload = bytes(buf[payload_offset:total])
        out.append((stream_id, payload))
        self.packets_yielded += 1
        del buf[:total]
        return total


def keep_clear_video_pes(original: bytes, decrypted: bytes) -> bytes:
    """Undo the decryption of video PES packets that were not scrambled.

    The HP7 on firmware 5.4 scrambles only some video PES packets (the ones
    with keyframes and parameter sets) and marks them with
    PES_scrambling_control; the others are sent in the clear (#22). The
    decryption works on every NAL, so it garbles those. ``original`` must be
    complete packets, as handed to the decryption; ``decrypted`` the same
    length.
    """
    if len(original) != len(decrypted):
        return decrypted
    out = bytearray(decrypted)
    i = 0
    n = len(original)
    while i + 6 <= n:
        if original[i:i + 3] != b"\x00\x00\x01":
            idx = original.find(b"\x00\x00\x01", i + 1)
            if idx < 0:
                break
            i = idx
            continue
        stream_id = original[i + 3]
        if stream_id == 0xBA:
            if i + 14 > n:
                break
            i += 14 + (original[i + 13] & 0x07)
            continue
        if stream_id == 0xB9:
            i += 4
            continue
        length = (original[i + 4] << 8) | original[i + 5]
        if length:
            end = min(n, i + 6 + length)
        else:
            idx = original.find(b"\x00\x00\x01", i + 6)
            end = n if idx < 0 else idx
        if 0xE0 <= stream_id <= 0xEF and i + 6 < end and not (original[i + 6] >> 4) & 0x03:
            out[i:end] = original[i:end]
        i = max(end, i + 4)
    return bytes(out)


PACK_START = b"\x00\x00\x01\xba"


def cut_to_packet_start(buf: bytearray, stalled: bool = False) -> bytes:
    """Remove and return the bytes in front of the first MPEG-PS packet (#22).

    pyezvizapi parses MPEG-PS from the first byte of what it gets. Cloud
    chunks are not cut at packet boundaries, and the key can arrive in the
    middle of a packet, so the buffer may start inside one and would then
    never yield anything to decrypt. The bytes up to the next pack header are
    handed back to be passed on as they are. ``stalled`` means the buffer grew
    without yielding anything, so a start code at its first byte is skipped
    too.
    """
    if len(buf) < 4:
        return b""
    if not stalled and buf[:3] == b"\x00\x00\x01" and 0xBA <= buf[3] <= 0xEF:
        return b""
    start = buf.find(PACK_START, 1)
    if start < 0:
        start = len(buf) - 3  # the last bytes may begin a pack header
    head = bytes(buf[:start])
    del buf[:start]
    return head
