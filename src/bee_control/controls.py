"""The control registry the UI (and anything else) renders from.

To add a new control as we discover more of the protocol: add one `Control`
entry below with a handler. The web UI picks it up automatically — no HTML/JS
changes needed. Keep `status` accurate: it's shown as a badge in the UI so
we don't forget which controls are actually confirmed against real hardware
(see PROTOCOL.md) versus just structurally implemented.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .client import BeeDevice

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRETS_PATH = REPO_ROOT / "device_secrets.json"


class Status(str, Enum):
    CONFIRMED = "confirmed"  # verified against real hardware to do what it says
    NO_EFFECT = "no_effect"  # ACKs but doesn't observably change device state
    UNTESTED = "untested"  # implemented, not yet tried against real hardware


Handler = Callable[[BeeDevice, Any], Awaitable[Any]]


@dataclass
class Control:
    id: str
    label: str
    group: str
    kind: str  # "button" | "slider" | "toggle"
    handler: Handler
    status: Status
    note: str = ""
    min: float | None = None
    max: float | None = None
    step: float | None = None
    default: float | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "group": self.group,
            "kind": self.kind,
            "status": self.status.value,
            "note": self.note,
            "min": self.min,
            "max": self.max,
            "step": self.step,
            "default": self.default,
        }


async def _refresh_status(device: BeeDevice, _value: Any) -> dict:
    state, codec = await device.get_device_state()
    brightness = await device.get_brightness()
    major, minor, patch, hw_rev = await device.get_firmware_version()
    tx = await device.get_tx_level()
    battery_pct, charging = await device.get_battery_and_charge_state()
    bitrate = await device.get_bitrate()
    return {
        "device_state": getattr(state, "name", state),
        "codec": getattr(codec, "name", codec),
        "brightness": brightness,
        "firmware": f"{major}.{minor}.{patch}" + (f" (hw rev {hw_rev})" if hw_rev is not None else ""),
        "tx_power_level": tx,
        "battery_pct": battery_pct,
        "charging": charging,
        "bitrate": bitrate,
    }


async def _ensure_encrypted_session(device: BeeDevice) -> None:
    """Mute/unmute (SetDeviceState MUTE/RECORDING) only sticks on real hardware
    once the device has a live encryption key + session nonce established in
    the current connection — confirmed in PROTOCOL.md's "long-standing
    mute/recording mystery is resolved" section; earlier testing that skipped
    this step got an ACK but GetDeviceState never actually changed. Sleep/
    power-off don't need this (confirmed working with zero key setup), so
    only the mute controls below call this. Re-sending the real captured
    token is confirmed safe to repeat within a connection.
    """
    secrets = json.loads(SECRETS_PATH.read_text())
    real_token = bytes.fromhex(secrets["sealed_token_hex"])
    await device.set_encryption_key(real_token)
    await device.get_session_nonce()


async def _set_mute(device: BeeDevice, mute: bool) -> dict:
    try:
        await _ensure_encrypted_session(device)
    except (OSError, KeyError, ValueError) as e:
        raise RuntimeError(
            f"couldn't establish encrypted session ({e}) — mute/recording state won't "
            'stick without one; see PROTOCOL.md\'s "Local-only operation" section'
        ) from e
    await device.set_mute(mute)
    state, _codec = await device.get_device_state()
    return {"device_state": getattr(state, "name", state)}


async def _get_info(device: BeeDevice, _value: Any) -> dict:
    out: dict[str, Any] = {}
    try:
        out["device_id"] = await device.get_device_id()
    except RuntimeError as e:
        out["device_id"] = f"<error: {e}>"
    try:
        out["dsn"] = await device.get_dsn()
    except RuntimeError as e:
        out["dsn"] = f"<error: {e}> (may be Frontier-hardware-only)"
    return out


CONTROLS: list[Control] = [
    Control(
        id="refresh_status",
        label="Refresh status",
        group="Status",
        kind="button",
        handler=_refresh_status,
        status=Status.CONFIRMED,
        note="Reads device state, brightness, firmware, TX power, battery, bitrate.",
    ),
    Control(
        id="get_info",
        label="Get device info",
        group="Status",
        kind="button",
        handler=_get_info,
        status=Status.CONFIRMED,
        note="Device ID confirmed; DSN returns UnknownMessage on legacy hardware.",
    ),
    Control(
        id="brightness",
        label="Brightness",
        group="Display",
        kind="slider",
        handler=lambda device, value: device.set_brightness(int(value)),
        status=Status.CONFIRMED,
        min=0,
        max=100,
        step=1,
        default=65,
        note="0-100 confirmed as the firmware-enforced range (101+ returns InvalidParam); the stock app's 35-100 UI clamp is stricter than firmware actually requires.",
    ),
    Control(
        id="power_off",
        label="Power off",
        group="Power",
        kind="button",
        handler=lambda device, _value: device.power_off(),
        status=Status.CONFIRMED,
        note="Confirmed: device disconnects and stops advertising entirely.",
    ),
    Control(
        id="sleep_hw_vad",
        label="Sleep (HW VAD wake)",
        group="Power",
        kind="button",
        handler=lambda device, _value: device.sleep_hw_vad(),
        status=Status.CONFIRMED,
    ),
    Control(
        id="sleep_sw_vad",
        label="Sleep (SW VAD wake)",
        group="Power",
        kind="button",
        handler=lambda device, _value: device.sleep_sw_vad(),
        status=Status.CONFIRMED,
    ),
    Control(
        id="mute_on",
        label="Mute on",
        group="Audio",
        kind="button",
        handler=lambda device, _value: _set_mute(device, True),
        status=Status.UNTESTED,
        note=(
            "Requires device_secrets.json: establishes a live encrypted session "
            "first, then mutes — a plain SetDeviceState write ACKs but doesn't "
            "stick without that session (see PROTOCOL.md). Confirmed live via a "
            "raw test script; not yet re-verified through this exact button."
        ),
    ),
    Control(
        id="mute_off",
        label="Mute off",
        group="Audio",
        kind="button",
        handler=lambda device, _value: _set_mute(device, False),
        status=Status.UNTESTED,
        note=(
            "Requires device_secrets.json: establishes a live encrypted session "
            "first, then unmutes — a plain SetDeviceState write ACKs but doesn't "
            "stick without that session (see PROTOCOL.md). Confirmed live via a "
            "raw test script; not yet re-verified through this exact button."
        ),
    ),
    Control(
        id="tx_power",
        label="TX power",
        group="Radio",
        kind="slider",
        handler=lambda device, value: device.set_tx_power(int(value)),
        status=Status.UNTESTED,
        min=-20,
        max=20,
        step=1,
        default=8,
    ),
    Control(
        id="bitrate",
        label="Audio bitrate",
        group="Audio",
        kind="slider",
        handler=lambda device, value: device.set_bitrate(int(value)),
        status=Status.UNTESTED,
        min=4000,
        max=64000,
        step=1000,
        default=32000,
    ),
]


CONTROLS_BY_ID: dict[str, Control] = {c.id: c for c in CONTROLS}
