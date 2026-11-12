"""Finding keyframes and parameter sets in the live video, and telling H.264 from HEVC."""

from live_nal import (
    _has_keyframe,
    _has_param_sets,
    _iter_nal_types,
    _param_set_nals,
    _sniff_video_codec,
)

SC4 = b"\x00\x00\x00\x01"
SC3 = b"\x00\x00\x01"

H264_SPS = SC4 + b"\x67\x42\x00\x1f\xaa"
H264_PPS = SC4 + b"\x68\xce\x3c\x80"
H264_IDR = SC4 + b"\x65\x88\x84\x00"
H264_P = SC4 + b"\x41\x9a\x02\x00"

HEVC_VPS = SC4 + b"\x40\x01\x0c\x01"
HEVC_SPS = SC4 + b"\x42\x01\x01\x01"
HEVC_PPS = SC4 + b"\x44\x01\xc1\x72"
HEVC_IDR = SC4 + b"\x26\x01\xaf\x00"
HEVC_CRA = SC3 + b"\x2a\x01\xaf\x00"
HEVC_TRAIL = SC4 + b"\x02\x01\xd0\x00"


def test_nal_headers_are_found_after_both_kinds_of_start_code() -> None:
    data = SC4 + b"\x67\xaa" + SC3 + b"\x68\xbb" + SC4 + b"\x65\xcc"
    assert list(_iter_nal_types(data)) == [0x67, 0x68, 0x65]


def test_a_start_code_at_the_very_end_has_no_header() -> None:
    assert list(_iter_nal_types(H264_IDR + SC4)) == [0x65]


def test_hevc_is_recognised_by_its_parameter_sets() -> None:
    assert _sniff_video_codec(HEVC_VPS + HEVC_SPS + HEVC_PPS + HEVC_IDR) == "hevc"
    assert _sniff_video_codec(SC3 + b"\x42\x01\x01") == "hevc"


def test_h264_is_recognised_by_its_parameter_sets_or_idr() -> None:
    assert _sniff_video_codec(H264_SPS + H264_PPS + H264_IDR) == "h264"
    assert _sniff_video_codec(H264_P + H264_IDR) == "h264"


def test_an_h264_p_slice_is_not_taken_for_hevc() -> None:
    """0x41 reads as HEVC type 32 (VPS) under a naive decode; it must give no answer."""
    assert _sniff_video_codec(H264_P * 3) is None
    assert _sniff_video_codec(HEVC_TRAIL) is None
    assert _sniff_video_codec(b"no start codes here") is None


def test_keyframes_of_both_codecs_are_seen() -> None:
    for chunk in (H264_SPS, H264_IDR, HEVC_VPS, HEVC_IDR, HEVC_CRA, b"junk" + H264_IDR):
        assert _has_keyframe(chunk), chunk


def test_ordinary_frames_are_not_keyframes() -> None:
    assert not _has_keyframe(H264_P + H264_P)
    assert not _has_keyframe(HEVC_TRAIL)
    assert not _has_keyframe(b"")


def test_an_idr_alone_carries_no_parameter_sets() -> None:
    assert not _has_param_sets(H264_IDR)
    assert not _has_param_sets(HEVC_IDR + HEVC_CRA)
    assert _has_param_sets(HEVC_IDR + HEVC_PPS)


def params(payload: bytes) -> bytes:
    return b"".join(_param_set_nals(payload).values())


def test_parameter_sets_are_cut_out_with_their_framing() -> None:
    assert params(HEVC_VPS + HEVC_SPS + HEVC_PPS + HEVC_IDR + HEVC_TRAIL) == HEVC_VPS + HEVC_SPS + HEVC_PPS
    assert params(H264_P + H264_SPS + H264_PPS + H264_IDR) == H264_SPS + H264_PPS


def test_parameter_sets_at_the_end_of_a_chunk_are_kept_whole() -> None:
    assert params(H264_P + H264_SPS + H264_PPS) == H264_SPS + H264_PPS


def test_parameter_sets_in_packets_of_their_own_are_found() -> None:
    # The local MPEG-PS can give each NAL a video packet of its own.
    header = b"\x00\x00\x01\xe0\x00\x20\x80\x00\x00"
    found = _param_set_nals(header + HEVC_VPS + header + HEVC_SPS + header + HEVC_PPS + header + HEVC_IDR)
    assert found == {0x40: HEVC_VPS, 0x42: HEVC_SPS, 0x44: HEVC_PPS}


def test_no_parameter_sets_gives_nothing() -> None:
    assert _param_set_nals(H264_IDR + H264_P) == {}
    assert _param_set_nals(b"") == {}
