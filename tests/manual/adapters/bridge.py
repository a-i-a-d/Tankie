"""Bridge daemon adapter (issue #29/#33) — the primary layer for most tests.

Talks to the Pi bridge daemon over its Unix socket (one JSON line in -> one
JSON line out) and reads the tank state from the bridge state store
(``/var/lib/tankie/state.json``). This is the same path ``ai_control.py`` and
the CLI (``tankie-serial.py``) use, so it exercises the real control link.

Config keys (all optional, sensible defaults):
    socket_path  — default ``/run/tankie/bridge.sock``
    state_file   — default ``/var/lib/tankie/state.json``
    timeout_s    — socket timeout, default 3.0
"""

from __future__ import annotations

import json
import os
import socket
from typing import Any, List, Mapping, Tuple

from .base import Adapter

DEFAULT_SOCKET = "/run/tankie/bridge.sock"
DEFAULT_STATE = "/var/lib/tankie/state.json"
DEFAULT_TIMEOUT_S = 3.0


class BridgeAdapter(Adapter):
    name = "bridge"

    def __init__(self, cfg: Mapping[str, Any] | None = None):
        super().__init__(cfg)
        self.socket_path = self.cfg.get("socket_path", DEFAULT_SOCKET)
        self.state_file = self.cfg.get("state_file", DEFAULT_STATE)
        self.timeout_s = float(self.cfg.get("timeout_s", DEFAULT_TIMEOUT_S))

    # -- low-level ----------------------------------------------------------
    def _send(self, cmd: Mapping[str, Any]) -> dict:
        """Send one command to the bridge daemon, return the parsed reply."""
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(self.timeout_s)
            try:
                s.connect(self.socket_path)
                s.sendall((json.dumps(cmd) + "\n").encode())
                data = b""
                while b"\n" not in data:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            finally:
                s.close()
            if not data:
                return {"ok": False, "error": "bridge returned no response"}
            reply = json.loads(data.decode().strip())
            if not isinstance(reply, dict):
                return {"ok": False, "error": "bridge returned a non-object reply"}
            return reply
        except (OSError, socket.timeout, ValueError) as exc:
            return {"ok": False, "error": f"bridge unreachable: {exc}"}

    def _read_state(self) -> dict:
        try:
            with open(self.state_file) as f:
                state = json.load(f)
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            return {}

    # -- Adapter interface --------------------------------------------------
    def run(self, cmd: Mapping[str, Any]) -> dict:
        reply = self._send(cmd)
        # Always attach the current state store snapshot so assertions can
        # check e.g. ``state.last_state.speed`` or ``state.link_up``.
        return {"reply": reply, "state": self._read_state()}

    def preflight(self) -> List[Tuple[str, bool, str]]:
        checks = []
        sock_ok = os.path.exists(self.socket_path)
        checks.append(("bridge socket present", sock_ok,
                       f"{self.socket_path} {'found' if sock_ok else 'MISSING'}"))
        state_ok = os.path.exists(self.state_file)
        checks.append(("bridge state store present", state_ok,
                       f"{self.state_file} {'found' if state_ok else 'MISSING'}"))
        return checks

    def describe(self) -> str:
        return ("bridge daemon (Unix socket + state store) — the real control "
                "link used by the CLI and ai_control.py")
