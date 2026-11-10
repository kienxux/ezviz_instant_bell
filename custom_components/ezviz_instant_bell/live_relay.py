"""Live video camera entity for the HP7 / CP7 — VTM cloud relay path.

The HP7 / CP7 don't register on the Hikvision UDP P2P cloud (verified
2026-06-06 against a live HP7: every P2P_SETUP response on the 5 cloud
servers came back as a bare ClientID echo without the 0xFF sub-TLV — the
cloud cannot route a P2P_SETUP to a consumer doorbell). The official
EZVIZ app streams them through the VTM cloud relay: a TCP ysproto
session delivering MPEG-PS that wraps H.264 video (PES stream_id 0xE0)
and AAC-LC 16 kHz mono audio that ships as AAC-ADTS but is mis-labelled
inside the PES as MP2 (stream_id 0xC0). The standard ffmpeg `mpeg`
demuxer therefore rejects every audio packet.

Per viewing session this module:

    HP7  ->  VTM cloud (ysproto://...:8554/live)
        |
        v  VtmStreamClient.iter_payloads()  (sync, executor thread)
        |
        v  _pes.PesParser  (Python MPEG-PS PES splitter)
        |
        +--->  video bytes ->  queue.Queue  ->  TCP 127.0.0.1:V
        +--->  audio bytes ->  queue.Queue  ->  TCP 127.0.0.1:A
                                                |
                                                v
        ffmpeg subprocess:
            -f h264 -i tcp://127.0.0.1:V
            -f aac  -i tcp://127.0.0.1:A
            -c:v copy -c:a aac -ar 16000 -ac 1 -b:a 32k
            -max_interleave_delta 0 -f mpegts pipe:1
        (stdout) ->  TCP relay 127.0.0.1:<port>  ->  HA Stream / HLS

The audio leg is re-encoded (rather than `copy`) so the MPEG-TS muxer
gets the AudioSpecificConfig extradata that ADTS strips, and
`-use_wallclock_as_timestamps 1` gives ffmpeg something to anchor on
since the raw h264/aac inputs carry no container timestamps.

A circuit breaker rate-limits accept attempts: MIN_RETRY_INTERVAL = 30 s,
LOCKOUT_THRESHOLD = 3 consecutive failures flips to LOCKOUT_BACKOFF
(failures that merely mean "the device didn't answer" back off along their
own gentler curve instead, up to OFFLINE_MAX_BACKOFF)
(10 min). HA's Stream component is happy to reconnect every few seconds
when an upstream stream errors; without this throttle a single bad
config could lock the EZVIZ account in under a minute.
"""
# Adapted for EZVIZ HP7 / CP7 Video Intercom from the ezviz_hp7 integration by Bobsilvio
# (MIT, https://github.com/Bobsilvio/ezviz_hp7); see THIRD_PARTY_LICENSES.md.
# The local-network video protocol and the motion-JPEG view are by albrzmr (MIT).
from __future__ import annotations

import asyncio
import errno
import logging
import queue
import socket
import threading
import time
from typing import TYPE_CHECKING, Any, List, Optional

from ._pes import PesParser, keep_clear_video_pes
from pyezvizapi.cloud_stream import open_cloud_stream

from .const import DOMAIN
from .live_io import _free_local_port, _mk_drain, _put_sentinel, _sender_thread, _start_local_listener
from .live_nal import _extract_param_sets, _has_keyframe, _has_param_sets, _sniff_video_codec
from .live_sources import Cpd7LanSource, _PreStartedSource

if TYPE_CHECKING:
    from .stream_support import StreamSupport

_LOGGER = logging.getLogger(__name__)

VIDEO_STREAM_ID = 0xE0
AUDIO_STREAM_ID = 0xC0

FFMPEG_KILL_TIMEOUT = 2.0
RELAY_CHUNK = 65536
PAYLOAD_QUEUE_SIZE = 256
# Video carries whole MPEG-PS chunks and cannot tolerate gaps the way audio
# can, so give it a deep buffer: at ~460 KB/s a 4096-slot queue is tens of
# seconds of slack, which in practice means the drop path is never reached.
# Dropping is then a genuine last resort rather than routine behaviour, and
# the saturation warning below tells us whether it ever actually happens.
VIDEO_QUEUE_SIZE = 4096
# Watchdog on the relay ffmpeg's output (#41): if it produces nothing for
# this long the session is dead — either the decode is failing silently
# (HPD5 bad-NAL case: relay ingested 141 MB while emitting 0 bytes for 2 min)
# or the viewer is gone and we'd never notice because we only detect a dead
# peer on write. Without this, _handle_client blocks on proc.stdout.read()
# forever and each retry leaks a broadcast subscriber + an ffmpeg process.
# First-output allowance is generous: the 10 MB probe window (0.13.9) can
# legitimately take ~60 s on a low-bitrate stream before the first byte.
FIRST_OUTPUT_TIMEOUT = 90.0
OUTPUT_STALL_TIMEOUT = 30.0
# Upper bound on the GOP cache (#37). A 1 Mbit/s stream with a 30 s keyframe
# interval accumulates ~4 MB per GOP; 8 MB covers that with headroom. If a
# GOP exceeds this the cache is dropped for that GOP (new viewers fall back
# to waiting for the next keyframe — the pre-cache behaviour).
GOP_CACHE_MAX = 8 * 1024 * 1024
# Interval between relay progress log lines (#44).
PROGRESS_LOG_SEC = 60.0
# A subscriber queue saturated this long with nothing draining it has no live
# consumer left; evict it so its leaked ffmpeg stops competing for CPU (#44).
DEAD_SUB_TIMEOUT = 30.0

MIN_RETRY_INTERVAL = 30.0
LOCKOUT_THRESHOLD = 3
LOCKOUT_BACKOFF = 600.0
# Ceiling for the "device offline" retry cadence (#52). That branch stays
# fast so a reboot recovers in ~60-120s, but it must not stay fast for
# ever: each LAN retry drops the cached control key and re-fetches it,
# which costs a p2p-register plus a CAS query. A stall lasting hours would
# otherwise mean hundreds of cloud calls — the very thing LOCKOUT_BACKOFF
# exists to prevent.
OFFLINE_MAX_BACKOFF = 300.0

# Pre-warm: how long a shared VTM session stays alive after the last HA
# Stream client disconnected before we tear it down. Long enough that the
# typical "ring -> open dashboard" workflow finds an already-running session
# (sub-second first frame), short enough that we don't leave a cloud session
# open all day after a stray motion event.
PREWARM_IDLE_TIMEOUT = 120.0


def _is_offline_error(exc: BaseException) -> bool:
    """True when ``exc`` looks like "the device didn't answer" rather than a
    misconfiguration or an EZVIZ-account/cloud problem.

    The circuit breaker's LOCKOUT_BACKOFF exists to protect the EZVIZ
    account from a bad config hammering the cloud (see module docstring) —
    that's a real ban risk. A doorbell that's mid-reboot is not: it comes
    back on its own in ~60-120s, and every source in this codebase that
    detects it says so explicitly (LAN InviteStream timeout/refusal, VTM
    stream-info timeout, the local SDK's "offline or unreachable"). Treating
    that the same as a bad config means paying a 10-minute penalty for a
    problem that was already fixing itself.
    """
    if isinstance(
        exc, (ConnectionRefusedError, TimeoutError, socket.timeout, ConnectionError)
    ):
        return True
    # EHOSTUNREACH / ENETUNREACH: the LAN can't route to the doorbell's IP at
    # all — no ARP reply, cable/wifi down. Neither maps to a ConnectionError
    # subclass in Python (only ECONNREFUSED/ECONNRESET/EPIPE/ECONNABORTED
    # do), so it needs its own check. Observed in practice ~1s after a
    # doorbell power-off, before the LAN InviteStream starts answering with
    # its own explicit "offline" rejection.
    if getattr(exc, "errno", None) in (errno.EHOSTUNREACH, errno.ENETUNREACH):
        return True
    text = str(exc).lower()
    return (
        "offline or unreachable" in text
        or "timed out waiting for vtm stream info" in text
        or "no <session> in invitestream" in text
        or "connection refused" in text
        or "host is unreachable" in text
        or "network is unreachable" in text
    )


class StreamRelay:
    """Per-entry TCP server. Each accept opens a VTM session and forwards
    muxed MPEG-TS (H.264 + AAC) to the connected client (HA Stream component)."""

    def __init__(
        self,
        api: "StreamSupport",
        serial: str,
        channel: int = 1,
        ffmpeg_path: str = "ffmpeg",
        listen_port: int = 0,
        bind_host: str = "127.0.0.1",
        aggressive_mpegts: bool = False,
        video_codec: str = "auto",
        stream_source: str = "cloud",
        stream_mode: str = "mjpeg",
        stream_quality: str = "main",
        diagnostic_dumps: bool = False,
        encryption_key: str = "",
        hass: Any = None,
    ) -> None:
        self._hass = hass
        self._api = api
        self._serial = serial
        self._channel = channel
        self._ffmpeg_path = ffmpeg_path
        # Delivery mode ("webrtc" | "mjpeg"). Only used to warn once when we
        # sniff HEVC while on WebRTC, where the browser can't decode a copy and
        # go2rtc must transcode (grey screen / dial refused on weak hosts).
        self._stream_mode = (stream_mode or "mjpeg").lower()
        self._warned_hevc_webrtc = False
        # Stream source: "cloud" (VTM relay), "local" (CPD7 LAN), or "auto"
        # (try LAN, fall back to cloud). LAN bypasses the cloud entirely and
        # works on firmware whose VTM channel never pushes (#33/#36/#37).
        self._stream_source = (stream_source or "cloud").lower()
        # LAN encoder stream: "main" (full res) or "sub" (low-res, far
        # cheaper to decode on weak hosts — #44).
        self._stream_quality = (stream_quality or "main").lower()
        # Configured codec: "auto" | "h264" | "hevc". "auto" inspects the
        # first NAL units off the broadcast and sets _detected_codec; the
        # other two force it. Newer HP7 (HPD7) streams HEVC (#36, #37).
        self._video_codec = (video_codec or "auto").lower()
        self._detected_codec: Optional[str] = None
        # Requested fixed port (0 = pick a free one). External consumers
        # like go2rtc need a stable URL; OptionsFlow exposes this.
        self._listen_port = int(listen_port) if listen_port else 0
        # Bind host. 127.0.0.1 keeps the unauthenticated raw stream
        # local-only; 0.0.0.0 (opt-in) lets an external host — Frigate on
        # another box — ingest it (#41).
        self._bind_host = (bind_host or "127.0.0.1").strip() or "127.0.0.1"
        # Opt-in toggle to re-emit SPS/PPS in front of every IDR. Helps
        # firmwares that emit them only at the first IDR (CP5, some HP7
        # builds — #33); breaks the firmwares that already inline them.
        self._aggressive_mpegts = bool(aggressive_mpegts)
        self._server: Optional[asyncio.AbstractServer] = None
        self._port: int = 0
        self._last_attempt: float = 0.0
        self._last_error: Optional[str] = None
        # Whether the last prewarm() failure looked like "device offline"
        # rather than a misconfiguration — see _is_offline_error(). Keeps
        # the breaker's 10-minute lockout reserved for the risk it was
        # actually built to guard against.
        self._last_error_offline: bool = False
        self._consecutive_failures: int = 0
        self._connect_lock = asyncio.Lock()
        # Shared (pre-warmed) VTM session + broadcast bookkeeping.
        self._shared_lock = asyncio.Lock()
        self._shared_vtm: Any = None
        self._shared_stop = threading.Event()
        self._shared_reader: Optional[threading.Thread] = None
        # Event loop the shared session was opened from, so the broadcast
        # reader thread can hop back onto it if it dies on its own (see
        # _handle_reader_exit).
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Per-subscriber queues populated by the shared reader.
        self._sub_v_qs: List["queue.Queue[Optional[bytes]]"] = []
        self._sub_a_qs: List["queue.Queue[Optional[bytes]]"] = []
        # Per-client queues for the LAN raw-MPEG-PS path (single muxed feed).
        self._sub_raw_qs: List["queue.Queue[Optional[bytes]]"] = []
        # Diagnostic ES dump (#41): when debug logging is on, the first
        # 64 KB of demuxed video ES per session is written to the HA config
        # dir so misframed NAL payloads (HPD5) can be hex-inspected without
        # a packet capture.
        self._diagnostic_dumps = bool(diagnostic_dumps)
        self._configured_key = (encryption_key or "").strip()
        self._es_dump_buf = bytearray()
        self._es_dump_done = False
        # Raw MPEG-PS dump + keyframe-marker counter (#41): the HPD5 "synced"
        # stream turned out to contain no parameter sets in the video ES, so
        # we also need to see the container *before* demux and whether
        # VPS/SPS markers ever occur across the whole session.
        self._ps_dump_buf = bytearray()
        self._ps_dump_done = False
        self._kf_markers_seen = 0
        # Dropped-chunk counter (#44). A full per-subscriber queue means we
        # silently discard stream data — which corrupts that viewer's feed and
        # looks exactly like stuttering/freezing. Counting it separates
        # HA-side backpressure (drops > 0: a consumer can't keep up) from
        # device-side starvation (drops == 0: the doorbell itself sends late).
        self._drops = 0
        # id(queue) -> monotonic time it first saturated, for dead-subscriber
        # eviction (#44). Cleared as soon as a chunk goes in cleanly.
        # id(queue) -> monotonic time its consumer last took a chunk. Seeded
        # on subscribe so a fresh viewer is never evicted before it starts.
        self._q_last_drain: dict[int, float] = {}
        # Video queues currently waiting for a keyframe after saturating.
        self._q_resync: set[int] = set()
        self._warned_saturation = False
        self._warned_scrambled = False
        # Payload decryption (#47): key + carry-over buffer. A NAL can span
        # several video PES packets, so only the "complete" prefix reported
        # by mpeg_ps_decryptable_prefix_length() may be decrypted per call.
        self._decrypt_key: Optional[str] = None
        self._decrypt_buf = bytearray()
        self._decrypt_failed = False
        # Clear codec header bytes before the encrypted NAL body: 2 for HEVC,
        # 1 for H.264, 0 when the header is encrypted too (HP7 firmware 5.4,
        # #22). Found once from the first chunk that shows it; 2 until then.
        self._decrypt_header_size: Optional[int] = None
        # GOP cache (#37): the stream since the last keyframe. A new viewer
        # can only start painting from an IDR, and some doorbells emit
        # keyframes tens of seconds apart — replaying this to each new
        # subscriber makes the live view start immediately instead of
        # sitting blank until the next keyframe. Guarded by _gop_lock
        # (updated from the broadcast reader thread, read at subscribe).
        self._gop_buf = bytearray()
        # Most recent VPS/SPS/PPS seen, kept apart from the GOP buffer:
        # this firmware emits IDRs far more often than it repeats the
        # parameter sets, so a GOP that starts on an IDR still needs them
        # prepended before a decoder can use it (#51).
        self._param_sets: bytes = b""
        self._gop_lock = threading.Lock()
        # True while the active shared source is the CPD7 LAN pipeline.
        self._active_lan: bool = False
        self._idle_handle: Optional[asyncio.TimerHandle] = None
        self._active_clients: int = 0

    @property
    def port(self) -> int:
        return self._port

    @property
    def stream_url(self) -> str:
        return f"tcp://127.0.0.1:{self._port}"

    @property
    def detected_codec(self) -> Optional[str]:
        """Video codec sniffed off the live stream ("h264"/"hevc"), or None."""
        return self._detected_codec

    @property
    def has_lan_viewers(self) -> bool:
        """True while someone watches through the local-network connection."""
        return self._active_lan and self._active_clients > 0

    @property
    def active_source(self) -> str:
        """Which source is actually feeding the stream right now.

        Reflects reality rather than the configured option: with source=auto
        the relay may have fallen back cloud->local (or vice-versa), so report
        what's live. Falls back to the configured value when nothing is open.
        """
        if self._active_lan:
            return "local"
        if self._shared_vtm is not None:
            return "cloud"
        return self._stream_source

    def _es_dump(self, payload: bytes) -> None:
        """Collect the first 64 KB of video ES and write it once (#41).

        Debug-only (gated on the logger level). Runs on the reader thread,
        so the blocking file write is fine. The dump lets HPD5 users share
        exactly what sits in front of the NAL start codes.
        """
        if self._es_dump_done or not self._diagnostic_dumps:
            return
        self._es_dump_buf.extend(payload)
        if len(self._es_dump_buf) < 64 * 1024:
            return
        self._es_dump_done = True
        try:
            if self._hass is not None:
                path = self._hass.config.path(
                    f"ezviz_instant_bell_es_dump_{self._serial}.bin"
                )
            else:
                path = f"/tmp/ezviz_instant_bell_es_dump_{self._serial}.bin"
            with open(path, "wb") as fh:
                fh.write(self._es_dump_buf[: 64 * 1024])
            _LOGGER.info(
                "StreamRelay: wrote 64 KB video-ES diagnostic dump to %s "
                "(first bytes: %s)",
                path, self._es_dump_buf[:24].hex(" "),
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("StreamRelay: ES dump write failed: %s", exc)
        finally:
            self._es_dump_buf = bytearray()

    def _fanout(
        self,
        qs: List["queue.Queue[Optional[bytes]]"],
        item: bytes,
        video: bool = False,
    ) -> None:
        """Broadcast ``item`` to subscribers, evicting dead ones (#44).

        A subscriber whose queue stays saturated for DEAD_SUB_TIMEOUT with
        nothing ever draining it has no live consumer — its client task
        failed to tear down. AnthoPakPak reported ``subs=9`` with only
        Frigate plus an occasional preview open, i.e. ~7 abandoned
        subscribers; since every subscriber costs a relay ffmpeg (plus an
        mjpeg ffmpeg per viewer), those leaked processes compete for the
        same CPU as the live one. Evicting them frees that up.
        """
        now = time.monotonic()
        kf = video and _has_keyframe(item)
        for q in list(qs):
            qid = id(q)
            if video and qid in self._q_resync:
                # Saturated earlier: skip everything until a keyframe so the
                # decoder resumes on a decodable boundary instead of being
                # handed a stream with holes punched mid-GOP (which freezes
                # the picture on the last good frame while audio, carried on
                # its own queue, keeps playing normally).
                if not kf:
                    self._drops += 1
                    continue
                self._q_resync.discard(qid)
            if not self._put_latest(q, item) and video:
                if not self._warned_saturation:
                    self._warned_saturation = True
                    _LOGGER.warning(
                        "StreamRelay: VIDEO QUEUE SATURATED (%d slots) — "
                        "the consumer is slower than the doorbell; dropping "
                        "to the next keyframe (serial=%s)",
                        VIDEO_QUEUE_SIZE, self._serial,
                    )
                # Drop the whole backlog and resync at the next keyframe:
                # keeps the viewer current AND decodable.
                self._q_resync.add(qid)
                try:
                    while True:
                        q.get_nowait()
                except queue.Empty:
                    pass
            # Liveness is "did the consumer take anything recently", NOT "is
            # the queue full". Under the drop-oldest policy a queue whose
            # consumer is even marginally slower than the producer sits full
            # permanently by design, so the previous saturation test evicted
            # perfectly healthy viewers after 30 s.
            last = self._q_last_drain.get(id(q))
            if last is None or now - last < DEAD_SUB_TIMEOUT:
                continue
            self._q_last_drain.pop(id(q), None)
            try:
                qs.remove(q)
            except ValueError:
                pass
            # Sentinel so the sender thread exits and the client tears down.
            # The queue is full here by definition, so this has to drain it
            # first or the sentinel never lands and the client leaks (#51).
            _put_sentinel(q)
            _LOGGER.warning(
                "StreamRelay: evicted a %s subscriber whose consumer took "
                "nothing for %.0fs (serial=%s, %d left)",
                "video" if video else "audio",
                DEAD_SUB_TIMEOUT, self._serial, len(qs),
            )

    def _put_latest(self, q: "queue.Queue[Optional[bytes]]", item: bytes) -> bool:
        """Enqueue ``item``, discarding the OLDEST chunk when the queue is full.

        Previously a full queue dropped the *newest* chunk and kept the stale
        backlog, so a viewer that fell behind once kept draining old data
        forever — the picture stuttered and the delay grew without bound
        (#44 AnthoPakPak, drops=22.8M). Dropping the oldest instead lets a
        saturated viewer skip forward to live data and resync at the next
        keyframe, which is the behaviour a live stream wants.

        Returns True if the chunk went in without evicting anything (i.e. the
        consumer is keeping up), False if it saturated.
        """
        try:
            q.put_nowait(item)
            return True
        except queue.Full:
            pass
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(item)
        except queue.Full:
            pass
        self._drops += 1
        return False

    def _ps_dump(self, body: bytes) -> None:
        """Collect the first 128 KB of raw (decrypted) MPEG-PS and write it
        once (#41). Debug-only. Complements the ES dump: if VPS/SPS exist
        here but not in the demuxed ES, the demux is stripping them; if they
        don't exist here either, the device delivers them out-of-band.
        """
        if self._ps_dump_done or not self._diagnostic_dumps:
            return
        self._ps_dump_buf.extend(body)
        if len(self._ps_dump_buf) < 128 * 1024:
            return
        self._ps_dump_done = True
        try:
            if self._hass is not None:
                path = self._hass.config.path(
                    f"ezviz_instant_bell_ps_dump_{self._serial}.bin"
                )
            else:
                path = f"/tmp/ezviz_instant_bell_ps_dump_{self._serial}.bin"
            with open(path, "wb") as fh:
                fh.write(self._ps_dump_buf[: 128 * 1024])
            _LOGGER.info(
                "StreamRelay: wrote 128 KB raw MPEG-PS diagnostic dump "
                "to %s",
                path,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("StreamRelay: PS dump write failed: %s", exc)
        finally:
            self._ps_dump_buf = bytearray()

    def _gop_update(self, chunk: bytes) -> None:
        """Track the stream since the last random-access point (#37, #51).

        Called from the broadcast reader thread for every video-bearing
        chunk. The cache restarts whenever a chunk carries a point a
        decoder can start on — parameter sets or an IDR/CRA. Anything else
        is appended. Chunk boundaries are arbitrary, so the cache may lead
        with a partial pack or NAL — both the mpeg demuxer and the raw ES
        parsers resync on the next start code, and +discardcorrupt drops
        the remnant frame.

        Restarting on an IDR alone is not enough to hand a decoder, since
        it still needs VPS/SPS/PPS and this firmware does not repeat them
        with every IDR (#51). So the most recent parameter sets are kept
        separately and prepended at snapshot time.
        """
        with self._gop_lock:
            if _has_param_sets(chunk):
                extracted = _extract_param_sets(chunk)
                if extracted:
                    self._param_sets = extracted
            if _has_keyframe(chunk):
                self._kf_markers_seen += 1
                self._gop_buf = bytearray(chunk)
            elif self._gop_buf:
                if len(self._gop_buf) + len(chunk) > GOP_CACHE_MAX:
                    # Oversized GOP: drop and wait for the next keyframe —
                    # new viewers get the pre-cache behaviour for this GOP.
                    self._gop_buf = bytearray()
                else:
                    self._gop_buf.extend(chunk)

    def _gop_snapshot(self) -> List[bytes]:
        """Return a decodable GOP prefix as queue-sized chunks (#51).

        Preloading a segment the decoder cannot start on is worse than
        preloading nothing: ffmpeg burns through it emitting "Could not
        find ref with POC n" for every frame and shows no picture anyway,
        so the viewer waits for the next real keyframe regardless. Measured
        on an HPD7 (V5.4.0): 27 undecodable frames replayed in 31 ms, then
        10-12 s of black.

        So only hand back a buffer that starts on a random-access point,
        and prepend the last parameter sets when the buffer itself lacks
        them.
        """
        with self._gop_lock:
            buf = bytes(self._gop_buf)
            params = self._param_sets
        if not buf or not _has_keyframe(buf):
            return []
        if not _has_param_sets(buf):
            if not params:
                # An IDR with no parameter sets anywhere is not decodable.
                return []
            buf = params + buf
        return [buf[i:i + RELAY_CHUNK] for i in range(0, len(buf), RELAY_CHUNK)]

    def _maybe_decrypt(self, body: bytes) -> bytes:
        """Decrypt scrambled video payloads when the device encrypts them.

        Image/Video Encryption leaves MPEG-PS/PES and Annex B NAL framing in
        the clear and encrypts only the NAL bodies, which is why such a
        stream looks structurally perfect yet decodes to garbage. pyEzvizApi
        can undo it given the camera key, so on firmware where encryption
        can no longer be switched off at all (#47) this is the only way to
        get a picture. Returns the input unchanged when decryption isn't
        active or isn't possible.
        """
        if self._decrypt_key is None or self._decrypt_failed:
            return body
        try:
            from pyezvizapi.stream import (
                decrypt_hikvision_ps_video,
                detect_hikvision_ps_video_nalu_header_size,
                mpeg_ps_decryptable_prefix_length,
            )

            self._decrypt_buf.extend(body)
            n = mpeg_ps_decryptable_prefix_length(bytes(self._decrypt_buf))
            if n <= 0:
                return b""
            chunk = bytes(self._decrypt_buf[:n])
            del self._decrypt_buf[:n]
            if self._decrypt_header_size is None:
                self._decrypt_header_size = detect_hikvision_ps_video_nalu_header_size(
                    chunk, self._decrypt_key, default=None
                )
                if self._decrypt_header_size is not None:
                    _LOGGER.debug(
                        "StreamRelay: decrypting with %d clear NAL header byte(s) (serial=%s)",
                        self._decrypt_header_size, self._serial,
                    )
            return keep_clear_video_pes(chunk, decrypt_hikvision_ps_video(
                chunk,
                self._decrypt_key,
                nalu_header_size=2 if self._decrypt_header_size is None else self._decrypt_header_size,
            ))
        except Exception as exc:  # noqa: BLE001
            self._decrypt_failed = True
            _LOGGER.error(
                "StreamRelay: stream decryption failed, passing through "
                "unchanged (serial=%s): %s", self._serial, exc,
            )
            return body

    def _enable_decryption(self) -> None:
        """Fetch the camera key so scrambled payloads can be decrypted."""
        if self._decrypt_key is not None or self._decrypt_failed:
            return
        key = self._configured_key
        if not key:
            try:
                key = self._api.get_camera_encryption_key(self._serial)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("StreamRelay: camera key unavailable: %s", exc)
                key = None
        if not key:
            if self._serial_needs_otp():
                # The failed request above is what makes EZVIZ send the code,
                # so it is already in the user's inbox — and it dies in 30
                # minutes. Put a form in front of them rather than a log line
                # nobody reads (#47).
                _LOGGER.warning(
                    "StreamRelay: the stream is encrypted and EZVIZ has "
                    "just e-mailed the 4-digit code that unlocks it "
                    "(serial=%s). Enter it in Settings -> Repairs within 30 "
                    "minutes, or call ezviz_instant_bell.fetch_encryption_key with "
                    "it. The 6-character label code will NOT work here.",
                    self._serial,
                )
                if self._hass is not None:
                    try:
                        self._hass.loop.call_soon_threadsafe(
                            self._raise_otp_repair
                        )
                    except Exception:  # noqa: BLE001
                        pass
                return
            _LOGGER.warning(
                "StreamRelay: the stream is encrypted but no key is "
                "available (serial=%s). Call ezviz_instant_bell.fetch_encryption_key "
                "and enter the 4-digit code EZVIZ e-mails in Settings -> "
                "Repairs. The 6-character label code will NOT work here.",
                self._serial,
            )
            return
        self._decrypt_key = key
        _LOGGER.warning(
            "StreamRelay: stream is encrypted — decrypting it with the "
            "camera key (serial=%s)", self._serial,
        )

    def _serial_needs_otp(self) -> bool:
        """True when the cloud asked for elevation on the last key request."""
        try:
            bare = self._api._bare_serial(self._serial)
            return bare in getattr(self._api, "key_needs_otp", set())
        except Exception:  # noqa: BLE001
            return False

    def _raise_otp_repair(self) -> None:
        """Repairs form asking for the code EZVIZ just mailed (loop thread)."""
        try:
            from homeassistant.helpers import issue_registry as ir

            ir.async_create_issue(
                self._hass,
                DOMAIN,
                f"encryption_otp_{self._serial}",
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="encryption_otp",
                translation_placeholders={"serial": self._serial},
                data={"serial": self._serial},
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("StreamRelay: could not raise OTP repair: %s", exc)

    def _check_scrambled(self, parser: Any) -> None:
        """Tell the user when the device is scrambling the stream (#41).

        With Image/Video Encryption enabled the container and NAL framing
        stay perfectly readable while the payloads are scrambled, so ffmpeg
        spins on garbage and every symptom points at a demux bug. The stream
        announces the condition itself via PES_scrambling_control, so say so
        instead of letting people chase it — and note that a firmware or app
        update can silently re-enable encryption behind the user's back.
        """
        if self._warned_scrambled or parser.scrambled_packets < 8:
            return
        self._warned_scrambled = True
        _LOGGER.warning(
            "StreamRelay: the doorbell is SCRAMBLING the video "
            "(PES_scrambling_control set on %d packets, serial=%s). Image/"
            "Video Encryption is enabled on the device; looking for the key "
            "so the stream can be decrypted.",
            parser.scrambled_packets, self._serial,
        )
        self._enable_decryption()
        if self._decrypt_key is not None:
            # Decrypting, so no need to nag the user about the app setting.
            return
        if self._hass is not None:
            try:
                self._hass.loop.call_soon_threadsafe(self._raise_scrambled_repair)
            except Exception:  # noqa: BLE001
                pass

    def _raise_scrambled_repair(self) -> None:
        """Repairs notice for an encrypted stream (loop thread)."""
        try:
            from homeassistant.helpers import issue_registry as ir

            ir.async_create_issue(
                self._hass,
                DOMAIN,
                f"stream_encrypted_{self._serial}",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="stream_encrypted",
                translation_placeholders={"serial": self._serial},
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("StreamRelay: could not raise repair: %s", exc)

    def _warn_if_hevc_on_webrtc(self, codec: Optional[str]) -> None:
        """Log a one-shot warning when HEVC is seen on the WebRTC path.

        Browsers can't decode an HEVC copy over WebRTC, so go2rtc must
        transcode; on weak hosts that fails with a grey screen or
        'dial tcp ... connection refused' (#36 andresako). MJPEG mode
        sidesteps this entirely and is now the default — steer the user there.
        """
        if (
            codec == "hevc"
            and self._stream_mode == "webrtc"
            and not self._warned_hevc_webrtc
        ):
            self._warned_hevc_webrtc = True
            _LOGGER.warning(
                "StreamRelay: HEVC/H.265 detected while Stream mode is "
                "'webrtc' (serial=%s). Browsers can't show HEVC over WebRTC and "
                "go2rtc must transcode, which fails on weak hosts. Switch "
                "Stream mode to 'mjpeg' (Configure) for a codec-agnostic, "
                "go2rtc-free live view.",
                self._serial,
            )
            # Surface it in the UI (Settings -> Repairs), not just the log.
            # The sniff runs in the broadcast reader thread, so hop to the loop.
            if self._hass is not None:
                try:
                    self._hass.loop.call_soon_threadsafe(self._raise_hevc_repair)
                except Exception:  # noqa: BLE001
                    pass

    def _raise_hevc_repair(self) -> None:
        """Create a Repairs issue steering the user to MJPEG (loop thread)."""
        try:
            from homeassistant.helpers import issue_registry as ir

            ir.async_create_issue(
                self._hass,
                DOMAIN,
                f"hevc_webrtc_{self._serial}",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="hevc_webrtc",
                translation_placeholders={"serial": self._serial},
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("StreamRelay: could not raise HEVC repair: %s", exc)

    async def start(self) -> None:
        if self._server is not None:
            return
        # Try the configured fixed port first; fall back to a random free
        # one if it's taken so HA setup doesn't fail when (e.g.) the user
        # left a previous relay running.
        try:
            self._server = await asyncio.start_server(
                self._handle_client, self._bind_host, self._listen_port
            )
        except OSError as exc:
            if self._listen_port:
                _LOGGER.warning(
                    "StreamRelay: fixed port %d busy (%s); falling back to "
                    "a random one",
                    self._listen_port, exc,
                )
                self._server = await asyncio.start_server(
                    self._handle_client, self._bind_host, 0
                )
            else:
                raise
        sock = self._server.sockets[0]
        self._port = int(sock.getsockname()[1])
        _LOGGER.debug(
            "StreamRelay listening on tcp://%s:%d "
            "(serial=%s aggressive_mpegts=%s video_codec=%s)",
            self._bind_host, self._port, self._serial,
            self._aggressive_mpegts, self._video_codec,
        )
        if self._diagnostic_dumps:
            _LOGGER.warning(
                "StreamRelay: diagnostic dumps ENABLED — writing stream "
                "captures to the config dir (serial=%s). Turn the option off "
                "again once you have collected them.",
                self._serial,
            )
        if self._bind_host not in ("127.0.0.1", "localhost", "::1"):
            _LOGGER.warning(
                "StreamRelay: relay bound to %s — the raw stream on port "
                "%d is UNAUTHENTICATED; make sure only trusted hosts can "
                "reach it",
                self._bind_host, self._port,
            )

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        try:
            await self._server.wait_closed()
        except Exception:
            pass
        self._server = None
        self._port = 0
        await self._shutdown_shared()

    # ------------------------------------------------------------------
    # Shared VTM pre-warm
    # ------------------------------------------------------------------

    async def _open_source(self, loop, ezviz_client):
        """Open the configured stream source (cloud VTM or CPD7 LAN).

        Returns an object exposing ``start()`` / ``iter_payloads()`` /
        ``close()``. For "auto" the LAN path is tried first and the cloud
        VTM is used as fallback.
        """
        def _open_cloud():
            return open_cloud_stream(
                ezviz_client, self._serial, channel=self._channel
            )

        def _open_lan():
            return Cpd7LanSource(
                self._api, self._serial, channel=self._channel,
                stream_quality=self._stream_quality,
            )

        if self._stream_source == "cloud":
            return await loop.run_in_executor(None, _open_cloud)
        if self._stream_source == "local":
            return await loop.run_in_executor(None, _open_lan)
        # auto: prefer LAN, fall back to cloud on any LAN failure.
        try:
            src = await loop.run_in_executor(None, _open_lan)
            # Probe the LAN handshake now so a failure falls back to cloud
            # before we commit the broadcast reader to it.
            await loop.run_in_executor(None, src.start)
            return _PreStartedSource(src)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.info(
                "StreamRelay: LAN source unavailable (%s) — falling back "
                "to cloud VTM (serial=%s)", exc, self._serial,
            )
            return await loop.run_in_executor(None, _open_cloud)

    async def prewarm(self) -> None:
        """Open (or extend) a shared VTM session that future HA Stream
        clients can reuse instead of paying the cloud handshake cost.

        Safe to call repeatedly: if a shared session is already active the
        idle teardown timer is just reset.
        """
        loop = asyncio.get_event_loop()
        self._loop = loop
        async with self._shared_lock:
            if self._shared_vtm is not None:
                self._arm_idle_timer()
                _LOGGER.debug(
                    "StreamRelay: prewarm extended (serial=%s)", self._serial
                )
                return
            # Rate-limit shares the relay's circuit-breaker.
            wait = self._seconds_until_next_attempt()
            if wait > 0:
                _LOGGER.debug(
                    "StreamRelay: prewarm skipped — rate-limited (%.0fs)",
                    wait,
                )
                return
            self._last_attempt = loop.time()
            try:
                ezviz_client = self._api.client
                if ezviz_client is None and self._stream_source == "cloud":
                    raise RuntimeError("EZVIZ session not available")
                vtm = await self._open_source(loop, ezviz_client)
                info = await loop.run_in_executor(None, vtm.start)
                self._consecutive_failures = 0
                self._last_error = None
                self._last_error_offline = False
                _LOGGER.info(
                    "StreamRelay: pre-warm source up (serial=%s src=%s ssn=%s)",
                    self._serial,
                    self._stream_source,
                    getattr(info, "streamssn", "?"),
                )
            except Exception as exc:
                self._consecutive_failures += 1
                self._last_error = str(exc)
                self._last_error_offline = _is_offline_error(exc)
                _LOGGER.warning(
                    "StreamRelay: prewarm failed (%d/%d, offline=%s, "
                    "source=%s): %s",
                    self._consecutive_failures, LOCKOUT_THRESHOLD,
                    self._last_error_offline, self._stream_source, exc,
                )
                return
            self._shared_vtm = vtm
            self._active_lan = isinstance(
                vtm, (Cpd7LanSource, _PreStartedSource)
            )
            self._shared_stop = threading.Event()
            self._shared_reader = threading.Thread(
                target=self._broadcast_reader,
                name=f"hp7-vtm-broadcast-{self._serial}",
                daemon=True,
            )
            self._shared_reader.start()
            self._arm_idle_timer()

    def _arm_idle_timer(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        if self._active_clients > 0:
            # Don't tear down while clients are connected — they cover us.
            return
        loop = asyncio.get_event_loop()
        self._idle_handle = loop.call_later(
            PREWARM_IDLE_TIMEOUT, self._idle_expired
        )

    def _idle_expired(self) -> None:
        self._idle_handle = None
        if self._active_clients > 0:
            return
        asyncio.create_task(self._shutdown_shared())

    async def _shutdown_shared(self) -> None:
        async with self._shared_lock:
            vtm = self._shared_vtm
            self._shared_vtm = None
            self._shared_stop.set()
            for q in (
                list(self._sub_v_qs)
                + list(self._sub_a_qs)
                + list(self._sub_raw_qs)  # LAN clients subscribe here (#51)
            ):
                _put_sentinel(q)
            if self._idle_handle is not None:
                self._idle_handle.cancel()
                self._idle_handle = None
        if vtm is not None:
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(None, vtm.close)
            except Exception:
                pass
        if self._shared_reader is not None:
            # A sender thread can sit in accept() or sendall() past the timeout,
            # so join off the event loop rather than blocking it.
            await asyncio.get_event_loop().run_in_executor(None, self._shared_reader.join, 2.0)
            self._shared_reader = None
        _LOGGER.debug("StreamRelay: shared VTM torn down (serial=%s)", self._serial)

    async def _handle_reader_exit(self, my_vtm: Any) -> None:
        """Reset shared-session state after the broadcast reader dies on
        its own (network error, device offline, etc. — NOT a requested
        teardown via _shutdown_shared).

        Without this, self._shared_vtm stays non-None forever: prewarm()
        keeps seeing "a shared session is already active" and every future
        client just subscribes to queues nobody is feeding any more, then
        sits through the full ffmpeg stall timeout before giving up — on
        loop, until HA itself is restarted.
        """
        async with self._shared_lock:
            if self._shared_vtm is not my_vtm:
                # Already superseded by a clean shutdown/restart; nothing
                # to do here.
                return
            self._shared_vtm = None
            self._active_lan = False
            if self._idle_handle is not None:
                self._idle_handle.cancel()
                self._idle_handle = None
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, my_vtm.close)
        except Exception:  # noqa: BLE001
            pass
        self._shared_reader = None
        _LOGGER.debug(
            "StreamRelay: shared VTM reset after unexpected reader exit "
            "(serial=%s)", self._serial,
        )

    def _broadcast_reader(self) -> None:
        """Read VTM payloads -> PesParser -> fan out to per-client queues."""
        my_vtm = self._shared_vtm
        parser = PesParser()
        # Fresh session — a cached GOP from the previous source is stale.
        with self._gop_lock:
            self._gop_buf = bytearray()
            self._param_sets = b""
        self._es_dump_buf = bytearray()
        self._es_dump_done = False
        self._ps_dump_buf = bytearray()
        self._ps_dump_done = False
        self._kf_markers_seen = 0
        self._drops = 0
        self._q_last_drain = {}
        self._q_resync = set()
        self._decrypt_buf = bytearray()
        self._warned_saturation = False
        self._warned_scrambled = False
        self._decrypt_buf = bytearray()
        self._decrypt_header_size = None
        v_bytes = a_bytes = 0
        # Time-based, not byte-based: at ~400 KB/s a 256 KB threshold emitted
        # an INFO line every ~0.6 s forever — a 24/7 Frigate consumer logged
        # tens of thousands of lines (#44, 20 GB session).
        next_v_log = next_a_log = time.monotonic() + PROGRESS_LOG_SEC
        try:
            for body in self._shared_vtm.iter_payloads():
                if self._shared_stop.is_set():
                    break
                if not body:
                    continue
                if self._active_lan:
                    # LAN path: the decoder emits complete MPEG-PS (muxed
                    # video + audio). Fan the raw feed out for the VIDEO
                    # (one ffmpeg `-f mpeg` input, demuxed internally) AND
                    # separately extract the AUDIO PES (0xC0) — the LAN
                    # MPEG-PS PMT mislabels the audio as MP2 but it's
                    # actually AAC ADTS (confirmed from a capture: ff f1
                    # ... = AAC LC 16 kHz mono). ffmpeg can't decode it as
                    # MP2, so we feed the extracted AAC to a second input
                    # instead. Video stays a clean raw passthrough; audio
                    # comes from the de-mislabelled PES.
                    body = self._maybe_decrypt(body)
                    if not body:
                        continue
                    v_bytes += len(body)
                    self._gop_update(body)
                    self._ps_dump(body)
                    self._fanout(self._sub_raw_qs, body, video=True)
                    for stream_id, payload in parser.feed(body):
                        if stream_id == AUDIO_STREAM_ID and payload:
                            a_bytes += len(payload)
                            self._fanout(self._sub_a_qs, payload)
                        elif stream_id == VIDEO_STREAM_ID and payload:
                            self._es_dump(payload)
                            if self._detected_codec is None:
                                # Sniff h264 vs hevc so "auto" knows whether
                                # to transcode on the LAN path too (CP7
                                # streams HEVC, which WebRTC can't show as a
                                # copy — #37 Quenbo).
                                guess = _sniff_video_codec(payload)
                                if guess is not None:
                                    self._detected_codec = guess
                                    _LOGGER.info(
                                        "StreamRelay: detected LAN video "
                                        "codec=%s (serial=%s)",
                                        guess, self._serial,
                                    )
                                    self._warn_if_hevc_on_webrtc(guess)
                    self._check_scrambled(parser)
                    if time.monotonic() >= next_v_log:
                        _LOGGER.info(
                            "StreamRelay: broadcast LAN MPEG-PS progress "
                            "%d B audio=%d B subs=%d kf_markers=%d drops=%d",
                            v_bytes, a_bytes, len(self._sub_raw_qs),
                            self._kf_markers_seen, self._drops,
                        )
                        next_v_log = time.monotonic() + PROGRESS_LOG_SEC
                    continue
                for stream_id, payload in parser.feed(body):
                    if not payload:
                        continue
                    if stream_id == VIDEO_STREAM_ID:
                        v_bytes += len(payload)
                        self._gop_update(payload)
                        self._es_dump(payload)
                        if self._detected_codec is None:
                            guess = _sniff_video_codec(payload)
                            if guess is not None:
                                self._detected_codec = guess
                                _LOGGER.info(
                                    "StreamRelay: detected video codec=%s "
                                    "(serial=%s)",
                                    guess, self._serial,
                                )
                                self._warn_if_hevc_on_webrtc(guess)
                        self._fanout(self._sub_v_qs, payload, video=True)
                        if time.monotonic() >= next_v_log:
                            _LOGGER.info(
                                "StreamRelay: broadcast video progress %d B "
                                "subs=%d drops=%d",
                                v_bytes,
                                len(self._sub_v_qs),
                                self._drops,
                            )
                            next_v_log = time.monotonic() + PROGRESS_LOG_SEC
                    elif stream_id == AUDIO_STREAM_ID:
                        a_bytes += len(payload)
                        self._fanout(self._sub_a_qs, payload)
                        if time.monotonic() >= next_a_log:
                            _LOGGER.info(
                                "StreamRelay: broadcast audio progress %d B "
                                "subs=%d",
                                a_bytes,
                                len(self._sub_a_qs),
                            )
                            next_a_log = time.monotonic() + PROGRESS_LOG_SEC
        except Exception as exc:
            # EBADF here is the expected race when the shared VTM session
            # is torn down by another thread (idle timeout or stop()) while
            # the reader is still parked in recv(). Demote to debug; only
            # surface unexpected exceptions at WARNING.
            errno_val = getattr(exc, "errno", None)
            if (
                self._shared_stop.is_set()
                or errno_val == 9  # EBADF
                or "Bad file descriptor" in str(exc)
            ):
                _LOGGER.debug(
                    "StreamRelay: broadcast reader stopped (expected "
                    "teardown): %s",
                    exc,
                )
            else:
                _LOGGER.warning(
                    "StreamRelay: broadcast reader stopped: %s", exc
                )
        finally:
            for q in (
                list(self._sub_v_qs)
                + list(self._sub_a_qs)
                + list(self._sub_raw_qs)
            ):
                _put_sentinel(q)
            if not self._shared_stop.is_set() and self._loop is not None:
                # Exited on its own (network error) rather than via
                # _shutdown_shared, which would have already cleared
                # self._shared_vtm itself. Hop onto the event loop to reset
                # it there too, so the next prewarm() opens a fresh session
                # instead of reusing a session nothing is feeding any more.
                try:
                    asyncio.run_coroutine_threadsafe(
                        self._handle_reader_exit(my_vtm), self._loop
                    )
                except Exception:  # noqa: BLE001
                    pass
            _LOGGER.info(
                "StreamRelay: broadcast done video=%d B audio=%d B "
                "resync_drops=%d",
                v_bytes, a_bytes, parser.resync_drops,
            )

    def _required_cooldown(self) -> float:
        if self._last_error_offline:
            # A rebooting doorbell isn't the account-ban risk the lockout
            # exists for (see module docstring), so this branch starts at
            # the normal cadence and a reboot (~60-120s) doesn't cost a
            # 10-minute wait.
            #
            # It does back off, though. "Offline" also covers the LAN
            # channel refusing InviteStream, which #52 measured lasting
            # 2h20m on an HP5 while the device was demonstrably alive —
            # and since every LAN retry now invalidates the cached control
            # key, each one costs a p2p-register plus a CAS query. Held at
            # a flat 30s that stall would be ~280 retries and ~560 cloud
            # calls; doubling every second failure up to OFFLINE_MAX_BACKOFF
            # makes it ~33, while the first two retries still land inside
            # the first minute where a genuine reboot recovers.
            step = max(0, self._consecutive_failures - 3) // 2
            return min(MIN_RETRY_INTERVAL * (2 ** step), OFFLINE_MAX_BACKOFF)
        if self._consecutive_failures >= LOCKOUT_THRESHOLD:
            return LOCKOUT_BACKOFF
        return MIN_RETRY_INTERVAL

    def _seconds_until_next_attempt(self) -> float:
        if self._last_error is None or self._last_attempt == 0.0:
            return 0.0
        elapsed = asyncio.get_event_loop().time() - self._last_attempt
        return self._required_cooldown() - elapsed

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        _LOGGER.debug("StreamRelay: client connected from %s", peer)

        wait = self._seconds_until_next_attempt()
        if wait > 0:
            _LOGGER.warning(
                "StreamRelay: rate-limited (last error: %s; %d consecutive "
                "failures; refusing for another %.0fs)",
                self._last_error, self._consecutive_failures, wait,
            )
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            return

        proc: Optional[asyncio.subprocess.Process] = None
        v_listener: Optional[socket.socket] = None
        a_listener: Optional[socket.socket] = None
        threads: List[threading.Thread] = []
        stop_event = threading.Event()
        v_q: "queue.Queue[Optional[bytes]]" = queue.Queue(
            maxsize=VIDEO_QUEUE_SIZE
        )
        a_q: "queue.Queue[Optional[bytes]]" = queue.Queue(
            maxsize=PAYLOAD_QUEUE_SIZE
        )
        raw_q: "queue.Queue[Optional[bytes]]" = queue.Queue(
            maxsize=VIDEO_QUEUE_SIZE
        )
        raw_listener: Optional[socket.socket] = None

        # Ensure a shared VTM is running (this is the cold path on first
        # connect; subsequent connects reuse the same session for the next
        # PREWARM_IDLE_TIMEOUT seconds after the last client leaves).
        await self.prewarm()
        if self._shared_vtm is None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            return

        # Subscribe to the broadcast. LAN serves one muxed MPEG-PS feed;
        # cloud serves split video/audio PES streams.
        lan = self._active_lan
        # Preload the cached GOP (#37) so this viewer starts painting from
        # the last keyframe immediately instead of sitting blank until the
        # doorbell emits the next one (tens of seconds on a quiet scene).
        # Preload BEFORE subscribing: chunks land in order ahead of the live
        # feed; a queue.Full mid-preload truncates the replay and the viewer
        # just falls back to waiting for the next keyframe.
        gop = self._gop_snapshot()
        if lan:
            for chunk in gop:
                try:
                    raw_q.put_nowait(chunk)
                except queue.Full:
                    break
            now0 = time.monotonic()
            self._q_last_drain[id(raw_q)] = now0
            self._q_last_drain[id(a_q)] = now0
            self._sub_raw_qs.append(raw_q)
            self._sub_a_qs.append(a_q)  # de-mislabelled AAC audio
        else:
            for chunk in gop:
                try:
                    v_q.put_nowait(chunk)
                except queue.Full:
                    break
            now0 = time.monotonic()
            self._q_last_drain[id(v_q)] = now0
            self._q_last_drain[id(a_q)] = now0
            self._sub_v_qs.append(v_q)
            self._sub_a_qs.append(a_q)
        if gop:
            _LOGGER.debug(
                "StreamRelay: preloaded %d GOP chunks to new client",
                len(gop),
            )
        self._active_clients += 1
        self._arm_idle_timer()  # cancel idle teardown while we're connected

        # Wall-clock re-timestamping was added for #37: the device's
        # uptime-based DTS is already large and wraps the 33-bit MPEG-TS
        # clock within hours, which makes HA's stream worker abort with
        # "Timestamp discontinuity". But it paces the output by ARRIVAL
        # time instead of capture time, so any systematic pacing error
        # accumulates linearly and never recovers — the unbounded delay in
        # #44, which VLC saw too (i.e. upstream of the MJPEG path). Only
        # HA's stream worker actually needs it, so restrict it to WebRTC
        # and let +genpts derive timestamps from the stream everywhere else.
        wallclock = (
            ["-use_wallclock_as_timestamps", "1"]
            if self._stream_mode == "webrtc"
            else []
        )
        try:
            if lan:
                # LAN: one input, the muxed MPEG-PS feed. ffmpeg demuxes it
                # internally and copies video. Codec autodetected by the
                # mpeg demuxer, so no -f h264/hevc guessing.
                #
                # Audio is dropped (-an) for now. The LAN MPEG-PS tags audio
                # as MP2 but the frames have broken headers ("mp2 Header
                # missing", #36 bob) — it's almost certainly AAC/G.711
                # mislabelled, like the VTM cloud path. Undecodable MP2 can't
                # be re-encoded to the AAC that HA's WebRTC needs, and copying
                # raw MP2 doesn't play over WebRTC either, so we keep video
                # clean and stable and revisit audio once the real codec is
                # identified from a capture.
                raw_port = _free_local_port()
                a_port = _free_local_port()
                raw_listener = _start_local_listener(raw_port)
                a_listener = _start_local_listener(a_port)
                # Transcode HEVC -> H.264 so HA's WebRTC path shows a picture
                # (HEVC copy = grey screen / decoder errors, #36 hehsni, #37
                # Quenbo CP7). Transcode when the user forces hevc OR when
                # auto sniffed HEVC off the stream. hevc_copy / h264 pass the
                # elementary stream through untouched (HEVC-capable players,
                # Frigate, H.264 firmware).
                # In MJPEG mode the per-viewer ffmpeg decodes whatever codec
                # to JPEG itself, so transcoding HEVC->H.264 here is pure waste
                # (a double decode+encode) and its decoder was the source of
                # the "VPS 0 does not exist" errors on HPD7 (#39). Only the
                # WebRTC path actually needs H.264, so gate the transcode on it.
                lan_transcode = self._stream_mode == "webrtc" and (
                    self._video_codec == "hevc"
                    or (
                        self._video_codec == "auto"
                        and self._detected_codec == "hevc"
                    )
                )
                cmd = [
                    self._ffmpeg_path,
                    "-hide_banner", "-loglevel", "error",
                    # No +nobuffer on the input: nobuffer pins analyzeduration
                    # to 0, so ffmpeg gives up before it sees a full HEVC IDR
                    # with its VPS/SPS/PPS and can't determine the frame size
                    # ("Could not find codec parameters ... unspecified size",
                    # #39 HPD7). A large analyzeduration/probesize is a ceiling,
                    # not a fixed wait — it starts as soon as params are known.
                    "-fflags", "+genpts", "-flags", "low_delay",
                    "-analyzeduration", "10000000", "-probesize", "10000000",
                    # NO wall-clock re-timestamping on this input. It is a
                    # real MPEG-PS container that already carries correct
                    # timestamps; stamping by arrival instead collapses the
                    # timeline, because the doorbell delivers over TCP faster
                    # than realtime. Measured on a live capture: with
                    # wallclock consecutive frames land ~11 us apart (15
                    # frames inside one millisecond), so HA's stream worker
                    # sees zero-duration frames and cannot build segments —
                    # the picture freezes while the separately re-encoded
                    # audio keeps playing. With +genpts alone the same
                    # capture yields a clean 66.667 ms spacing (15 fps).
                    # Wallclock stays on the raw elementary inputs below and
                    # on the cloud path, which genuinely have no timestamps.
                    "-f", "mpeg",
                    "-i", f"tcp://127.0.0.1:{raw_port}",
                    "-analyzeduration", "200000", "-probesize", "200000",
                    *wallclock,
                    "-f", "aac",
                    "-i", f"tcp://127.0.0.1:{a_port}",
                    # Video from the raw MPEG-PS input (its mislabelled MP2
                    # audio is ignored, not mapped); audio from the extracted
                    # AAC input.
                    "-map", "0:v:0", "-map", "1:a:0",
                ]
                if lan_transcode:
                    cmd += [
                        "-c:v", "libx264",
                        "-preset", "ultrafast", "-tune", "zerolatency",
                        "-pix_fmt", "yuv420p",
                    ]
                else:
                    cmd += ["-c:v", "copy"]
                cmd += [
                    "-c:a", "aac", "-ar", "16000", "-ac", "1", "-b:a", "32k",
                    "-max_interleave_delta", "0",
                    "-mpegts_flags", "+resend_headers",
                    "-pat_period", "1",
                    "-sdt_period", "1",
                    "-f", "mpegts", "pipe:1",
                ]
            else:
                v_port = _free_local_port()
                a_port = _free_local_port()
                v_listener = _start_local_listener(v_port)
                a_listener = _start_local_listener(a_port)

                # Resolve the input codec + whether to transcode.
                #   h264       -> input h264, copy
                #   hevc       -> input hevc, transcode to h264 (WebRTC-safe)
                #   hevc_copy  -> input hevc, copy (low-power hosts, #36)
                #   auto       -> sniff; hevc gets transcoded, else copy
                if self._video_codec in ("h264", "hevc", "hevc_copy"):
                    cfg = self._video_codec
                elif self._detected_codec == "hevc":
                    cfg = "hevc"
                else:
                    cfg = "h264"
                in_is_hevc = cfg in ("hevc", "hevc_copy")
                transcode = cfg == "hevc"
                in_fmt = "hevc" if in_is_hevc else "h264"

                cmd = [
                    self._ffmpeg_path,
                    "-hide_banner", "-loglevel", "error",
                    "-fflags", "+genpts+nobuffer", "-flags", "low_delay",
                    "-analyzeduration", "200000", "-probesize", "200000",
                    *wallclock,
                    "-f", in_fmt, "-r", "15",
                    "-i", f"tcp://127.0.0.1:{v_port}",
                    "-analyzeduration", "200000", "-probesize", "200000",
                    *wallclock,
                    "-f", "aac",
                    "-i", f"tcp://127.0.0.1:{a_port}",
                    "-map", "0:v:0", "-map", "1:a:0",
                ]
                if transcode:
                    # HEVC->H.264: HA's go2rtc/WebRTC path can't hand H.265 to
                    # most browsers, so a copy leaves a grey screen (#36, #37).
                    # zerolatency keeps the relay live. Heavy on weak CPUs —
                    # hevc_copy skips this for hosts that can't afford it.
                    cmd += [
                        "-c:v", "libx264",
                        "-preset", "ultrafast", "-tune", "zerolatency",
                        "-pix_fmt", "yuv420p",
                    ]
                else:
                    cmd += ["-c:v", "copy"]
                cmd += [
                    # Re-encode audio so the mpegts muxer gets a proper AAC
                    # AudioSpecificConfig extradata (inbound ADTS strips it).
                    "-c:a", "aac", "-ar", "16000", "-ac", "1", "-b:a", "32k",
                    "-max_interleave_delta", "0",
                ]
                # Opt-in: re-prepend SPS/PPS in front of every IDR for
                # firmwares that only emit them at the first IDR (#33).
                # Skipped on transcode (libx264 already inlines them).
                if self._aggressive_mpegts and not transcode:
                    cmd += ["-bsf:v", "dump_extra"]
                cmd += [
                    "-mpegts_flags", "+resend_headers",
                    "-pat_period", "1",
                    "-sdt_period", "1",
                    "-f", "mpegts", "pipe:1",
                ]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            # Drain ffmpeg stderr to the logger so 0.9.x's silent-failure
            # mode (#33) becomes visible. One line per ffmpeg message.
            if proc.stderr is not None:
                async def _drain_ff_err(stream: asyncio.StreamReader) -> None:
                    try:
                        while True:
                            line = await stream.readline()
                            if not line:
                                return
                            _LOGGER.debug(
                                "StreamRelay: ffmpeg | %s",
                                line.decode(errors="replace").rstrip(),
                            )
                    except Exception:
                        return
                asyncio.create_task(_drain_ff_err(proc.stderr))

            # Reader is the broadcast (shared source); only senders are
            # per-client. LAN feeds a single muxed socket; cloud feeds two.
            if lan:
                raw_sender_t = threading.Thread(
                    target=_sender_thread,
                    args=(raw_listener, raw_q, stop_event, "mpegps", _mk_drain(self._q_last_drain, raw_q)),
                    name=f"hp7-lan-send-{self._serial}",
                    daemon=True,
                )
                a_sender_t = threading.Thread(
                    target=_sender_thread,
                    args=(a_listener, a_q, stop_event, "audio", _mk_drain(self._q_last_drain, a_q)),
                    name=f"hp7-lan-asend-{self._serial}",
                    daemon=True,
                )
                threads = [raw_sender_t, a_sender_t]
            else:
                v_sender_t = threading.Thread(
                    target=_sender_thread,
                    args=(v_listener, v_q, stop_event, "video", _mk_drain(self._q_last_drain, v_q)),
                    name=f"hp7-vtm-vsend-{self._serial}",
                    daemon=True,
                )
                a_sender_t = threading.Thread(
                    target=_sender_thread,
                    args=(a_listener, a_q, stop_event, "audio", _mk_drain(self._q_last_drain, a_q)),
                    name=f"hp7-vtm-asend-{self._serial}",
                    daemon=True,
                )
                threads = [v_sender_t, a_sender_t]
            for t in threads:
                t.start()

            assert proc.stdout is not None
            got_output = False
            while True:
                try:
                    data = await asyncio.wait_for(
                        proc.stdout.read(RELAY_CHUNK),
                        timeout=(
                            OUTPUT_STALL_TIMEOUT
                            if got_output
                            else FIRST_OUTPUT_TIMEOUT
                        ),
                    )
                except asyncio.TimeoutError:
                    _LOGGER.warning(
                        "StreamRelay: relay ffmpeg produced no output for "
                        "%.0fs (serial=%s, got_output=%s) — closing session",
                        OUTPUT_STALL_TIMEOUT if got_output
                        else FIRST_OUTPUT_TIMEOUT,
                        self._serial, got_output,
                    )
                    break
                if not data:
                    break
                got_output = True
                try:
                    writer.write(data)
                    await writer.drain()
                except (ConnectionResetError, BrokenPipeError):
                    break
        except Exception as exc:
            _LOGGER.warning(
                "StreamRelay: stream error for serial=%s: %s",
                self._serial, exc,
            )
        finally:
            stop_event.set()
            # Unsubscribe from the shared broadcast.
            for q in (v_q, a_q, raw_q):
                _put_sentinel(q)
            for qs, q in (
                (self._sub_v_qs, v_q),
                (self._sub_a_qs, a_q),
                (self._sub_raw_qs, raw_q),
            ):
                try:
                    qs.remove(q)
                except ValueError:
                    pass
            for lst in (v_listener, a_listener, raw_listener):
                if lst is not None:
                    try:
                        lst.close()
                    except OSError:
                        pass
            self._active_clients = max(0, self._active_clients - 1)
            self._arm_idle_timer()

            if proc is not None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=FFMPEG_KILL_TIMEOUT)
                except (asyncio.TimeoutError, Exception):
                    pass
            for listener in (v_listener, a_listener):
                if listener is not None:
                    try:
                        listener.close()
                    except OSError:
                        pass
            for t in threads:
                # These threads can block in sendall()/accept(); join off the loop.
                await asyncio.get_event_loop().run_in_executor(None, t.join, 2.0)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            _LOGGER.debug("StreamRelay: client %s closed", peer)
