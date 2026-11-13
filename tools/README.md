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
