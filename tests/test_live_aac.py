"""The sound of an encrypted live stream: decrypted, left alone or replaced by silence (#22)."""

from Crypto.Cipher import AES
from live_aac import SETTLE_FRAMES, AacDecryptor, _reads_as_aac

KEY = "ABCDEF"
# Frames encrypted with this key read as AAC neither as they are nor decrypted with KEY.
WRONG = "WRONG1"
# Start of a real AAC-LC 16 kHz mono frame body: an SCE with a long window.
MONO = bytes.fromhex("00 d4 35 a8") + bytes(range(60))
# A CPE without common window, as a stereo frame starts.
STEREO = bytes.fromhex("20 64 00 01 90 00 0e") + bytes(range(57))


def adts(body: bytes, channels: int = 1) -> bytes:
    """An ADTS frame around ``body``: AAC-LC, 16 kHz, no CRC."""
    size = 7 + len(body)
    return bytes((
        0xFF, 0xF1, 0x60 | (channels >> 2), ((channels & 0x03) << 6) | (size >> 11),
        (size >> 3) & 0xFF, ((size & 0x07) << 5) | 0x1F, 0xFC,
    )) + body


def encrypted(frame: bytes, key: str = KEY) -> bytes:
    """``frame`` as the door station sends it with encryption on: the whole 16-byte blocks of the body."""
    end = 7 + (len(frame) - 7) // 16 * 16
    cipher = AES.new(key.encode().ljust(16, b"\0"), AES.MODE_ECB)
    return frame[:7] + cipher.encrypt(frame[7:end]) + frame[end:]


def bits(text: str) -> bytes:
    text += "0" * (-len(text) % 8)
    return int(text, 2).to_bytes(len(text) // 8, "big")


def test_real_frame_starts_read_as_aac_and_encrypted_ones_do_not() -> None:
    assert _reads_as_aac(adts(MONO)) and _reads_as_aac(adts(STEREO, channels=2))
    assert not _reads_as_aac(encrypted(adts(MONO)))
    assert not _reads_as_aac(adts(STEREO))  # a CPE where the header says mono


def test_fill_elements_in_front_of_the_channel_are_skipped() -> None:
    """ffmpeg's encoder puts its name into a fill element every 256 frames."""
    sce = format(int.from_bytes(MONO[:4], "big"), "032b")
    assert _reads_as_aac(adts(bits("110" + "0010" + "0" * 16 + sce) + MONO[4:]))
    assert not _reads_as_aac(adts(bits("110" + "1111" + "1" * 8) + MONO))  # 269 fill bytes: past the end


def test_encrypted_frames_are_decrypted() -> None:
    frame = adts(MONO)
    aac = AacDecryptor(KEY)
    assert aac.feed(encrypted(frame) * 3) == frame * 3
    assert (aac.clear, aac.decrypted, aac.silenced) == (0, 3, 0)


def test_clear_frames_are_left_alone_although_the_video_is_encrypted() -> None:
    frame = adts(STEREO, channels=2)
    aac = AacDecryptor(KEY)
    assert aac.feed(frame * 2) == frame * 2
    assert (aac.clear, aac.decrypted, aac.silenced) == (2, 0, 0)


def test_unreadable_frames_become_silence_of_the_same_kind() -> None:
    """Without sound ffmpeg would not show the picture either."""
    aac = AacDecryptor(KEY)
    assert aac.feed(encrypted(adts(MONO), WRONG)) == adts(bytes.fromhex("00 c8 00 07"))
    assert aac.feed(encrypted(adts(STEREO, channels=2), WRONG)) == adts(
        bytes.fromhex("20 64 00 01 90 00 0e"), channels=2
    )
    assert aac.silenced == 2


def test_once_most_frames_were_unreadable_the_rest_is_silence() -> None:
    frame = adts(MONO)
    aac = AacDecryptor(KEY)
    aac.feed(encrypted(frame, WRONG) * SETTLE_FRAMES)
    assert aac.feed(frame) == adts(bytes.fromhex("00 c8 00 07"))
    assert aac.silenced == SETTLE_FRAMES + 1


def test_an_odd_unreadable_frame_does_not_silence_the_rest() -> None:
    frame = adts(MONO)
    aac = AacDecryptor(KEY)
    aac.feed(encrypted(frame) * SETTLE_FRAMES + encrypted(frame, WRONG))
    assert aac.feed(encrypted(frame)) == frame
    assert (aac.decrypted, aac.silenced) == (SETTLE_FRAMES + 1, 1)


def test_a_frame_cut_into_two_packets_waits_for_its_rest() -> None:
    frame = adts(MONO)
    aac = AacDecryptor(KEY)
    assert aac.feed(encrypted(frame)[:20]) == b""
    assert aac.feed(encrypted(frame)[20:]) == frame


def test_bytes_that_start_no_frame_are_passed_on() -> None:
    frame = adts(MONO)
    assert AacDecryptor(KEY).feed(b"junk\xff\x00" + encrypted(frame)) == b"junk\xff\x00" + frame
