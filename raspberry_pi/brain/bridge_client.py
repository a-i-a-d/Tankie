"""Thin Unix-socket client for the serial bridge (issue #80, Phase 2 T4).

One-shot request/response over the bridge's Unix socket
(``/run/tankie/bridge.sock`` by default): connect, send one JSON line, read
one JSON line, disconnect. This is the reflex loop's **only** write path to
the bridge (invariant 1: the reflex loop is the single writer of drive
commands within the brain).

Contract (mirrors the LocalAI client, T2):
  * success -> ``{"ok": True, ...}``
  * failure -> ``{"ok": False, "error": <str>}``
  * **never raises** — a missing socket, a timeout, or a malformed response
    all become data. The reflex loop treats a dead bridge as "stop" and keeps
    sending ``drive 0 0`` (invariant 4).

The socket path is read from ``BrainConfig`` (no literal in this module, A4);
tests inject a fake server socket or a temp path.
"""

from __future__ import annotations

import json
import socket
from typing import Any, Mapping, Optional

__all__ = ["BridgeClient"]


def _err(message: str) -> dict:
    """Uniform failure shape."""
    return {"ok": False, "error": str(message)}


class BridgeClient:
    """One-shot Unix-socket client for the serial bridge.

    Parameters
    ----------
    socket_path:
        Path to the bridge's Unix socket (from ``BrainConfig.bridge_socket``).
    timeout_s:
        Timeout (seconds) applied to both the connect and the read.
    """

    def __init__(self, socket_path: str, timeout_s: float = 1.0) -> None:
        self._socket_path = socket_path
        self._timeout_s = max(0.05, float(timeout_s))

    @property
    def socket_path(self) -> str:
        return self._socket_path

    def send(self, cmd: Mapping[str, Any]) -> dict:
        """Send one command dict and read one JSON-line response.

        ``cmd`` e.g. ``{"cmd": "drive", "speed": 40, "steer": 0}``. Returns
        the bridge's parsed response on success (``{"ok": True, ...}``), or
        ``{"ok": False, "error": ...}`` on any failure (no socket, timeout,
        malformed JSON). Never raises.
        """
        try:
            line = json.dumps(cmd, separators=(",", ":")) + "\n"
        except (TypeError, ValueError) as exc:
            return _err(f"unserialisable command: {type(exc).__name__}: {exc}")

        sock: Optional[socket.socket] = None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self._timeout_s)
            sock.connect(self._socket_path)
            sock.sendall(line.encode("utf-8"))

            data = b""
            while b"\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
            if not data:
                return _err("bridge closed without a response")

            try:
                parsed = json.loads(data.decode("utf-8").strip())
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                return _err(f"malformed bridge response: {type(exc).__name__}")
            if not isinstance(parsed, dict):
                return _err("bridge response was not a JSON object")
            return parsed
        except (socket.timeout, TimeoutError):
            return _err(f"bridge timeout after {self._timeout_s}s")
        except (OSError, ConnectionError) as exc:
            return _err(f"bridge unreachable: {type(exc).__name__}: {exc}")
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
