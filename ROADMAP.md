# Roadmap

Where this integration is heading. Items are ideas and plans, not promises: only 0.6.x and 0.7 are planned
in some detail, everything under *Later* has no date. Each item has an issue; add a 👍 there or open a new
one to say what you need.

## Done

- 0.10.0: motion and person as event types of the Doorbell entity (options, off by default), with their message codes in the diagnostics; required CI checks on `main`; fixes for encrypted HEVC video on HP7 firmware 5.4 (#22); the persistent-notification blueprint shows the picture inline
- 0.9.0: pictures of motion and person events saved in the media folder (option); devices named as in the EZVIZ app, indoor monitors shown as connected to their door station; the live video code split into modules, with tests for the clip store, the media source and the stream relay
- Local first, checked (November 2026): captures of the EZVIZ app at home (live view, door, gate, two rings) show that a CPD5 reports rings and receives door and gate commands only through the EZVIZ cloud, even with the phone on the same network. Its local broadcasts are a timed heartbeat, not events. Only the live video is local, and that already works. Owners of other HP7/CP7 models who see different behaviour are welcome to open an issue
- Next release: starts and shows the local live video when the EZVIZ cloud is down
- German translation (after 0.7.0)
- 0.7.0: option to save the video of every ring (event `ezviz_instant_bell_clip_saved`, sensor *Last saved clip*); reconfigure flow; logbook entry and event for who opened the door from Home Assistant; one encryption key per door station; the LAN client refuses oversized answers and non-local hosts; works with the device registry of Home Assistant 2026.10
- 0.6.1: setup no longer fails on device values the library does not know (CP5, issue #1); diagnostics download; `pyezvizapi` limited to 1.0.x with a test that guards the calls we rely on
- 0.6.0: visitor log dashboard card (rings, unlocks, pictures and SD-card recordings in one list); hardening of the ring blueprint, the encryption actions, unlock retry and clip fetching
- 0.5.x: browse and play the SD-card recordings; history of event pictures

## Next

- Check the motion/person mapping against real messages (diagnostics `recent_messages`)

## Later (ideas, no dates; built if someone asks)

- Two-way audio. The library only knows talk as a capability flag and has no way to send audio, so this needs protocol research with a real door station (see issue #15).

## Not planned

- Anything that opens the door without a deliberate action by a person.
- A PIN for the unlock button (#8) and schedules for Do Not Disturb and privacy (#14). Home Assistant user rights and automations already cover these.
- Live video through an authenticated Home Assistant view (#13). The relay listens only on this computer by default.
