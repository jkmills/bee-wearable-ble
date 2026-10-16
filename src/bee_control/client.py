"""Async BLE client for the Bee wearable's control characteristic."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from . import protocol as proto
from .audio import AdtsFrame, AdtsReassembler, decrypt_audio_packet

SCAN_TIMEOUT_S = 10.0
RESPONSE_TIMEOUT_S = 5.0


def _looks_like_bee(device: BLEDevice, adv) -> bool:
    if (device.name or adv.local_name) == proto.ADVERTISED_NAME:
        return True
    if proto.ADVERTISED_SERVICE_UUID.lower() in [u.lower() for u in adv.service_uuids]:
        return True
    return any(
        cid in (proto.LEGACY_COMPANY_ID, proto.FRONTIER_COMPANY_ID)
        for cid in (adv.manufacturer_data or {})
    )


async def scan(timeout: float = SCAN_TIMEOUT_S) -> list[BLEDevice]:
    """Scan for BLE devices that look like a Bee (by name, advertised UUID, or manufacturer ID)."""
    found: dict[str, BLEDevice] = {}

    def on_detect(device: BLEDevice, adv) -> None:
        if _looks_like_bee(device, adv):
            found[device.address] = device

    async with BleakScanner(on_detect):
        await asyncio.sleep(timeout)
    return list(found.values())


class BeeDevice:
    """Wraps a connected Bee device: send commands, await matching responses."""

    def __init__(self, client: BleakClient):
        self._client = client
        self._pending: dict[int, asyncio.Future[proto.Notification]] = {}
        self._on_event: Callable[[proto.Notification], None] | None = None
        self._on_disconnect: Callable[[], None] | None = None
        self._audio_key: bytes | None = None
        self._audio_nonce: bytes | None = None
        self._audio_reassembler: AdtsReassembler | None = None
        self._audio_subscribed = False

    @classmethod
    async def connect(
        cls,
        address_or_device: str | BLEDevice,
        on_disconnect: Callable[[], None] | None = None,
    ) -> "BeeDevice":
        self = cls.__new__(cls)
        self._pending = {}
        self._on_event = None
        self._on_disconnect = on_disconnect

        def _disconnected(_client: BleakClient) -> None:
            if self._on_disconnect is not None:
                self._on_disconnect()

        client = BleakClient(address_or_device, disconnected_callback=_disconnected)
        await client.connect()
        self._client = client
        await client.start_notify(proto.CONTROL_CHARACTERISTIC_UUID, self._on_notify)
        return self

    @property
    def is_connected(self) -> bool:
        return self._client.is_connected

    async def disconnect(self) -> None:
        await self._client.disconnect()

    def on_event(self, callback: Callable[[proto.Notification], None]) -> None:
        """Register a callback for unsolicited events (button press, battery, etc)."""
        self._on_event = callback

    def _on_notify(self, _handle: int, data: bytearray) -> None:
        note = proto.decode(bytes(data))
        if note is None:
            return
        if note.code == proto.OK and note.echoed_command is not None:
            fut = self._pending.pop(note.echoed_command, None)
            if fut is not None and not fut.done():
                fut.set_result(note)
                return
        if note.code == proto.SET_ENCRYPTION_KEY_RESPONSE:
            # SetEncryptionKeyResponse doesn't use the OK-wrapper echoed-command
            # convention other commands do — it's its own top-level response code.
            fut = self._pending.pop(proto.Cmd.SET_ENCRYPTION_KEY, None)
            if fut is not None and not fut.done():
                fut.set_result(note)
                return
        if note.code in (proto.ERROR, proto.INVALID_PARAM, proto.UNKNOWN_MESSAGE):
            # Can't always tell which pending request this answers; fail the oldest.
            if self._pending:
                cmd, fut = next(iter(self._pending.items()))
                if not fut.done():
                    fut.set_exception(RuntimeError(f"device returned {note.name}"))
                self._pending.pop(cmd, None)
            return
        if self._on_event is not None:
            self._on_event(note)

    async def _send(self, command: int, request_bytes: bytes, timeout: float = RESPONSE_TIMEOUT_S) -> proto.Notification:
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[proto.Notification] = loop.create_future()
        self._pending[command] = fut
        try:
            await self._client.write_gatt_char(
                proto.CONTROL_CHARACTERISTIC_UUID, request_bytes, response=True
            )
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(command, None)

    async def _send_fire_and_forget(self, request_bytes: bytes) -> None:
        await self._client.write_gatt_char(
            proto.CONTROL_CHARACTERISTIC_UUID, request_bytes, response=True
        )

    # --- high-level manual controls ---------------------------------------

    async def get_brightness(self) -> int:
        note = await self._send(proto.Cmd.GET_BRIGHTNESS, proto.req_get_brightness())
        return proto.decode_brightness(note.payload)

    async def set_brightness(self, value: int) -> None:
        note = await self._send(proto.Cmd.SET_BRIGHTNESS, proto.req_set_brightness(value))
        _ = note  # SetBrightness has no payload; success == response received

    async def get_device_state(self) -> tuple[proto.DeviceState, proto.CodecType]:
        note = await self._send(proto.Cmd.GET_DEVICE_STATE, proto.req_get_device_state())
        return proto.decode_device_state(note.payload)

    async def set_device_state(self, state: proto.DeviceState) -> bool:
        note = await self._send(proto.Cmd.SET_DEVICE_STATE, proto.req_set_device_state(state))
        return proto.decode_set_device_state_response(note.payload)

    async def power_off(self) -> None:
        await self.set_device_state(proto.DeviceState.POWER_OFF)

    async def sleep_hw_vad(self) -> None:
        await self.set_device_state(proto.DeviceState.SLEEP_HW_VAD)

    async def sleep_sw_vad(self) -> None:
        await self.set_device_state(proto.DeviceState.SLEEP_SW_VAD)

    async def set_mute(self, mute: bool) -> None:
        await self.set_device_state(proto.DeviceState.MUTE if mute else proto.DeviceState.RECORDING)

    async def get_firmware_version(self) -> tuple[int, int, int, int | None]:
        note = await self._send(proto.Cmd.GET_FIRMWARE_VERSION, proto.req_get_firmware_version())
        return proto.decode_firmware_version(note.payload)

    async def get_battery_and_charge_state(self) -> tuple[int, bool]:
        """Returns (percentage, charging). Legacy-hardware format only — see PROTOCOL.md
        if your unit is Frontier hardware (mfg company ID 0xBEEF rather than 0xBEEE)."""
        note = await self._send(
            proto.Cmd.GET_BATTERY_AND_CHARGE_STATE, proto.req_get_battery_and_charge_state()
        )
        return proto.decode_battery_and_charge_state(note.payload)

    async def get_tx_level(self) -> int:
        note = await self._send(proto.Cmd.GET_TX_LEVEL, proto.req_get_tx_level())
        return proto.decode_tx_level(note.payload)

    async def set_tx_power(self, power: int) -> None:
        await self._send(proto.Cmd.SET_TX_POWER, proto.req_set_tx_power(power))

    async def get_bitrate(self) -> int:
        note = await self._send(proto.Cmd.GET_BITRATE, proto.req_get_bitrate())
        return proto.decode_bitrate(note.payload)

    async def set_bitrate(self, bitrate: int) -> None:
        await self._send(proto.Cmd.SET_BITRATE, proto.req_set_bitrate(bitrate))

    async def get_device_id(self) -> str:
        note = await self._send(proto.Cmd.GET_DEVICE_ID, proto.req_get_device_id())
        return proto.decode_device_id(note.payload)

    async def get_dsn(self) -> str:
        note = await self._send(proto.Cmd.GET_DSN, proto.req_get_dsn())
        return proto.decode_dsn(note.payload)

    async def get_session_nonce(self) -> bytes:
        note = await self._send(proto.Cmd.GET_SESSION_NONCE, proto.req_get_session_nonce())
        return note.payload

    async def get_secure_ids(self) -> bytes:
        note = await self._send(proto.Cmd.GET_SECURE_IDS, proto.req_get_secure_ids())
        return note.payload

    async def set_encryption_key(self, token: bytes) -> bytes:
        """See PROTOCOL.md "Local-only operation" section. Sends `token` verbatim as the
        SetEncryptionKey payload and returns the raw response payload bytes (empty for a
        fresh/first key set via 0x8000; echoes the command id for a re-key via 0x8004).

        CONFIRMED (live testing): a bare self-generated key/random payload, with no prior
        context, is rejected (InvalidParam or Error depending on connection state) — the
        device validates *something* about the token, not just its length. CONFIRMED,
        separately: the real captured token's "SKV1" + 8-byte device-id + 80-byte trailer
        can be reused with an arbitrary self-generated 16-byte key spliced in at offset
        12-28, *as long as the real unmodified token is sent first in that same fresh
        connection* (see unlock_and_set_local_key). The trailer does not appear to
        cryptographically commit to the key value — only the first call in a connection
        needs to match the device's real current state."""
        note = await self._send(proto.Cmd.SET_ENCRYPTION_KEY, proto.req_set_encryption_key(token))
        return note.payload

    async def unlock_and_set_local_key(self, real_token: bytes, new_key: bytes | None = None) -> bytes:
        """CONFIRMED working end-to-end, including across a full disconnect/reconnect:
        sends `real_token` (the legitimately captured token, unmodified) first to unlock
        the connection, then splices `new_key` (or a fresh os.urandom(16) if not given)
        into the same magic/device-id/trailer and sends that as a re-key. Returns the
        16-byte key that is now active on the device. Requires a FRESH connection — this
        was not tested as working when the self-generated key is sent as the first
        SetEncryptionKey call of a connection (that was rejected in testing).

        `real_token` must be the real captured 108-byte token: see PROTOCOL.md's
        "Local-only operation" section and device_secrets.json's `sealed_token_hex`.
        """
        if len(real_token) < 28:
            raise ValueError("real_token too short to contain magic+device_id+key header")
        magic_and_device_id = real_token[0:12]
        trailer = real_token[28:]
        key = new_key if new_key is not None else os.urandom(16)
        if len(key) != 16:
            raise ValueError("new_key must be exactly 16 bytes (AES-128)")

        await self.set_encryption_key(real_token)
        spliced = magic_and_device_id + key + trailer
        await self.set_encryption_key(spliced)
        return key

    # --- audio capture (NOT yet confirmed against real audio traffic) -----

    async def start_audio_capture(
        self,
        key: bytes,
        session_nonce: bytes,
        on_frame: Callable[[AdtsFrame], None],
        on_raw_packet: Callable[[bytes, bytes | None, Exception | None], None] | None = None,
    ) -> None:
        """Subscribe to the audio characteristic and decrypt+reassemble ADTS/AAC frames
        from it, calling `on_frame` for each complete frame as it's reassembled.

        `key` must be the AES-128-CTR key currently active on the device (see
        unlock_and_set_local_key) and `session_nonce` must be from a GetSessionNonce
        call made in the SAME connection as this capture (the nonce changes every
        connection). NOT YET CONFIRMED against real audio — no capture so far has
        caught the device unmuted and streaming; see PROTOCOL.md.

        `on_raw_packet`, if given, is called for EVERY raw notification on the audio
        characteristic, before/regardless of frame reassembly: (raw_bytes, decrypted_bytes
        or None, exception or None). Use it to distinguish "no raw packets ever arrived"
        from "packets arrived but decrypt/framing failed."
        """
        if len(key) != 16:
            raise ValueError("key must be 16 bytes (AES-128)")
        if len(session_nonce) != 12:
            raise ValueError("session_nonce must be 12 bytes")
        self._audio_key = key
        self._audio_nonce = session_nonce
        self._audio_reassembler = AdtsReassembler()

        def _on_audio_notify(_handle: int, data: bytearray) -> None:
            assert self._audio_key is not None
            assert self._audio_nonce is not None
            assert self._audio_reassembler is not None
            raw = bytes(data)
            try:
                decrypted = decrypt_audio_packet(self._audio_key, self._audio_nonce, raw)
            except ValueError as e:
                if on_raw_packet is not None:
                    on_raw_packet(raw, None, e)
                return
            if on_raw_packet is not None:
                on_raw_packet(raw, decrypted, None)
            for frame in self._audio_reassembler.feed(decrypted):
                on_frame(frame)

        await self._client.start_notify(proto.AUDIO_CHARACTERISTIC_UUID, _on_audio_notify)
        self._audio_subscribed = True

    async def stop_audio_capture(self) -> None:
        if self._audio_subscribed:
            await self._client.stop_notify(proto.AUDIO_CHARACTERISTIC_UUID)
            self._audio_subscribed = False
        self._audio_key = None
        self._audio_nonce = None
        self._audio_reassembler = None
