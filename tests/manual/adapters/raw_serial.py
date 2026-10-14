"""Raw serial adapter (issue #29) — direct pyserial NDJSON to the ESP8266.

Bypasses the bridge daemon and talks straight to the ESP8266 over the serial
port. This is the lowest integration layer: it tests the firmware's serial
protocol in isolation. Requires ``pyserial`` (declared in requirements.txt).

Config keys (all optional):
    serial_port  — default ``/dev/ttyS0`` (the Pi native UART0 control link)
    baud         — default ``921600``
    read_s       — how long to read the response after a write, default 1.0
"""

from __future__ import annotations

import json
import os
from typing import Any, List, Mapping, Tuple

from .base import Adapter

DEFAULT_PORT = "/dev/ttyS0"
DEFAULT_BAUD = 921600
DEFAULT_READ_S = 1.0


class RawSerialAdapter(Adapter):
    name = "raw_serial"

    def __init__(self, cfg: Mapping[str, Any] | None = None):
        super().__init__(cfg)
        self.serial_port = self.cfg.get("serial_port", DEFAULT_PORT)
        self.baud = int(self.cfg.get("baud", DEFAULT_BAUD))
        self.read_s = float(self.cfg.get("read_s", DEFAULT_READ_S))

    def _pyserial(self):
        try:
            import serial  # noqa: PLC0415 - lazy: optional dep
            return serial
        except ImportError:
            return None

    def run(self, cmd: Mapping[str, Any]) -> dict:
        serial = self._pyserial()
        if serial is None:
            return {"reply": {"ok": False,
                              "error": "pyserial not installed (pip3 install pyserial)"}}
        if not os.path.exists(self.serial_port):
            return {"reply": {"ok": False,
                              "error": f"serial port {self.serial_port} not found"}}
        s = serial.Serial(self.serial_port, self.baud, timeout=1.0)
        try:
            s.reset_input_buffer()
            line = json.dumps(cmd, separators=(",", ":")) + "\n"
            s.write(line.encode())
            s.flush()
            import time
            end = time.time() + self.read_s
            resp = b""
            while time.time() < end:
                d = s.read(256)
                if d:
                    resp += d
                    if b"\n" in resp:
                        break
            raw = resp.decode("utf-8", errors="replace")
            # Parse every NDJSON line into a list (hello/ack/error/state/...).
            lines = []
            for ln in raw.splitlines():
                ln = ln.strip()
                if ln.startswith("{"):
                    try:
                        lines.append(json.loads(ln))
                    except json.JSONDecodeError:
                        pass
            # ``reply`` = the first protocol line (the direct response), so
            # assertions can target reply.type / reply.ok / reply.code.
            reply = lines[0] if lines else {"ok": False, "error": "no protocol response"}
            return {"reply": reply, "lines": lines, "raw": raw}
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            return {"reply": {"ok": False, "error": f"serial error: {exc}"}}
        finally:
            s.close()

    def preflight(self) -> List[Tuple[str, bool, str]]:
        checks = []
        serial = self._pyserial()
        checks.append(("pyserial installed", serial is not None,
                       "ok" if serial else "pip3 install pyserial"))
        port_ok = os.path.exists(self.serial_port)
        checks.append(("serial port present", port_ok,
                       f"{self.serial_port} {'found' if port_ok else 'MISSING'}"))
        return checks

    def describe(self) -> str:
        return ("raw serial (pyserial NDJSON) — the firmware's serial protocol "
                "in isolation, bypassing the bridge daemon")
