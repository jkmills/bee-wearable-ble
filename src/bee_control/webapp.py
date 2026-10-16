"""Local web UI for bee-control.

Run with `uv run bee-control-ui`, then open http://127.0.0.1:8765 in a browser.
Everything runs locally against your own machine's Bluetooth adapter — nothing
is exposed outside localhost.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import protocol as proto
from .audio import AdtsFrame
from .client import BeeDevice, scan
from .controls import CONTROLS, CONTROLS_BY_ID

STATIC_DIR = Path(__file__).parent / "static"
REPO_ROOT = Path(__file__).resolve().parents[2]
SECRETS_PATH = REPO_ROOT / "device_secrets.json"

app = FastAPI(title="bee-control")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort safety net: any endpoint that forgets to catch an error would
    otherwise surface to the browser as FastAPI's default {"detail": "..."} body,
    which the frontend's {ok, error} handling can't read -- showing up there as
    the unhelpful "Capture start failed: undefined". This guarantees every
    endpoint's failure path is at least visible in the UI log and the response
    shape the JS expects."""
    msg = str(exc) or repr(exc)
    await session.log_and_broadcast("error", f"{request.url.path}: {msg}")
    return JSONResponse({"ok": False, "error": msg}, status_code=500)


class Session:
    """Holds the single active device connection and broadcasts events to all
    connected browser tabs. One Bee, one connection at a time — matches how
    the real protocol works (one GATT link, serialized operation queue)."""

    def __init__(self) -> None:
        self.device: BeeDevice | None = None
        self.address: str | None = None
        self.lock = asyncio.Lock()
        self.sockets: set[WebSocket] = set()
        self.log: list[dict] = []
        self.capturing = False
        self.capture_aac_file: Any = None
        self.capture_raw_file: Any = None
        self.capture_frame_count = 0
        self.capture_output_path: str | None = None

    def _close_capture_files(self) -> None:
        if self.capture_aac_file is not None:
            self.capture_aac_file.close()
            self.capture_aac_file = None
        if self.capture_raw_file is not None:
            self.capture_raw_file.close()
            self.capture_raw_file = None

    def _append_log(self, level: str, message: str) -> dict:
        entry = {"ts": time.time(), "level": level, "message": message}
        self.log.append(entry)
        self.log = self.log[-200:]
        return entry

    async def broadcast(self, payload: dict) -> None:
        dead = []
        for ws in self.sockets:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.sockets.discard(ws)

    async def log_and_broadcast(self, level: str, message: str) -> None:
        entry = self._append_log(level, message)
        await self.broadcast({"type": "log", **entry})

    async def broadcast_connection_state(self) -> None:
        await self.broadcast(
            {"type": "connection", "connected": self.device is not None, "address": self.address}
        )

    async def broadcast_capture_state(self) -> None:
        await self.broadcast(
            {
                "type": "capture",
                "capturing": self.capturing,
                "path": self.capture_output_path,
                "frames": self.capture_frame_count,
            }
        )

    def on_event(self, note: proto.Notification) -> None:
        asyncio.create_task(
            self.log_and_broadcast("event", f"{note.name} payload={note.payload.hex()}")
        )
        # The device pushes this notification itself, in real time, whenever
        # its real recording state changes -- for a button press or any
        # other cause -- confirmed live (payload is [state, codec], same
        # layout as GetDeviceState's response). This is the authoritative
        # signal for exactly when to save a capture: as soon as the device
        # reports it's no longer RECORDING, stop and save (takes the
        # opportunity to finalize the file right when the device itself
        # stops streaming, not whenever we next happen to poll); as soon as
        # it reports RECORDING, start. No extra round-trip needed. This
        # supersedes reacting to the button event directly, since a button
        # press is just one of the things that can cause this notification
        # to fire -- reacting here instead catches every cause.
        if note.code == proto.EVENT_DEVICE_STATE:
            try:
                state, _codec = proto.decode_device_state(note.payload)
            except ValueError:
                return
            is_recording = state == proto.DeviceState.RECORDING
            if is_recording and not self.capturing:
                asyncio.create_task(_start_capture(trigger="device state event (RECORDING)"))
            elif not is_recording and self.capturing:
                state_name = getattr(state, "name", state)
                asyncio.create_task(_stop_capture(trigger=f"device state event (now {state_name})"))

    def on_disconnect(self) -> None:
        self.device = None
        was_capturing = self.capturing
        if was_capturing:
            self.capturing = False
            self._close_capture_files()
        asyncio.create_task(
            self.log_and_broadcast(
                "warn", "Device disconnected." + (" Capture stopped." if was_capturing else "")
            )
        )
        asyncio.create_task(self.broadcast_connection_state())
        if was_capturing:
            asyncio.create_task(self.broadcast_capture_state())


session = Session()


class ConnectRequest(BaseModel):
    address: str


class CommandRequest(BaseModel):
    value: Any | None = None


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/controls")
async def get_controls() -> list[dict]:
    return [c.to_dict() for c in CONTROLS]


@app.get("/api/state")
async def get_state() -> dict:
    return {
        "connected": session.device is not None,
        "address": session.address,
        "capturing": session.capturing,
        "capture_path": session.capture_output_path,
        "capture_frames": session.capture_frame_count,
        "log": session.log[-50:],
    }


@app.post("/api/scan")
async def api_scan() -> list[dict]:
    devices = await scan(timeout=8.0)
    return [{"address": d.address, "name": d.name} for d in devices]


@app.post("/api/connect")
async def api_connect(req: ConnectRequest) -> dict:
    async with session.lock:
        if session.device is not None:
            if session.capturing:
                try:
                    await session.device.stop_audio_capture()
                except Exception:
                    pass
                session.capturing = False
                session._close_capture_files()
            await session.device.disconnect()
            session.device = None
        try:
            device = await BeeDevice.connect(req.address, on_disconnect=session.on_disconnect)
        except Exception as e:
            await session.log_and_broadcast("error", f"Connect failed: {e}")
            return {"ok": False, "error": str(e)}
        device.on_event(session.on_event)
        session.device = device
        session.address = req.address
    await session.log_and_broadcast("info", f"Connected to {req.address}")
    await session.broadcast_connection_state()
    return {"ok": True}


@app.post("/api/disconnect")
async def api_disconnect() -> dict:
    was_capturing = False
    async with session.lock:
        if session.device is not None:
            if session.capturing:
                was_capturing = True
                try:
                    await session.device.stop_audio_capture()
                except Exception:
                    pass
                session.capturing = False
                session._close_capture_files()
            await session.device.disconnect()
            session.device = None
    await session.log_and_broadcast("info", "Disconnected")
    await session.broadcast_connection_state()
    if was_capturing:
        await session.broadcast_capture_state()
    return {"ok": True}


async def _start_capture(trigger: str = "UI Start button") -> dict:
    """Core start-capture logic, shared by the HTTP endpoint and the physical
    button press (see Session.on_event) -- the device's button and this UI's
    Start/Stop buttons are meant to be interchangeable triggers for the exact
    same action: a single press toggles capture, same as clicking Start then
    Stop."""
    async with session.lock:
        device = session.device
        if device is None:
            return {"ok": False, "error": "not connected"}
        if session.capturing:
            return {"ok": False, "error": "capture already running"}
        try:
            secrets = json.loads(SECRETS_PATH.read_text())
            real_token = bytes.fromhex(secrets["sealed_token_hex"])
            real_key = bytes.fromhex(secrets["aes_ctr_key_hex"])
        except (OSError, KeyError, ValueError) as e:
            msg = f"couldn't load {SECRETS_PATH.name}: {e} (see PROTOCOL.md's \"Local-only operation\" section)"
            await session.log_and_broadcast("error", f"Capture start failed: {msg}")
            return {"ok": False, "error": msg}

        aac_path: Path | None = None
        raw_path: Path | None = None
        try:
            await device.set_encryption_key(real_token)
            nonce = await device.get_session_nonce()
            try:
                await device.set_device_state(proto.DeviceState.RECORDING)
            except RuntimeError:
                pass  # best-effort unmute -- capture still proceeds either way, see PROTOCOL.md

            stamp = time.strftime("%Y%m%d-%H%M%S")
            aac_path = REPO_ROOT / f"captured_audio_{stamp}.aac"
            raw_path = REPO_ROOT / f"captured_audio_{stamp}_raw_decrypted.bin"
            session.capture_aac_file = open(aac_path, "wb")
            session.capture_raw_file = open(raw_path, "wb")
            session.capture_frame_count = 0
            session.capture_output_path = str(aac_path)

            def on_frame(frame: AdtsFrame) -> None:
                session.capture_frame_count += 1
                session.capture_aac_file.write(frame.data)
                session.capture_aac_file.flush()
                if session.capture_frame_count <= 3 or session.capture_frame_count % 20 == 0:
                    asyncio.create_task(
                        session.log_and_broadcast(
                            "event", f"capture: frame {session.capture_frame_count} ({len(frame.data)} bytes)"
                        )
                    )
                    asyncio.create_task(session.broadcast_capture_state())

            def on_raw_packet(_raw: bytes, decrypted: bytes | None, err: Exception | None) -> None:
                if decrypted is not None:
                    session.capture_raw_file.write(decrypted)
                    session.capture_raw_file.flush()
                if err is not None:
                    asyncio.create_task(session.log_and_broadcast("warn", f"capture: decrypt error: {err}"))

            await device.start_audio_capture(real_key, nonce, on_frame, on_raw_packet=on_raw_packet)
            session.capturing = True
        except Exception as e:
            # Nothing was captured if setup failed partway -- don't leave empty
            # placeholder files behind on disk.
            if session.capture_frame_count == 0:
                for p in (aac_path, raw_path):
                    if p is not None:
                        p.unlink(missing_ok=True)
            session._close_capture_files()
            session.capture_output_path = None
            msg = str(e) or repr(e)
            await session.log_and_broadcast("error", f"Capture start failed: {msg}")
            return {"ok": False, "error": msg}

    await session.log_and_broadcast(
        "info", f"Capture started ({trigger}) -- streaming to {aac_path.name}."
    )
    await session.broadcast_capture_state()
    return {"ok": True, "path": str(aac_path)}


async def _stop_capture(trigger: str = "UI Stop button") -> dict:
    """Core stop-capture logic -- see _start_capture's docstring."""
    async with session.lock:
        if not session.capturing:
            return {"ok": False, "error": "not capturing"}
        device = session.device
        if device is not None:
            try:
                await device.stop_audio_capture()
            except Exception as e:
                await session.log_and_broadcast("warn", f"stop_audio_capture error: {e}")
        frame_count = session.capture_frame_count
        path = session.capture_output_path
        session._close_capture_files()
        session.capturing = False

    await session.log_and_broadcast("info", f"Capture stopped ({trigger}): {frame_count} frames saved to {path}.")
    await session.broadcast_capture_state()
    return {"ok": True, "frames": frame_count, "path": path}


@app.post("/api/capture/start")
async def api_capture_start() -> dict:
    return await _start_capture()


@app.post("/api/capture/stop")
async def api_capture_stop() -> dict:
    return await _stop_capture()


@app.post("/api/command/{control_id}")
async def run_command(control_id: str, req: CommandRequest) -> dict:
    control = CONTROLS_BY_ID.get(control_id)
    if control is None:
        return {"ok": False, "error": f"unknown control {control_id!r}"}
    async with session.lock:
        if session.device is None:
            return {"ok": False, "error": "not connected"}
        try:
            result = await control.handler(session.device, req.value)
        except Exception as e:
            await session.log_and_broadcast("error", f"{control.label} failed: {e}")
            return {"ok": False, "error": str(e)}
    if isinstance(result, dict):
        msg = f"{control.label}: " + ", ".join(f"{k}={v}" for k, v in result.items())
    elif result is None:
        msg = f"{control.label}: done"
    else:
        msg = f"{control.label}: {result}"
    await session.log_and_broadcast("info", msg)
    return {"ok": True, "result": result}


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    session.sockets.add(websocket)
    try:
        await websocket.send_json(
            {"type": "connection", "connected": session.device is not None, "address": session.address}
        )
        await websocket.send_json(
            {
                "type": "capture",
                "capturing": session.capturing,
                "path": session.capture_output_path,
                "frames": session.capture_frame_count,
            }
        )
        while True:
            # We don't expect incoming messages, but need to await something
            # to detect disconnects.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        session.sockets.discard(websocket)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def run() -> None:
    import uvicorn

    uvicorn.run("bee_control.webapp:app", host="127.0.0.1", port=8765, reload=False)


if __name__ == "__main__":
    run()
