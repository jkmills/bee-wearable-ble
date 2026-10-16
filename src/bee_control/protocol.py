"""Bee wearable GATT protocol primitives.

Reverse-engineered from com.bee.android v1.0.3 (base.apk) via static
decompilation. See ../../PROTOCOL.md for the full write-up and evidence.
Nothing here has been validated against real hardware traffic yet — treat
response parsing as best-effort until confirmed against a real device.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

SERVICE_UUID = "03d5d5c4-a86c-11ee-9d89-8f2089a49e7e"
CONTROL_CHARACTERISTIC_UUID = "05e1f93c-d8d0-5ed8-dd88-379e4c1a3e3e"
AUDIO_CHARACTERISTIC_UUID = "b189a505-a86c-11ee-a5fb-8f2089a49e7e"

# The device does NOT advertise SERVICE_UUID directly (confirmed against real
# hardware) — it advertises this short 16-bit UUID for discovery instead, plus
# manufacturer data under one of the two company IDs below. The 128-bit
# SERVICE_UUID only becomes visible after connecting and discovering services.
ADVERTISED_SERVICE_UUID = "0000d5c4-0000-1000-8000-00805f9b34fb"
ADVERTISED_NAME = "Bee"
LEGACY_COMPANY_ID = 0xBEEE
FRONTIER_COMPANY_ID = 0xBEEF


class DeviceState(IntEnum):
    MUTE = 0
    RECORDING = 1
    SLEEP_HW_VAD = 2
    POWER_OFF = 3
    SLEEP_SW_VAD = 4


class CodecType(IntEnum):
    OPUS = 0
    AAC = 1
    UNKNOWN = 255


# --- Command IDs (phone -> device), little-endian uint16 on the wire -----

class Cmd(IntEnum):
    SET_TX_POWER = 0xC001
    GET_TX_LEVEL = 0xC002
    GET_CHARGING_SPEED = 0xC004
    GET_FIRMWARE_VERSION = 0xC005
    SET_DEVICE_STATE = 0xC006
    SET_BITRATE = 0xC008
    GET_BITRATE = 0xC009
    GET_DEVICE_STATE = 0xC00A
    GET_DEVICE_ID = 0xC00E
    GET_BATTERY_AND_CHARGE_STATE = 0xC00F
    SET_BRIGHTNESS = 0xC016
    GET_BRIGHTNESS = 0xC017
    SET_ENCRYPTION_KEY = 0xC018
    GET_SESSION_NONCE = 0xC019
    GET_SECURE_IDS = 0xC01B
    GET_DSN = 0xC01D


# --- Outer response/event codes (device -> phone) ------------------------

OK = 0x8000
ERROR = 0x8001
INVALID_PARAM = 0x8002
UNKNOWN_MESSAGE = 0x8003
SET_ENCRYPTION_KEY_RESPONSE = 0x8004
EVENT_BUTTON_DOUBLE_PRESS = 0xA001
EVENT_BUTTON_HOLD = 0xA002
EVENT_DEVICE_STATE = 0xA003
EVENT_BATTERY_LEVEL = 0xA004
EVENT_CHARGING = 0xA005


def encode(command: int, payload: bytes = b"") -> bytes:
    """Build a control-characteristic write: 2-byte little-endian command id + payload."""
    return struct.pack("<H", command) + payload


# --- Request builders, mirroring BeeRequest subclasses -------------------

def req_get_tx_level() -> bytes:
    return encode(Cmd.GET_TX_LEVEL)


def req_set_tx_power(power: int) -> bytes:
    """power: signed byte, device TX power level."""
    return encode(Cmd.SET_TX_POWER, struct.pack("<b", power))


def req_get_firmware_version() -> bytes:
    return encode(Cmd.GET_FIRMWARE_VERSION)


def req_get_bitrate() -> bytes:
    return encode(Cmd.GET_BITRATE)


def req_set_bitrate(bitrate: int) -> bytes:
    return encode(Cmd.SET_BITRATE, struct.pack("<i", bitrate))


def req_get_device_state() -> bytes:
    return encode(Cmd.GET_DEVICE_STATE)


def req_set_device_state(state: DeviceState) -> bytes:
    return encode(Cmd.SET_DEVICE_STATE, bytes([int(state)]))


def req_get_device_id() -> bytes:
    return encode(Cmd.GET_DEVICE_ID)


def req_get_battery_and_charge_state() -> bytes:
    return encode(Cmd.GET_BATTERY_AND_CHARGE_STATE)


def req_get_brightness() -> bytes:
    # The app always sends a fixed probe byte `1` with this request.
    return encode(Cmd.GET_BRIGHTNESS, bytes([1]))


def req_set_brightness(brightness: int) -> bytes:
    """brightness: 0-100, confirmed against real hardware as the firmware-enforced range
    (101+ returns InvalidParam). The stock app's UI additionally clamps to [35, 100],
    but that's a stricter UX choice on top of firmware's real 0-100 limit, not the limit itself."""
    return encode(Cmd.SET_BRIGHTNESS, bytes([1, brightness & 0xFF]))


def req_get_dsn() -> bytes:
    return encode(Cmd.GET_DSN)


def req_get_session_nonce() -> bytes:
    return encode(Cmd.GET_SESSION_NONCE)


def req_get_secure_ids() -> bytes:
    return encode(Cmd.GET_SECURE_IDS)


def req_set_encryption_key(key: bytes) -> bytes:
    """key: the raw token bytes to send to the device. The stock app sends the
    full (possibly ~44-byte) token it got from Bee's backend/Block Store verbatim,
    not just the extracted 16-byte AES key — see PROTOCOL.md's "Local-only operation"
    section. For a self-generated key, a bare 16-byte AES-128 key is the natural
    thing to try first; whether the device requires a specific token format/length
    is unconfirmed."""
    return encode(Cmd.SET_ENCRYPTION_KEY, key)


# Convenience wrappers for the states that have named factories in the app.
def req_power_off() -> bytes:
    return req_set_device_state(DeviceState.POWER_OFF)


def req_sleep_hw_vad() -> bytes:
    return req_set_device_state(DeviceState.SLEEP_HW_VAD)


def req_sleep_sw_vad() -> bytes:
    return req_set_device_state(DeviceState.SLEEP_SW_VAD)


def req_set_mute(mute: bool) -> bytes:
    return req_set_device_state(DeviceState.MUTE if mute else DeviceState.RECORDING)


# --- Notification decoding -------------------------------------------------

@dataclass
class Notification:
    code: int
    echoed_command: int | None
    payload: bytes

    @property
    def name(self) -> str:
        names = {
            OK: "OK",
            ERROR: "Error",
            INVALID_PARAM: "InvalidParam",
            UNKNOWN_MESSAGE: "UnknownMessage",
            SET_ENCRYPTION_KEY_RESPONSE: "SetEncryptionKeyResponse",
            EVENT_BUTTON_DOUBLE_PRESS: "EventButtonDoublePress",
            EVENT_BUTTON_HOLD: "EventButtonHold",
            EVENT_DEVICE_STATE: "EventDeviceState",
            EVENT_BATTERY_LEVEL: "EventBatteryLevel",
            EVENT_CHARGING: "EventCharging",
        }
        return names.get(self.code, f"Unknown(0x{self.code:04x})")


def decode(data: bytes) -> Notification | None:
    if len(data) < 2:
        return None
    code = struct.unpack_from("<H", data, 0)[0]
    rest = data[2:]
    if code == OK and len(rest) >= 2:
        echoed = struct.unpack_from("<H", rest, 0)[0]
        return Notification(code, echoed, rest[2:])
    return Notification(code, None, rest)


def decode_firmware_version(payload: bytes) -> tuple[int, int, int, int | None]:
    """Returns (major, minor, patch, hardware_revision_or_none)."""
    if len(payload) < 3:
        raise ValueError("firmware version payload too short")
    major, minor, patch = payload[2], payload[1], payload[0]
    hw_rev = payload[4] if len(payload) >= 5 else None
    return major, minor, patch, hw_rev


def decode_device_state(payload: bytes) -> tuple[DeviceState, CodecType]:
    if len(payload) < 2:
        raise ValueError("device state payload too short")
    try:
        state = DeviceState(payload[0])
    except ValueError:
        state = payload[0]  # unknown, leave raw
    try:
        codec = CodecType(payload[1])
    except ValueError:
        codec = CodecType.UNKNOWN
    return state, codec


def decode_brightness(payload: bytes) -> int:
    if not payload:
        raise ValueError("brightness payload empty")
    return payload[0]


def decode_bitrate(payload: bytes) -> int:
    if len(payload) < 4:
        raise ValueError("bitrate payload too short")
    return struct.unpack_from("<i", payload, 0)[0]


def decode_tx_level(payload: bytes) -> int:
    if not payload:
        raise ValueError("tx level payload empty")
    return payload[0]


def decode_device_id(payload: bytes) -> str:
    """Matches BeeAdvertisement.parseDeviceId: two little-endian uint32s, byte-swapped,
    formatted as a 16-hex-digit string. Confirmed against a live device: this command's
    response payload is the same raw 8 bytes seen in the BLE advertisement manufacturer data.
    """
    if len(payload) < 8:
        raise ValueError("device id payload too short")
    hi = struct.unpack_from("<I", payload, 4)[0]
    lo = struct.unpack_from("<I", payload, 0)[0]
    return f"{hi:08X}{lo:08X}"


def decode_dsn(payload: bytes) -> str:
    trimmed = payload.rstrip(b"\x00\xff")
    return trimmed.decode("ascii", errors="replace")


def decode_battery_and_charge_state(payload: bytes) -> tuple[int, bool]:
    """Non-Frontier format only (confirmed against a legacy-hardware unit, mfg company
    ID 0xBEEE): data[0] = battery percentage, data[1] != 0 = charging. Frontier hardware
    (company ID 0xBEEF) reportedly uses a 3-byte millivolt-based format instead — not
    implemented here; see PROTOCOL.md.
    """
    if len(payload) < 2:
        raise ValueError("battery payload too short")
    return payload[0], payload[1] != 0


def decode_set_device_state_response(payload: bytes) -> bool:
    """Returns `muted` as reported by the stock app's own decoder (data[0] == 0)."""
    if not payload:
        raise ValueError("set-device-state response payload empty")
    return payload[0] == 0
