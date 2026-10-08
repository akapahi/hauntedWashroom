"""HTTP API + dashboard.

ESP32:      POST /api/trigger            (header X-Trigger-Token if TRIGGER_TOKEN is set)
Dashboard:  GET  /                       POST /api/simulate   POST /api/stop   POST /api/shift
            GET/PUT /api/settings        POST /api/agent/sync WS /ws (live logs + status)
            POST /api/session/claim      POST /api/session/report   (the dashboard that runs the voice)
            GET  /res/<file>             (media the dashboard plays, e.g. the idle loop)
Anyone:     GET  /api/health             (Render's health check)

If DASHBOARD_PASSWORD is set, everything but /api/trigger and /api/health asks for it (HTTP Basic; the
browser remembers it, and a cookie covers the websocket and media requests).
"""

import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import socket
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.concurrency import run_in_threadpool  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from . import persona  # noqa: E402
from .agent_setup import describe_error  # noqa: E402
from .entity import EntityController, approach_cue_url  # noqa: E402
from .logbus import bus, setup_logging  # noqa: E402
from .settings import LIMITS, RES_DIR, ROOT_DIR, SettingsStore  # noqa: E402

TRIGGER_TOKEN = os.getenv("TRIGGER_TOKEN", "").strip()
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "").strip()
PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")   # Render sets this; blank elsewhere
AUTH_COOKIE = "washroom_dash"
PUBLIC_PATHS = {"/api/trigger", "/api/health"}
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
    base = PUBLIC_URL or f"http://{_lan_ip()}:{os.getenv('PORT', '8000')}"
    bus.emit("system", f"Server up. ESP32 trigger URL: {base}/api/trigger"
             + (" (token required)" if TRIGGER_TOKEN else " (no token set)"))
    if PUBLIC_URL:
        if not DASHBOARD_PASSWORD:
            bus.emit("system", "DASHBOARD_PASSWORD is not set: anyone with the URL can control the entity", "warn")
        if not TRIGGER_TOKEN:
            bus.emit("system", "TRIGGER_TOKEN is not set: anyone with the URL can trigger the entity", "warn")
    bus.emit("system", "The entity's voice plays on the dashboard with voice enabled; open it on the washroom machine")
    entity = EntityController(store)
    # Sync the agent in the background so the dashboard is reachable even if ElevenLabs is slow/unreachable.
    threading.Thread(target=_safe_sync, daemon=True).start()
    yield
    entity.stop()


def _safe_sync():
    try:
        entity.sync_agent()
    except Exception:
        pass  # already reported on the bus


app = FastAPI(title="Washroom Bhoot", lifespan=lifespan)

if RES_DIR.is_dir():
    # StaticFiles (not FileResponse) so the browser gets range requests and can loop the track smoothly.
    app.mount("/res", StaticFiles(directory=RES_DIR), name="res")


# ---- dashboard password -----------------------------------------------------------------

def _cookie_token() -> str:
    return hmac.new(DASHBOARD_PASSWORD.encode(), b"washroom-bhoot-dashboard", hashlib.sha256).hexdigest()


def _authorized(headers, cookies) -> bool:
    if not DASHBOARD_PASSWORD:
        return True
    cookie = cookies.get(AUTH_COOKIE, "")
    if cookie and secrets.compare_digest(cookie, _cookie_token()):
        return True
    auth = headers.get("authorization", "")
    if auth[:6].lower() == "basic ":
        try:
            _, _, password = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return secrets.compare_digest(password, DASHBOARD_PASSWORD)
    return False


@app.middleware("http")
async def dashboard_auth(request: Request, call_next):
    if request.url.path in PUBLIC_PATHS or _authorized(request.headers, request.cookies):
        response = await call_next(request)
        if DASHBOARD_PASSWORD and request.url.path not in PUBLIC_PATHS and AUTH_COOKIE not in request.cookies:
            response.set_cookie(
                AUTH_COOKIE, _cookie_token(), max_age=30 * 86400, httponly=True, samesite="lax",
                secure=request.url.scheme == "https",
            )
        return response
    return Response(
        "Washroom Bhoot: password required", status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Washroom Bhoot"'},
    )


# ---- routes ------------------------------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health():
    return {"ok": True, "state": entity.state if entity else "starting"}


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
    """Move the entity's voice now, on the dashboard with voice enabled (to check the speaker placement)."""
    if not entity.voice_clients:
        raise HTTPException(status_code=409, detail="No dashboard with voice enabled is connected")
    s = store.get()
    bus.command("shift", shift_width=s.shift_width, shift_ms=s.shift_ms)
    return {"ok": True}


@app.get("/api/status")
async def status():
    return {"state": entity.state, "agent_id": entity.agent_id, "voice_clients": entity.voice_clients}


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
    return {"settings": asdict(new)}


@app.post("/api/agent/sync")
async def resync_agent():
    try:
        await run_in_threadpool(entity.sync_agent)
    except Exception as e:
        raise HTTPException(status_code=502, detail=describe_error(e))
    return {"agent_id": entity.agent_id}


# ---- the dashboard that runs the voice ----------------------------------------------------------

def _ids(body: dict) -> tuple[str, str]:
    session_id, client_id = body.get("session_id"), body.get("client_id")
    if not isinstance(session_id, str) or not isinstance(client_id, str):
        raise HTTPException(status_code=400, detail="session_id and client_id are required")
    return session_id, client_id


@app.post("/api/session/claim")
async def claim_session(body: dict):
    """Called at ai_start_ms by the dashboard with voice enabled: returns the signed URL and the persona overrides."""
    session_id, client_id = _ids(body)
    try:
        return await run_in_threadpool(entity.claim, session_id, client_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=describe_error(e))


@app.post("/api/session/report")
async def report_session(body: dict):
    """Events from the browser's conversation: connected, entity/visitor lines, ending, ended, errors."""
    session_id, client_id = _ids(body)
    event = body.get("event")
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="event must be an object")
    try:
        return {"ok": entity.report(session_id, client_id, event)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    if not _authorized(websocket.headers, websocket.cookies):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    queue = bus.subscribe()
    client_id: str | None = None

    async def pump():
        history, status_msg = bus.snapshot()
        await websocket.send_json({"type": "history", "entries": history})
        await websocket.send_json(status_msg)
        while True:
            await websocket.send_json(await queue.get())

    sender = asyncio.create_task(pump())
    try:
        while True:
            # The dashboard says hello with its client id and whether it has voice enabled.
            msg = await websocket.receive_json()
            if isinstance(msg, dict) and msg.get("type") == "hello" and isinstance(msg.get("client_id"), str):
                client_id = msg["client_id"][:64]
                entity.client_update(client_id, bool(msg.get("voice")))
    except (WebSocketDisconnect, RuntimeError, ValueError):
        pass
    finally:
        sender.cancel()
        bus.unsubscribe(queue)
        if client_id:
            entity.client_gone(client_id)
