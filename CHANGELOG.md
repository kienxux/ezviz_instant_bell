# Changelog

All notable changes. The newest version is first. Versions follow the `version` in `manifest.json`.

## 0.8.2

- New admin action `ezviz_instant_bell.probe_lan` (research aid for local unlock and rings): asks the door station over the local network which commands it knows and what it answers to an empty request, one connection at a time. The key stays in Home Assistant; values that look like keys, passwords or codes are hidden in the answer.

## 0.8.1

- Local live video (source `local` or `auto`): the relay no longer gives up on ffmpeg's audio input after 20 seconds. ffmpeg first waits for a keyframe on the video input, which can take longer on an HP7, and the early give-up left the live view without a picture (#20).
- Closing the live view while the relay was still waiting no longer crashes a thread with "Bad file descriptor" (#20).
- README: what to do when the HACS search does not find the integration yet.

## 0.8.0

- The visitor log card now shows an **Open door** button by default (it still asks to confirm). Set `unlock: both` for a door and a gate button, or `show_unlock: false` to hide it.

## 0.7.3

- Fixed the visitor log card not loading ("Custom element doesn't exist: ezviz-visitor-log-card"): the card is now added to the frontend as soon as the frontend is ready, regardless of the order the components start in.

## 0.7.2

- When the cloud retry hits a wrong password, it now stops retrying and asks you to re-authenticate, instead of trying the old password every 5 minutes (which EZVIZ can lock the account for).

## 0.7.1

- **Fully independent of the `ezviz_hp7` integration.** Removed the setup step that read its stored login and the automatic takeover of its entities and devices; you enter your EZVIZ login and pick your door station in this integration's own flow.
- Clip file checks and stream-thread shutdown no longer run on the event loop.
- Unlock events carry only the unlock method and the holder's name, never the card id, the raw alarm record or the household's enrolled-key list.
- The local-stream decoder no longer logs any key material.
- Starts without the EZVIZ cloud: the local live video works with the stored local key and address; a repair notice explains it, and everything starts again when EZVIZ answers.
- Unlocking or changing a setting while EZVIZ cannot be reached says so clearly.

## 0.7.0

- Option to save the video of every ring; event `ezviz_instant_bell_clip_saved` and sensor *Last saved clip*
- Event `ezviz_instant_bell_door_opened` and logbook entry for who opened the door or gate from Home Assistant
- Reconfigure flow: change region, password or door stations without removing the integration
- One encryption key per door station; `fetch_encryption_key` takes a `serial`
- The local network client refuses oversized answers and addresses that are not on your network
- Works with the device registry of Home Assistant 2026.10
- German translation
- Product name is now **EZVIZ HP7 / CP7 Video Intercom**; README rewritten

## 0.6.1

- Setup no longer fails when the device reports a value the EZVIZ library does not know (CP5 with sound mode 5)
- Diagnostics download without account data, serial numbers, addresses or keys
- `pyezvizapi` is limited to 1.0.x; a test checks the library calls this integration relies on

## 0.6.0

- Visitor log dashboard card
- The ring blueprint uses a random ID for its Open door button
- The encryption actions need an administrator
- A failed unlock no longer blocks the retry for a few seconds
- A flood of ffmpeg messages can no longer stall the clip queue

## 0.5.x

- 0.5.1: the ring blueprint ignored the first ring of a new entity
- 0.5.0: browse and play the SD-card recordings of the door station

## 0.4.x

- 0.4.1: the history showed no unlock entries
- 0.4.0: pictures of past events in the Media browser; the blueprint can notify several phones

## 0.3.x

- 0.3.1: the "Open door" / "Open gate" buttons of 0.1.0 and their entity IDs are kept
- 0.3.0: live video (cloud and local network) and encryption handling

## 0.2.0

- Full set of sensors, switches, sliders and buttons; entities of the earlier `ezviz_hp7` integration move over with their IDs

## 0.1.0

- Instant ring picture and Open door
