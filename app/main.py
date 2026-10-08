"""HTTP API + dashboard.

ESP32:      POST /api/trigger            (header X-Trigger-Token if TRIGGER_TOKEN is set)
Dashboard:  GET  /                       POST /api/simulate   POST /api/stop   POST /api/shift
            GET/PUT /api/settings        POST /api/agent/sync WS /ws (live logs + status)
            GET  /res/<file>             (media the dashboard plays, e.g. the idle loop)
"""

import asyncio
import os
import secrets
import socket
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.concurrency import run_in_threadpool  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from . import persona  # noqa: E402
from .agent_setup import describe_error  # noqa: E402
from .entity import EntityController, approach_cue_url  # noqa: E402
from .logbus import bus, setup_logging  # noqa: E402
from .settings import LIMITS, RES_DIR, ROOT_DIR, SettingsStore  # noqa: E402

TRIGGER_TOKEN = os.getenv("TRIGGER_TOKEN", "").strip()
STATIC_DIR = ROOT_DIR / "static"

store = SettingsStore()
entity: EntityController | None = None


def _lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global entity
    bus.bind_loop(asyncio.get_running_loop())
    setup_logging(bus)
    port = os.getenv("PORT", "8000")
    bus.emit("system", f"Server up. ESP32 trigger URL: http://{_lan_ip()}:{port}/api/trigger"
             + (" (token required)" if TRIGGER_TOKEN else " (no token set)"))
    entity = EntityController(store)
    # Sync the agent in the background so the dashboard is reachable even if ElevenLabs is slow/unreachable.
    threading.Thread(target=_safe_sync, daemon=True).start()
    yield
    entity.stop()
    entity.shifter.close()


def _safe_sync():
    entity.apply_shift_settings()  # connect to Voicemeeter and put the entity at its first spot
    try:
        entity.sync_agent()
    except Exception:
        pass  # already reported on the bus


app = FastAPI(title="Washroom Bhoot", lifespan=lifespan)

if RES_DIR.is_dir():
    # StaticFiles (not FileResponse) so the browser gets range requests and can loop the track smoothly.
    app.mount("/res", StaticFiles(directory=RES_DIR), name="res")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/trigger")
async def trigger(request: Request, sensor: str = "esp32", token: str = ""):
    if TRIGGER_TOKEN:
        supplied = request.headers.get("X-Trigger-Token", token)
        if not secrets.compare_digest(supplied, TRIGGER_TOKEN):
            raise HTTPException(status_code=401, detail="bad trigger token")
    accepted, reason = entity.trigger(f"sensor '{sensor}'")
    return {"accepted": accepted, "reason": reason, "state": entity.state}


@app.post("/api/simulate")
async def simulate():
    accepted, reason = entity.trigger("dashboard (simulated sensor)")
    return {"accepted": accepted, "reason": reason, "state": entity.state}


@app.post("/api/stop")
async def stop():
    entity.stop()
    return {"ok": True}


@app.post("/api/shift")
async def test_shift():
    """Move the entity's voice now, without a conversation (for testing the Voicemeeter setup)."""
    target = await run_in_threadpool(entity.shifter.shift)
    if not target:
        raise HTTPException(status_code=409, detail="Shift not possible right now, see the logs")
    return {"to": target}


@app.get("/api/status")
async def status():
    return {"state": entity.state, "agent_id": entity.agent_id}


@app.get("/api/settings")
async def get_settings():
    personas = [
        {"id": p.id, "name": p.name, "available": p.id in entity.persona_voices, "approach_cue": approach_cue_url(p)}
        for p in persona.PERSONAS
    ]
    return {"settings": asdict(store.get()), "limits": LIMITS, "personas": personas}


@app.put("/api/settings")
async def put_settings(changes: dict):
    try:
        new, changed = store.update(changes)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    if changed:
        bus.emit("system", "Settings updated: " + ", ".join(f"{k}={getattr(new, k)}" for k in sorted(changed)))
        bus.set_status(max_turns=new.max_turns)
    if any(k.startswith("shift_") for k in changed):
        await run_in_threadpool(entity.apply_shift_settings)
    return {"settings": asdict(new)}


@app.post("/api/agent/sync")
async def resync_agent():
    try:
        await run_in_threadpool(entity.sync_agent)
    except Exception as e:
        raise HTTPException(status_code=502, detail=describe_error(e))
    return {"agent_id": entity.agent_id}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    queue = bus.subscribe()
    try:
        history, status_msg = bus.snapshot()
        await websocket.send_json({"type": "history", "entries": history})
        await websocket.send_json(status_msg)
        while True:
            await websocket.send_json(await queue.get())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        bus.unsubscribe(queue)
