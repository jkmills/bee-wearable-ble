"""Audio-characteristic decryption and ADTS/AAC frame reassembly.

Reverse-engineered from com.bee.android's PacketDecryptor and
AudioMessageDecoder (see PROTOCOL.md's "Local-only operation" section).
CONFIRMED against real hardware: the AES-128-CTR key exchange and session
nonce retrieval work end-to-end (see client.BeeDevice.unlock_and_set_local_key).
NOT YET CONFIRMED: actually decrypting a real captured audio packet — no
capture so far has caught the device unmuted and streaming. Treat this
module's correctness as derived-but-unverified until tested against real
audio traffic.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ADTS_SYNCWORD_MASK = 0xFFF0
ADTS_SYNCWORD = 0xFFF0


def decrypt_audio_packet(key: bytes, session_nonce: bytes, packet: bytes) -> bytes:
    """Decrypt one raw audio-characteristic notification.

    Wire format (from PacketDecryptor.decryptPacket): the first 4 bytes are a
    big-endian sequence number, the rest is AES-128-CTR ciphertext. The 16-byte
    CTR IV is session_nonce(12 bytes) || seq(4 bytes, big-endian). No auth tag —
    confidentiality only.
    """
    if len(key) != 16:
        raise ValueError("key must be 16 bytes (AES-128)")
    if len(session_nonce) != 12:
        raise ValueError("session_nonce must be 12 bytes")
    if len(packet) < 4:
        raise ValueError("packet too short to contain a sequence number")

    seq = packet[0:4]  # already big-endian on the wire; used verbatim in the IV
    ciphertext = packet[4:]
    iv = session_nonce + seq

    cipher = Cipher(algorithms.AES(key), modes.CTR(iv))
    decryptor = cipher.decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()


@dataclass
class AdtsFrame:
    data: bytes  # complete frame, header included

    @property
    def profile(self) -> int:
        return (self.data[2] >> 6) & 0b11

    @property
    def sample_rate_index(self) -> int:
        return (self.data[2] >> 2) & 0b1111

    @property
    def channel_config(self) -> int:
        return ((self.data[2] & 0b1) << 2) | ((self.data[3] >> 6) & 0b11)


ADTS_SAMPLE_RATES = [
    96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050,
    16000, 12000, 11025, 8000, 7350, 0, 0, 0,
]


class AdtsReassembler:
    """Buffers decrypted bytes and yields complete ADTS frames as they arrive.

    Scans for the standard 0xFFF ADTS sync word and the 13-bit aac_frame_length
    field, matching AudioMessageDecoder's behavior (see PROTOCOL.md).
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        # AudioMessageDecoder discards the first 3 successfully-parsed frames
        # unconditionally (an undocumented warmup period) — matched here for
        # byte-exact parity with the real app, per REA verification.
        self._warmup_remaining = 3

    def feed(self, decrypted: bytes) -> list[AdtsFrame]:
        self._buf.extend(decrypted)
        frames: list[AdtsFrame] = []
        while True:
            frame = self._try_extract_one()
            if frame is None:
                break
            if self._warmup_remaining > 0:
                self._warmup_remaining -= 1
                continue
            frames.append(frame)
        return frames

    def _try_extract_one(self) -> AdtsFrame | None:
        buf = self._buf
        # Find sync word: 0xFF followed by top nibble 0xF.
        i = 0
        n = len(buf)
        while i + 1 < n:
            if buf[i] == 0xFF and (buf[i + 1] & 0xF0) == 0xF0:
                break
            i += 1
        else:
            # No sync word found at all; drop everything except a possible
            # trailing 0xFF that might start a frame once more bytes arrive.
            if n > 0 and buf[-1] == 0xFF:
                del buf[: n - 1]
            else:
                buf.clear()
            return None

        if i > 0:
            del buf[:i]
            n = len(buf)

        if n < 7:
            return None  # need the full fixed ADTS header to read frame length

        # ADTS fixed header is 7 bytes (no CRC) or 9 bytes (with CRC, protection
        # absent bit == 0). Bit layout for frame length spans bytes 3-5.
        protection_absent = buf[1] & 0x01
        header_len = 7 if protection_absent else 9
        if n < header_len:
            return None

        frame_length = ((buf[3] & 0x03) << 11) | (buf[4] << 3) | ((buf[5] >> 5) & 0x07)
        if frame_length <= 7:
            # Matches AudioMessageDecoder: on a too-small frame_length, wait for
            # more data rather than skipping a byte and resyncing — a corrupt
            # sync match at this exact position might become valid once more
            # bytes arrive (e.g. if we're mid-header). Verified against REA
            # decompile; previously this skipped 1 byte and retried instead.
            return None
        if n < frame_length:
            return None  # wait for more data

        frame_bytes = bytes(buf[:frame_length])
        del buf[:frame_length]
        return AdtsFrame(frame_bytes)


def decode_sample_rate(frame: AdtsFrame) -> int:
    idx = frame.sample_rate_index
    if 0 <= idx < len(ADTS_SAMPLE_RATES):
        return ADTS_SAMPLE_RATES[idx]
    return 0
