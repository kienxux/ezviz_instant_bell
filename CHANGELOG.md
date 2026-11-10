# Changelog

All notable changes. The newest version is first. Versions follow the `version` in `manifest.json`.

## 0.9.0

- New option **Save the picture of every motion and person event**, off by default. The picture of each motion and person event is stored in the media folder of Home Assistant (*Media > My media > ezviz_instant_bell*), in one folder per door station and day, about 5 to 12 seconds after the event and without an automation. The pictures are kept for 30 days by default (option **Keep the saved pictures for**). Each saved picture fires the event `ezviz_instant_bell_picture_saved`. Rings and door or gate openings are not saved this way.
- Fewer entities that do not apply to your model. The sensors for card, face, palm and code unlock and for the smart and intelligent detection alarms are now made only once the door station has reported such an alarm (EZVIZ has no reliable flag for them). The first one is still shown and still fires `ezviz_instant_bell_unlock`. After the update, those of the six that your door station never reported are removed. If you had renamed one, its new entity ID is the default again when it comes back.
- Ready for Home Assistant 2026.12: the integration no longer reloads through an update listener, which logged a warning that this stops working in 2026.12.0 (#23). Saving the options and storing a camera key still reload the entry as before.
- Devices are named as in the EZVIZ app (#16), for example *Front door* instead of *EZVIZ HP7 (BG0000000)*. An indoor monitor with the same name as its door station gets *Monitor* added, and it is shown as connected to its door station. A name changed in the EZVIZ app is taken over at the next start. Entity IDs that already exist do not change, and a device you renamed in Home Assistant keeps your name. New entities get IDs from the new name.
- The live video code (`live_camera.py`, about 2,200 lines) is split into the source, the ffmpeg feed, the relay and the camera entity (#17). Nothing changes in behaviour. Log lines of the relay now come from `custom_components.ezviz_instant_bell.live_relay`.
- Tests for the clip store, the media source and the stream relay (#18): 84 new tests, including the retry and lockout timing of the live video, the replay from the last keyframe for a new viewer, and the handling of slow viewers.
- Saving the options no longer forgets the camera keys fetched with the 4-digit e-mail code. With several door stations every key was lost and EZVIZ mailed new codes. Clearing the *encryption key* field keeps the fetched keys; typing a different key there replaces the key it showed.
- Two more ring blueprints, neither of which opens the door: **persistent notification** (the ring with its picture link stays in Home Assistant until you dismiss it, or until a time you set) and **speaker announcement** (a speaker says the ring out loud through a text-to-speech entity, optionally at a set volume) (#10, #21, thanks @zheng-guannan577).

## 0.8.3

- Removed the `probe_lan` action again. Two captures of the EZVIZ app at home showed that the door station reports rings and receives door and gate commands only through the EZVIZ cloud, even when the phone is on the same network, so there is no local path to build on. The local live video is unchanged.

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
