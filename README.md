# EZVIZ Video Intercom for Home Assistant

A Home Assistant integration for EZVIZ video intercoms (HP7 / CP7 family, door station with indoor monitor). It shows everything the intercom reports, lets you change its settings, and adds what other integrations do not have: **when someone rings you get the picture at once, and you can open the door with one tap without answering the call.**

> **Status: version 0.5.1.** Ring detection and push events were measured on a real door station (CS-CPD5 with CS-CP5 monitor), and the reading of its data is tested against an anonymised copy of that device's answers. The door and gate buttons, the chime sliders and the switches use the same EZVIZ calls as the earlier `ezviz_hp7` integration but have not yet been checked on a real device with this code; please check each (see "First check"). **Live video** (cloud or local network) is ported from the earlier integration and has not yet been run on a real device with this code: please check it (see "First check"). Not affiliated with EZVIZ or Hikvision.

## What you get

| Kind | Entities |
| --- | --- |
| **Instant bell** | `Doorbell` (event: `ring`, `door_unlock`, `gate_open`, `other`) and `Last picture` (image), both within seconds. A ready-made notification automation with an **Open door** button is included. |
| **Open** | Buttons `Unlock Door` and `Unlock Gate`, and the services `ezviz_instant_bell.unlock_door` / `unlock_gate`. |
| **Sensors** | Name, firmware, online status, WiFi signal, SSID, local and WAN IP, motion, last alarm and its type, seconds since last motion, update available, microphone volume. |
| **Binary sensors** | Motion; short pulses (3 s) for doorbell ringing, gate open, lock unlocked and how (app, RFID card, face, palm, code), smart and intelligent detection alarm; read-only states of mute, loitering, stranger and human detection. |
| **Switches** | Doorbell chime and motion sound alert (door station and indoor monitor), do not disturb, privacy, armed, name-tag light. |
| **Sliders** | Chime volume (0-7) and ringtones for the doorbell and for the motion alert, on the door station and on every indoor monitor. |
| **Camera** | `Live`: the live video of the door station (cloud or local network, with audio in the Home Assistant stream mode). `Last Snapshot`: the picture of the latest alarm. |
| **Events** | `ezviz_instant_bell_unlock` (and, for old automations, `ezviz_hp7_unlock`) with the way the lock was opened, the card id and the card holder's name. |
| **Recordings** | *Media > EZVIZ Video Intercom > Recordings (SD card)*: the videos on the door station's SD card, by day, played through the EZVIZ cloud. |
| **History** | *Media > EZVIZ Video Intercom > Pictures of events*: the pictures of past events (rings, unlocks, motion), by day for the last 14 days. |
| **Services** | `unlock_door`, `unlock_gate`, `set_video_encryption`, `fetch_encryption_key`. |
| **Account** | `Terminals in use` and a repair notice before the 10-terminal limit of EZVIZ is reached. |

## How the instant bell works

- **Unlock and gate events** arrive by EZVIZ push within seconds.
- **Rings** are not sent by push on these devices. The integration asks EZVIZ for new entries in your message list every 5 seconds (adjustable, minimum 2). Expect 5 to 12 seconds from pressing the bell to the picture; most of that is EZVIZ's own delay, which the EZVIZ app has too.
- **Stable terminal ID.** EZVIZ allows 10 logged-in terminals per account. The published `pyezvizapi` derives its ID from the MAC address, which changes in Docker and fills the 10 slots over time. This integration creates one random ID, stores it in `<config>/.storage/ezviz_instant_bell_feature_code` and uses it for every login. You can force a value with the environment variable `EZVIZ_INSTANT_BELL_FEATURE_CODE`.
- **Status** (sensors, switches, sliders) is read every 15 seconds, and straight away after a ring or unlock. A short cloud outage keeps the last values; a longer one makes the entities unavailable and tries a new login once.

## Install

1. In HACS open the three-dot menu, **Custom repositories**, add `https://github.com/kienxux/ezviz_instant_bell` as type **Integration**.
2. Install **EZVIZ Video Intercom** and restart Home Assistant.
3. **Settings > Devices & services > Add integration > EZVIZ Video Intercom**. Log in with your EZVIZ account (and the verification code if EZVIZ sends one) and choose the door station. For an HP7 or CP7 it is the entry marked `BDoorBell`. The indoor monitor is found automatically.

Logging in registers one new terminal named `Hassio` in your EZVIZ account (one of the 10 slots).

## Moving over from the `ezviz_hp7` integration

You keep every entity ID, so dashboards, automations and phone widgets go on working.

1. Install this integration (steps above) but do not add it yet.
2. **Switch the old one off**: *Settings > Devices & services > EZVIZ HP7 > three dots > Disable* (or remove `ezviz_hp7` in HACS and restart). Do **not** delete its entry: deleting removes its entities.
3. Add **EZVIZ Video Intercom**. It offers *Use the login of my existing EZVIZ HP7 setup*; choose it. Your entities (sensors, switches, sliders, buttons, snapshot camera) move over with their IDs, device names and areas.
4. Check your dashboard and the phone widget. If something is off, the old entities are still in the registry; ask in an issue.

The `Live` camera moves over too, together with the live-video settings of the old setup (source, mode, codec, port, key).

If the old integration is still running when you add this one, a repair notice says so, and the old entities are taken over the next time you reload this integration after switching the old one off.

## First check (after installing)

- Press **Unlock Door** while you stand at the door and check that the door opens (and not the gate). If it is the gate, swap the numbers in the options. A failed call gives an error message and never tries the other lock.
- Change the **Monitor volume** slider and see that the monitor follows.
- Toggle **Do Not Disturb** and **Privacy** and see them change in the EZVIZ app.
- Open the **Live** camera. If it stays black, set the source to `local` or `cloud`, and see the log for "StreamRelay" lines.

## Live video

The `Live` camera works like the one of the earlier integration:

- **Source**: `cloud` (EZVIZ relay, the default), `local` (straight from the door station on your network; Home Assistant must be on the same network) or `auto` (local first, then cloud).
- **Mode**: `auto` looks at the video once and picks: H.264 uses the Home Assistant stream (sound, low delay), HEVC uses motion JPEG (every browser, no sound). `webrtc` and `mjpeg` force one of them.
- **Encrypted video**: some firmware encrypts the video and offers no switch in the EZVIZ app. EZVIZ then e-mails a 4-digit code when the live view is opened; a repair notice asks for it (or use the action `fetch_encryption_key`). The 6-character code on the label does not work for this. `set_video_encryption` turns the device's encryption on or off, and only when you call it.
- The relay listens on `127.0.0.1` only. A fixed port and `0.0.0.0` are for go2rtc or Frigate on another machine; the socket has no password, so use it only on a network you trust.
- While someone watches the local stream, the status is read a quarter as often, because the door station's processor is small.
- A ring or unlock wakes the stream up, so the first picture is quick when you open the dashboard.

## Recordings of the SD card

Open **Media** in the Home Assistant sidebar and choose **EZVIZ Video Intercom** > your door station > **Recordings (SD card)** > a day. Each video the door station recorded is listed with its start time and length (newest first); the picture of the ring or motion event next to it is used as the thumbnail when there is one. The same list is reachable from dashboard cards (media browser) and from automations (`media-source://ezviz_instant_bell/...`).

- The video is asked from the EZVIZ servers when you press play: it needs the door station to be online and its SD card to hold the recording. No EZVIZ cloud plan is needed.
- The servers send the video at about normal speed, so a 50-second clip needs about 50 seconds to arrive. Playback starts after a few seconds and runs while the rest arrives (you cannot skip ahead yet). When the clip is complete it is stored as an ordinary MP4 in `<config>/.storage/ezviz_instant_bell_clips` and later views play at once and can be skipped through.
- Stored clips are removed, oldest first, when the folder is larger than 500 MB. The clips are part of a Home Assistant backup unless you exclude that folder.
- One clip is fetched at a time per account. Video encryption must be off (as for the live view without a key).
- `ffmpeg` must be available (it is part of Home Assistant OS and the Docker image).

## History of events (pictures)

Open **Media** in the Home Assistant sidebar and choose **EZVIZ Video Intercom** > your door station > **Pictures of events** > a day. You get every event EZVIZ kept for that day with its picture: rings, door and gate openings, motion. The same list can be used in a dashboard card (media browser) or in automations (`media-source://ezviz_instant_bell/...`).

- The list is what EZVIZ keeps in its message list (about the last days, like the EZVIZ app). The videos are under *Recordings (SD card)*.
- Pictures are fetched on demand and the last 100 are kept in memory; nothing is written to disk.
- The first view of a day takes a moment (up to 4 requests of 50 entries); the list is then kept for a minute (today) or 15 minutes (older days).

## Notification automation

Import the blueprint, then create an automation from it:

[![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fkienxux%2Fezviz_instant_bell%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fezviz_instant_bell%2Fring_notification.yaml)

Choose the doorbell event, the picture, the **Unlock Door** button and the notification service of each phone (for example `notify.mobile_app_my_phone`; add one entry per phone, and whoever taps **Open door** first opens it). It works with the Home Assistant companion app on Android and iPhone. The door opens only if you tap the button for that ring, within a time limit you set. Nothing opens by itself.

## Options

Ring check interval (seconds), serial numbers of the indoor monitors (empty = automatic), the lock numbers for door (2) and gate (1), the ones seen on an HP7, and the live video settings (source, mode, codec, quality, port, listen address, key).

## Differences from `ezviz_hp7`

- A failed **Unlock Door** no longer tries the gate (and the other way round).
- Failed settings show an error message instead of only a log line.
- Do not disturb is read from both EZVIZ flags (alarm and call).
- The list of enrolled cards comes with the normal status read; no extra request.

## Privacy

Your EZVIZ login is stored by Home Assistant like other integrations. Pictures are kept only in memory (the latest one, and up to 100 from the history browser). Recordings you play are stored as MP4 files in the `.storage` folder, up to 500 MB. Nothing is sent anywhere except to EZVIZ.

## Credits and licence

MIT licence. Built on [pyezvizapi](https://github.com/RenierM26/pyEzvizApi) (Apache-2.0), installed as a normal dependency. The way recordings of the SD card are asked from the EZVIZ servers (playback address, client type, stream type) was found by [pyEzvizApi pull request #266](https://github.com/RenierM26/pyEzvizApi/pull/266); it is written here independently from that description and confirmed on a real door station.

The live video is **ported from open-source work, with thanks**:

- the [ezviz_hp7](https://github.com/Bobsilvio/ezviz_hp7) integration by **Bobsilvio** (MIT): the stream relay, the live camera, the encryption handling, and the model for the set of entities and their behaviour;
- the [ezviz_hp7 fork](https://github.com/albrzmr/ezviz_hp7) by **albrzmr** (MIT): the local-network protocol of the CPD7/CP7 family and the motion-JPEG view; contributors to that project found much of the firmware behaviour described in the code comments.

Their copyright notices and licence text are in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md). The rest (instant bell, status reading, settings, migration) is written independently.
