"""The AAC sound of an encrypted live stream: decrypt it, or replace it with silence (#22).

Pure Python (no Home Assistant imports). With Image/Video Encryption on, the
HP7 on firmware 5.4 encrypts the sound as well as the picture. The ADTS header
of each frame stays readable; the frame body after it is AES-ECB encrypted
with the camera key in whole 16-byte blocks, and a shorter tail stays clear
(the scheme pyezvizapi 2.0 uses for the SD-card playback of EZVIZ cameras).
ffmpeg cannot decode a single such frame, and as long as it has decoded no
audio frame it writes no output at all, so the picture never showed either.

A frame that already reads as AAC is passed on as it is, so clear sound is
never decrypted just because the video is encrypted. A frame that reads as AAC
neither as it is nor decrypted becomes a silent frame: the live view then
shows the picture without sound instead of nothing.
"""

from __future__ import annotations

from Crypto.Cipher import AES

# A silent AAC raw data block per channel configuration: one channel element
# (SCE for mono, CPE without common window for stereo) with global_gain 100, a
# long window, no scale factor bands and no pulse, TNS or gain control data,
# then END. ffmpeg decodes them to digital silence.
_SILENCE = {
    1: bytes.fromhex("00 c8 00 07"),
    2: bytes.fromhex("20 64 00 01 90 00 0e"),
}

# Largest max_sfb of a long window at any sample rate (32 kHz has 51 bands).
_MAX_SFB_LONG = 51

# Syntactic element ids of a raw data block.
_SCE, _CPE, _DSE, _FIL = 0, 1, 4, 6

# After this many frames (about 6 s) the way the sound is handled is settled:
# when most of them were unreadable, the rest is silence too, since a frame
# that still passes the check is then chance and not sound.
SETTLE_FRAMES = 100


def _header_size(frame: bytes) -> int:
    """7 bytes, or 9 when the header carries a CRC."""
    return 7 if frame[1] & 0x01 else 9


def _frame_size(data: bytes | bytearray, i: int) -> int | None:
    """Size of the ADTS frame at ``i``, header included; None if no frame starts there."""
    if data[i] != 0xFF or (data[i + 1] & 0xF6) != 0xF0:
        return None
    size = ((data[i + 3] & 0x03) << 11) | (data[i + 4] << 3) | (data[i + 5] >> 5)
    return size if size > _header_size(data[i:i + 2]) else None


def _channels(frame: bytes) -> int:
    """The channel configuration of the ADTS header."""
    return ((frame[2] & 0x01) << 2) | (frame[3] >> 6)


class _Bits:
    """Reads ``data`` bit by bit, most significant first; past the end raises IndexError."""

    def __init__(self, data: bytes) -> None:
        self._value = int.from_bytes(data, "big")
        self._size = len(data) * 8
        self.pos = 0

    def take(self, count: int) -> int:
        self.pos += count
        if self.pos > self._size:
            raise IndexError("end of frame")
        return (self._value >> (self._size - self.pos)) & ((1 << count) - 1)


def _reads_as_aac(frame: bytes) -> bool:
    """Whether the frame body starts like real AAC: a cheap check, not a decoder.

    Fill and data elements in front are skipped (ffmpeg's encoder writes its
    name into one every 256 frames). The first channel element must be the one
    the channel configuration calls for (a CPE for stereo, otherwise an SCE),
    with a clear ics_info reserved bit, a max_sfb that fits a long window and,
    for AAC-LC, no prediction. Random bytes pass about once in 20 frames.
    """
    bits = _Bits(frame[_header_size(frame):])
    try:
        element = bits.take(3)
        while element in (_FIL, _DSE):
            if element == _FIL:
                count = bits.take(4)
                if count == 15:
                    count += bits.take(8) - 1
            else:
                bits.take(4)  # instance tag
                aligned = bits.take(1)
                count = bits.take(8)
                if count == 255:
                    count += bits.take(8)
                if aligned:
                    bits.pos = (bits.pos + 7) // 8 * 8
            bits.pos += 8 * count
            element = bits.take(3)
        if element != (_CPE if _channels(frame) == 2 else _SCE):
            return False
        bits.take(4)  # instance tag
        if element == _SCE or not bits.take(1):
            bits.take(8)  # global_gain; a CPE with a common window has none here
        if bits.take(1):  # ics_info reserved bit
            return False
        if bits.take(2) == 2:
            return True  # eight short windows: a 4-bit max_sfb, no prediction flag
        bits.take(1)  # window shape
        if bits.take(6) > _MAX_SFB_LONG:
            return False
        return not (frame[2] >> 6 == 1 and bits.take(1))  # AAC-LC has no prediction
    except IndexError:
        return False


def _silence(frame: bytes) -> bytes:
    """A silent frame with the sample rate and channels of ``frame``; other layouts stay as they are."""
    block = _SILENCE.get(_channels(frame))
    if block is None:
        return frame
    header = bytearray(frame[:7])
    header[1] |= 0x01  # no CRC
    size = 7 + len(block)
    header[3] = (header[3] & 0xFC) | (size >> 11)
    header[4] = (size >> 3) & 0xFF
    header[5] = ((size & 0x07) << 5) | 0x1F  # buffer fullness 0x7FF: variable bit rate
    header[6] = 0xFC  # one raw data block
    return bytes(header) + block


class AacDecryptor:
    """Make the AAC frames of an encrypted stream readable; feed it the audio PES payloads in order."""

    def __init__(self, key: str) -> None:
        # ECB with the key zero-padded to 16 bytes, as the door station
        # encrypts: compatibility with its media format, not a choice of mode.
        self._cipher = AES.new(key.encode().ljust(16, b"\0")[:16], AES.MODE_ECB)
        self._buf = bytearray()
        # Which way the last frame went; decides a frame that reads as AAC
        # both as it is and decrypted.
        self._decrypting = False
        self.clear = self.decrypted = self.silenced = 0

    @property
    def frames(self) -> int:
        return self.clear + self.decrypted + self.silenced

    def feed(self, data: bytes) -> bytes:
        """The whole frames so far, made readable; a frame cut off at the end waits for its rest."""
        buf = self._buf
        buf += data
        out = bytearray()
        i = 0
        while len(buf) - i >= 7:
            size = _frame_size(buf, i)
            if size is None:
                # Not a frame start: pass the bytes on up to the next possible
                # one, as ffmpeg would skip them too.
                nxt = buf.find(b"\xff", i + 1)
                nxt = len(buf) if nxt < 0 else nxt
                out += buf[i:nxt]
                i = nxt
                continue
            if len(buf) - i < size:
                break
            out += self._frame(bytes(buf[i:i + size]))
            i += size
        del buf[:i]
        return bytes(out)

    def _frame(self, frame: bytes) -> bytes:
        if self.frames >= SETTLE_FRAMES and self.silenced * 2 > self.frames:
            self.silenced += 1
            return _silence(frame)
        header = _header_size(frame)
        end = header + (len(frame) - header) // 16 * 16
        decrypted = frame[:header] + self._cipher.decrypt(frame[header:end]) + frame[end:]
        as_is, after = _reads_as_aac(frame), _reads_as_aac(decrypted)
        if as_is and not (after and self._decrypting):
            self._decrypting = False
            self.clear += 1
            return frame
        if after:
            self._decrypting = True
            self.decrypted += 1
            return decrypted
        self.silenced += 1
        return _silence(frame)
