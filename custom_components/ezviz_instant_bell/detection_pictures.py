"""Pictures of motion and person events, written to the media folder of Home Assistant.

Pure Python (no Home Assistant imports). A picture goes to
``<media>/ezviz_instant_bell/<door station>/<YYYY-MM-DD>/<HH-MM-SS>_<title>.jpg``,
so it shows under *Media > My media*. Day folders older than the kept number
of days are removed; nothing outside the integration's own folder is touched.
"""

from __future__ import annotations

import re
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

FOLDER = "ezviz_instant_bell"


def slug(text: str, limit: int = 40) -> str:
    """``Motion Detection Alarm`` -> ``motion_detection_alarm`` (safe for any file system)."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return cleaned[:limit].rstrip("_") or "event"


def picture_path(root: Path, device: str, occurred: datetime, title: str) -> Path:
    """Where the picture of one event goes; a free name if two events share the second."""
    folder = root / FOLDER / slug(device) / occurred.strftime("%Y-%m-%d")
    stem = f"{occurred.strftime('%H-%M-%S')}_{slug(title)}"
    path = folder / f"{stem}.jpg"
    number = 2
    while path.exists():
        path = folder / f"{stem}_{number}.jpg"
        number += 1
    return path


def write_picture(path: Path, data: bytes) -> None:
    """Write the file whole or not at all (a half picture would show in the media browser)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(".part")
    part.write_bytes(data)
    part.replace(path)


def prune(device_folder: Path, today: date, keep_days: int) -> list[Path]:
    """Remove the day folders older than ``keep_days`` (today counts as one). Returns what was removed."""
    if not device_folder.is_dir():
        return []
    oldest = today - timedelta(days=keep_days - 1)
    removed: list[Path] = []
    for folder in device_folder.iterdir():
        try:
            day = datetime.strptime(folder.name, "%Y-%m-%d").date()
        except ValueError:
            continue  # not one of ours
        if folder.is_dir() and day < oldest:
            shutil.rmtree(folder, ignore_errors=True)
            removed.append(folder)
    return removed
