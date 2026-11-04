# Roadmap

Where this integration is heading. Items are ideas and plans, not promises: only 0.6.x and 0.7 are planned
in some detail, everything under *Later* has no date. Each item has an issue; add a 👍 there or open a new
one to say what you need.

## Done

- Next release: starts and shows the local live video when the EZVIZ cloud is down
- German translation (after 0.7.0)
- 0.7.0: option to save the video of every ring (event `ezviz_instant_bell_clip_saved`, sensor *Last saved clip*); reconfigure flow; logbook entry and event for who opened the door from Home Assistant; one encryption key per door station; the LAN client refuses oversized answers and non-local hosts; works with the device registry of Home Assistant 2026.10
- 0.6.1: setup no longer fails on device values the library does not know (CP5, issue #1); diagnostics download; `pyezvizapi` limited to 1.0.x with a test that guards the calls we rely on
- 0.6.0: visitor log dashboard card (rings, unlocks, pictures and SD-card recordings in one list); hardening of the ring blueprint, the encryption actions, unlock retry and clip fetching
- 0.5.x: browse and play the SD-card recordings; history of event pictures

## Next

- Local first: find out whether the door station rings, unlocks and talks over the local network (needs a capture of the EZVIZ app at home), then move those parts off the cloud
- More about what the door station reports: motion and person events

## Later (ideas, no dates; built if someone asks)

- Motion and person events as events or binary sensors
- Live video through an authenticated Home Assistant view instead of the local relay socket
- Optional PIN for the unlock button; more blueprint variants; schedules for Do Not Disturb. Home Assistant automations can already cover these.
- Better naming and grouping with several door stations or indoor monitors
- Two-way audio. The library only knows talk as a capability flag and has no way to send audio, so this needs protocol research with a real door station (see issue #15).

## Housekeeping

- Split `live_camera.py` (about 2,200 lines) into relay, source and entity modules, when it next has to be changed, after tests exist
- Tests for the clip store, the media source and the stream relay
- Make the HACS, hassfest and test checks required

## Not planned

Anything that opens the door without a deliberate action by a person.
