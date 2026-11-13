"""The live stream relay without a door station or ffmpeg: retry limits, GOP cache, viewers, sources."""

from __future__ import annotations

import asyncio
import errno
import queue
import threading
import time
from unittest.mock import patch

import pytest

from custom_components.ezviz_instant_bell import cpd7, live_relay
from custom_components.ezviz_instant_bell.live_relay import (
    DEAD_SUB_TIMEOUT,
    PREWARM_IDLE_TIMEOUT,
    LOCKOUT_BACKOFF,
    MIN_RETRY_INTERVAL,
    OFFLINE_MAX_BACKOFF,
    StreamRelay,
    _is_offline_error,
)
from custom_components.ezviz_instant_bell._pes import ps_from_keyframe, ps_from_pack
from custom_components.ezviz_instant_bell.live_nal import _first_keyframe
from custom_components.ezviz_instant_bell.live_sources import Cpd7LanSource, _PreStartedSource

SERIAL = "BE0000001-BE0000002"
SC4 = b"\x00\x00\x00\x01"
SPS_PPS = SC4 + b"\x67\x42\x00\x1f" + SC4 + b"\x68\xce\x3c\x80"
IDR = SC4 + b"\x65\x88\x84\x00"
P = SC4 + b"\x41\x9a\x02\x00"
HEVC_VPS = SC4 + b"\x40\x01\x0c\x01"
PACK = b"\x00\x00\x01\xba\x44\x00\x04\x00\x04\x01\x00\x00\x03\xf8"  # MPEG-2 pack header, no stuffing


class FakeApi:
    def __init__(self, client: object | None = object()) -> None:
        self.client = client


class FakeSource:
    """A cloud or LAN session: hands out ``payloads``, then ends (or waits for close if ``hold``)."""

    def __init__(self, payloads: list[bytes] = (), hold: bool = False, fail_start: Exception | None = None):
        self.payloads = list(payloads)
        self.hold = hold
        self.fail_start = fail_start
        self.started = self.closed = 0
        self._closed = threading.Event()

    def start(self):
        self.started += 1
        if self.fail_start:
            raise self.fail_start
        return self

    def iter_payloads(self):
        yield from self.payloads
        if self.hold:
            self._closed.wait(5)

    def close(self):
        self.closed += 1
        self._closed.set()


def pes(stream_id: int, payload: bytes, scrambled: bool = False) -> bytes:
    """One MPEG-PS PES packet with an empty optional header."""
    body = (b"\xa0" if scrambled else b"\x80") + b"\x00\x00" + payload
    return b"\x00\x00\x01" + bytes((stream_id, len(body) >> 8, len(body) & 0xFF)) + body


def relay(**kwargs) -> StreamRelay:
    return StreamRelay(kwargs.pop("api", FakeApi()), SERIAL, **kwargs)


@pytest.fixture(autouse=True)
def _decide_the_header_size_at_once():
    """The synthetic streams here are far smaller than the 24 KB a real one must show first."""
    with patch.object(live_relay, "DECRYPT_DETECT_MIN_BYTES", 0):
        yield


async def wait_for(condition, seconds: float = 5.0) -> None:
    end = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < end, "condition not reached"
        await asyncio.sleep(0.02)


# --- what counts as "the door station is offline" ---------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError(),
        ConnectionRefusedError(),
        ConnectionResetError(),
        OSError(errno.EHOSTUNREACH, "No route to host"),
        OSError(errno.ENETUNREACH, "unreachable"),
        RuntimeError("Device is offline or unreachable"),
        RuntimeError("Timed out waiting for VTM stream info"),
        RuntimeError("No <Session> in InviteStream response"),
    ],
)
def test_a_door_station_that_does_not_answer_is_offline(exc) -> None:
    assert _is_offline_error(exc)


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("EZVIZ session not available"),
        ValueError("bad verification code"),
        OSError(errno.EACCES, "Permission denied"),
        KeyError("ssn"),
    ],
)
def test_configuration_and_account_errors_are_not_offline(exc) -> None:
    assert not _is_offline_error(exc)


# --- the circuit breaker ---------------------------------------------------------


def test_waiting_time_grows_into_the_lockout_after_three_failures() -> None:
    r = relay()
    r._last_error = "bad"
    waits = []
    for failures in (1, 2, 3, 4):
        r._consecutive_failures = failures
        waits.append(r._required_cooldown())
    assert waits == [MIN_RETRY_INTERVAL, MIN_RETRY_INTERVAL, LOCKOUT_BACKOFF, LOCKOUT_BACKOFF]


def test_an_offline_door_station_backs_off_gently_up_to_a_ceiling() -> None:
    r = relay()
    r._last_error, r._last_error_offline = "timed out", True
    waits = {}
    for failures in (1, 3, 4, 5, 7, 9, 11, 40):
        r._consecutive_failures = failures
        waits[failures] = r._required_cooldown()
    assert waits == {1: 30, 3: 30, 4: 30, 5: 60, 7: 120, 9: 240, 11: OFFLINE_MAX_BACKOFF, 40: OFFLINE_MAX_BACKOFF}


async def test_a_failed_start_is_counted_and_the_next_try_waits() -> None:
    r = relay()
    calls = []

    def refuse(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("stream refused")

    with patch.object(live_relay, "open_cloud_stream", refuse):
        await r.prewarm()
        await r.prewarm()  # right away: rate-limited, the cloud is not asked again
    assert len(calls) == 1
    assert r._consecutive_failures == 1 and r._last_error == "stream refused" and not r._last_error_offline
    assert 0 < r._seconds_until_next_attempt() <= MIN_RETRY_INTERVAL
    assert r.active_source == "cloud" and r._shared_vtm is None


async def test_three_failures_lock_the_relay_out_for_ten_minutes() -> None:
    r = relay()
    calls = []

    def refuse(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("stream refused")

    with patch.object(live_relay, "open_cloud_stream", refuse):
        for _ in range(3):
            await r.prewarm()
            r._last_attempt -= MIN_RETRY_INTERVAL + 1
        await r.prewarm()
        assert len(calls) == 3  # 31 s later is not enough any more
        r._last_attempt -= LOCKOUT_BACKOFF
        await r.prewarm()
    assert len(calls) == 4


async def test_an_offline_door_station_is_retried_without_the_lockout() -> None:
    r = relay()
    calls = []

    def offline(*args, **kwargs):
        calls.append(args)
        raise TimeoutError("timed out")

    with patch.object(live_relay, "open_cloud_stream", offline):
        for _ in range(4):
            await r.prewarm()
            r._last_attempt -= MIN_RETRY_INTERVAL + 1
    assert len(calls) == 4 and r._last_error_offline


async def test_no_cloud_session_is_a_failure_without_asking_the_cloud() -> None:
    r = relay(api=FakeApi(client=None))
    with patch.object(live_relay, "open_cloud_stream") as opened:
        await r.prewarm()
    opened.assert_not_called()
    assert r._consecutive_failures == 1 and "not available" in r._last_error


# --- a running session --------------------------------------------------------------


async def test_a_working_session_feeds_viewers_and_resets_itself_when_it_ends() -> None:
    video = SPS_PPS + IDR
    source = FakeSource([pes(0xE0, video), pes(0xC0, b"\xff\xf1AAC"), pes(0xE0, P)])
    r = relay()
    v_q, a_q = queue.Queue(), queue.Queue()
    r._sub_v_qs.append(v_q)
    r._sub_a_qs.append(a_q)
    r._consecutive_failures, r._last_error = 2, "earlier failure"
    with patch.object(live_relay, "open_cloud_stream", return_value=source):
        await r.prewarm()
        reader = r._shared_reader
        await wait_for(lambda: r._shared_vtm is None)  # the source ended: the shared session is reset
    reader.join(2)
    assert not reader.is_alive()
    assert r._consecutive_failures == 0 and r._last_error is None
    assert r.detected_codec == "h264"
    assert [v_q.get_nowait() for _ in range(3)] == [video, P, None]
    assert [a_q.get_nowait() for _ in range(2)] == [b"\xff\xf1AAC", None]
    assert source.closed and r._idle_handle is None
    assert r._gop_snapshot() == [video + P]


async def test_prewarm_while_a_session_runs_does_not_open_another() -> None:
    source = FakeSource(hold=True)
    r = relay()
    with patch.object(live_relay, "open_cloud_stream", return_value=source) as opened:
        await r.prewarm()
        await r.prewarm()
        assert opened.call_count == 1 and r.active_source == "cloud"
        await r._shutdown_shared()
    assert source.closed and r._shared_vtm is None and r._shared_reader is None


async def test_a_held_session_outlives_the_idle_timeout() -> None:
    source = FakeSource(hold=True)
    r = relay()
    with patch.object(live_relay, "open_cloud_stream", return_value=source), patch.object(
        live_relay, "KEEP_OPEN_CHECK", 0.01
    ):
        r.hold(600)
        await wait_for(lambda: r._shared_vtm is not None)
        delay = r._idle_handle.when() - asyncio.get_running_loop().time()
        assert 590 < delay <= 600 and r._held()
        r._idle_handle.cancel()
        r._idle_expired()  # an early timer does not close a held session
        await asyncio.sleep(0.05)
        assert r._shared_vtm is not None
        await r.stop()
        await r._shutdown_shared()
    assert r._keeper is None and not r._held() and source.closed


async def test_without_a_hold_the_usual_idle_timeout_applies() -> None:
    r = relay()
    with patch.object(live_relay, "open_cloud_stream", return_value=FakeSource(hold=True)):
        await r.prewarm()
        delay = r._idle_handle.when() - asyncio.get_running_loop().time()
        assert PREWARM_IDLE_TIMEOUT - 5 < delay <= PREWARM_IDLE_TIMEOUT
        await r._shutdown_shared()


async def test_keep_open_reopens_a_local_session_that_ended(fake_lan) -> None:
    r = relay(stream_source="local", keep_open=True)
    assert r._keep_open
    with patch.object(live_relay, "KEEP_OPEN_CHECK", 0.01), patch.object(live_relay, "MIN_RETRY_INTERVAL", 0):
        r._start_keeper()
        await wait_for(lambda: r._shared_vtm is not None)
        assert r._idle_handle is None  # nothing times it out
        fake_lan.made[0].close()  # the door station ends the session
        await wait_for(lambda: len(fake_lan.made) == 2 and r._shared_vtm is not None)
        await r.stop()
        await r._shutdown_shared()
    assert r._keeper is None


async def test_a_held_session_that_ended_is_reopened_at_once(fake_lan) -> None:
    r = relay(stream_source="local", keep_open=True)
    with patch.object(live_relay, "KEEP_OPEN_CHECK", 60), patch.object(live_relay, "MIN_RETRY_INTERVAL", 0), patch.object(
        live_relay, "KEEP_REOPEN_MIN", 0.5
    ):
        r._start_keeper()
        await wait_for(lambda: r._shared_vtm is not None)
        opened = time.monotonic()
        fake_lan.made[0].close()  # the door station ends the session
        await wait_for(lambda: len(fake_lan.made) == 2 and r._shared_vtm is not None, 2.0)
        assert time.monotonic() - opened >= 0.4  # not sooner than KEEP_REOPEN_MIN after the last open
        await r.stop()
        await r._shutdown_shared()


def test_keep_open_is_ignored_for_the_cloud() -> None:
    assert not relay(stream_source="cloud", keep_open=True)._keep_open
    assert not relay(stream_source="auto", keep_open=True)._keep_open


class FakeLan(FakeSource):
    """Stands in for the Cpd7LanSource class (the relay checks the type of its source)."""

    made: list[FakeLan] = []
    fail: Exception | None = None

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(hold=True, fail_start=FakeLan.fail)
        FakeLan.made.append(self)


@pytest.fixture
def fake_lan():
    FakeLan.made, FakeLan.fail = [], None
    with patch.object(live_relay, "Cpd7LanSource", FakeLan):
        yield FakeLan


async def test_auto_prefers_the_local_network(fake_lan) -> None:
    r = relay(stream_source="auto")
    with patch.object(live_relay, "open_cloud_stream") as cloud:
        await r.prewarm()
        assert r.active_source == "local" and isinstance(r._shared_vtm, _PreStartedSource)
        await r._shutdown_shared()
    cloud.assert_not_called()
    lan = fake_lan.made[0]
    assert lan.started == 1 and lan.closed


async def test_auto_falls_back_to_the_cloud_when_the_local_network_refuses(fake_lan) -> None:
    fake_lan.fail = ConnectionRefusedError("refused")
    cloud = FakeSource(hold=True)
    r = relay(stream_source="auto")
    with patch.object(live_relay, "open_cloud_stream", return_value=cloud):
        await r.prewarm()
        assert r.active_source == "cloud" and not r.has_lan_viewers
        assert r._consecutive_failures == 0
        await r._shutdown_shared()
    assert cloud.closed


# --- GOP cache --------------------------------------------------------------------------


def test_nothing_is_replayed_before_the_first_keyframe() -> None:
    r = relay()
    r._gop_update(P)
    r._gop_update(P)
    assert r._gop_snapshot() == []


def test_a_new_viewer_gets_the_stream_since_the_last_keyframe() -> None:
    r = relay()
    for chunk in (SPS_PPS + IDR, P, P, SPS_PPS + IDR, P):
        r._gop_update(chunk)
    assert r._gop_snapshot() == [SPS_PPS + IDR + P]


def test_an_idr_without_parameter_sets_gets_the_last_ones_put_in_front() -> None:
    r = relay()
    r._gop_update(SPS_PPS + IDR)
    r._gop_update(IDR)  # the firmware repeats IDRs more often than parameter sets
    r._gop_update(P)
    assert r._gop_snapshot() == [SPS_PPS + IDR + P]


def test_an_idr_with_no_parameter_sets_at_all_is_not_replayed() -> None:
    r = relay()
    r._gop_update(IDR)
    r._gop_update(P)
    assert r._gop_snapshot() == []


def test_an_oversized_gop_is_dropped_until_the_next_keyframe() -> None:
    r = relay()
    with patch.object(live_relay, "GOP_CACHE_MAX", 40):
        r._gop_update(SPS_PPS + IDR)
        for _ in range(5):
            r._gop_update(P)
        assert r._gop_snapshot() == []
        r._gop_update(SPS_PPS + IDR)
        assert r._gop_snapshot() == [SPS_PPS + IDR]


def test_the_replay_comes_in_relay_sized_chunks() -> None:
    r = relay()
    r._gop_update(SPS_PPS + IDR + b"x" * 30)
    with patch.object(live_relay, "RELAY_CHUNK", 16):
        chunks = r._gop_snapshot()
    assert all(len(c) <= 16 for c in chunks) and b"".join(chunks) == SPS_PPS + IDR + b"x" * 30


def pes_pts(stream_id: int, payload: bytes) -> bytes:
    """A PES packet with a time stamp, as the first packet of a frame has."""
    body = b"\x80\x80\x05" + b"\x21\x00\x01\x00\x01" + payload
    return b"\x00\x00\x01" + bytes((stream_id, len(body) >> 8, len(body) & 0xFF)) + body


def test_a_local_keyframe_packet_is_cut_to_start_on_the_keyframe() -> None:
    # The packet carries the end of the frame before and then the keyframe (a viewer joining a running session).
    chunk = PACK + pes_pts(0xE0, P + SPS_PPS + IDR) + pes(0xC0, b"\xff\xf1AAC")
    assert ps_from_keyframe(chunk, _first_keyframe(chunk)) == PACK + pes_pts(0xE0, SPS_PPS + IDR) + pes(0xC0, b"\xff\xf1AAC")


def test_parameter_sets_go_inside_the_keyframe_packet() -> None:
    chunk = PACK + pes_pts(0xE0, P + IDR)
    assert ps_from_keyframe(chunk, _first_keyframe(chunk), SPS_PPS) == PACK + pes_pts(0xE0, SPS_PPS + IDR)


def test_a_keyframe_packet_without_a_time_stamp_is_not_used() -> None:
    chunk = PACK + pes(0xE0, P + SPS_PPS + IDR)
    assert ps_from_keyframe(chunk, _first_keyframe(chunk)) is None


def test_a_late_local_viewer_gets_the_gop_from_the_keyframe_packet() -> None:
    r = relay()
    r._active_lan = True
    r._gop_update(PACK + pes_pts(0xE0, SPS_PPS + IDR))
    r._gop_update(PACK + pes_pts(0xE0, P + IDR))  # this IDR comes without parameter sets
    r._gop_update(PACK + pes_pts(0xE0, P))
    assert r._gop_snapshot() == [PACK + pes_pts(0xE0, SPS_PPS + IDR) + PACK + pes_pts(0xE0, P)]


def test_a_local_viewer_resumes_on_the_keyframe_packet() -> None:
    r = relay()
    r._active_lan = True
    q = queue.Queue()
    r._sub_raw_qs.append(q)
    r._q_resync.add(id(q))
    r._fanout(r._sub_raw_qs, PACK + pes_pts(0xE0, P), video=True)
    assert q.empty()
    r._fanout(r._sub_raw_qs, PACK + pes_pts(0xE0, P + SPS_PPS + IDR), video=True)
    r._fanout(r._sub_raw_qs, PACK + pes_pts(0xE0, P), video=True)
    assert [q.get_nowait() for _ in range(2)] == [PACK + pes_pts(0xE0, SPS_PPS + IDR), PACK + pes_pts(0xE0, P)]


# HP7: every NAL in a video packet of its own, only the first packet of a frame
# with a time stamp, and a program stream map in front of a keyframe.
PSM = b"\x00\x00\x01\xbc\x00\x04\xe0\xff\x00\x00"
SPS = SC4 + b"\x67\x42\x00\x1f"
PPS = SC4 + b"\x68\xce\x3c\x80"


def hp7_keyframe(params: bool = True) -> bytes:
    nals = [SPS, PPS, IDR] if params else [b"\x00\x00\x00\x01\x06\x05", IDR]
    return PACK + PSM + pes_pts(0xE0, nals[0]) + b"".join(pes(0xE0, n) for n in nals[1:])


def test_a_keyframe_packet_without_a_time_stamp_starts_on_the_pack_header() -> None:
    chunk = PACK + pes_pts(0xE0, P) + hp7_keyframe(params=False)
    at = _first_keyframe(chunk)
    assert ps_from_keyframe(chunk, at) is None
    start = chunk.rindex(PACK)
    assert ps_from_pack(chunk, at) == chunk[start:]
    # Parameter sets the keyframe lacks go into the frame's first video packet.
    assert ps_from_pack(chunk, at, SPS_PPS) == PACK + PSM + pes_pts(0xE0, SPS_PPS + b"\x00\x00\x00\x01\x06\x05") + pes(0xE0, IDR)


def test_parameter_sets_in_packets_of_their_own_are_kept() -> None:
    r = relay()
    r._active_lan = True
    r._gop_update(hp7_keyframe())
    assert r._param_sets == SPS + PPS


def test_a_late_local_viewer_gets_a_gop_whose_keyframe_packet_has_no_time_stamp() -> None:
    # 1.1.1 and 1.1.2 gave such a viewer nothing at all.
    r = relay()
    r._active_lan = True
    r._gop_update(hp7_keyframe())
    r._gop_update(PACK + pes_pts(0xE0, P))
    r._gop_update(hp7_keyframe(params=False))
    r._gop_update(PACK + pes_pts(0xE0, P))
    gop = b"".join(r._gop_snapshot())
    assert gop.startswith(PACK + PSM + pes_pts(0xE0, SPS + PPS + b"\x00\x00\x00\x01\x06\x05"))
    assert gop.endswith(pes(0xE0, IDR) + PACK + pes_pts(0xE0, P))


def test_a_local_viewer_resumes_on_a_pack_header_in_the_chunk_before() -> None:
    r = relay()
    r._active_lan = True
    q = queue.Queue()
    r._sub_raw_qs.append(q)
    r._q_resync.add(id(q))
    frame = hp7_keyframe()
    for chunk in (PACK + pes_pts(0xE0, P), frame[:20], frame[20:]):
        r._gop_update(chunk)
        r._fanout(r._sub_raw_qs, chunk, video=True)
    # The keyframe's packet has a time stamp: cut as in 1.1.1, from the pack header of the chunk before.
    entry = PACK + frame[len(PACK) + len(PSM):]
    assert q.get_nowait() == entry
    assert b"".join(r._gop_snapshot()) == entry

# --- viewers ----------------------------------------------------------------------------


def test_a_full_queue_loses_its_oldest_chunk() -> None:
    r = relay()
    q = queue.Queue(maxsize=2)
    assert r._put_latest(q, b"1") and r._put_latest(q, b"2")
    assert not r._put_latest(q, b"3")
    assert [q.get_nowait(), q.get_nowait()] == [b"2", b"3"] and r._drops == 1


def test_a_viewer_that_fell_behind_skips_to_the_next_keyframe() -> None:
    r = relay()
    q = queue.Queue(maxsize=2)
    r._q_last_drain[id(q)] = time.monotonic()
    q.put(b"old")
    q.put(b"old")
    r._fanout([q], P, video=True)  # saturates: the backlog goes
    assert q.empty()
    r._fanout([q], P, video=True)  # not decodable on its own: skipped
    assert q.empty()
    r._fanout([q], SPS_PPS + IDR, video=True)
    r._fanout([q], P, video=True)
    assert [q.get_nowait(), q.get_nowait()] == [SPS_PPS + IDR, P]


def test_a_local_session_that_starts_inside_a_packet_starts_its_viewers_on_a_pack() -> None:
    # The decoder found no pack header in front of the first parameter set and started on it.
    r = relay(stream_source="local")
    r._active_lan = True
    q: queue.Queue = queue.Queue()
    r._q_last_drain[id(q)] = time.monotonic()
    r._sub_raw_qs.append(q)
    r._shared_vtm = FakeSource([SPS_PPS + IDR + P, PACK + pes_pts(0xE0, SPS_PPS + IDR)])
    r._broadcast_reader()
    first = q.get_nowait()
    assert first.startswith(PACK) and SPS_PPS in first


def test_a_full_audio_queue_keeps_playing_without_a_resync() -> None:
    r = relay()
    q = queue.Queue(maxsize=1)
    r._q_last_drain[id(q)] = time.monotonic()
    r._fanout([q], b"a1")
    r._fanout([q], b"a2")
    r._fanout([q], b"a3")
    assert q.get_nowait() == b"a3" and not r._q_resync


def test_a_viewer_that_takes_nothing_for_too_long_is_dropped() -> None:
    r = relay()
    dead, alive = queue.Queue(maxsize=2), queue.Queue(maxsize=2)
    dead.put(b"stale")
    dead.put(b"stale")
    subs = [dead, alive]
    r._q_last_drain[id(dead)] = time.monotonic() - DEAD_SUB_TIMEOUT - 1
    r._q_last_drain[id(alive)] = time.monotonic()
    r._fanout(subs, b"chunk")
    assert subs == [alive] and alive.get_nowait() == b"chunk"
    assert dead.get_nowait() is None and dead.empty()  # the sentinel lands although the queue was full
    assert id(dead) not in r._q_last_drain


# --- local-network source ---------------------------------------------------------------


class FakeLanApi:
    def __init__(self, ip: str | None = "192.0.2.10", fresh_ip: str | None = None, fresh_key: bytes | None = None) -> None:
        self.ip = ip
        self.fresh_ip = fresh_ip or ip
        self.fresh_key = fresh_key or b"k" * 16
        self.asked_cloud = 0

    def fetch_lan_aes_key(self, serial, force=False):
        if force:
            self.asked_cloud += 1
            return self.fresh_key
        return b"k" * 16

    def get_local_ip(self, serial, force=False):
        return self.fresh_ip if force else self.ip

    def get_related_device(self, serial):
        return "BE0000001"


class Refusing:
    """A door station that refuses every start except with ``accepts``' key and address."""

    accepts: tuple[bytes, str] | None = None
    tried: list[tuple[bytes, str]] = []

    def __init__(self, host, related, key, **kwargs):
        self.host, self.key = host, key
        self.ecdh_priv = None

    def start(self):
        Refusing.tried.append((self.key, self.host))
        if (self.key, self.host) != Refusing.accepts:
            raise RuntimeError("No <Session> in InviteStream response")


def test_a_local_start_needs_no_cloud() -> None:
    Refusing.tried, Refusing.accepts = [], (b"k" * 16, "192.0.2.10")
    api = FakeLanApi()
    with patch.object(cpd7, "Cpd7LanClient", Refusing), patch.object(cpd7, "StreamDecoder"):
        Cpd7LanSource(api, SERIAL).start()
    assert api.asked_cloud == 0 and Refusing.tried == [(b"k" * 16, "192.0.2.10")]


def test_a_refused_local_start_is_tried_again_with_what_the_cloud_says() -> None:
    # the router gave the door station a new address
    Refusing.tried, Refusing.accepts = [], (b"k" * 16, "192.0.2.20")
    api = FakeLanApi(fresh_ip="192.0.2.20")
    with patch.object(cpd7, "Cpd7LanClient", Refusing), patch.object(cpd7, "StreamDecoder"):
        Cpd7LanSource(api, SERIAL).start()
    assert api.asked_cloud == 1 and Refusing.tried == [(b"k" * 16, "192.0.2.10"), (b"k" * 16, "192.0.2.20")]
    # the same key and address from the cloud: not tried again
    Refusing.tried, Refusing.accepts = [], None
    api = FakeLanApi()
    with patch.object(cpd7, "Cpd7LanClient", Refusing), pytest.raises(RuntimeError, match="Session"):
        Cpd7LanSource(api, SERIAL).start()
    assert api.asked_cloud == 1 and len(Refusing.tried) == 1


def test_a_door_station_without_a_local_address_is_not_tried() -> None:
    with patch.object(cpd7, "Cpd7LanClient") as client, pytest.raises(RuntimeError, match="LAN IP"):
        Cpd7LanSource(FakeLanApi(ip=None), SERIAL).start()
    client.assert_not_called()


def test_the_local_stream_tolerates_short_gaps_but_ends_on_silence() -> None:
    class Client:
        def __init__(self, reads):
            self.reads = list(reads)

        def read_chunk(self):
            return self.reads.pop(0) if self.reads else b""

    class Decoder:
        def __init__(self):
            self.fed = b""

        def feed(self, buf):
            self.fed += buf

        def take(self):
            out, self.fed = self.fed, b""
            return out

    source = Cpd7LanSource(FakeLanApi(), SERIAL)
    source._client = Client([b"a", b"", b"", b"", b"b", b"", b"", b"", b"", b"never"])
    source._decoder = Decoder()
    assert list(source.iter_payloads()) == [b"a", b"b"]
    assert source.streamssn == f"lan:{SERIAL}"


def test_a_started_source_is_not_started_twice() -> None:
    inner = FakeSource([b"x"])
    wrapped = _PreStartedSource(inner)
    assert wrapped.start() is inner and inner.started == 0
    assert list(wrapped.iter_payloads()) == [b"x"]
    wrapped.close()
    assert inner.closed == 1 and wrapped.streamssn == "lan"


def _hevc_ps(key: bytes, clear_header: int) -> tuple[bytes, bytes]:
    """A video PES of HEVC parameter sets and a keyframe, plain and with each NAL encrypted after ``clear_header`` bytes."""
    from Crypto.Cipher import AES

    plain = encrypted = b""
    for header in (b"\x40\x01", b"\x42\x01", b"\x44\x01", b"\x26\x01"):
        nal = header + bytes(range(1, 47))
        cut = clear_header + (len(nal) - clear_header) // 16 * 16
        body = AES.new(key.ljust(16, b"\0"), AES.MODE_ECB).encrypt(nal[clear_header:cut])
        plain += SC4 + nal
        encrypted += SC4 + nal[:clear_header] + body + nal[cut:]
    assert b"\x00\x00\x01" not in encrypted.replace(SC4, b"")
    return pes(0xE0, plain), pes(0xE0, encrypted, scrambled=True)


@pytest.mark.parametrize("clear_header", [2, 0])
def test_encrypted_hevc_is_decrypted_with_clear_or_encrypted_nal_headers(clear_header: int) -> None:
    """Firmware 5.4 of the HP7 encrypts the HEVC NAL header too (#22)."""
    plain, encrypted = _hevc_ps(b"ABCDEF", clear_header)
    r = relay()
    r._decrypt_key = "ABCDEF"
    out = r._maybe_decrypt(encrypted + pes(0xC0, b"\xff\xf1"))
    assert out.startswith(plain[:6] + b"\xa0" + plain[7:])
    assert r._decrypt_header_size == clear_header


def test_video_packets_that_are_not_scrambled_are_passed_unchanged() -> None:
    """The HP7 on firmware 5.4 scrambles only the keyframe packets (#22)."""
    plain, encrypted = _hevc_ps(b"ABCDEF", 2)
    p_frame = pes(0xE0, SC4 + b"\x02\x01\xd0\x01\x6d\xc4\x11\x80\xfd\x59\xc2\xf2\x91\x30\x00\x00\x03\x01\xb6\x1d")
    r = relay()
    r._decrypt_key = "ABCDEF"
    out = r._maybe_decrypt(encrypted + p_frame + pes(0xC0, b"\xff\xf1"))
    assert out[len(encrypted):].startswith(p_frame)
    assert out[7:len(encrypted)] == plain[7:]


def test_decryption_starts_at_the_next_packet_when_the_buffer_begins_inside_one() -> None:
    """Cloud chunks are not cut at packets; a buffer starting inside one never yielded anything (#22)."""
    plain, encrypted = _hevc_ps(b"ABCDEF", 2)
    tail = encrypted[-30:]  # the end of a packet that began before the key was known
    r = relay()
    r._decrypt_key = "ABCDEF"
    out = r._maybe_decrypt(tail + PACK + encrypted + pes(0xC0, b"\xff\xf1"))
    assert out.startswith(tail + PACK + plain[:6] + b"\xa0" + plain[7:])


def test_a_buffer_that_yields_nothing_for_too_long_skips_to_the_next_packet() -> None:
    plain, encrypted = _hevc_ps(b"ABCDEF", 2)
    stuck = b"\x00\x00\x01\xe0\xff\xff\x80\x00\x00" + bytes(100)  # announces 64 KB that never come
    r = relay()
    r._decrypt_key = "ABCDEF"
    with patch.object(live_relay, "DECRYPT_STALL_BYTES", 64):
        out = r._maybe_decrypt(stuck + PACK + encrypted + pes(0xC0, b"\xff\xf1"))
    assert out.startswith(stuck + PACK + plain[:6] + b"\xa0" + plain[7:])
    assert not r._decrypt_buf


def _aac_frame(key: bytes) -> tuple[bytes, bytes]:
    """An AAC-LC 16 kHz mono frame, plain and with its body encrypted as on firmware 5.4."""
    from Crypto.Cipher import AES

    header = b"\xff\xf1\x60\x40\x08\xff\xfc"  # 71 bytes, no CRC
    body = b"\x00\xd4\x35\xa8" + bytes(range(60))
    return header + body, header + AES.new(key.ljust(16, b"\0"), AES.MODE_ECB).encrypt(body)


def test_the_sound_is_left_alone_while_no_key_is_in_use() -> None:
    _, encrypted = _aac_frame(b"ABCDEF")
    assert relay()._readable_audio(encrypted) == encrypted


async def test_the_encrypted_sound_of_a_local_session_reaches_the_viewers_decrypted() -> None:
    """ffmpeg showed no picture as long as it could not decode the sound (#22)."""
    plain, encrypted = _aac_frame(b"ABCDEF")

    class Lan(FakeSource):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__([pes(0xC0, encrypted, scrambled=True)])

    r = relay(stream_source="local")
    r._decrypt_key = "ABCDEF"
    a_q = queue.Queue()
    r._sub_a_qs.append(a_q)
    with patch.object(live_relay, "Cpd7LanSource", Lan):
        await r.prewarm()
        reader = r._shared_reader
        await wait_for(lambda: r._shared_vtm is None)
    reader.join(2)
    assert [a_q.get_nowait() for _ in range(2)] == [plain, None]


async def test_an_encrypted_cloud_session_reaches_the_viewers_decrypted() -> None:
    """The cloud source passed encrypted picture and sound on as they came (#22)."""
    plain_video, encrypted_video = _hevc_ps(b"ABCDEF", 2)
    plain_sound, encrypted_sound = _aac_frame(b"ABCDEF")
    source = FakeSource([encrypted_video, pes(0xC0, encrypted_sound, scrambled=True)])
    r = relay()
    r._decrypt_key = "ABCDEF"
    v_q, a_q = queue.Queue(), queue.Queue()
    r._sub_v_qs.append(v_q)
    r._sub_a_qs.append(a_q)
    with patch.object(live_relay, "open_cloud_stream", return_value=source):
        await r.prewarm()
        reader = r._shared_reader
        await wait_for(lambda: r._shared_vtm is None)
    reader.join(2)
    assert v_q.get_nowait() == plain_video[9:]  # the payload behind the PES header
    assert a_q.get_nowait() == plain_sound
    assert r.detected_codec == "hevc"


async def test_a_key_found_in_the_middle_of_a_cloud_session_decrypts_what_follows() -> None:
    """The next cloud chunk began inside a packet, and nothing reached the viewers any more (#22)."""
    plain, encrypted = _hevc_ps(b"ABCDEF", 2)
    cut = len(encrypted) // 2
    source = FakeSource([
        pes(0xE0, P, scrambled=True) * 8 + encrypted[:cut],
        encrypted[cut:] + PACK + encrypted + pes(0xC0, b"\xff\xf1AAC"),
    ])
    r = relay(encryption_key="ABCDEF")
    v_q = queue.Queue()
    r._sub_v_qs.append(v_q)
    with patch.object(live_relay, "open_cloud_stream", return_value=source):
        await r.prewarm()
        reader = r._shared_reader
        await wait_for(lambda: r._shared_vtm is None)
    reader.join(2)
    viewed = [v_q.get_nowait() for _ in range(v_q.qsize())]
    assert r._decrypt_key == "ABCDEF" and plain[9:] in viewed
    assert r.detected_codec == "hevc"


async def test_scrambled_video_does_not_decide_the_codec_before_it_is_decrypted() -> None:
    """With the NAL header encrypted too, random bytes looked like an H.264 SPS (#22)."""
    source = FakeSource([pes(0xE0, SPS_PPS + IDR, scrambled=True), pes(0xC0, b"\xff\xf1AAC")])
    r = relay()
    with patch.object(live_relay, "open_cloud_stream", return_value=source):
        await r.prewarm()
        reader = r._shared_reader
        await wait_for(lambda: r._shared_vtm is None)
    reader.join(2)
    assert r.detected_codec is None


# --- still images and viewers that leave ----------------------------------------------


class FakeEncoder:
    """A one-shot ffmpeg that turns whatever it gets into ``jpeg``."""

    def __init__(self, jpeg: bytes = b"JPEG") -> None:
        self.jpeg = jpeg
        self.input = b""

    async def communicate(self, data: bytes = b"") -> tuple[bytes, None]:
        self.input = data
        return self.jpeg, None

    def kill(self) -> None:
        pass


class FakeExec:
    """Stands in for asyncio.create_subprocess_exec and remembers each command."""

    def __init__(self, make=FakeEncoder) -> None:
        self.make = make
        self.runs: list[tuple[tuple[str, ...], object]] = []

    async def __call__(self, *cmd: str, **kwargs):
        proc = self.make()
        self.runs.append((cmd, proc))
        return proc


async def test_still_images_are_encoded_once_per_keyframe() -> None:
    """Cards asked every second and each request ran its own ffmpeg against the relay (#22)."""
    source = FakeSource([pes(0xE0, SPS_PPS + IDR), pes(0xE0, P)], hold=True)
    r = relay()
    encoder = FakeExec()
    with patch.object(live_relay, "open_cloud_stream", return_value=source), patch.object(
        live_relay.asyncio, "create_subprocess_exec", encoder
    ):
        assert await r.still_image() == b"JPEG"
        assert await r.still_image() == b"JPEG"
        assert len(encoder.runs) == 1
        cmd, proc = encoder.runs[0]
        assert cmd[cmd.index("-f") + 1] == "h264" and proc.input.startswith(SPS_PPS + IDR)
        r._gop_update(SPS_PPS + IDR)  # the next keyframe
        assert await r.still_image() == b"JPEG"
        assert len(encoder.runs) == 2
        await r._shutdown_shared()


async def test_still_image_requests_at_the_same_time_share_one_encoder_run() -> None:
    source = FakeSource([pes(0xE0, SPS_PPS + IDR)], hold=True)
    r = relay()
    encoder = FakeExec()
    with patch.object(live_relay, "open_cloud_stream", return_value=source), patch.object(
        live_relay.asyncio, "create_subprocess_exec", encoder
    ):
        assert await asyncio.gather(*(r.still_image() for _ in range(3))) == [b"JPEG"] * 3
        assert len(encoder.runs) == 1
        await r._shutdown_shared()


async def test_no_still_image_and_no_waiting_without_a_session() -> None:
    r = relay()
    encoder = FakeExec()
    started = time.monotonic()
    with patch.object(live_relay, "open_cloud_stream", side_effect=RuntimeError("cloud down")), patch.object(
        live_relay.asyncio, "create_subprocess_exec", encoder
    ):
        assert await r.still_image() is None
    assert not encoder.runs and time.monotonic() - started < 2


class SilentFfmpeg:
    """A relay ffmpeg that never writes anything."""

    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.stdout.feed_eof()

    async def wait(self) -> int:
        return 0


class FakeListener:
    """ffmpeg's input socket; already closed, so its sender thread ends at once."""

    def settimeout(self, timeout: float) -> None:
        pass

    def accept(self):
        raise OSError("closed")

    def close(self) -> None:
        pass


class FakeWriter:
    def __init__(self) -> None:
        self.closed = False

    def get_extra_info(self, name: str):
        return ("127.0.0.1", 40000)

    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


async def test_a_viewer_that_leaves_before_any_output_stops_its_ffmpeg() -> None:
    """It used to run for the full 90 s, one per still-image request, up to ~50 at once (#22)."""
    source = FakeSource(hold=True)
    r = relay()
    ffmpeg = FakeExec(SilentFfmpeg)
    viewer = asyncio.StreamReader()
    viewer.feed_eof()  # the viewer has already hung up
    writer = FakeWriter()
    with patch.object(live_relay, "open_cloud_stream", return_value=source), patch.object(
        live_relay.asyncio, "create_subprocess_exec", ffmpeg
    ), patch.object(live_relay, "_free_local_port", return_value=1), patch.object(
        live_relay, "_start_local_listener", return_value=FakeListener()
    ):
        await asyncio.wait_for(r._handle_client(viewer, writer), timeout=10)
        await r._shutdown_shared()
    assert ffmpeg.runs[0][1].killed and writer.closed and r._active_clients == 0


async def lan_viewer_cmd(r: StreamRelay, gop: bytes | None, fed: bool = False) -> tuple[str, ...]:
    """The relay ffmpeg command of a local viewer, with ``gop`` cached when it joins.

    ``fed``: the session has already handed out chunks.
    """
    viewer = asyncio.StreamReader()
    viewer.feed_eof()
    ffmpeg = FakeExec(SilentFfmpeg)
    with patch.object(live_relay.asyncio, "create_subprocess_exec", ffmpeg), patch.object(
        live_relay, "_free_local_port", return_value=1
    ), patch.object(live_relay, "_start_local_listener", return_value=FakeListener()):
        await r.prewarm()
        await asyncio.sleep(0.05)  # the session start empties the GOP cache
        if gop:
            r._gop_update(gop)
        r._lan_fed = fed
        await asyncio.wait_for(r._handle_client(viewer, FakeWriter()), timeout=10)
        await r._shutdown_shared()
    return ffmpeg.runs[0][0]


def probe_of(cmd: tuple[str, ...]) -> str:
    return cmd[cmd.index("-analyzeduration") + 1]


async def test_a_viewer_on_the_cached_gop_gets_a_short_probe(fake_lan) -> None:
    # MPEG-PS made ffmpeg probe its whole analyzeduration: 10 s before the first picture.
    r = relay(stream_source="local")
    cmd = await lan_viewer_cmd(r, PACK + pes_pts(0xE0, SPS_PPS + IDR))
    assert probe_of(cmd) == live_relay.LAN_PROBE_PRELOADED


async def test_a_viewer_of_a_fresh_session_gets_a_short_probe_too(fake_lan) -> None:
    # it starts with the session, which the decoder starts on a keyframe
    r = relay(stream_source="local")
    assert probe_of(await lan_viewer_cmd(r, None)) == live_relay.LAN_PROBE_PRELOADED


async def test_a_viewer_joining_a_running_local_session_starts_on_the_next_keyframe(fake_lan) -> None:
    r = relay(stream_source="local")
    await lan_viewer_cmd(r, None)
    assert not r._q_resync  # with the session: its first chunk is the keyframe
    cmd = await lan_viewer_cmd(r, None, fed=True)
    assert len(r._q_resync) == 1 and probe_of(cmd) == live_relay.LAN_PROBE_PRELOADED


def test_the_mjpeg_probe_follows_the_cached_gop() -> None:
    from custom_components.ezviz_instant_bell.mjpeg import _build_ffmpeg_cmd

    r = relay()
    assert r.viewer_probe == live_relay.LAN_PROBE_COLD
    r._active_lan = True
    assert r.viewer_probe == live_relay.LAN_PROBE_PRELOADED  # the local output starts on a keyframe
    r._active_lan = False
    r._gop_update(SPS_PPS + IDR)
    assert r.viewer_probe == live_relay.LAN_PROBE_PRELOADED
    cmd = _build_ffmpeg_cmd("ffmpeg", "tcp://x", fps=8, width=1, height=1, quality=5, probe_us=r.viewer_probe)
    assert cmd[cmd.index("-analyzeduration") + 1] == live_relay.LAN_PROBE_PRELOADED


# --- what a real encrypted HP7 session taught (2026-11-13) ---------------------------


def test_the_header_size_is_decided_only_after_enough_of_the_stream() -> None:
    """On the first kilobyte the detection was wrong in two real sessions; it holds."""
    plain, encrypted = _hevc_ps(b"ABCDEF", 0)
    r = relay()
    r._decrypt_key = "ABCDEF"
    r._decrypt_min_bytes = len(encrypted) + 1
    assert r._maybe_decrypt(encrypted) == b""
    assert r._decrypt_header_size is None
    assert r._decrypt_buf
    out = r._maybe_decrypt(encrypted + pes(0xC0, b"\xff\xf1"))
    assert r._decrypt_header_size == 0
    assert out.startswith(plain[:6] + b"\xa0" + plain[7:])
    assert out.count(plain[7:]) == 2  # both packets, decrypted with the one decision


def test_a_scrambled_stream_syncs_the_decoder_on_a_pack_header() -> None:
    """The SPS of an encrypted stream is unreadable: waiting for it took 8 to 110 s."""
    d = cpd7.StreamDecoder(None, sync_on_pack=True)
    d._absorb_plain(b"\x1a\xd7\xf8\xc6" + PACK + b"video")
    assert d.take() == PACK + b"video"
    d._absorb_plain(b"more")
    assert d.take() == b"more"
    plain = cpd7.StreamDecoder(None)
    plain._absorb_plain(b"\x1a\xd7\xf8\xc6" + PACK + b"video")
    assert plain.take() == b""  # a clear stream still waits for its keyframe


def test_a_local_source_with_a_stream_key_asks_for_the_pack_sync() -> None:
    class Client:
        ecdh_priv = object()

        def __init__(self, *a, **k) -> None:
            pass

        def start(self) -> None:
            pass

    seen = {}

    def decoder(priv, sync_on_pack=False):
        seen["sync"] = sync_on_pack
        return object()

    api = FakeApi()
    api.fetch_lan_aes_key = lambda serial, force=False: b"0123456789abcdef"
    api.get_local_ip = lambda serial, force=False: "192.168.1.2"
    api.get_related_device = lambda serial: serial
    with patch.object(cpd7, "Cpd7LanClient", Client), patch.object(cpd7, "StreamDecoder", decoder):
        Cpd7LanSource(api, SERIAL, scrambled=True).start()
    assert seen["sync"] is True
    with patch.object(cpd7, "Cpd7LanClient", Client), patch.object(cpd7, "StreamDecoder", decoder):
        Cpd7LanSource(api, SERIAL).start()
    assert seen["sync"] is False
