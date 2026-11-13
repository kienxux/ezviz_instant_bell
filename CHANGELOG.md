# Changelog

All notable changes. The newest version is first. Versions follow the `version` in `manifest.json`.

## 1.2.1

- Encrypted local live video no longer turns to garbage by chance. The size of the clear part of each video packet was decided on the first kilobyte of the session, where the answer is a coin toss (wrong twice in two real sessions on an HP7); a wrong answer made every frame of the session unreadable, and a session reopened in the background could flip from good to bad at any moment. The decision now waits for 24 KB (about 2 seconds), where it was right in 300 of 300 checks.
- Encrypted local live video starts right away. The decoder waited for a keyframe marker that an encrypted stream does not show, so it started on a chance match: after 8 seconds in one real session, after 110 in the next. With a stream key in use it now starts at the first packet.
- When the door station refuses a local session because another one is still open (it allows one at a time), the log says so instead of showing the raw answer. Measured on a real HP7: the station answered this to every attempt while another Home Assistant instance held a session.

## 1.2.0

- The local live video starts in about 2 to 3 seconds instead of 11 to 12, also when nobody watched before. ffmpeg waited out its whole 10-second probe, twice in the motion-JPEG view. Every local viewer now starts on a keyframe with its parameter sets, so a 1-second probe is enough. Measured with a synthetic HP7-style stream; not yet confirmed on a door station.
- The local live video no longer asks the EZVIZ cloud at each start. Before, every start downloaded the account's whole device list for the door station's address, and the local key was fetched again every hour. The stored key and address are now used; the cloud is asked only when the door station refuses them or cannot be reached, and the start is tried once more if either changed. The log says "The LAN key of … has changed since it was stored" if it ever does.
- A local stream that stops sending is noticed after about 8 seconds instead of 32, and the log says how long the session lasted and whether the door station closed it. With **Keep the live video open all the time** or after a ring, it is reopened at once instead of at the next 15-second check (at most every 5 seconds).
- A viewer that joins a running local stream gets every chunk exactly once; before, a chunk arriving at that moment could be lost or doubled.
- Diagnostics: each door station (and indoor monitor) now also lists every plain setting the EZVIZ cloud reports for it, by its path, with its value (numbers and yes/no only; texts, lists, cards and network data are left out). The HP7 Pro's keyboard backlight is not among the switches (#25); two downloads, one with the backlight on and one with it off, can now show whether it is one of these settings.

## 1.1.4

- Fixed: an HP7 that the EZVIZ account lists only once, as its indoor monitor (category `DBScreen`, no `BDoorBell` entry), got no unlock buttons, chime controls or name-tag light since 0.11.0; they were removed as if it were a camera (#27). `DBScreen` now counts as an HP7 / CP7 too. The setup form ticks the `BDoorBell` entry, or the `DBScreen` one when the account has no `BDoorBell`. On such an account, leave **Indoor monitor serials** empty: the monitor is the device itself.
- Fixed: downloading the diagnostics read the pyezvizapi version from disk on Home Assistant's event loop, which logged "Detected blocking call".

## 1.1.3

- Fixed: with the source `local`, a viewer that joined a running live video got no picture on an HP7 since 1.1.1. Frigate, the camera card and the dashboard showed nothing, or kept the last picture from before the update. 1.1.1 waited for a keyframe whose own video packet has a time stamp and dropped everything else; a door station that puts each part of a frame in a packet of its own, with a time stamp only on the first, never sends one. Such a viewer now starts on the pack header in front of the keyframe. Parameter sets that come in packets of their own are kept too, so a keyframe without them can still be shown.
- Fixed: with the door station's own serial entered as indoor monitor, setup stopped with "A device can not be its own via device" (#27). The monitor's controls now stay on the door station's device.

## 1.1.2

- Fixed: with Image/Video Encryption on, opening the door made EZVIZ e-mail a security code each time. The picture of the event is encrypted, and pyezvizapi asked the EZVIZ cloud for the camera key for every picture, up to four times per event; each request mails a code. Event pictures are now decrypted with the key stored in the options, without asking the cloud.
- Without a stored key the cloud is asked at most once per 30 minutes (about as long as a mailed code is valid). The repair form's "send a new code" still asks right away.
- The local live video starts in about 2 seconds instead of about 11 when it is already open (with **Keep the live video open all the time**, after a ring, or while another viewer watches). Both ffmpeg steps waited out their whole 10-second probe before the first picture, because MPEG-PS and MPEG-TS can add streams at any time. A viewer that starts on the cached keyframe now gets a 1-second probe; a fresh session keeps the long one.

## 1.1.1

- Fixed: a viewer that joined a local live video that was already running got no picture. ffmpeg received the end of the previous frame first, without a time stamp, and stopped. That is what Frigate ran into with **Keep the live video open all the time**, and it could also hit a viewer opening the live video after a ring. Such a viewer now starts on the keyframe itself.

## 1.1.0

- New option **Open the live video at a ring for (minutes)**: at a ring, and at motion or a person when those events are on, the live video is opened straight away and kept open for that many minutes, so the picture shows at once when you look. Off (0) by default.
- New option **Keep the live video open all the time**: the live video stays open day and night, as it does when Frigate reads it, and shows at once. Only with the source `local`; with `cloud` or `auto` it is ignored and the log says so. Off by default.

## 1.0.0

- First stable version. From now on the entity IDs, events, actions and options stay as they are; a change that breaks them would come with 2.0.0. Nothing was removed for it: the old event name `ezviz_hp7_unlock`, the buttons of 0.1.0 and the relay socket for other machines keep working.
- The options are grouped in sections: *Rings, recordings and pictures*, *Live video*, *Other apps (go2rtc, Frigate)* and *Troubleshooting*. The older live video options got short names, with the explanation below each field instead of inside the name, in English and German. Saved options stay as they are.
- A repair notice appears while the **Relay listen host** is not this computer (for example `0.0.0.0`): that socket has no password, and the addresses with a token are the safer way for go2rtc and Frigate. It goes away when the host is set back to `127.0.0.1`.
- The action `set_video_encryption` no longer says that encryption blocks the local live stream; the camera key decrypts both stream sources.
- The tests now also run on Home Assistant 2025.9.0, the oldest version the integration supports, besides a current one.
- The roadmap is up to date.

## 0.12.1

- The diagnostics download shows each setting (switch) of a door station with its state, on or off, instead of only its number. A setting the integration does not know yet is found by switching it in the EZVIZ app between two downloads (#25).

## 0.12.0

- New switch **Doorbell Indicator** for the light at the call button of the door station (the "Doorbell indicator" of the EZVIZ app, EZVIZ switch 3). It is made only when the door station reports this setting (#22).
- The diagnostics download lists the numbers of the settings (switches) each door station reports. A control the integration does not know yet, such as the keyboard backlight of the HP7 Pro, can be added from them.
- The README is reorganised: a table of contents, a section on Frigate and go2rtc with examples, the events and actions for automations, and the options as a table.

## 0.11.0

- New option **Let other apps read the live video (go2rtc, Frigate)**, off by default. Home Assistant then serves the live video of each door station at `/api/ezviz_instant_bell/live/<serial>?token=…` (MPEG-TS with sound) and `/api/ezviz_instant_bell/mjpeg/<serial>?token=…` (motion JPEG). The options show both addresses; **Make a new token** stops the addresses handed out so far. A wrong token counts as a failed login, so Home Assistant's IP ban applies. The relay socket without a password (`0.0.0.0`) still works but is no longer the recommended way. The diagnostics download hides the token.
- Other EZVIZ cameras and doorbells of the account no longer get the **Unlock Door / Unlock Gate** buttons, the chime switches and sliders or the name-tag light: only an HP7 / CP7 door station (EZVIZ category `BDoorBell`) has them. The unlock actions refuse such a device. Those entities made by earlier versions for a camera are removed after the update. A device whose category EZVIZ does not report keeps everything, as before.
- The visitor log card is now put into *Settings > Dashboards > Resources* by the integration, so a browser that stayed open while Home Assistant restarted no longer shows "Custom element doesn't exist". Its address carries the version, so browsers load the new card after an update, and it is taken out again when the last account is removed. Resources kept in YAML are left alone; the card is then loaded as before.

## 0.10.4

- Still images of the live camera (dashboard pictures, thumbnails, cards that refresh every second) are now made from the latest keyframe the running live stream already has: encoded once per keyframe and shared by all requests. Each request used to start its own ffmpeg that read the stream like a viewer, which took about 10 seconds with the local stream source and showed nothing while the stream gave nothing (#22).
- A live viewer that disconnects before the picture starts now stops its ffmpeg at once. Before, it kept running for 90 seconds; with a card refreshing every second that added up to dozens of ffmpeg processes.

## 0.10.3

- The encrypted cloud live view of 0.10.2 still stayed black (#22). The cloud hands the stream over in pieces that do not start at a packet, and once the key was found in the middle of one, nothing was decrypted or passed on any more (`video sender done (0 B sent)`). Decryption now starts at the next packet. A decryption buffer that grows past 2 MB without anything to decrypt now skips ahead instead of growing without limit.

## 0.10.2

- Encrypted live video now also plays with the cloud stream source (#22). Until now only the local source decrypted it, and the cloud live view stayed black. With the cloud source the integration now also notices the encryption and asks for the camera key (or the 4-digit e-mail code in Settings > Repairs), as it does locally.
- On door stations that also encrypt the header of each video frame part, random bytes could look like an H.264 header, and the integration settled on the wrong codec. It now reads the codec only from video that is unencrypted or already decrypted; with the cloud source the wrong codec also gave ffmpeg the wrong input format.

## 0.10.1

- Encrypted live video on HP7 firmware 5.4 still stayed black with 0.10.0 (#22). The door station encrypts the sound as well, and ffmpeg sends no picture until it has decoded some sound. The sound is now decrypted with the same camera key; frames that still cannot be read are played as silence, so the picture shows in any case. This works with the local stream source (`local`, or `auto` on the same network); the cloud source decrypts neither picture nor sound. Door stations without encryption are not affected.

## 0.10.0

- New options **Motion as an event of the Doorbell entity** and **Person as an event of the Doorbell entity**, both off by default (#12). The **Doorbell** event entity then also has the types `motion` and `person`. They come from the cloud message list a few seconds after the event and are told apart by the message's name and code; an unrecognised message stays `other`. With the options off nothing changes. Their picture does not replace the ring's in **Last picture**.
- The diagnostics download lists the codes of the latest messages (`recent_messages`: subtype, alarm type, recognised kind; the text only for motion and person messages), to check how a door station names them.
- Encrypted live video on newer HP7 firmware (5.4) stayed black although the camera key was stored (#22). This firmware also encrypts the header of each video frame part, and the integration now finds out per session which way the door station encrypts before decrypting. Door stations that already worked are not affected.
- On the same firmware only the video packets with keyframes are encrypted; the others come in the clear and were garbled by decrypting them too (`PPS id out of range`). Packets the door station does not mark as scrambled are now passed on unchanged (#22).
- The **persistent notification** ring blueprint shows the picture in the notification instead of printing its address as text. The picture link expires after a few minutes, so the notification also says where the picture stays: *Media > EZVIZ HP7 / CP7 Video Intercom*. Re-import the blueprint to get the change.
- The log message for an encrypted stream without a key no longer points to the 6-character label code; it names the 4-digit e-mail code and Settings > Repairs.

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
