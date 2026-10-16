"""Live audio capture: connect, establish an encryption key, subscribe to the
audio characteristic, decrypt + reassemble ADTS/AAC frames in real time, and
save them to a playable .aac file.

Runs **continuously by default** -- there's no fixed capture window; it
streams for as long as the device sends audio and stops when you press
Ctrl-C. This matches how the real app works: it has no client-side "record
for N seconds" timer either. It just stays connected with the audio
characteristic subscribed, and packets flow whenever the device itself
decides to send them (unmuted + voice detected), for as long as the BLE
connection is open -- see PROTOCOL.md's "App architecture: audio capture,
streaming, and transcription" section. Pass a number of seconds as the first
argument if you want the old fixed-duration behavior instead.

Frames (and the raw decrypted byte stream) are written to disk as they
arrive, not buffered until the end -- so a long-running/continuous capture
that's interrupted still keeps everything captured up to that point.

DIAGNOSTIC MODE (--real-key, now the default): uses the REAL captured key
unmodified (no splice) rather than a self-generated one, to isolate whether
a previous run's garbled audio was caused by self-generated keys not actually
propagating to the device's real audio-encryption engine (control-plane
commands ACK regardless of key correctness, so their success doesn't prove
audio crypto got the same key -- see PROTOCOL.md). Pass --self-key to go back
to pushing a fresh self-generated key instead, once --real-key is confirmed
working.

Usage:
    cd bee-wearable-ble  # this repo's directory
    uv run python3 capture_audio.py [duration_seconds] [--self-key]

    # Capture indefinitely, Ctrl-C to stop and save:
    uv run python3 capture_audio.py

Before running:
  1. Put the device in pairing/discoverable mode (same as before).
  2. If your Mac has a stale Bluetooth record for it, forget it first in
     System Settings -> Bluetooth.
  3. Be ready to speak to / interact with the device once the script says
     "capturing" -- also try the device's own wake/button gesture if nothing
     happens in the first few seconds.

Output: captured_audio.aac in the current directory (raw ADTS stream,
playable with ffplay/VLC/etc), plus captured_audio_raw_decrypted.bin (the
full undivided decrypted byte stream, for offline verification independent
of the online frame-sync scanner). Both gitignored -- your own voice.
"""

from __future__ import annotations

import asyncio
import json
import sys

sys.path.insert(0, "src")

from bee_control import protocol as proto  # noqa: E402
from bee_control.audio import AdtsFrame, decode_sample_rate  # noqa: E402
from bee_control.client import BeeDevice  # noqa: E402

SECRETS_PATH = "device_secrets.json"
KNOWN_ADDRESS = "B8BB598D-AE32-CBF5-7B25-4FAE8312B1B2"
OUTPUT_PATH = "captured_audio.aac"


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    use_self_key = "--self-key" in sys.argv[1:]
    duration = float(args[0]) if args else None  # None = run until Ctrl-C

    with open(SECRETS_PATH) as f:
        secrets = json.load(f)
    real_token = bytes.fromhex(secrets["sealed_token_hex"])
    real_key = bytes.fromhex(secrets["aes_ctr_key_hex"])

    print(f"Connecting to {KNOWN_ADDRESS}...")
    device = await BeeDevice.connect(KNOWN_ADDRESS)
    try:
        if use_self_key:
            print("Establishing SELF-GENERATED local encryption key (unlock + re-key)...")
            key = await device.unlock_and_set_local_key(real_token)
        else:
            print("Establishing REAL captured encryption key (diagnostic mode, no splice)...")
            await device.set_encryption_key(real_token)
            key = real_key
        print(f"Active key: {key.hex()}")

        nonce = await device.get_session_nonce()
        print(f"Session nonce: {nonce.hex()}")

        state, codec = await device.get_device_state()
        print(f"Device state before unmute attempt: state={state} codec={codec}")

        print("Attempting to unmute (SetDeviceState(RECORDING))...")
        try:
            await device.set_device_state(proto.DeviceState.RECORDING)
        except RuntimeError as e:
            print(f"  (unmute command failed: {e} -- continuing anyway)")

        state, codec = await device.get_device_state()
        print(f"Device state after unmute attempt: state={state} codec={codec}")
        if state != proto.DeviceState.RECORDING:
            print(
                "  NOTE: device still doesn't report RECORDING -- this matches prior "
                "testing where mute/unmute writes didn't stick. Try the device's own "
                "button/wake gesture during the capture window below as a fallback."
            )

        frame_count = 0
        total_frame_bytes = 0
        raw_packet_count = 0
        raw_error_count = 0
        raw_decrypted_path = "captured_audio_raw_decrypted.bin"

        # Written incrementally as data arrives (not buffered to the end) --
        # the whole point of a continuous, no-fixed-duration capture is that
        # it can be stopped at any time without losing what came before.
        aac_file = open(OUTPUT_PATH, "wb")
        raw_file = open(raw_decrypted_path, "wb")

        def on_frame(frame: AdtsFrame) -> None:
            nonlocal frame_count, total_frame_bytes
            frame_count += 1
            total_frame_bytes += len(frame.data)
            aac_file.write(frame.data)
            aac_file.flush()
            sr = decode_sample_rate(frame)
            print(f"  [frame {frame_count}] {len(frame.data)} bytes, sample_rate={sr}Hz")

        def on_raw_packet(raw: bytes, decrypted: bytes | None, err: Exception | None) -> None:
            nonlocal raw_packet_count, raw_error_count
            raw_packet_count += 1
            if decrypted is not None:
                raw_file.write(decrypted)
                raw_file.flush()
            if err is not None:
                raw_error_count += 1
                print(f"  [raw #{raw_packet_count}] {len(raw)} bytes, decrypt error: {err}")
            elif raw_packet_count <= 3 or raw_packet_count % 10 == 0:
                print(f"  [raw #{raw_packet_count}] {len(raw)} bytes -> decrypted {len(decrypted or b'')} bytes: {(decrypted or b'')[:8].hex()}...")

        try:
            print()
            if duration is None:
                print("Starting audio capture -- SPEAK TO THE DEVICE NOW")
                print("Capturing continuously; press Ctrl-C to stop and save.")
            else:
                print(f"Starting audio capture for {duration:.0f}s -- SPEAK TO THE DEVICE NOW")
            print("(also try its button/wake gesture if nothing happens in the first few seconds)")
            await device.start_audio_capture(key, nonce, on_frame, on_raw_packet=on_raw_packet)

            elapsed = 0.0
            try:
                while duration is None or elapsed < duration:
                    await asyncio.sleep(1.0)
                    elapsed += 1.0
                    if int(elapsed) % 5 == 0:
                        print(f"  ...{int(elapsed)}s elapsed, {frame_count} frames so far")
            except asyncio.CancelledError:
                print("\nCtrl-C received -- stopping capture and saving what was recorded...")

            await device.stop_audio_capture()
        finally:
            aac_file.close()
            raw_file.close()

        print()
        print(f"Capture complete: {frame_count} ADTS frames decoded, {raw_packet_count} raw packets total.")
        print(f"Wrote full undivided decrypted byte stream to {raw_decrypted_path} for offline verification.")

        if frame_count:
            print(f"Wrote {OUTPUT_PATH} ({total_frame_bytes} bytes).")
            print(f"Try playing it: ffplay {OUTPUT_PATH}   (or open in VLC)")
            print()
            key_desc = "a self-generated" if use_self_key else "the REAL captured"
            print(f"=> SUCCESS: real audio decrypted locally with {key_desc} key.")
            if use_self_key:
                print("   This is full end-to-end confirmation of local-only operation.")
            else:
                print("   This confirms the decrypt/framing pipeline itself is correct.")
                print("   Next: try --self-key to see if a self-generated key also works for audio.")

            print()
            print("Transcribing locally (faster-whisper, first run downloads the model)...")
            from bee_control.transcribe import format_transcript, transcribe_file

            segments = transcribe_file(OUTPUT_PATH)
            if segments:
                transcript_text = format_transcript(segments)
                print(transcript_text)
                with open("captured_audio_transcript.txt", "w") as f:
                    f.write(transcript_text + "\n")
                print("Wrote captured_audio_transcript.txt")
            else:
                print("(no speech detected by the transcription model)")
        else:
            print("No audio frames were decoded during the capture window.")
            if not raw_packet_count:
                print(
                    "Zero raw notifications arrived on the audio characteristic at all -- "
                    "the device never actually streamed anything, regardless of decrypt/framing."
                )
            else:
                print(
                    f"{raw_packet_count} raw notifications DID arrive ({raw_error_count} failed to decrypt) "
                    "but never assembled into a complete ADTS frame -- decrypt succeeded but framing "
                    "logic likely needs adjustment. Report this back with the raw/decrypted byte counts above."
                )
    finally:
        await device.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
