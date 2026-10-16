"""Unofficial manual-control CLI for the Bee wearable's BLE control protocol.

See PROTOCOL.md at the repo root for the reverse-engineered wire format this
is built on. Use at your own risk against your own hardware.
"""

from .cli import main

__all__ = ["main"]
