"""Feeding ffmpeg: local listening sockets and the sender threads that write a queue to them."""
# Adapted for EZVIZ HP7 / CP7 Video Intercom from the ezviz_hp7 integration by Bobsilvio
# (MIT, https://github.com/Bobsilvio/ezviz_hp7); see THIRD_PARTY_LICENSES.md.
# The local-network video protocol and the motion-JPEG view are by albrzmr (MIT).
from __future__ import annotations

import logging
import queue
import socket
import threading
import time
from contextlib import closing
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)

# How long a single sendall() to a client's ffmpeg may block before we give
# up on that client. Slightly above DEAD_SUB_TIMEOUT so the gentler
# eviction path gets its chance first (#51).
SEND_STALL_TIMEOUT = 45.0

# How long a sender waits for ffmpeg to connect to its input, as a backstop
# only: the wait normally ends with the session (its stop event). ffmpeg
# opens its inputs one after another and first probes the video input until
# it has seen a keyframe, which on an HP7 can be tens of seconds away (#20,
# #37) — a short limit closed the audio input before ffmpeg got to it.
INPUT_ACCEPT_TIMEOUT = 90.0
INPUT_ACCEPT_POLL = 0.5


def _free_local_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_local_listener(port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(1)
    return s


def _mk_drain(table: dict, q: Any):
    """Return a callback stamping this queue's last-drain time (#44)."""
    qid = id(q)

    def _stamp() -> None:
        table[qid] = time.monotonic()

    return _stamp


def _put_sentinel(q: "queue.Queue[Optional[bytes]]") -> None:
    """Deliver the stop sentinel, clearing the queue first if need be (#51).

    Under the drop-oldest policy a subscriber queue whose consumer is even
    marginally slower than the producer sits full permanently by design, so
    a bare put_nowait(None) raises queue.Full exactly when the sentinel
    matters most — on eviction and on teardown. The sender thread then never
    sees it, _handle_client never reaches its finally, and its ffmpeg is
    never killed: one leaked process per session, plus the mjpeg one.
    Draining first costs the queued backlog, which is being discarded
    anyway.
    """
    try:
        q.put_nowait(None)
        return
    except queue.Full:
        pass
    try:
        while True:
            q.get_nowait()
    except queue.Empty:
        pass
    try:
        q.put_nowait(None)
    except queue.Full:
        # Producer refilled it between the drain and the put; the sender
        # also watches the stop event, so this is not the only way out.
        pass


def _sender_thread(
    listener: socket.socket,
    q: "queue.Queue[Optional[bytes]]",
    stop: threading.Event,
    label: str,
    on_drain: Any = None,
) -> None:
    """Wait for ffmpeg to connect, then forward the queue into that socket."""
    try:
        listener.settimeout(INPUT_ACCEPT_POLL)
        deadline = time.monotonic() + INPUT_ACCEPT_TIMEOUT
        while True:
            if stop.is_set():
                return  # the session ended before ffmpeg got to this input
            try:
                conn, peer = listener.accept()
                break
            except socket.timeout:
                if time.monotonic() >= deadline:
                    _LOGGER.warning(
                        "StreamRelay: ffmpeg %s input accept timed out", label
                    )
                    return
            except OSError:
                # The session closed the listener while we waited (#20).
                return
        _LOGGER.debug("StreamRelay: %s accepted from %s", label, peer)
    finally:
        try:
            listener.close()
        except OSError:
            pass

    sent = 0
    # A blocking sendall() to an ffmpeg that has stopped reading never
    # returns, and neither the stop event nor a queue sentinel can reach a
    # thread parked inside it — that is how sessions leaked an ffmpeg each
    # (#51). A send timeout is the only thing that unblocks it. A consumer
    # that has taken nothing for this long is gone, so treat the timeout as
    # terminal rather than resuming a half-written frame.
    try:
        conn.settimeout(SEND_STALL_TIMEOUT)
    except OSError:
        pass
    try:
        while not stop.is_set():
            try:
                payload = q.get(timeout=0.5)
            except queue.Empty:
                continue
            if payload is None:
                return
            if on_drain is not None:
                # Liveness signal: this subscriber's consumer is alive and
                # actually taking data (#44 follow-up).
                on_drain()
            try:
                conn.sendall(payload)
                sent += len(payload)
            except socket.timeout:
                _LOGGER.warning(
                    "StreamRelay: %s consumer stopped reading for %.0fs — "
                    "closing it so its ffmpeg can be reaped (%d B sent)",
                    label, SEND_STALL_TIMEOUT, sent,
                )
                return
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
    finally:
        _LOGGER.debug("StreamRelay: %s sender done (%d B sent)", label, sent)
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass
