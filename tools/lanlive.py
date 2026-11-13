"""Open a LAN live session with the door station and report what the stream looks like.

Usage: python tools/lanlive.py [SECONDS] [OUTDIR]   (default 240 s, /tmp/lanlive/out)

Stop Home Assistant first: the door station allows one local session, and
INVITE answers Result 1052683 while another one is open. See tools/README.md.

Secrets come from $LANLIVE_SECRETS, default /tmp/lanlive/secrets.env
(KEY=VALUE lines, never printed; keep the file outside the repository):
    HOST=192.168.x.y
    SERIAL=MAIN-CAM          (the serial as shown in Home Assistant)
    LAN_KEY=<16 characters>  (.storage/ezviz_instant_bell.local.<entry_id> -> "lan_key")
    VERIFY_CODE=<key>        (the stream key stored in the entry options; leave out if encryption is off)

Writes raw.ps (as received), per-window decrypted PS files and a timeline:
per 10 s window the bytes, pack headers, the clear-NAL-header size the
detection gives NOW, the SPS fingerprint, and what ffmpeg makes of it.
The decryption is done twice: with the header size detected once at the
start (what the relay does) and with the size detected per window.

Switches (environment):
    LANLIVE_SYNC_ON_PACK=0|1  decoder starts at the first pack header
                              (default: 1 with a stream key, as the relay)
    LANLIVE_DETECT_MIN=BYTES  bytes collected before the header size is
                              decided (default: the relay's 24 KB)
    LANLIVE_REPO=PATH         repository to import the integration from
                              (default: the one this script is in)
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.environ.get("LANLIVE_REPO", str(Path(__file__).resolve().parents[1])))
SECRETS = Path(os.environ.get("LANLIVE_SECRETS", "/tmp/lanlive/secrets.env"))
from custom_components.ezviz_instant_bell._pes import (  # noqa: E402
    cut_to_packet_start,
    keep_clear_video_pes,
)
from custom_components.ezviz_instant_bell.cpd7 import (  # noqa: E402
    Cpd7LanClient,
    StreamDecoder,
)
from custom_components.ezviz_instant_bell.live_relay import (  # noqa: E402
    DECRYPT_DETECT_MIN_BYTES,
)
from pyezvizapi.stream import (  # noqa: E402
    decrypt_hikvision_ps_video,
    detect_hikvision_ps_video_nalu_header_size,
    mpeg_ps_decryptable_prefix_length,
)

DETECT_MIN = int(os.environ.get("LANLIVE_DETECT_MIN", DECRYPT_DETECT_MIN_BYTES))
WINDOW = 10.0
SPS_MARKS = (b"\x00\x00\x01\x67", b"\x00\x00\x01\x42", b"\x00\x00\x01\x40")


def load_secrets() -> dict[str, str]:
    out = {}
    for line in SECRETS.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def sps_fingerprint(data: bytes) -> str:
    for mark in SPS_MARKS:
        i = data.find(mark)
        if i >= 0:
            return data[i + 3 : i + 19].hex()
    return "-"


def ffmpeg_check(path: Path) -> str:
    if path.stat().st_size == 0:
        return "empty"
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
         "-f", "mpeg", "-probesize", "2M", "-analyzeduration", "2M",
         "-i", str(path), "-an", "-f", "null", "-"],
        capture_output=True, text=True, timeout=60,
    )
    err = proc.stderr
    bad = len(re.findall(r"error while decoding|corrupt|left block|Overread", err))
    frames = re.search(r"frame=\s*(\d+)", err)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,width,height,nb_read_frames",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=60,
    ).stdout.strip()
    return f"ffprobe={probe or '?'} decode-errors={bad}" + (f" frames={frames.group(1)}" if frames else "")


def main() -> None:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 240.0
    outdir = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/lanlive/out")
    outdir.mkdir(parents=True, exist_ok=True)
    s = load_secrets()
    host, serial = s["HOST"], s["SERIAL"]
    lan_key = s["LAN_KEY"].encode("ascii")
    verify = s.get("VERIFY_CODE") or None
    sync_on_pack = os.environ.get("LANLIVE_SYNC_ON_PACK", "1" if verify else "0") == "1"
    related = serial.split("-", 1)[1] if "-" in serial else serial
    print(f"host={host} serial={serial} related={related} lan_key={len(lan_key)} chars "
          f"verify_code={'set' if verify else 'none'} duration={seconds:.0f}s")

    t0 = time.monotonic()
    client = Cpd7LanClient(host, related, lan_key, channel=1, stream_quality="main", recv_timeout=2.0)
    try:
        client.start()
    except Exception as exc:  # noqa: BLE001
        print(f"INVITE/handshake FAILED after {time.monotonic() - t0:.1f}s: {exc!r}")
        return
    print(f"session up after {time.monotonic() - t0:.2f}s")
    decoder = StreamDecoder(client.ecdh_priv, sync_on_pack=sync_on_pack)
    print(f"decoder sync_on_pack={sync_on_pack} detect_min={DETECT_MIN}")

    raw = (outdir / "raw.ps").open("wb")
    dec_buf = bytearray()
    fixed_size = None          # what the relay does: detect once, keep
    sizes_seen = []
    win_idx, win_start = 0, time.monotonic()
    win_bytes = win_packs = 0
    win_fixed = (outdir / f"w{win_idx:02d}-fixed.ps").open("wb")
    win_adapt = (outdir / f"w{win_idx:02d}-adaptive.ps").open("wb")
    timeline = []
    empty = 0
    end_reason = "duration reached"

    def close_window():
        nonlocal win_idx, win_start, win_bytes, win_packs, win_fixed, win_adapt
        win_fixed.close(); win_adapt.close()
        row = (win_idx, round(win_start - t0), win_bytes, win_packs,
               sizes_seen[-1] if sizes_seen else None,
               sps_fingerprint((outdir / f"w{win_idx:02d}-adaptive.ps").read_bytes()))
        timeline.append(row)
        print(f"  w{row[0]:02d} t={row[1]:>4}s bytes={row[2]:>8} packs={row[3]:>5} "
              f"hdr_now={row[4]} sps={row[5]}", flush=True)
        win_idx += 1
        win_start = time.monotonic()
        win_bytes = win_packs = 0
        win_fixed = (outdir / f"w{win_idx:02d}-fixed.ps").open("wb")
        win_adapt = (outdir / f"w{win_idx:02d}-adaptive.ps").open("wb")

    raw_bytes = 0
    first_raw = first_out = None
    wire = (outdir / "wire.bin").open("wb")   # bytes as received, before the decoder
    try:
        while time.monotonic() - t0 < seconds:
            buf = client.read_chunk()
            if not buf:
                empty += 1
                if empty > 3:
                    end_reason = ("closed by the door station" if client.peer_closed
                                  else "no data for 8 s")
                    break
                continue
            empty = 0
            raw_bytes += len(buf)
            wire.write(buf)
            if first_raw is None:
                first_raw = time.monotonic() - t0
                print(f"  first bytes from the station after {first_raw:.2f}s ({len(buf)} B)")
            decoder.feed(buf)
            body = decoder.take()
            if not body:
                if raw_bytes and int(raw_bytes / 65536) != int((raw_bytes - len(buf)) / 65536):
                    print(f"  decoder still waiting for a keyframe: {raw_bytes} B received, "
                          f"keys derived={decoder.keys_derived} t={time.monotonic() - t0:.1f}s")
                continue
            if first_out is None:
                first_out = time.monotonic() - t0
                print(f"  decoder synced after {first_out:.2f}s, {raw_bytes} B received so far")
            raw.write(body)
            win_bytes += len(body)
            win_packs += body.count(b"\x00\x00\x01\xba")
            if verify is None:
                win_fixed.write(body); win_adapt.write(body)
            else:
                dec_buf.extend(body)
                head = cut_to_packet_start(dec_buf)
                if fixed_size is None and len(dec_buf) < DETECT_MIN:
                    win_fixed.write(head); win_adapt.write(head)
                    continue
                n = mpeg_ps_decryptable_prefix_length(bytes(dec_buf))
                if n <= 0 and len(dec_buf) > 2 * 1024 * 1024:
                    head += cut_to_packet_start(dec_buf, stalled=True)
                    n = mpeg_ps_decryptable_prefix_length(bytes(dec_buf))
                    print(f"  !! stall pass-through at t={time.monotonic() - t0:.0f}s ({len(head)} B undecrypted)")
                if n > 0:
                    chunk = bytes(dec_buf[:n]); del dec_buf[:n]
                    now = detect_hikvision_ps_video_nalu_header_size(chunk, verify, default=None)
                    if not sizes_seen or sizes_seen[-1] != now:
                        print(f"  header-size detection -> {now} at t={time.monotonic() - t0:.0f}s")
                    sizes_seen.append(now)
                    if fixed_size is None:
                        fixed_size = now
                    try:
                        f = keep_clear_video_pes(chunk, decrypt_hikvision_ps_video(
                            chunk, verify, nalu_header_size=2 if fixed_size is None else fixed_size))
                        a = keep_clear_video_pes(chunk, decrypt_hikvision_ps_video(
                            chunk, verify, nalu_header_size=2 if now is None else now))
                    except Exception as exc:  # noqa: BLE001
                        print(f"  !! decryption raised at t={time.monotonic() - t0:.0f}s: {exc!r}")
                        f = a = chunk
                    win_fixed.write(head + f); win_adapt.write(head + a)
                else:
                    win_fixed.write(head); win_adapt.write(head)
            if time.monotonic() - win_start >= WINDOW:
                close_window()
    finally:
        close_window()
        raw.close()
        wire.close()
        client.close()
    print(f"wire bytes {raw_bytes}, first bytes at {first_raw}, decoder synced at {first_out}")
    print(f"session ended after {time.monotonic() - t0:.1f}s: {end_reason}")
    print(f"header sizes over the session: {sorted(set(map(str, sizes_seen)))} "
          f"(first={fixed_size}, detections={len(sizes_seen)})")

    print("\nffmpeg per window (fixed header size | adaptive):")
    for w, t, *_ in timeline:
        fx = ffmpeg_check(outdir / f"w{w:02d}-fixed.ps")
        ad = ffmpeg_check(outdir / f"w{w:02d}-adaptive.ps")
        print(f"  w{w:02d} t={t:>4}s  fixed: {fx}\n              adaptive: {ad}")


if __name__ == "__main__":
    main()
