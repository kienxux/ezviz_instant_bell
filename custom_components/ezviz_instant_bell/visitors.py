"""What the visitor log shows for one day: events paired with their recordings.

Pure Python (no Home Assistant imports), so it can be tested on its own.
"""

from __future__ import annotations

from datetime import datetime

try:
    from .history import HistoryItem
    from .parsing import RING_SUBTYPE
    from .recordings import Recording
except ImportError:  # imported on its own by the tests that do not load Home Assistant
    from history import HistoryItem
    from parsing import RING_SUBTYPE
    from recordings import Recording

KIND_RING = "ring"
KIND_UNLOCK = "unlock"
KIND_OTHER = "other"

UNLOCK_SUBTYPE = 2401  # "EZVIZ app open the door / gate", as seen on real entries
CLIP_MATCH_SECONDS = 60  # a recording belongs to an event that happened this close to it


def kind_of(item: HistoryItem) -> str:
    if item.subtype == RING_SUBTYPE:
        return KIND_RING
    if item.subtype == UNLOCK_SUBTYPE:
        return KIND_UNLOCK
    return KIND_OTHER


def clip_for(occurred: datetime | None, recordings: list[Recording]) -> Recording | None:
    """The recording that was running (or about to start) when the event happened.

    Recording times are the door station's local clock without a zone, so the
    zone of the event is dropped before comparing.
    """
    if occurred is None:
        return None
    moment = occurred.replace(tzinfo=None)
    best: tuple[tuple[int, float], Recording] | None = None
    for rec in recordings:
        gap = abs((moment - rec.begin).total_seconds())
        running = rec.begin <= moment <= rec.end
        if not running and gap > CLIP_MATCH_SECONDS:
            continue
        score = (0 if running else 1, gap)  # a running recording first; then the nearest start
        if best is None or score < best[0]:
            best = (score, rec)
    return best[1] if best else None
