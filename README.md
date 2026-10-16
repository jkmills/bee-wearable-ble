# bee-control

Unofficial manual-control CLI for the Bee wearable's Bluetooth LE protocol,
built from reverse-engineering the official Android app. See
[`PROTOCOL.md`](./PROTOCOL.md) for the full write-up of the GATT service,
characteristics, command IDs, and wire format this is built on.

> **Disclaimer.** This is an independent, unofficial project with no
> affiliation to, or endorsement from, Bee, Amazon, or any related company.
> It was built by reverse-engineering the official Android app and observing
> traffic from one legitimately-owned device. It's intended for use only
> with a device you own, for local-only control/capture as an alternative to
> the vendor's cloud-dependent app. The protocol is undocumented and can
> change at any time; use at your own risk. No warranty of any kind — see
> [`LICENSE`](./LICENSE).
>
> This repo intentionally does not include and has never included any
> device-specific secrets (pairing keys, device IDs), captured audio, or the
> vendor's own app binaries — see "Getting your own device's secrets" below
> for how to obtain the one real key your own device needs.

The stock app only exposes brightness and power-off as user controls. This
tool exposes everything the protocol supports: sleep modes (HW/SW voice
activity detection wake), mute, TX power, audio bitrate, firmware/device
info, and a live event watcher for button presses and battery/charging
events.

**Status: verified against a real (legacy-hardware) unit.** `scan`, `status`,
`info`, `brightness` (read+write, 0-100), `power-off`, `sleep hw-vad`, and
`sleep sw-vad` are all confirmed working against physical hardware.
`tx-power` and `bitrate` writes are implemented but untested live.

**`mute`/unmute only sticks with a live encrypted session first** — a plain
`SetDeviceState(MUTE/RECORDING)` write ACKs but `GetDeviceState` reads back
unchanged; see `PROTOCOL.md`'s "long-standing mute/recording mystery is
resolved" section. The web UI's Mute on/off buttons
(`src/bee_control/controls.py`'s `_set_mute`) establish that session (resend
the real captured token + fetch a session nonce) before muting, matching the
sequence confirmed live, and read `GetDeviceState` back afterward so the log
shows whether it actually stuck — this requires `device_secrets.json`. The
plain CLI's `bee-control mute` command hasn't been updated to do this yet
and will still not reliably stick.

**Local-only operation (no Bee/Amazon cloud) is confirmed working, for both
control and real audio capture.** `BeeDevice.unlock_and_set_local_key()`
pushes a self-generated AES-128-CTR key to the device, confirmed to persist
across a real disconnect/reconnect with all control commands (sleep, power,
brightness, mute/recording state) behaving normally — no further dependency
on Bee's backend for any of that. **Audio specifically still requires the
one real captured key** rather than a self-generated one (see the Known
gaps note below) — but since that key is already captured and stored
locally, this still means zero ongoing cloud dependency for audio either.
`capture_audio.py` demonstrates the full pipeline: connect, establish the
key, subscribe to the audio characteristic, decrypt AES-128-CTR, reassemble
ADTS/AAC frames, save a playable file — confirmed against real captured
audio. All of this requires one legitimately captured real pairing token
first (see `PROTOCOL.md`'s "Local-only operation" section and
`device_secrets.json`, gitignored, never committed — it holds live key
material for this specific device).

**Important — only applies the first time this Mac connects to a given
device, or after "forgetting" it:** an unbonded central (like this tool, the
first time) can only connect while the device is in its explicit
discoverable/pairing mode — plain "disconnected, searching for its last
phone" advertising isn't enough; `connect()` will reliably time out in that
state. Put the device into its pairing mode before that first connection.
**Confirmed live: once that first connect succeeds, macOS bonds with the
device** (it shows up under System Settings → Bluetooth / `system_profiler
SPBluetoothDataType`'s device list from then on), and every later connect —
including across restarting `bee-control-ui`, reconnecting after a
disconnect, or a fresh `scan`+`connect` — works with the device just sitting
idle/advertising normally, no pairing mode needed. If your Mac has
previously connected to the device, you may also need to "forget" it in
System Settings → Bluetooth first, so macOS treats the next connection as a
fresh pairing.

## Setup

Requires `uv` (https://docs.astral.sh/uv/). On macOS, Bluetooth permission
will be requested the first time you scan/connect.

```bash
cd bee-wearable-ble  # this directory
uv sync
```

## Getting your own device's secrets

Control commands other than `mute`/`unmute` work with no setup beyond
`uv sync` (see Known gaps below for why mute needs more). **Mute/unmute and
audio capture need one real, legitimately-captured pairing token from your
own device**, saved locally as `device_secrets.json` (gitignored — never
commit this file; it's never read by anything but your own local run):

```json
{
  "device_id_hex": "<from GetDeviceID or the BLE advertisement, see PROTOCOL.md>",
  "sealed_token_hex": "<the full SetEncryptionKey write payload, see below>",
  "aes_ctr_key_hex": "<sealed_token_hex bytes 12:28 -- i.e. sealed_token[24:56] in hex-string terms>"
}
```

To capture this from your own device with your own phone (no third-party
account access needed, nothing leaves your own devices):

1. On the Android phone already paired with your Bee in the real app,
   enable Bluetooth HCI snoop logging in Settings → Developer options (the
   UI toggle, not `adb shell settings put` directly — see `PROTOCOL.md`'s
   "Local-only operation" section for why that distinction matters on some
   builds).
2. In the real Bee app, forget the device, then re-pair it from scratch,
   with the snoop log running throughout. The key exchange (`SetEncryptionKey`,
   opcode `0xC018`) only happens once, at pairing time — it won't show up in
   ordinary reconnect traffic.
3. Pull the snoop log (e.g. via `adb bugreport`, since the raw log file
   itself usually isn't readable over a plain non-rooted `adb shell`) and
   parse it for the BTSnoop-wrapped ATT Write Request on your device's
   control characteristic carrying that opcode. `PROTOCOL.md` documents the
   exact 108-byte payload structure (`"SKV1"` magic, device ID, the AES key
   at bytes 12-28, and 80 bytes of opaque sealed material) to confirm you've
   found the right packet.
4. Save the full payload as `sealed_token_hex` and bytes 12-28 as
   `aes_ctr_key_hex` in `device_secrets.json` as shown above.

This only needs to be done once per device — the key persists on the device
across reconnects, and this repo's tools never send it anywhere but to your
own Bee over your own local BLE connection.

## Web UI

```bash
uv run bee-control-ui
```

Opens a local server at http://127.0.0.1:8765 (nothing is exposed outside
localhost). Scan, connect, and run controls from the browser; a live log
panel shows device events (button presses, battery/charging) and the result
of every command as they happen, over a WebSocket. The control list on the
page is generated from `src/bee_control/controls.py` — to add a newly
discovered control, add one `Control` entry there (id, label, group, kind,
handler, status) and it shows up in the UI automatically, no HTML/JS changes
needed. Each control is tagged `confirmed` / `no_effect` / `untested` so it's
always clear which ones are verified against real hardware (see
`PROTOCOL.md`) versus just structurally implemented.

## CLI usage

```bash
# Find your device's BLE address (must be powered on, not connected elsewhere)
uv run bee-control scan

# Everything else needs --address
uv run bee-control --address AA:BB:CC:DD:EE:FF status
uv run bee-control --address AA:BB:CC:DD:EE:FF info
uv run bee-control --address AA:BB:CC:DD:EE:FF brightness        # read
uv run bee-control --address AA:BB:CC:DD:EE:FF brightness 80     # write
uv run bee-control --address AA:BB:CC:DD:EE:FF mute on
uv run bee-control --address AA:BB:CC:DD:EE:FF mute off
uv run bee-control --address AA:BB:CC:DD:EE:FF sleep hw-vad
uv run bee-control --address AA:BB:CC:DD:EE:FF sleep sw-vad
uv run bee-control --address AA:BB:CC:DD:EE:FF power-off
uv run bee-control --address AA:BB:CC:DD:EE:FF tx-power
uv run bee-control --address AA:BB:CC:DD:EE:FF bitrate
uv run bee-control --address AA:BB:CC:DD:EE:FF watch            # live events, Ctrl-C to stop
```

## Local audio capture + transcription + speaker ID

```bash
# Capture continuously (Ctrl-C to stop and save), decrypt it, and
# transcribe it (whisper, fully local)
uv run python3 capture_audio.py

# Or capture for a fixed window instead, same as before:
uv run python3 capture_audio.py 30

# On an already-captured file: transcribe + diarize (who spoke when) +
# match/name speakers + flag likely background TV/radio/music
uv run python3 process_audio.py captured_audio.aac
```

`capture_audio.py` connects, establishes the encryption key (real key by
default — see the caveat above), subscribes to the audio characteristic,
decrypts and reassembles ADTS/AAC frames live, saves `captured_audio.aac`,
and transcribes it with `faster-whisper` (downloads a small model on first
run, then fully offline).

**Capture is continuous by default — there's no set duration unless you ask
for one.** This matches how the real app works: decompiling it
(`BeeProtocolAudioStreamPlugin`, see `PROTOCOL.md`) shows it has no
client-side "record for N seconds" timer either — it just stays connected
with the audio characteristic subscribed, and packets flow whenever the
device itself decides to send them (unmuted + voice detected), for as long
as the BLE connection stays open; the app relays whatever arrives to its
backend live. `capture_audio.py` does the same thing locally: run it with no
argument and it streams for as long as you leave it running, writing frames
(and the raw decrypted byte stream, for diagnostics) to disk as they arrive
rather than buffering until the end, so stopping it with Ctrl-C at any point
still saves everything captured up to that point. Pass a number of seconds
as the first argument for the old fixed-duration behavior instead.

**Status: confirmed working end-to-end** — transcription, diarization,
speaker labeling, and media tagging all ran successfully together against
real captured audio. Two environment fixes were needed and are already
applied/documented: `av` must be pinned below 19.x (`pyproject.toml` already
does this) since `faster-whisper` 1.2.1 isn't compatible with newer PyAV's
API, and a system FFmpeg install (`brew install ffmpeg`) is required for
`pyannote.audio`'s `torchcodec`-based audio loading (separate from
`faster-whisper`'s bundled-FFmpeg PyAV path). Diarization accuracy on
multi-speaker recordings and the media-tagging heuristic's real-world
usefulness are both still only lightly tested (so far: one short
single-speaker clip) — worth stress-testing with a longer, multi-person
recording and a real TV/radio playing in the background.

`process_audio.py` runs the fuller local pipeline on any already-captured
file:
- **Transcription** (`src/bee_control/transcribe.py`, `faster-whisper`).
- **Speaker diarization** — who spoke when (`src/bee_control/diarize.py`,
  `pyannote.audio`). **Requires one-time setup**: a free Hugging Face
  account, accepting the (free) terms for
  [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)
  and [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0),
  and running `uv run hf auth login` once with a read-access token from
  [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens).
  The token is stored by Hugging Face's own tooling in your local cache —
  this project never sees or stores it. After that one-time setup,
  diarization runs fully offline.
- **Speaker naming/matching** (`src/bee_control/speakers.py`) — computes a
  voice embedding per speaker in the recording, matches it by cosine
  similarity against `speaker_profiles.json` (gitignored — local voice-ID
  data), and prompts you to name any unrecognized speaker. Named profiles
  persist and improve (running average) across recordings.
- **Background/media detection** (`src/bee_control/audio_tagging.py`) — a
  PANNs (AudioSet-pretrained) audio-event classifier flags segments that
  look like TV/radio/music rather than direct foreground speech. One-time
  setup: run `scripts/fetch_panns_model.sh` to download the model (curl,
  since `panns_inference`'s own downloader expects `wget`, not installed on
  macOS by default). **This is a coarse heuristic, not a reliable
  classifier** — AudioSet taggers weren't built to reliably distinguish "a
  person talking in the room" from "a person talking in a TV show playing
  in the room"; treat its output as something to review, not trust blindly.

## Known gaps

- **The CLI's `mute on`/`off` ACK but don't appear to change anything** —
  it sends a plain `SetDeviceState(MUTE/RECORDING)` with no session setup,
  and `GetDeviceState` reads back unchanged. This isn't because that
  `DeviceState` value is the wrong switch: PROTOCOL.md's follow-up testing
  found mute/recording transitions are gated on having a live encryption
  key + session nonce established first, unlike `POWER_OFF`/`SLEEP_HW_VAD`/
  `SLEEP_SW_VAD` (all reachable via named `Companion` factories), which work
  unauthenticated with no key setup at all. The web UI's Mute controls do
  the session setup and are expected to stick (see the note above);
  `bee-control mute` (the CLI) still doesn't and needs the same fix applied.
  Separately: real physical button presses also don't trigger this —
  decompiling `BeeProtocolButtonEventsPlugin` showed the button path only
  relays events to the app's backend (via Socket.IO), never sending a BLE
  command back to the device. See `PROTOCOL.md` for the full analysis.
- **Audio decryption is confirmed working end-to-end** (`src/bee_control/audio.py`,
  `BeeDevice.start_audio_capture`, `capture_audio.py`) — a real capture
  produced 64 consistent, valid ADTS/AAC frames at 16kHz. **Important
  caveat**: this only works with the real captured key
  (`device_secrets.json`'s `aes_ctr_key_hex`) — a self-generated key
  (via `unlock_and_set_local_key`) is accepted by the device's control-plane
  and works fine for every other command, but does *not* propagate to the
  device's real-time audio-encryption engine, so audio decrypted with a
  self-generated key comes out as noise. `capture_audio.py` defaults to the
  real key for this reason; pass `--self-key` only to reproduce/re-verify
  that discrepancy. See `PROTOCOL.md`'s "Audio packet layout and ADTS
  framing" section for the full investigation.
- Battery/charging decoding only implements the legacy (non-Frontier,
  company ID `0xBEEE`) 2-byte format, confirmed against real hardware. A
  Frontier unit (`0xBEEF`) reportedly uses a 3-byte millivolt-based format
  (`BatteryLevelCalculator`'s exact curve wasn't decompiled) — untested.
- `GetDSN` returned `UnknownMessage` on the test unit (legacy hardware) —
  possibly Frontier-only.
- There's a third GATT service (`8d53dc1d-1db7-4cd3-868b-8a527460aa84`) that
  showed up on live connection but wasn't predicted by static analysis and
  isn't used by this tool — purpose unknown, see `PROTOCOL.md`.
- Initial OS-level pairing/bonding (the one-time `CompanionDeviceManager`
  flow the Android app uses) is not implemented by this tool, and doesn't
  need to be: a plain BLE connect while the device is in discoverable/pairing
  mode is enough to get the *first* connection through, and **confirmed
  live**, macOS's own Bluetooth stack bonds with the device at that point and
  remembers it from then on — every later connect (including across
  restarting `bee-control-ui` or reconnecting after a disconnect) works with
  the device just advertising normally, no pairing mode needed again unless
  you "forget" it (see the callout above).
