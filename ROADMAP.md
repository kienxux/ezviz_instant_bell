# Roadmap

Where this integration is heading. Items are ideas and plans, not promises: only 0.6.x and 0.7 are planned
in some detail, everything under *Later* has no date. Each item has an issue; add a 👍 there or open a new
one to say what you need.

## Done

- Next release: one encryption key per door station; the LAN client refuses oversized answers and non-local hosts; works with Home Assistant 2026.10's device registry
- 0.6.1: setup no longer fails on device values the library does not know (CP5, issue #1); diagnostics download; `pyezvizapi` limited to 1.0.x with a test that guards the calls we rely on
- 0.6.0: visitor log dashboard card (rings, unlocks, pictures and SD-card recordings in one list)
- 0.6.0: hardening: random action ID in the ring blueprint, admin-only encryption services, unlock retry after a failure, no clip-queue stall from ffmpeg output
- 0.5.x: browse and play the SD-card recordings; history of event pictures

## 0.7: what makes it worth installing

- Save a clip when the bell rings and link it to the ring event (the SD-card recording of whoever stood there)
- Reconfigure flow (change region, password or devices without deleting the entry)
- A time-boxed look at two-way audio: find out whether the protocols carry it, then decide

## Later (ideas, no dates; built if someone asks)

- Motion and person events as events or binary sensors
- Live video through an authenticated Home Assistant view instead of the local relay socket
- Optional PIN for the unlock button; more blueprint variants; schedules for Do Not Disturb. Home Assistant automations can already cover these.
- Logbook entry for who opened the door (check first what Home Assistant already records)
- Better naming and grouping with several door stations or indoor monitors
- Two-way audio itself. **Unproven:** it is not known yet whether the protocols we use carry talk-back.

## Housekeeping

- Split `live_camera.py` (about 2,200 lines) into relay, source and entity modules, when it next has to be changed, after tests exist
- Tests for the clip store, the media source and the stream relay
- Make the HACS, hassfest and test checks required

## Not planned

Anything that opens the door without a deliberate action by a person.
