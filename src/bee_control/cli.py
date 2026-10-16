"""Command-line entry point: `bee-control <command> [args]`.

Examples:
    bee-control scan
    bee-control --address AA:BB:CC:DD:EE:FF status
    bee-control --address AA:BB:CC:DD:EE:FF brightness 80
    bee-control --address AA:BB:CC:DD:EE:FF power-off
    bee-control --address AA:BB:CC:DD:EE:FF mute on
    bee-control --address AA:BB:CC:DD:EE:FF sleep hw-vad
    bee-control --address AA:BB:CC:DD:EE:FF watch
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import protocol as proto
from .client import BeeDevice, scan


async def _cmd_scan(_args: argparse.Namespace) -> None:
    print(f"Scanning for devices advertising service {proto.SERVICE_UUID}...")
    devices = await scan()
    if not devices:
        print("No Bee devices found. Make sure it's powered on and not already connected to another phone/app.")
        return
    for d in devices:
        print(f"  {d.address}  {d.name or '(no name)'}")


async def _with_device(args: argparse.Namespace, fn) -> None:
    if not args.address:
        print("error: --address is required for this command (run `bee-control scan` first)", file=sys.stderr)
        sys.exit(2)
    device = await BeeDevice.connect(args.address)
    try:
        await fn(device)
    finally:
        await device.disconnect()


async def _cmd_status(args: argparse.Namespace) -> None:
    async def run(d: BeeDevice) -> None:
        state, codec = await d.get_device_state()
        brightness = await d.get_brightness()
        major, minor, patch, hw_rev = await d.get_firmware_version()
        tx = await d.get_tx_level()
        battery_pct, charging = await d.get_battery_and_charge_state()
        bitrate = await d.get_bitrate()
        print(f"device state   : {state.name if hasattr(state, 'name') else state}")
        print(f"codec          : {codec.name}")
        print(f"brightness     : {brightness}")
        print(f"firmware       : {major}.{minor}.{patch}" + (f" (hw rev {hw_rev})" if hw_rev is not None else ""))
        print(f"tx power level : {tx}")
        print(f"battery        : {battery_pct}%" + (" (charging)" if charging else ""))
        print(f"bitrate        : {bitrate}")

    await _with_device(args, run)


async def _cmd_brightness(args: argparse.Namespace) -> None:
    async def run(d: BeeDevice) -> None:
        if args.value is None:
            print(await d.get_brightness())
        else:
            await d.set_brightness(args.value)
            print(f"brightness set to {args.value}")

    await _with_device(args, run)


async def _cmd_power_off(args: argparse.Namespace) -> None:
    async def run(d: BeeDevice) -> None:
        await d.power_off()
        print("power-off command sent")

    await _with_device(args, run)


async def _cmd_mute(args: argparse.Namespace) -> None:
    async def run(d: BeeDevice) -> None:
        mute = args.state == "on"
        await d.set_mute(mute)
        print(f"{'muted' if mute else 'unmuted'}")

    await _with_device(args, run)


async def _cmd_sleep(args: argparse.Namespace) -> None:
    async def run(d: BeeDevice) -> None:
        if args.mode == "hw-vad":
            await d.sleep_hw_vad()
        else:
            await d.sleep_sw_vad()
        print(f"sleep ({args.mode}) command sent")

    await _with_device(args, run)


async def _cmd_tx_power(args: argparse.Namespace) -> None:
    async def run(d: BeeDevice) -> None:
        if args.value is None:
            print(await d.get_tx_level())
        else:
            await d.set_tx_power(args.value)
            print(f"tx power set to {args.value}")

    await _with_device(args, run)


async def _cmd_bitrate(args: argparse.Namespace) -> None:
    async def run(d: BeeDevice) -> None:
        if args.value is None:
            print(await d.get_bitrate())
        else:
            await d.set_bitrate(args.value)
            print(f"bitrate set to {args.value}")

    await _with_device(args, run)


async def _cmd_info(args: argparse.Namespace) -> None:
    async def run(d: BeeDevice) -> None:
        try:
            print(f"device id : {await d.get_device_id()}")
        except RuntimeError as e:
            print(f"device id : <error: {e}>")
        try:
            print(f"dsn       : {await d.get_dsn()}")
        except RuntimeError as e:
            print(f"dsn       : <error: {e}> (DSN may be a Frontier-hardware-only command)")

    await _with_device(args, run)


async def _cmd_watch(args: argparse.Namespace) -> None:
    async def run(d: BeeDevice) -> None:
        def on_event(note: proto.Notification) -> None:
            print(f"[event] {note.name} payload={note.payload.hex()}", flush=True)

        d.on_event(on_event)
        if args.duration:
            print(f"Watching for {args.duration}s. Interact with the device now.", flush=True)
            await asyncio.sleep(args.duration)
            print("Done watching.", flush=True)
        else:
            print("Watching for button/battery/charging events. Ctrl-C to stop.", flush=True)
            try:
                while True:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass

    await _with_device(args, run)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bee-control", description=__doc__.split("\n\n")[0])
    p.add_argument("--address", help="BLE address of the paired Bee device")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("scan", help="scan for nearby Bee devices").set_defaults(fn=_cmd_scan)
    sub.add_parser("status", help="print device state, brightness, firmware, tx power").set_defaults(fn=_cmd_status)
    sub.add_parser("info", help="print device id and DSN").set_defaults(fn=_cmd_info)

    b = sub.add_parser("brightness", help="get/set brightness (0-255; stock app clamps to 35-100)")
    b.add_argument("value", type=int, nargs="?", help="omit to read current value")
    b.set_defaults(fn=_cmd_brightness)

    sub.add_parser("power-off", help="power off the device").set_defaults(fn=_cmd_power_off)

    m = sub.add_parser("mute", help="mute or unmute the microphone")
    m.add_argument("state", choices=["on", "off"])
    m.set_defaults(fn=_cmd_mute)

    s = sub.add_parser("sleep", help="put device to sleep")
    s.add_argument("mode", choices=["hw-vad", "sw-vad"], help="wake method: hardware or software VAD")
    s.set_defaults(fn=_cmd_sleep)

    t = sub.add_parser("tx-power", help="get/set BLE TX power")
    t.add_argument("value", type=int, nargs="?", help="omit to read current value")
    t.set_defaults(fn=_cmd_tx_power)

    br = sub.add_parser("bitrate", help="get/set audio bitrate")
    br.add_argument("value", type=int, nargs="?", help="omit to read current value")
    br.set_defaults(fn=_cmd_bitrate)

    w = sub.add_parser("watch", help="stay connected and print button/battery/charging events")
    w.add_argument("--duration", type=float, default=None, help="auto-stop after N seconds instead of running until Ctrl-C")
    w.set_defaults(fn=_cmd_watch)

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        asyncio.run(args.fn(args))
    except KeyboardInterrupt:
        pass
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
