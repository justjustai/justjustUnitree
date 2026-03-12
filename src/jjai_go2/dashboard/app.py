"""FastAPI dashboard for Unitree Go2 — real-time control + monitoring.

Serves at /unitreego2 with:
- Live robot status (SSE)
- Command buttons (WebSocket)
- Camera feed (MJPEG)
- Voice transcript log
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from ..core import EventBus, ServiceRegistry
from ..core.types import CommandResult, RobotState, VoiceEvent
from ..robot.client import Go2Robot

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="JJAI Go2 Dashboard", docs_url="/unitreego2/docs")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

if STATIC_DIR.exists():
    app.mount("/unitreego2/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# These get set by the main entry point before starting uvicorn
_bus: EventBus | None = None
_registry: ServiceRegistry | None = None
_robot: Go2Robot | None = None
_voice_log: list[dict] = []  # Circular buffer of voice events
_MAX_VOICE_LOG = 100

# Parkour subprocess state
_parkour_proc: subprocess.Popen | None = None
_PARKOUR_VENV_PYTHON = "/home/unitree/parkour_venv/bin/python3"
_PARKOUR_SCRIPT = "/home/unitree/extreme_parkour/run_parkour_dds.py"
_PARKOUR_LOGDIR = "/home/unitree/extreme_parkour/traced"
_PARKOUR_STATUS_FILE = "/tmp/parkour_status.json"


def init_dashboard(bus: EventBus, registry: ServiceRegistry) -> None:
    """Wire up the dashboard to the running system."""
    global _bus, _registry, _robot
    _bus = bus
    _registry = registry
    _robot = registry.get("go2_robot", Go2Robot) if registry.has("go2_robot") else None


# --- Pages ---

@app.get("/unitreego2", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    """Main dashboard page."""
    return templates.TemplateResponse("unitreego2.html", {"request": request})


# --- API ---

@app.get("/unitreego2/api/status")
async def get_status():
    """Current robot state as JSON."""
    if _robot:
        state = _robot.get_state()
        return {
            "connected": state.connected,
            "battery": round(state.battery_percent, 1),
            "posture": state.posture.value,
            "position": {"x": state.position[0], "y": state.position[1], "yaw": state.position[2]},
            "velocity": {"vx": state.velocity[0], "vy": state.velocity[1], "vyaw": state.velocity[2]},
            "cpu_temp": round(state.cpu_temp, 1),
            "timestamp": state.timestamp,
        }
    return {"connected": False, "error": "Robot not initialized"}


@app.post("/unitreego2/api/command")
async def send_command(request: Request):
    """Execute a robot command. Body: {"command": "stand_up", "args": {}}"""
    if not _robot:
        return {"success": False, "error": "Robot not connected"}

    body = await request.json()
    cmd = body.get("command", "")
    args = body.get("args", {})

    t0 = time.time()
    try:
        result = await _execute_command(cmd, args)
        duration_ms = (time.time() - t0) * 1000
        return {"success": True, "result": result, "duration_ms": round(duration_ms, 1)}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/unitreego2/api/voice-log")
async def get_voice_log():
    """Recent voice transcript entries."""
    return {"entries": _voice_log[-50:]}


# --- Parkour API ---

@app.post("/unitreego2/api/parkour/start")
async def parkour_start(request: Request):
    """Start parkour subprocess."""
    global _parkour_proc

    if _parkour_proc and _parkour_proc.poll() is None:
        return {"success": False, "error": "Parkour already running"}

    body = await request.json()
    mode = body.get("mode", "parkour")
    dryrun = body.get("dryrun", True)
    camera_rotation = body.get("camera_rotation", 0)

    cmd = [
        _PARKOUR_VENV_PYTHON,
        _PARKOUR_SCRIPT,
        "--logdir", _PARKOUR_LOGDIR,
        "--mode", mode,
        "--camera-rotation", str(camera_rotation),
        "--status-file", _PARKOUR_STATUS_FILE,
    ]
    if not dryrun:
        cmd.append("--nodryrun")

    env = {
        **os.environ,
        "LD_LIBRARY_PATH": "/home/unitree/cyclonedds_ws/install/cyclonedds/lib",
    }

    try:
        _parkour_proc = subprocess.Popen(
            cmd, env=env,
            stdout=open("/tmp/parkour_stdout.log", "w"),
            stderr=subprocess.STDOUT,
        )
        logger.info("Parkour started: PID=%d, mode=%s, dryrun=%s", _parkour_proc.pid, mode, dryrun)
        return {"success": True, "pid": _parkour_proc.pid}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/unitreego2/api/parkour/stop")
async def parkour_stop():
    """Stop parkour subprocess."""
    global _parkour_proc

    if not _parkour_proc or _parkour_proc.poll() is not None:
        _parkour_proc = None
        return {"success": True, "message": "Not running"}

    pid = _parkour_proc.pid
    _parkour_proc.send_signal(signal.SIGTERM)

    # Wait up to 3s for graceful shutdown
    for _ in range(30):
        if _parkour_proc.poll() is not None:
            break
        await asyncio.sleep(0.1)

    if _parkour_proc.poll() is None:
        _parkour_proc.kill()
        logger.warning("Parkour PID %d force-killed", pid)

    _parkour_proc = None
    return {"success": True, "pid": pid}


@app.get("/unitreego2/api/parkour/status")
async def parkour_status():
    """Read parkour status from JSON file."""
    try:
        with open(_PARKOUR_STATUS_FILE, "r") as f:
            status = json.load(f)
        age = time.time() - status.get("timestamp", 0)
        status["stale"] = age > 3
        return status
    except FileNotFoundError:
        return {"state": "idle", "stale": True}
    except Exception as e:
        return {"state": "error", "error": str(e), "stale": True}


# --- SSE: Real-time status stream ---

@app.get("/unitreego2/api/stream/status")
async def stream_status():
    """Server-Sent Events stream of robot status. HTMX connects to this."""
    return StreamingResponse(
        _status_sse_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _status_sse_generator() -> AsyncGenerator[str, None]:
    """Yield SSE events with robot status every second."""
    while True:
        if _robot:
            state = _robot.get_state()
            data = json.dumps({
                "connected": state.connected,
                "battery": round(state.battery_percent, 1),
                "posture": state.posture.value,
                "cpu_temp": round(state.cpu_temp, 1),
                "timestamp": round(state.timestamp, 1),
            })
        else:
            data = json.dumps({"connected": False})

        yield f"data: {data}\n\n"
        await asyncio.sleep(1.0)


# --- SSE: Voice transcript stream ---

@app.get("/unitreego2/api/stream/voice")
async def stream_voice():
    """SSE stream of voice transcript events."""
    return StreamingResponse(
        _voice_sse_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _voice_sse_generator() -> AsyncGenerator[str, None]:
    """Yield SSE events when new voice transcripts arrive."""
    if not _bus:
        return

    q = _bus.subscribe("voice/transcript", maxsize=50)
    try:
        while True:
            try:
                event: VoiceEvent = await asyncio.wait_for(q.get(), timeout=30.0)
                entry = {
                    "role": event.role,
                    "text": event.text,
                    "tool": event.tool_call,
                    "time": time.strftime("%H:%M:%S", time.localtime(event.timestamp)),
                }
                _voice_log.append(entry)
                if len(_voice_log) > _MAX_VOICE_LOG:
                    _voice_log.pop(0)
                yield f"data: {json.dumps(entry)}\n\n"
            except asyncio.TimeoutError:
                # Keepalive
                yield ": keepalive\n\n"
    finally:
        if _bus:
            _bus.unsubscribe("voice/transcript", q)


# --- Camera MJPEG stream ---

@app.get("/unitreego2/api/camera")
async def camera_stream():
    """MJPEG camera stream for <img> tag."""
    return StreamingResponse(
        _mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


async def _mjpeg_generator() -> AsyncGenerator[bytes, None]:
    """Yield MJPEG frames from Go2 camera."""
    while True:
        if _robot:
            frame = await _robot.get_frame()
            if frame is not None:
                from ..robot.camera import frame_to_jpeg
                jpeg = frame_to_jpeg(frame, quality=70)
                if jpeg:
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
        await asyncio.sleep(1 / 10)  # 10 FPS max for dashboard


# --- WebSocket: Bidirectional control ---

@app.websocket("/unitreego2/ws")
async def websocket_endpoint(ws: WebSocket):
    """WebSocket for real-time commands + status push."""
    await ws.accept()
    logger.info("Dashboard WebSocket connected")

    try:
        while True:
            data = await ws.receive_json()
            cmd = data.get("command", "")
            args = data.get("args", {})

            try:
                result = await _execute_command(cmd, args)
                await ws.send_json({"type": "result", "command": cmd, "result": result})
            except Exception as e:
                await ws.send_json({"type": "error", "command": cmd, "error": str(e)})
    except WebSocketDisconnect:
        logger.info("Dashboard WebSocket disconnected")


# --- Command Dispatch ---

async def _execute_command(cmd: str, args: dict) -> dict:
    """Dispatch a command to the robot."""
    if not _robot:
        return {"error": "Robot not connected"}

    from ..robot import motion, posture, tricks

    commands = {
        # Movement
        "move_forward": lambda: motion.move_forward(_robot, **args),
        "move_backward": lambda: motion.move_backward(_robot, **args),
        "move_left": lambda: motion.move_left(_robot, **args),
        "move_right": lambda: motion.move_right(_robot, **args),
        "rotate": lambda: motion.rotate(_robot, **args),
        "stop": lambda: _robot.stop_move(),
        # Posture
        "stand_up": lambda: posture.stand_up(_robot),
        "sit_down": lambda: posture.sit_down(_robot),
        "balance_stand": lambda: posture.balance_stand(_robot),
        "recovery_stand": lambda: posture.recovery_stand(_robot),
        "lie_down": lambda: posture.lie_down(_robot),
        # Tricks
        "dance": lambda: tricks.dance(_robot, **args),
        "hello": lambda: tricks.hello(_robot),
        "stretch": lambda: tricks.stretch(_robot),
        "wiggle_hips": lambda: tricks.wiggle_hips(_robot),
        "shake_hand": lambda: tricks.shake_hand(_robot),
        "front_flip": lambda: tricks.front_flip(_robot),
        "back_flip": lambda: tricks.back_flip(_robot),
        "walk_upright": lambda: tricks.walk_upright(_robot),
        "climb_stairs": lambda: tricks.climb_stairs(_robot),
    }

    handler = commands.get(cmd)
    if not handler:
        return {"error": f"Unknown command: {cmd}"}

    result = await handler()
    if isinstance(result, dict):
        return result
    return {"status": "ok"}
