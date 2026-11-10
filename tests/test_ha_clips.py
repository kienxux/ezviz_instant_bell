"""The clip store: SD-card recordings fetched once, converted to MP4 and kept in a folder."""

from __future__ import annotations

import asyncio
import os
import threading
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ezviz_instant_bell import clips
from custom_components.ezviz_instant_bell.clip_stream import ClipUnavailable
from custom_components.ezviz_instant_bell.clips import ClipJob, ClipStore, part_size

SERIAL = "BE0000001-BE0000002"
BEGIN, END = datetime(2026, 10, 28, 15, 42, 3), datetime(2026, 10, 28, 15, 42, 52)
LATER = datetime(2026, 10, 28, 16, 0, 0), datetime(2026, 10, 28, 16, 0, 30)


class FakeHub:
    client = object()
    call_lock = threading.Lock()


class Playback:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def store(hass: HomeAssistant, tmp_path) -> ClipStore:
    with patch.object(clips, "CLIP_CACHE_DIR", str(tmp_path / "clips")):
        return ClipStore(hass, FakeHub())


def fake_ffmpeg(tmp_path: Path, finalize_ok: bool = True) -> str:
    """Stand-in for ffmpeg: copies stdin (or the -i file) to the last argument."""
    script = tmp_path / "fake_ffmpeg"
    finalize = (
        'while [ $# -gt 0 ]; do [ "$1" = -i ] && src="$2"; shift; done; cp "$src" "$last"'
        if finalize_ok
        else "exit 1"
    )
    script.write_text(
        "#!/bin/sh\n"
        "for last; do :; done\n"
        f'case "$*" in *pipe:0*) cat > "$last";;\n*) {finalize};; esac\n'
    )
    script.chmod(0o755)
    return str(script)


def writing_fetch(content: bytes = b"MP4", calls: list | None = None, gate: threading.Event | None = None):
    """A ``_fetch`` that writes the final file directly."""

    def fetch(job: ClipJob, serial, begin, end) -> None:
        if calls is not None:
            calls.append((serial, begin, end))
        if gate is not None:
            assert gate.wait(5)
        job.final_path.parent.mkdir(parents=True, exist_ok=True)
        job.final_path.write_bytes(content)
        job.state = "done"

    return fetch


async def test_clip_files_are_named_after_the_door_station_and_the_times(store: ClipStore) -> None:
    path = store.path_for(SERIAL, BEGIN, END)
    assert path == store.root / SERIAL / "20261028T154203Z_20261028T154252Z.mp4"
    assert store.path_for("a/b c", BEGIN, END).parent.name == "a_b_c"


def test_a_missing_file_has_size_zero(tmp_path) -> None:
    assert part_size(tmp_path / "nothing.mp4") == 0


def test_ffmpeg_commands_read_the_stream_and_make_it_seekable(tmp_path) -> None:
    stream = clips.stream_command("ffmpeg", tmp_path / "a.part.mp4")
    assert stream[stream.index("-i") + 1] == "pipe:0" and stream[-1].endswith("a.part.mp4")
    assert "frag_keyframe+empty_moov+default_base_moof" in stream
    final = clips.finalize_command("ffmpeg", tmp_path / "a.part.mp4", tmp_path / "a.mp4")
    assert "+faststart" in final and final[-1].endswith("a.mp4")


async def test_a_clip_that_is_there_is_not_fetched_again(store: ClipStore) -> None:
    path = store.path_for(SERIAL, BEGIN, END)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"KEPT")
    with patch.object(store, "_fetch") as fetch:
        assert await store.async_save(SERIAL, BEGIN, END) == path
    fetch.assert_not_called()


async def test_two_viewers_of_one_clip_share_the_fetch(hass: HomeAssistant, store: ClipStore) -> None:
    calls: list = []
    with patch.object(store, "_fetch", writing_fetch(b"ONE", calls)):
        first, second = await asyncio.gather(
            store.async_save(SERIAL, BEGIN, END), store.async_save(SERIAL, BEGIN, END)
        )
        await hass.async_block_till_done(wait_background_tasks=True)
    assert first == second == store.path_for(SERIAL, BEGIN, END)
    assert first.read_bytes() == b"ONE" and len(calls) == 1


async def test_clips_are_fetched_one_at_a_time(hass: HomeAssistant, store: ClipStore) -> None:
    running, most = 0, 0
    lock = threading.Lock()
    gate = threading.Event()

    def fetch(job, serial, begin, end):
        nonlocal running, most
        with lock:
            running += 1
            most = max(most, running)
        gate.wait(0.3)
        writing_fetch()(job, serial, begin, end)
        with lock:
            running -= 1

    with patch.object(store, "_fetch", fetch):
        results = await asyncio.gather(store.async_save(SERIAL, BEGIN, END), store.async_save(SERIAL, *LATER))
        await hass.async_block_till_done(wait_background_tasks=True)
    assert all(results) and most == 1


async def test_a_failed_fetch_leaves_nothing_behind(hass: HomeAssistant, store: ClipStore) -> None:
    def fetch(job, serial, begin, end):
        job.part_path.parent.mkdir(parents=True, exist_ok=True)
        job.part_path.write_bytes(b"half")
        raise ClipUnavailable("The door station is busy")

    with patch.object(store, "_fetch", fetch):
        job = store.job_for(SERIAL, BEGIN, END)
        assert await store.async_save(SERIAL, BEGIN, END) is None
        await hass.async_block_till_done(wait_background_tasks=True)
    assert job.state == "failed" and job.error == "The door station is busy" and job.streaming_finished
    assert not job.part_path.exists()


async def test_stopping_marks_a_waiting_fetch_as_stopped(hass: HomeAssistant, store: ClipStore) -> None:
    gate = threading.Event()
    with patch.object(store, "_fetch", writing_fetch(gate=gate)):
        first = store.job_for(SERIAL, BEGIN, END)
        waiting = store.job_for(SERIAL, *LATER)  # queued behind the first
        await asyncio.sleep(0.05)
        await store.async_stop()
        gate.set()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert waiting.state == "failed" and waiting.error == "stopped" and waiting.done.is_set()
    assert first.done.is_set() and not store._jobs


async def test_a_clip_whose_conversion_fails_keeps_the_growing_file(
    hass: HomeAssistant, store: ClipStore, tmp_path
) -> None:
    playback = Playback()
    with patch.object(store, "_ffmpeg", return_value=fake_ffmpeg(tmp_path, finalize_ok=False)), patch.object(
        clips, "open_playback", return_value=playback
    ), patch.object(clips, "iter_media", return_value=iter([b"part-", b"one"])):
        path = await store.async_save(SERIAL, BEGIN, END)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert path is not None and path.read_bytes() == b"part-one" and playback.closed
    assert not list(store.root.glob("*/*.part.mp4")) and not list(store.root.glob("*/*.tmp.mp4"))


async def test_a_dropped_connection_still_keeps_what_arrived(
    hass: HomeAssistant, store: ClipStore, tmp_path
) -> None:
    def media(*_args, **_kwargs):
        yield b"first-half"
        raise ConnectionResetError("video server went away")

    playback = Playback()
    with patch.object(store, "_ffmpeg", return_value=fake_ffmpeg(tmp_path)), patch.object(
        clips, "open_playback", return_value=playback
    ), patch.object(clips, "iter_media", media):
        job = store.job_for(SERIAL, BEGIN, END)
        await asyncio.wait_for(job.done.wait(), 10)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert job.state == "done" and job.bytes_in == 10 and playback.closed
    assert store.ready_file(SERIAL, BEGIN, END).read_bytes() == b"first-half"


async def test_a_connection_that_drops_before_any_video_reports_why(
    hass: HomeAssistant, store: ClipStore, tmp_path
) -> None:
    def media(*_args, **_kwargs):
        raise ConnectionResetError("video server went away")
        yield b""  # pragma: no cover

    with patch.object(store, "_ffmpeg", return_value=fake_ffmpeg(tmp_path)), patch.object(
        clips, "open_playback", return_value=Playback()
    ), patch.object(clips, "iter_media", media):
        job = store.job_for(SERIAL, BEGIN, END)
        await asyncio.wait_for(job.done.wait(), 10)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert job.state == "failed" and job.error == "video server went away"
    assert store.ready_file(SERIAL, BEGIN, END) is None


async def test_pruning_spares_a_clip_that_is_still_arriving(store: ClipStore) -> None:
    folder = store.root / "S"
    folder.mkdir(parents=True)
    job = ClipJob(part_path=folder / "new.part.mp4", final_path=folder / "new.mp4")
    store._jobs[job.final_path] = job
    for index, name in enumerate(("old.mp4", "new.part.mp4")):
        path = folder / name
        path.write_bytes(b"x" * 100)
        os.utime(path, (1000 + index, 1000 + index))
    os.utime(folder / "new.part.mp4", (1, 1))  # oldest of all, but in use
    with patch.object(clips, "CLIP_CACHE_MAX_BYTES", 50):
        store.prune()
    assert sorted(p.name for p in folder.iterdir()) == ["new.part.mp4"]


async def test_pruning_an_empty_store_does_nothing(store: ClipStore) -> None:
    store.prune()
    assert not store.root.exists()
