"""Triggers the entity from the mmWave presence sensor (XIAO ESP32S3, esp32/) over USB serial.

The sensor prints a line every 2 s:
    status=PRESENT | link=OK frames=1234 bytes=29918 | state=3 move=67cm(55) static=64cm(100)
After sensor_confirm PRESENT status lines in a row, the entity is triggered. An EMPTY status (or the "EMPTY"
event line) resets the count. While the entity is busy (conversation, cooldown, ...) every line is ignored.

SENSOR_PORT in .env picks the port (e.g. /dev/ttyACM0 or COM5); blank = auto-detect, "off" = disabled.
"""

import os
import threading
import time

from .logbus import bus
from .settings import SettingsStore

BAUD = int(os.getenv("SENSOR_BAUD", "115200"))
RETRY_SECONDS = 5
ESPRESSIF_VID = 0x303A


def _find_port() -> str | None:
    from serial.tools import list_ports

    ports = list(list_ports.comports())
    for p in ports:
        if p.vid == ESPRESSIF_VID:
            return p.device
    for p in ports:
        if "ttyACM" in p.device or "ttyUSB" in p.device:
            return p.device
    return None


class SerialSensor:
    def __init__(self, entity, store: SettingsStore):
        self.entity = entity
        self.store = store
        self.port_setting = os.getenv("SENSOR_PORT", "").strip()
        self._stop = threading.Event()
        self._count = 0

    def start(self):
        if self.port_setting.lower() in ("off", "none", "0"):
            bus.emit("system", "Serial presence sensor disabled (SENSOR_PORT=off)")
            return
        threading.Thread(target=self._run, daemon=True, name="serial-sensor").start()

    def stop(self):
        self._stop.set()

    def _run(self):
        try:
            import serial
        except ImportError:
            bus.emit("error", "pyserial isn't installed (pip install pyserial); the serial sensor is off", "error")
            return
        warned = False
        while not self._stop.is_set():
            port = self.port_setting or _find_port()
            if not port:
                if not warned:
                    bus.emit("system", "No serial presence sensor found, still looking", "warn")
                    warned = True
                self._stop.wait(RETRY_SECONDS)
                continue
            try:
                with serial.Serial(port, BAUD, timeout=1) as ser:
                    bus.emit("system", f"Presence sensor connected on {port}")
                    warned = False
                    self._count = 0
                    while not self._stop.is_set():
                        raw = ser.readline()
                        if raw:
                            self._on_line(raw.decode(errors="replace").strip())
            except (serial.SerialException, OSError) as e:
                if not warned:
                    bus.emit("system", f"Presence sensor on {port} unavailable ({e}), retrying", "warn")
                    warned = True
                self._stop.wait(RETRY_SECONDS)

    def _on_line(self, line: str):
        if self.entity.state != "idle":
            self._count = 0   # triggered (or not ready): ignore the sensor until the entity is idle again
            return
        if line == "EMPTY":
            self._count = 0
        elif line.startswith("status="):
            if line.split("|", 1)[0].strip() == "status=PRESENT":
                self._count += 1
            else:
                self._count = 0
        else:
            return
        needed = self.store.get().sensor_confirm
        if self._count >= needed:
            self._count = 0
            bus.emit("system", f"Presence confirmed ({needed} PRESENT readings in a row)")
            self.entity.trigger("serial presence sensor")
