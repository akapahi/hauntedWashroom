"""Thread-safe event bus: keeps recent log entries and fans them out to dashboard websockets."""

import asyncio
import logging
import sys
import threading
import time
from collections import deque
from logging.handlers import RotatingFileHandler

from .settings import ROOT_DIR

log = logging.getLogger("entity")


class LogBus:
    def __init__(self, history: int = 500):
        self._lock = threading.Lock()
        self._history: deque[dict] = deque(maxlen=history)
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._status: dict = {}
        self._client_status: dict[str, dict] = {}

    def bind_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def emit(self, kind: str, text: str, level: str = "info", client: str | None = None):
        """kind: system | trigger | entity | visitor | error. client: the dashboard it concerns (None = everyone)."""
        entry = {"type": "log", "ts": time.time(), "kind": kind, "level": level, "text": text, "client": client}
        with self._lock:
            self._history.append(entry)
        getattr(log, "error" if level == "error" else "warning" if level == "warn" else "info")(
            "[%s]%s %s", kind, f"[{client[:8]}]" if client else "", text
        )
        self._broadcast(entry)

    def set_status(self, **status):
        """Status shared by every dashboard (agent, settings, how many have voice)."""
        with self._lock:
            self._status.update(status)
            snapshot = {"type": "status", **self._status}
        self._broadcast(snapshot)

    def set_client_status(self, client_id: str, **status):
        """One dashboard's own status (its state, turn, session). Other dashboards ignore it."""
        with self._lock:
            current = self._client_status.setdefault(client_id, {})
            current.update(status)
            snapshot = {"type": "status", "client": client_id, **current}
        self._broadcast(snapshot)

    def drop_client_status(self, client_id: str):
        with self._lock:
            self._client_status.pop(client_id, None)

    def command(self, name: str, client: str | None = None, **data):
        """A one-off instruction for a dashboard (e.g. a test shift), or all of them. Not kept in the history."""
        self._broadcast({"type": "command", "command": name, "client": client, **data})

    def snapshot(self) -> tuple[list[dict], dict]:
        with self._lock:
            return list(self._history), {"type": "status", **self._status}

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subscribers.discard(q)

    def _broadcast(self, message: dict):
        if not self._loop or self._loop.is_closed():
            return

        def push():
            for q in list(self._subscribers):
                if not q.full():
                    q.put_nowait(message)

        self._loop.call_soon_threadsafe(push)


class BusHandler(logging.Handler):
    """Forwards warnings/errors from third-party loggers (e.g. the ElevenLabs SDK) to the dashboard."""

    def __init__(self, bus: LogBus):
        super().__init__(level=logging.WARNING)
        self.bus = bus

    def emit(self, record: logging.LogRecord):
        if record.name.startswith("entity"):
            return  # already on the bus
        level = "error" if record.levelno >= logging.ERROR else "warn"
        self.bus.emit("error" if level == "error" else "system", f"{record.name}: {record.getMessage()}", level)


def setup_logging(bus: LogBus):
    logs_dir = ROOT_DIR / "logs"
    logs_dir.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(logs_dir / "entity.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="replace")  # Hindi text on non-UTF-8 consoles (e.g. Windows cp1252)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console)
    root.addHandler(BusHandler(bus))

    # Errors raised inside SDK threads (e.g. ElevenLabs refusing the connection: out of credits) would
    # otherwise only reach the console.
    def thread_excepthook(args: threading.ExceptHookArgs):
        name = args.thread.name if args.thread else "thread"
        bus.emit("error", f"{name}: {args.exc_type.__name__}: {args.exc_value}", "error")

    threading.excepthook = thread_excepthook


bus = LogBus()
