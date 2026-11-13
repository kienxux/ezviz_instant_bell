# Developer tools

Not part of the integration: HACS installs only `custom_components/ezviz_instant_bell`.

## lanlive.py

Opens one local (LAN) live session with a door station, the way the integration does, and reports
what the stream looks like: when the first bytes arrive, when the decoder syncs, the clear-NAL-header
size the detection gives, the SPS fingerprint and what ffmpeg makes of each 10-second window.
It was written to check the live video fixes of 1.2.1 against a real HP7.

```bash
python tools/lanlive.py [SECONDS] [OUTDIR]     # default 240 s, /tmp/lanlive/out
```

**Before a run**

- **Stop Home Assistant** (or make sure nothing watches the live video and *Keep the live video open
  all the time* is off). The door station allows one local session at a time; while another one is
  open, it answers every attempt with Result 1052683.
- Put the secrets in a file **outside the repository**, by default `/tmp/lanlive/secrets.env`
  (another path with `LANLIVE_SECRETS=...`):

  ```
  HOST=192.168.x.y
  SERIAL=<the serial as shown in Home Assistant>
  LAN_KEY=<16 characters: .storage/ezviz_instant_bell.local.<entry_id> -> "lan_key">
  VERIFY_CODE=<the stream key from the entry options; leave out if encryption is off>
  ```

  The script prints the length of the keys, never their values. Keys are credentials: they do not
  go into git, issues or chats.

**Rules**

- One connection at a time, with pauses between runs. Never scan the door station's ports: its LAN
  service once stopped answering after a parallel scan.
- The script sends only INIT, INVITE and PLAY, the same as Home Assistant. Anything that sends other
  commands needs the owner's go-ahead and the owner at the door.

**Output** (in OUTDIR): `wire.bin` (bytes as received), `raw.ps` (after the session decryption),
`wNN-fixed.ps` / `wNN-adaptive.ps` per window (video decrypted with the header size detected once,
as the relay does, and detected per window). They contain your video: keep them outside the
repository.

**Switches** (environment): `LANLIVE_SYNC_ON_PACK=0|1` (default 1 with a stream key, as the relay),
`LANLIVE_DETECT_MIN=<bytes>` (default the relay's 24 KB), `LANLIVE_REPO=<path>` (default the repository
the script is in).

## lancorrelate.py

Puts a long packet capture of the unit (for example a day) next to its events, to find out whether
anything is sent **locally** when it rings, unlocks or sees motion, or whether everything goes only
to the EZVIZ cloud. It only reads a file; nothing is sent to any device.

```bash
python tools/lancorrelate.py day.pcap --device 192.168.x.y --events notes.txt [--events history.csv]
```

- **Capture:** the FRITZ!Box packet capture (*Paketmitschnitt*) of the interface the unit is on; for
  an HP7 that is the indoor monitor on Wi-Fi (the door station reaches the monitor by wire). Turn off
  *Keep the live video open all the time* for the day, or the video fills the file. The file is read
  into memory at once.
- **`--device`:** the unit's IPv4 address. Its MAC address is learned from it, so IPv6 counts too.
  Without it the tool only lists the hosts of the capture with the names they look up (an EZVIZ unit
  looks up `*.ezvizlife.com`), so you can find the address.
- **`--timeline SECONDS`:** also lists the unit's traffic in buckets of that many seconds, with the
  busiest peer of each: a call or an unlock shows as a burst after a quiet stretch.
- **Time:** the FRITZ!Box writes the clock of the capture in UTC (checked against the server time of an
  EZVIZ push message: 13 ms apart). Give `--tz Europe/Berlin` (the default) for the times in your notes;
  the report prints the notes' time zone.
- **Formats:** pcap, pcapng and the FRITZ!Box "modified pcap" (magic `a1b2cd34`, file name `.eth`).
- **`--events`:** notes in local time, one per line (`2026-11-14 12:03:15 ring`, a comment may follow),
  and/or the CSV download of Home Assistant's History page for the event entities. Rings, unlocks and
  motion done on purpose at noted times are the best evidence.
- **Output:** the peers with their DNS names, whether each TCP connection is TLS (with its server name)
  or another protocol (its first 4 bytes), the traffic around each event, and per kind of event the
  packet signatures seen around most events and rarely otherwise. A local one seen around every event
  and almost never otherwise is marked `LOCAL CANDIDATE`.

The capture holds the whole network traffic of the unit: keep it outside the repository and do not
share it; the report contains addresses and names, but no content.
