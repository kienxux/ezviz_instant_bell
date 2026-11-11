"""H.264 / HEVC NAL unit helpers: find keyframes and parameter sets, tell the codec apart."""
# Adapted for EZVIZ HP7 / CP7 Video Intercom from the ezviz_hp7 integration by Bobsilvio
# (MIT, https://github.com/Bobsilvio/ezviz_hp7); see THIRD_PARTY_LICENSES.md.
# The local-network video protocol and the motion-JPEG view are by albrzmr (MIT).
from __future__ import annotations

from typing import Optional


def _iter_nal_spans(data: bytes):
    """Yield (start_code_offset, header_byte_offset) for every NAL.

    Same scan as _iter_nal_types, but keeps the offsets so callers can cut
    the payload at NAL boundaries.
    """
    n = len(data)
    i = 0
    while i < n - 3:
        if data[i] == 0 and data[i + 1] == 0:
            if data[i + 2] == 1:
                if i + 3 < n:
                    yield i, i + 3
                i += 4
                continue
            if data[i + 2] == 0 and i + 3 < n and data[i + 3] == 1:
                if i + 4 < n:
                    yield i, i + 4
                i += 5
                continue
        i += 1


def _iter_nal_types(data: bytes):
    """Yield raw NAL header bytes (the byte right after each start code).

    Handles both 3-byte (00 00 01) and 4-byte (00 00 00 01) start codes.
    """
    n = len(data)
    i = 0
    while i < n - 3:
        if data[i] == 0 and data[i + 1] == 0:
            if data[i + 2] == 1:
                if i + 3 < n:
                    yield data[i + 3]
                i += 4
                continue
            if data[i + 2] == 0 and i + 3 < n and data[i + 3] == 1:
                if i + 4 < n:
                    yield data[i + 4]
                i += 5
                continue
        i += 1


# Parameter-set start markers: HEVC VPS/SPS/PPS with nuh_layer_id=0 and
# temporal_id_plus1=1 (so the second byte pins them down and can't collide
# with an H.264 header), H.264 SPS/PPS. Both 4- and 3-byte start codes.
_PARAM_SET_MARKERS = (
    b"\x00\x00\x00\x01\x40\x01", b"\x00\x00\x01\x40\x01",   # HEVC VPS
    b"\x00\x00\x00\x01\x42\x01", b"\x00\x00\x01\x42\x01",   # HEVC SPS
    b"\x00\x00\x00\x01\x44\x01", b"\x00\x00\x01\x44\x01",   # HEVC PPS
    b"\x00\x00\x00\x01\x67", b"\x00\x00\x01\x67",           # H.264 SPS
    b"\x00\x00\x00\x01\x68", b"\x00\x00\x01\x68",           # H.264 PPS
)

# Random-access points — a frame a decoder can start on. HEVC BLA (16-18),
# IDR_W_RADL (19), IDR_N_LP (20) and CRA (21); H.264 IDR (5). These are
# what actually reset the reference picture set, and firmware does NOT
# necessarily precede every one of them with parameter sets (#51): the
# HPD7 on V5.4.0 repeats VPS/SPS/PPS far less often than it emits IDRs.
_RAP_MARKERS = (
    b"\x00\x00\x00\x01\x20\x01", b"\x00\x00\x01\x20\x01",   # HEVC BLA_W_LP
    b"\x00\x00\x00\x01\x22\x01", b"\x00\x00\x01\x22\x01",   # HEVC BLA_W_RADL
    b"\x00\x00\x00\x01\x24\x01", b"\x00\x00\x01\x24\x01",   # HEVC BLA_N_LP
    b"\x00\x00\x00\x01\x26\x01", b"\x00\x00\x01\x26\x01",   # HEVC IDR_W_RADL
    b"\x00\x00\x00\x01\x28\x01", b"\x00\x00\x01\x28\x01",   # HEVC IDR_N_LP
    b"\x00\x00\x00\x01\x2a\x01", b"\x00\x00\x01\x2a\x01",   # HEVC CRA
    b"\x00\x00\x00\x01\x65", b"\x00\x00\x01\x65",           # H.264 IDR
)

# Kept for the fan-out resync path, which only needs "can a viewer join
# here" — parameter sets and random-access points both qualify.
_KEYFRAME_MARKERS = _PARAM_SET_MARKERS + _RAP_MARKERS


def _has_keyframe(payload: bytes) -> bool:
    return any(m in payload for m in _KEYFRAME_MARKERS)


def _first_keyframe(payload: bytes) -> int:
    """Offset of the first parameter set or random-access NAL, -1 if there is none."""
    found = [i for i in (payload.find(m) for m in _KEYFRAME_MARKERS) if i >= 0]
    return min(found) if found else -1


def _has_param_sets(payload: bytes) -> bool:
    return any(m in payload for m in _PARAM_SET_MARKERS)


def _extract_param_sets(payload: bytes) -> bytes:
    """Return the parameter-set NALs in ``payload``, framing included.

    Slices from the first parameter set up to the start of the first NAL
    that isn't one, so the result is a self-contained Annex B prefix a
    decoder can be initialised with.
    """
    spans = [(off, payload[hdr]) for off, hdr in _iter_nal_spans(payload)]
    start = None
    for off, hdr in spans:
        is_ps = hdr in (0x40, 0x42, 0x44, 0x67, 0x68)
        if is_ps and start is None:
            start = off
        elif not is_ps and start is not None:
            return payload[start:off]
    return payload[start:] if start is not None else b""


def _sniff_video_codec(payload: bytes) -> Optional[str]:
    """Best-effort H.264 vs H.265 detection from raw NAL header bytes.

    We only trust the unambiguous parameter-set headers, matched as exact
    bytes (nuh_layer_id=0), to avoid false positives — e.g. an H.264
    P-slice header 0x41 would otherwise decode as HEVC nal_type 32 under a
    naive (byte>>1)&0x3F.

    HEVC param sets: VPS=0x40, SPS=0x42, PPS=0x44.
    H.264 param sets / IDR: SPS=0x67, PPS=0x68, IDR=0x65.

    Some firmware (#33) never emits parameter sets at all; for those this
    returns None and the caller falls back to the configured default.
    """
    for nal in _iter_nal_types(payload):
        if nal in (0x40, 0x42, 0x44):
            return "hevc"
        if nal in (0x67, 0x68, 0x65):
            return "h264"
    return None
