<h1 align="center">EZVIZ HP7 / CP7 Video Intercom<br><sub>for Home Assistant</sub></h1>

<p align="center">
  <b>Someone rings: you see who it is within seconds, and open the door with one tap, without answering the call.</b>
</p>

<p align="center">
  <a href="https://github.com/kienxux/ezviz_instant_bell/releases"><img alt="Release" src="https://img.shields.io/github/v/release/kienxux/ezviz_instant_bell?display_name=tag"></a>
  <a href="https://github.com/kienxux/ezviz_instant_bell/actions/workflows/tests.yml"><img alt="Tests" src="https://github.com/kienxux/ezviz_instant_bell/actions/workflows/tests.yml/badge.svg"></a>
  <a href="https://github.com/kienxux/ezviz_instant_bell/actions/workflows/validate.yml"><img alt="HACS and hassfest" src="https://github.com/kienxux/ezviz_instant_bell/actions/workflows/validate.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="MIT licence" src="https://img.shields.io/badge/licence-MIT-blue"></a>
  <a href="https://hacs.xyz"><img alt="HACS" src="https://img.shields.io/badge/HACS-Integration-41BDF5"></a>
</p>

A Home Assistant integration for the **EZVIZ HP7 and CP7 video intercoms**: the door station at your gate or door and the indoor monitor inside. It shows everything the intercom reports, lets you change its settings, and adds what makes a video intercom worth having in a smart home: a ring notification **with the picture**, an **Open door** button on your phone, the **recordings on the SD card**, and the **live video**, all in Home Assistant.

> Independent project. Not affiliated with or endorsed by EZVIZ or Hikvision.

## Highlights

- **Instant ring picture.** An event and the picture of the visitor arrive in about 5 to 12 seconds, most of which is EZVIZ's own delay.
- **Open the door from the notification.** A ready-made blueprint sends the picture to every phone with an **Open door** button. The door only opens when someone taps it for that ring.
- **Visitor log card.** One dashboard card with the rings and unlocks of the last days, their pictures and their SD-card videos, and an optional Open door button.
- **Recordings of the SD card.** Browse and play what the door station recorded, by day, in the Media browser. No EZVIZ cloud plan needed.
- **Live video.** Cloud or directly from your network, with sound in the Home Assistant stream mode.
- **Everything the intercom reports.** Sensors, switches, chime volume and ringtones for the door station and every indoor monitor, and who unlocked the door (app, card, face, palm or code).
- **Safe by design.** Opening the door is always a deliberate action. A failed unlock never tries a different lock.

## Which devices

| | |
| --- | --- |
| **Made for** | EZVIZ video intercoms of the **HP7 and CP7 family**: a door station with an indoor monitor |
| **Checked on real hardware** | CS-CPD5 door station with CS-CP5 monitor (EU account) |
| **Not for** | Other EZVIZ cameras, doorbells and locks: use Home Assistant's own EZVIZ integration for those |

The reading of the device's data is tested against an anonymised copy of a real device's answers. Door and gate buttons, chime sliders and switches use the same EZVIZ calls as the earlier `ezviz_hp7` integration, and live video is ported from it. If something does not work on your model, please [open an issue](https://github.com/kienxux/ezviz_instant_bell/issues/new/choose) and attach the diagnostics download (see [Troubleshooting](#troubleshooting)).

## Install

### With HACS

1. Open HACS and search for **EZVIZ HP7 / CP7 Video Intercom**, or add it as a custom repository (three-dot menu, **Custom repositories**, `https://github.com/kienxux/ezviz_instant_bell`, type **Integration**).
2. Install it and restart Home Assistant.

### Set up

1. **Settings > Devices & services > Add integration > EZVIZ HP7 / CP7 Video Intercom**.
2. Log in with your EZVIZ account (and the verification code if EZVIZ sends one), then choose the door station. For an HP7 or CP7 it is the entry marked `BDoorBell`. The indoor monitor is found automatically.

Logging in registers one new terminal named `Hassio` in your EZVIZ account. EZVIZ allows 10 terminals per account; the integration keeps a stable ID so that restarts and Docker do not fill the slots, and warns you before the limit is reached.

### First check

- Press **Unlock Door** while you stand at the door and check that the door opens (and not the gate). If it is the gate, swap the lock numbers in the options. A failed call shows an error and never tries the other lock.
- Change the **Monitor volume** slider and see that the monitor follows.
- Toggle **Do Not Disturb** and **Privacy** and see them change in the EZVIZ app.
- Open the **Live** camera. If it stays black, set the source to `local` or `cloud` and look for "StreamRelay" lines in the log.

## What you get

| Kind | Entities |
| --- | --- |
| **Instant bell** | `Doorbell` (event: `ring`, `door_unlock`, `gate_open`, `other`) and `Last picture` (image), both within seconds |
| **Open** | Buttons `Unlock Door` and `Unlock Gate`, and the actions `ezviz_instant_bell.unlock_door` / `unlock_gate` |
| **Sensors** | Name, firmware, online status, WiFi signal, SSID, local and WAN IP, motion, last alarm and its type, seconds since last motion, update available, microphone volume |
| **Binary sensors** | Motion; short pulses (3 s) for doorbell ringing, gate open, lock unlocked and how (app, RFID card, face, palm, code), smart and intelligent detection alarm; read-only states of mute, loitering, stranger and human detection |
| **Switches** | Doorbell chime and motion sound alert (door station and indoor monitor), do not disturb, privacy, armed, name-tag light |
| **Sliders** | Chime volume (0 to 7) and ringtones for the doorbell and the motion alert, on the door station and on every indoor monitor |
| **Camera** | `Live`: the live video (cloud or local network). `Last Snapshot`: the picture of the latest alarm |
| **Events** | `ezviz_instant_bell_unlock` (and, for old automations, `ezviz_hp7_unlock`) with the way the lock was opened, the card id and the card holder's name |
| **Media** | *Recordings (SD card)* and *Pictures of events* in the Media browser |
| **Dashboard** | The visitor log card |
| **Account** | `Terminals in use`, and a repair notice before EZVIZ's 10-terminal limit is reached |

## How the instant bell works

- **Unlock and gate events** arrive by EZVIZ push within seconds.
- **Rings** are not pushed on these devices. The integration asks EZVIZ for new entries in your message list every 5 seconds (adjustable, minimum 2). The EZVIZ app has the same delay.
- **Status** (sensors, switches, sliders) is read every 15 seconds, and straight away after a ring or unlock. A short cloud outage keeps the last values; a longer one makes the entities unavailable and tries a new login once.

## Visitor log card

A dashboard card that lists the rings, unlocks and other events of the last days, newest first, each with its picture and, when the door station recorded it, the video from the SD card. Tap a row to see the picture or play the video; the chips filter by kind. The integration adds the card by itself (reload the browser once after installing). Add it with *Edit dashboard > Add card > EZVIZ visitor log*, or in YAML:

```yaml
type: custom:ezviz-visitor-log-card
title: Front door            # optional
serial: BE0000001-BE0000002  # optional; all door stations when left out
days: 2                      # 1 to 7, default 1
show_unlock: true            # adds an "Open door" button (asks to confirm), default false
unlock: door                 # door or gate, default door
event_entity: event.front_door_doorbell   # optional: reload the moment a ring arrives
```

`show_unlock` needs `serial` when you have more than one door station. The list refreshes every minute. Pictures and videos are served through signed links that expire after ten minutes.

## Notification with an Open door button

Import the blueprint, then create an automation from it:

[![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fkienxux%2Fezviz_instant_bell%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fezviz_instant_bell%2Fring_notification.yaml)

Choose the doorbell event, the picture, the **Unlock Door** button and the notification service of each phone (for example `notify.mobile_app_my_phone`; add one entry per phone, and whoever taps **Open door** first opens it). It works with the Home Assistant companion app on Android and iPhone. The door opens only if you tap the button for that ring, within a time limit you set. Nothing opens by itself.

## Recordings of the SD card

Open **Media** in the sidebar, choose **EZVIZ HP7 / CP7 Video Intercom**, your door station, **Recordings (SD card)** and a day. Each video is listed with its start time and length, newest first; the picture of the ring or motion event next to it is its thumbnail. The same list is reachable from dashboard cards and from automations (`media-source://ezviz_instant_bell/...`).

- The video is asked from the EZVIZ servers when you press play. The door station must be online and its SD card must hold the recording. No EZVIZ cloud plan is needed.
- The servers send the video at about normal speed, so a 50-second clip needs about 50 seconds to arrive. Playback starts after a few seconds and runs while the rest arrives (no skipping ahead yet). When the clip is complete it is stored as an ordinary MP4 in `<config>/.storage/ezviz_instant_bell_clips`; later views start at once and can be skipped through.
- Stored clips are removed, oldest first, when the folder is larger than 500 MB. The clips are part of a Home Assistant backup unless you exclude that folder.
- One clip is fetched at a time per account. Video encryption must be off (as for the live view without a key).
- `ffmpeg` must be available (it is part of Home Assistant OS and the Docker image).

## Pictures of events

**Media > EZVIZ HP7 / CP7 Video Intercom > your door station > Pictures of events > a day** shows every event EZVIZ kept for that day with its picture: rings, door and gate openings, motion, for the last 14 days.

- The list is what EZVIZ keeps in its message list, like the EZVIZ app.
- Pictures are fetched on demand and the last 100 are kept in memory; nothing is written to disk.
- The first view of a day takes a moment (up to 4 requests of 50 entries); the list is then kept for a minute (today) or 15 minutes (older days).

## Live video

- **Source**: `cloud` (EZVIZ relay, the default), `local` (straight from the door station on your network; Home Assistant must be on the same network) or `auto` (local first, then cloud).
- **Mode**: `auto` looks at the video once and picks: H.264 uses the Home Assistant stream (sound, low delay), HEVC uses motion JPEG (every browser, no sound). `webrtc` and `mjpeg` force one of them.
- **Encrypted video.** Some firmware encrypts the video and offers no switch in the EZVIZ app. EZVIZ then e-mails a 4-digit code when the live view is opened; a repair notice asks for it, or use the action `fetch_encryption_key` (with `serial` when you have more than one door station). The 6-character code on the label does not work for this. `set_video_encryption` turns the device's encryption on or off, and only when you call it. Both actions need an administrator.
- **Relay.** The relay listens on `127.0.0.1` only. A fixed port and `0.0.0.0` are for go2rtc or Frigate on another machine; that socket has no password, so use it only on a network you trust.
- While someone watches the local stream, the status is read a quarter as often, because the door station's processor is small. A ring or unlock wakes the stream up, so the first picture is quick.

## Options

Ring check interval (seconds), serial numbers of the indoor monitors (empty = automatic), the lock numbers for door (2) and gate (1) as seen on an HP7, and the live video settings (source, mode, codec, quality, port, listen address, key).

## Troubleshooting

- **Download diagnostics** from *Settings > Devices & services > EZVIZ HP7 / CP7 Video Intercom > three dots* and attach the file to your issue. It contains no password, account name, serial numbers, addresses or keys.
- Switch on debug logging for `custom_components.ezviz_instant_bell` and look for "StreamRelay" (video) lines. Remove passwords, keys and serial numbers before sharing logs.
- A setup that fails with an unknown value from the device is a bug in this integration: please report it.

## Privacy and security

- Your EZVIZ login is stored by Home Assistant like for any integration. Nothing is sent anywhere except to EZVIZ (and, for the local stream, to your own door station).
- Pictures are kept only in memory (the latest one, and up to 100 from the history browser). Recordings you play are stored as MP4 files in the `.storage` folder, up to 500 MB.
- The administrator-only actions are the ones that change video encryption or fetch its key. The door can be opened by every Home Assistant user who may press the button, so give guests a dashboard without it.
- The ring blueprint uses a random ID per ring for its Open door button.

## Moving over from `ezviz_hp7`

You keep every entity ID, so dashboards, automations and phone widgets go on working.

1. Install this integration but do not add it yet.
2. **Switch the old one off**: *Settings > Devices & services > EZVIZ HP7 > three dots > Disable* (or remove it in HACS and restart). Do **not** delete its entry: deleting removes its entities.
3. Add this integration. It offers *Use the login of my existing EZVIZ HP7 setup*; choose it. Sensors, switches, sliders, buttons, the snapshot camera and the `Live` camera move over with their IDs, names and areas, together with the live-video settings.

If the old integration is still running when you add this one, a repair notice says so, and the old entities are taken over the next time you reload this integration after switching the old one off.

## Roadmap and contributing

See [ROADMAP.md](ROADMAP.md) for what is planned, and the [open issues](https://github.com/kienxux/ezviz_instant_bell/issues) to vote or add ideas. Bug reports and pull requests are welcome; the bug template asks for the version, model and diagnostics, which is what makes a problem fixable.

## Credits and licence

MIT licence. Built on [pyezvizapi](https://github.com/RenierM26/pyEzvizApi) (Apache-2.0), installed as a normal dependency. The way recordings of the SD card are asked from the EZVIZ servers (playback address, client type, stream type) was found by [pyEzvizApi pull request #266](https://github.com/RenierM26/pyEzvizApi/pull/266); it is written here independently from that description and confirmed on a real door station.

The live video is **ported from open-source work, with thanks**:

- the [ezviz_hp7](https://github.com/Bobsilvio/ezviz_hp7) integration by **Bobsilvio** (MIT): the stream relay, the live camera, the encryption handling, and the model for the set of entities and their behaviour;
- the [ezviz_hp7 fork](https://github.com/albrzmr/ezviz_hp7) by **albrzmr** (MIT): the local-network protocol of the CPD7/CP7 family and the motion-JPEG view; contributors to that project found much of the firmware behaviour described in the code comments.

Their copyright notices and licence text are in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md). The rest (instant bell, status reading, settings, visitor log, migration) is written independently.
