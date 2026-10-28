# Instant Bell for EZVIZ

A Home Assistant integration for EZVIZ video intercoms (HP7 / CP7 family). When someone rings, you get a picture on your phone and can open the door with one tap. You do not need to answer the call.

> **Status: early version (0.1.0).** The ring and picture detection was measured on a real HP7 door station. The door and gate buttons have **not yet been checked on a real lock**; see "Door and gate buttons" below. This project is not affiliated with EZVIZ or Hikvision.

## What you get

| Entity | What it does |
| --- | --- |
| **Doorbell** (event) | Fires `ring`, `door_unlock`, `gate_open` or `other` when it happens |
| **Last picture** (image) | The picture that came with the latest event |
| **Open door**, **Open gate** (buttons) | Optional, **off by default** (see below). Open exactly one lock; a failed door call never tries the gate |
| **Terminals in use** (sensor) | How many of your 10 EZVIZ terminal slots are taken, plus a repair notice at 8 |

A ready-made automation is included: ring, wait for the picture, notify your phone with an **Open door** button. The door opens only if you tap the button for that ring, within a time limit you set. Nothing opens by itself.

## How it works

- **Unlock and gate events** arrive by EZVIZ push within seconds.
- **Rings** are not sent by push. The integration asks EZVIZ for new entries in your message list every 5 seconds (adjustable, minimum 2). Expect about 5 to 12 seconds from pressing the bell to the notification; most of that is EZVIZ's own delay, which the EZVIZ app has too.
- **Stable terminal ID.** EZVIZ allows 10 logged-in terminals per account. The published `pyezvizapi` derives its ID from the computer's MAC address, which changes in Docker and fills the 10 slots over time. This integration creates one random ID, saves it in `<config>/.storage/ezviz_instant_bell_feature_code` and uses it for every login. You can force a value with the environment variable `EZVIZ_INSTANT_BELL_FEATURE_CODE`.

## Install

1. In HACS open the three-dot menu, **Custom repositories**, add `https://github.com/kienxux/ezviz_instant_bell` as type **Integration**.
2. Install **Instant Bell for EZVIZ** and restart Home Assistant.
3. **Settings > Devices & services > Add integration > Instant Bell for EZVIZ**. Log in with your EZVIZ account (and the verification code if EZVIZ sends one), then choose the doorbell. For an HP7 or CP7 it is the entry marked `BDoorBell`.

This integration uses its own domain, so it can run next to other EZVIZ integrations without touching their entities. Logging in registers one new terminal named `Hassio` in your EZVIZ account (one of the 10 slots).

## Notification automation

Import the blueprint, then create an automation from it:

[![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fkienxux%2Fezviz_instant_bell%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fezviz_instant_bell%2Fring_notification.yaml)

Choose the doorbell event, the picture, the Open door button and your phone's notification service (for example `notify.mobile_app_my_phone`). It works with the Home Assistant companion app on Android and iPhone.

## Door and gate buttons (optional)

By default this integration does **not** create buttons, so it cannot change the entity IDs of buttons you already use, for example in phone widgets. The notification blueprint lets you pick any existing button as the "Open door" button. To use this integration's own buttons, switch on **Create Open door and Open gate buttons** in its options.

The lock numbers (door 2, gate 1) are the ones seen on an HP7. Before you rely on the new buttons, stand at the door, press **Open door** in Home Assistant and check that it is the door that opens. If it is the gate, swap the numbers in the integration's options. If a call fails you get an error message; it never falls back to the other lock.

## Options

Ring check interval (seconds), whether to create the door and gate buttons, and the lock numbers for door and gate.

## Privacy

Your EZVIZ login is stored by Home Assistant like other integrations. Pictures are kept only in memory (the latest one); nothing is written to disk. Nothing is sent anywhere except to EZVIZ.

## Credits and licence

MIT licence. Built on [pyezvizapi](https://github.com/RenierM26/pyEzvizApi) (Apache-2.0), installed as a normal dependency. This is an independent project and does not contain code from other EZVIZ integrations.
