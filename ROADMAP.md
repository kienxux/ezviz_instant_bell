# Roadmap

Where this integration is heading. Items are ideas and plans, not promises: only 0.6.x and 0.7 are planned
in some detail, everything under *Later* has no date. Each item has an issue; add a 👍 there or open a new
one to say what you need.

## Done

- 0.6.0: visitor log dashboard card (rings, unlocks, pictures and SD-card recordings in one list)
- 0.6.0: hardening: random action ID in the ring blueprint, admin-only encryption services, unlock retry after a failure, no clip-queue stall from ffmpeg output
- 0.5.x: browse and play the SD-card recordings; history of event pictures

## 0.6.1: harden what exists

- Encryption keys per door station (today the last one wins)
- Limits and checks on what the LAN client reads from the door station
- Pin `pyezvizapi` to a range and test the private library calls we rely on
- Diagnostics download with passwords, sessions and keys removed

## 0.7: small features

- Reconfigure flow (change region, password or devices without deleting the entry)
- Logbook entry saying which Home Assistant user opened the door
- Optional confirmation (PIN) for the unlock button and services
- Save a clip automatically when the bell rings
- More blueprint variants: persistent notification, speaker announcement

## Later (ideas, no dates)

- Link each ring to its SD-card recording as an entity attribute
- Motion and person events as events or binary sensors
- Live video through an authenticated Home Assistant view instead of the local relay socket
- Schedules for Do Not Disturb and privacy mode
- Two-way audio. **Unproven:** it is not known yet whether the protocols we use carry talk-back.
- Better naming and grouping with several door stations or indoor monitors

## Housekeeping

- Split `live_camera.py` (about 2,200 lines) into relay, source and entity modules
- Tests for the clip store, the media source and the stream relay
- Make the HACS, hassfest and test checks required

## Not planned

Anything that opens the door without a deliberate action by a person.
