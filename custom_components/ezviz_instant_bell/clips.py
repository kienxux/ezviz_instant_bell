"""Clips of the SD card: fetched on demand, converted to MP4 and kept for a while.

The first viewer watches while the recording is still arriving (a growing
fragmented MP4 that the web view hands out as it grows); when it is complete it
becomes a normal, seekable MP4 and later viewers get that file. Old clips are
removed when the folder grows past ``CLIP_CACHE_MAX_BYTES``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from .clip_stream import ClipUnavailable, iter_media, open_playback
from .const import CLIP_CACHE_DIR, CLIP_CACHE_MAX_BYTES
from .recordings import clip_id

if TYPE_CHECKING:
    from .hub import InstantBellHub

_LOGGER = logging.getLogger(__name__)

FFMPEG_TIMEOUT = 60


def stream_command(ffmpeg: str, out: Path) -> list[str]:
    """ffmpeg: program stream on stdin -> MP4 that is playable while it grows."""
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-err_detect", "ignore_err", "-f", "mpeg", "-i", "pipe:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "48k",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", str(out),
    ]  # fmt: skip


def finalize_command(ffmpeg: str, source: Path, out: Path) -> list[str]:
    """ffmpeg: the growing MP4 -> an ordinary MP4 with its index in front (seekable)."""
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(source), "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(out),
    ]  # fmt: skip


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", text)


@dataclass
class ClipJob:
    """One recording that is being fetched."""

    part_path: Path
    final_path: Path
    state: str = "starting"  # starting -> streaming -> finalizing -> done | failed
    error: str = ""
    bytes_in: int = 0
    done: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def streaming_finished(self) -> bool:
        return self.state in ("finalizing", "done", "failed")


class ClipStore:
    """Fetches and keeps the clips of one config entry."""

    def __init__(self, hass: HomeAssistant, hub: InstantBellHub) -> None:
        self.hass = hass
        self.hub = hub
        self.root = Path(hass.config.path(".storage", CLIP_CACHE_DIR))
        self._jobs: dict[Path, ClipJob] = {}
        self._one_at_a_time = asyncio.Lock()  # the door station serves few video sessions at once
        self._tasks: set[asyncio.Task[None]] = set()

    # -- paths ------------------------------------------------------------

    def path_for(self, serial: str, begin: datetime, end: datetime) -> Path:
        return self.root / safe_name(serial) / f"{clip_id(begin, end)}.mp4"

    def ready_file(self, serial: str, begin: datetime, end: datetime) -> Path | None:
        path = self.path_for(serial, begin, end)
        return path if path.is_file() else None

    # -- fetching -----------------------------------------------------------

    def job_for(self, serial: str, begin: datetime, end: datetime) -> ClipJob:
        """The running job of this clip, started if there is none. Call from the event loop."""
        final = self.path_for(serial, begin, end)
        job = self._jobs.get(final)
        if job is not None:
            return job
        job = ClipJob(part_path=final.with_suffix(".part.mp4"), final_path=final)
        self._jobs[final] = job
        task = self.hass.async_create_background_task(
            self._run(job, serial, begin, end), f"{CLIP_CACHE_DIR}_{final.stem}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _run(self, job: ClipJob, serial: str, begin: datetime, end: datetime) -> None:
        try:
            async with self._one_at_a_time:
                await self.hass.async_add_executor_job(self._fetch, job, serial, begin, end)
        except asyncio.CancelledError:
            job.state, job.error = "failed", "stopped"
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Clip %s failed: %s", job.final_path.name, err)
            job.state, job.error = "failed", str(err) or type(err).__name__
        finally:
            job.done.set()
            self._jobs.pop(job.final_path, None)
            with suppress(OSError):
                if job.state != "done":
                    job.part_path.unlink(missing_ok=True)
            await self.hass.async_add_executor_job(self.prune)

    def _fetch(self, job: ClipJob, serial: str, begin: datetime, end: datetime) -> None:
        """Blocking: stream the recording into ffmpeg, then make the final file."""
        ffmpeg = self._ffmpeg()
        job.part_path.parent.mkdir(parents=True, exist_ok=True)
        client = self.hub.client
        playback = open_playback(client, serial, 1, begin, end, call_lock=self.hub.call_lock)
        proc = subprocess.Popen(  # noqa: S603
            stream_command(ffmpeg, job.part_path),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        failure: Exception | None = None
        try:
            job.state = "streaming"
            expected = max(1.0, (end - begin).total_seconds())
            for chunk in iter_media(playback, expected_seconds=expected):
                job.bytes_in += len(chunk)
                assert proc.stdin is not None  # noqa: S101
                proc.stdin.write(chunk)
        except Exception as err:  # noqa: BLE001 - a dropped connection still leaves a usable clip
            failure = err
        finally:
            playback.close()
            with suppress(OSError):
                if proc.stdin:
                    proc.stdin.close()
            try:
                proc.wait(timeout=FFMPEG_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
        if not job.bytes_in or not job.part_path.is_file() or job.part_path.stat().st_size == 0:
            err_text = proc.stderr.read().decode(errors="replace").strip()[-200:] if proc.stderr else ""
            raise ClipUnavailable(str(failure) if failure else (err_text or "No video arrived"))
        if failure:
            _LOGGER.debug("Clip %s is incomplete: %s", job.final_path.name, failure)
        job.state = "finalizing"
        tmp = job.final_path.with_suffix(".tmp.mp4")
        done = subprocess.run(  # noqa: S603
            finalize_command(ffmpeg, job.part_path, tmp),
            capture_output=True,
            timeout=FFMPEG_TIMEOUT,
            check=False,
        )
        if done.returncode != 0 or not tmp.is_file():
            tmp.unlink(missing_ok=True)
            # The growing file still plays; keep it as the result.
            job.part_path.replace(job.final_path)
        else:
            tmp.replace(job.final_path)
            job.part_path.unlink(missing_ok=True)
        job.state = "done"

    def _ffmpeg(self) -> str:
        try:
            from homeassistant.components.ffmpeg import get_ffmpeg_manager

            return str(get_ffmpeg_manager(self.hass).binary or "ffmpeg")
        except Exception:  # noqa: BLE001 - the ffmpeg integration is optional
            return "ffmpeg"

    # -- housekeeping ---------------------------------------------------------

    def prune(self) -> None:
        """Blocking: remove the oldest clips while the folder is over its limit."""
        if not self.root.is_dir():
            return
        active = {j.part_path for j in self._jobs.values()}
        files = [p for p in self.root.glob("*/*.mp4") if p not in active]
        known: dict[Path, tuple[float, int]] = {}  # file -> (modified, size)
        for path in files:
            try:  # a file can vanish at any time (another viewer, the user)
                stat = path.stat()
            except OSError:
                continue
            known[path] = (stat.st_mtime, stat.st_size)
        total = sum(size for _mtime, size in known.values())
        for path in sorted(known, key=lambda p: known[p][0]):
            if total <= CLIP_CACHE_MAX_BYTES:
                break
            with suppress(OSError):
                path.unlink()
                total -= known[path][1]

    async def async_stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


def read_chunk(handle: Any, size: int = 262144) -> bytes:
    return handle.read(size)


def part_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
