"""The relay's input senders: waiting for ffmpeg to connect, and stopping cleanly (#20)."""

import queue
import socket
import threading
import time
from unittest.mock import patch

import pytest

from custom_components.ezviz_instant_bell import live_io


@pytest.fixture(autouse=True)
def _local_sockets(socket_enabled):
    """The senders listen on a port of this computer."""
    yield


@pytest.fixture
def thread_errors():
    """Exceptions that escape a thread (the crash in #20 was one)."""
    seen: list = []
    original = threading.excepthook
    threading.excepthook = lambda args: seen.append(args.exc_value)
    yield seen
    threading.excepthook = original


def start_sender(stop: threading.Event, q: queue.Queue) -> tuple[threading.Thread, int, socket.socket]:
    port = live_io._free_local_port()
    listener = live_io._start_local_listener(port)
    thread = threading.Thread(target=live_io._sender_thread, args=(listener, q, stop, "audio"), daemon=True)
    thread.start()
    return thread, port, listener


def test_ffmpeg_connecting_late_still_gets_the_data(thread_errors) -> None:
    """ffmpeg reaches the second input only after probing the first; that is not a failure."""
    stop, q = threading.Event(), queue.Queue()
    with patch.object(live_io, "INPUT_ACCEPT_POLL", 0.05):
        thread, port, _ = start_sender(stop, q)
        time.sleep(0.3)  # several accept polls pass before ffmpeg connects
        with socket.create_connection(("127.0.0.1", port), timeout=2) as ffmpeg:
            q.put(b"audio-bytes")
            q.put(None)
            assert ffmpeg.recv(64) == b"audio-bytes"
        thread.join(2)
    assert not thread.is_alive() and not thread_errors


def test_session_ending_while_waiting_stops_quietly(thread_errors, caplog) -> None:
    """Closing the dialog before ffmpeg connected: no warning, no thread crash."""
    stop, q = threading.Event(), queue.Queue()
    with patch.object(live_io, "INPUT_ACCEPT_POLL", 0.05):
        thread, _, _ = start_sender(stop, q)
        time.sleep(0.15)
        stop.set()
        thread.join(2)
    assert not thread.is_alive() and not thread_errors
    assert "accept timed out" not in caplog.text


def test_listener_closed_under_a_waiting_sender_is_not_a_crash(thread_errors) -> None:
    """The teardown closes the listener while the thread sits in accept(): EBADF must not escape (#20)."""
    stop, q = threading.Event(), queue.Queue()
    with patch.object(live_io, "INPUT_ACCEPT_POLL", 1.0):  # the thread is inside accept() when it is closed
        thread, _, listener = start_sender(stop, q)
        time.sleep(0.2)
        listener.close()
        thread.join(3)
    assert not thread.is_alive() and not thread_errors


def test_ffmpeg_never_connecting_ends_with_the_backstop(thread_errors, caplog) -> None:
    stop, q = threading.Event(), queue.Queue()
    with patch.object(live_io, "INPUT_ACCEPT_POLL", 0.05), patch.object(live_io, "INPUT_ACCEPT_TIMEOUT", 0.3):
        thread, _, _ = start_sender(stop, q)
        thread.join(3)
    assert not thread.is_alive() and not thread_errors
    assert "audio input accept timed out" in caplog.text
