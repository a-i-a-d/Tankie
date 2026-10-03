#!/usr/bin/env python3
"""
Tankie serial bridge daemon — issue #29.

Holds /dev/ttyUSB0 exclusively and provides:
  * a reader thread  (NDJSON in from the ESP8266, seq/ack tracking, link health)
  * a writer thread  (250 ms keep-alive re-send of the active drive command)
  * a state store    (JSON file: last command, last state, link up/down)
  * a Unix socket    (the CLI sends commands to the daemon over this socket)

The ESP8266 firmware (tankie/serialproto.cpp) is the single hardened entry
point for motor/servo commands. This bridge is the Pi-side counterpart.

Protocol (NDJSON, one object per line, 921600 8N1):
  Pi  → ESP:  {"cmd":"drive","speed":50,"steer":0}
               {"cmd":"pan","angle":90}
               {"cmd":"tilt","angle":30}
               {"cmd":"stop"}
  ESP → Pi:   {"type":"hello","proto":1,"fw":"v0.1-serial"}
               {"type":"ack","seq":1}
               {"type":"error","seq":2,"code":"range","field":"speed"}
               {"type":"watchdog"}
               {"type":"state","seq":1,"battery":7.42,"speed":50,"steer":0,
                "pan":90,"tilt":90,"net_mode":"sta","net_ip":"192.168.1.42"}

Usage:
  python3 bridge.py [--config config.yaml] [--daemon]

  --daemon   Fork into the background (systemd uses this mode).
"""

import argparse
import json
import logging
import os
import socket
import struct
import sys
import threading
import time
from datetime import datetime, timezone

try:
    import serial
except ImportError:
    sys.exit("pyserial is required: pip3 install pyserial")

try:
    import yaml
except ImportError:
    yaml = None  # fall back to defaults if pyyaml is missing

log = logging.getLogger("tankie-bridge")

# ---------------------------------------------------------------------------
# Defaults (overridden by config.yaml)
# ---------------------------------------------------------------------------
DEFAULTS = {
    "serial_port": "/dev/ttyUSB0",
    "baud": 921600,
    "keepalive_ms": 250,
    "ack_timeout_ms": 500,
    "state_file": "/var/lib/tankie/state.json",
    "socket_path": "/run/tankie/bridge.sock",
    "log_level": "INFO",
}


def load_config(path):
    """Load config.yaml, falling back to defaults for missing keys."""
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path) and yaml:
        with open(path) as f:
            user_cfg = yaml.safe_load(f) or {}
        cfg.update(user_cfg)
        log.info("loaded config from %s", path)
    elif path and os.path.exists(path) and not yaml:
        log.warning("pyyaml not installed — using defaults (config file %s ignored)", path)
    return cfg


# ---------------------------------------------------------------------------
# State store
# ---------------------------------------------------------------------------
class StateStore:
    """Thread-safe JSON state store written to disk on every update."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._state = {
            "link_up": False,
            "last_ack_ms": 0,
            "last_seq": 0,
            "last_command": None,
            "last_state": None,
            "watchdog_fired": False,
            "updated_at": None,
        }
        self._ensure_dir()

    def _ensure_dir(self):
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)

    def update(self, **kwargs):
        with self._lock:
            self._state.update(kwargs)
            self._state["updated_at"] = datetime.now(timezone.utc).isoformat()
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._state, f, indent=2)
            os.replace(tmp, self.path)

    def read(self):
        with self._lock:
            return dict(self._state)


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------
class Bridge:
    """
    The serial bridge daemon.

    Threads:
      _reader_thread  — reads NDJSON lines from the ESP8266
      _writer_thread  — re-sends the active drive command (keep-alive)
      _socket_thread  — accepts CLI commands over the Unix socket
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.state = StateStore(cfg["state_file"])
        self._stop = threading.Event()
        self._active_drive = None      # {"speed": int, "steer": int} or None
        self._active_drive_lock = threading.Lock()
        self._last_send_ms = 0
        self._serial = None
        self._sock = None

    # -- lifecycle -----------------------------------------------------------

    def start(self):
        log.info("starting bridge: port=%s baud=%d keepalive=%dms",
                 self.cfg["serial_port"], self.cfg["baud"], self.cfg["keepalive_ms"])
        self._open_serial()
        self._create_socket()
        self.state.update(link_up=True)

        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._socket_thread = threading.Thread(target=self._socket_loop, daemon=True)
        self._reader_thread.start()
        self._writer_thread.start()
        self._socket_thread.start()
        log.info("bridge started")

    def stop(self):
        log.info("stopping bridge")
        self._stop.set()
        if self._serial:
            try:
                self._send_line({"cmd": "stop"})
            except Exception:
                pass
        if self._serial:
            self._serial.close()
        if self._sock:
            self._sock.close()
            try:
                os.unlink(self.cfg["socket_path"])
            except OSError:
                pass
        self.state.update(link_up=False)
        log.info("bridge stopped")

    # -- serial I/O ----------------------------------------------------------

    def _open_serial(self):
        port = self.cfg["serial_port"]
        if not os.path.exists(port):
            log.error("serial port %s not found", port)
            sys.exit(1)
        self._serial = serial.Serial(port, self.cfg["baud"], timeout=0.1)
        self._serial.reset_input_buffer()
        log.info("opened %s @ %d baud", port, self.cfg["baud"])

    def _send_line(self, obj):
        line = json.dumps(obj, separators=(",", ":")) + "\n"
        self._serial.write(line.encode())
        self._serial.flush()

    def _read_line(self):
        """Read one complete line from the serial port (blocking with timeout)."""
        buf = b""
        while True:
            b = self._serial.read(1)
            if not b:
                continue
            buf += b
            if b == b"\n":
                return buf.decode("utf-8", errors="replace").rstrip("\r\n")
            if len(buf) > 1024:
                return buf.decode("utf-8", errors="replace")

    # -- reader thread -------------------------------------------------------

    def _reader_loop(self):
        while not self._stop.is_set():
            try:
                line = self._read_line()
            except Exception as e:
                log.error("read error: %s", e)
                self.state.update(link_up=False)
                time.sleep(1)
                continue

            if not line or not line.startswith("{"):
                continue  # non-JSON (debug output, etc.)

            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            mtype = msg.get("type")
            now_ms = int(time.time() * 1000)

            if mtype == "hello":
                log.info("hello: proto=%s fw=%s", msg.get("proto"), msg.get("fw"))
                self.state.update(link_up=True, watchdog_fired=False)

            elif mtype == "ack":
                seq = msg.get("seq", 0)
                log.debug("ack seq=%d", seq)
                self.state.update(last_ack_ms=now_ms, last_seq=seq)

            elif mtype == "error":
                log.warning("error from ESP: code=%s field=%s seq=%s",
                            msg.get("code"), msg.get("field"), msg.get("seq"))
                self.state.update(last_seq=msg.get("seq", 0))

            elif mtype == "watchdog":
                log.warning("watchdog fired on ESP")
                self.state.update(watchdog_fired=True)

            elif mtype == "state":
                log.debug("state: speed=%s steer=%s pan=%s tilt=%s battery=%s "
                          "net_mode=%s net_ip=%s",
                          msg.get("speed"), msg.get("steer"),
                          msg.get("pan"), msg.get("tilt"), msg.get("battery"),
                          msg.get("net_mode"), msg.get("net_ip"))
                self.state.update(last_state=msg)

    # -- writer thread (keep-alive) ------------------------------------------

    def _writer_loop(self):
        keepalive_s = self.cfg["keepalive_ms"] / 1000.0
        while not self._stop.is_set():
            with self._active_drive_lock:
                drive = dict(self._active_drive) if self._active_drive else None

            if drive:
                now_ms = int(time.time() * 1000)
                if now_ms - self._last_send_ms >= self.cfg["keepalive_ms"]:
                    try:
                        self._send_line({"cmd": "drive",
                                         "speed": drive["speed"],
                                         "steer": drive["steer"]})
                        self._last_send_ms = now_ms
                    except Exception as e:
                        log.error("keep-alive send failed: %s", e)
                        self.state.update(link_up=False)

            time.sleep(keepalive_s)

    # -- socket thread (CLI interface) ---------------------------------------

    def _create_socket(self):
        sock_path = self.cfg["socket_path"]
        d = os.path.dirname(sock_path)
        if d:
            os.makedirs(d, exist_ok=True)
        try:
            os.unlink(sock_path)
        except OSError:
            pass
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(sock_path)
        self._sock.listen(5)
        self._sock.settimeout(1.0)
        log.info("listening on %s", sock_path)

    def _socket_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()

    def _handle_client(self, conn):
        try:
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            if not data:
                return
            try:
                cmd = json.loads(data.decode().strip())
            except json.JSONDecodeError:
                conn.sendall(b'{"error":"invalid json"}\n')
                return

            result = self._dispatch(cmd)
            conn.sendall((json.dumps(result) + "\n").encode())
        except Exception as e:
            log.error("socket handler error: %s", e)
        finally:
            conn.close()

    def _dispatch(self, cmd):
        """Handle a CLI command and return a JSON-serialisable result."""
        c = cmd.get("cmd")

        if c == "drive":
            speed = int(cmd.get("speed", 0))
            steer = int(cmd.get("steer", 0))
            with self._active_drive_lock:
                self._active_drive = {"speed": speed, "steer": steer}
            self._send_line({"cmd": "drive", "speed": speed, "steer": steer})
            self._last_send_ms = int(time.time() * 1000)
            self.state.update(last_command={"cmd": "drive", "speed": speed, "steer": steer})
            return {"ok": True}

        elif c == "pan":
            angle = int(cmd.get("angle", 90))
            self._send_line({"cmd": "pan", "angle": angle})
            self.state.update(last_command={"cmd": "pan", "angle": angle})
            return {"ok": True}

        elif c == "tilt":
            angle = int(cmd.get("angle", 90))
            self._send_line({"cmd": "tilt", "angle": angle})
            self.state.update(last_command={"cmd": "tilt", "angle": angle})
            return {"ok": True}

        elif c == "stop":
            with self._active_drive_lock:
                self._active_drive = None
            self._send_line({"cmd": "stop"})
            self.state.update(last_command={"cmd": "stop"})
            return {"ok": True}

        elif c == "state":
            return self.state.read()

        elif c == "watchdog-test":
            # Stop keep-alive briefly to let the ESP watchdog fire.
            with self._active_drive_lock:
                self._active_drive = None
            self._send_line({"cmd": "stop"})
            return {"ok": True, "note": "keep-alive disabled — ESP watchdog will fire after SERIAL_WATCHDOG_MS"}

        else:
            return {"error": f"unknown command: {c}"}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Tankie serial bridge daemon")
    ap.add_argument("--config", default=None, help="path to config.yaml")
    ap.add_argument("--daemon", action="store_true", help="fork into background")
    args = ap.parse_args()

    # Resolve config path relative to this file if not absolute
    cfg_path = args.config
    if cfg_path is None:
        here = os.path.dirname(os.path.abspath(__file__))
        candidate = os.path.join(here, "..", "conf", "serial_bridge.yaml")
        if os.path.exists(candidate):
            cfg_path = candidate

    cfg = load_config(cfg_path)

    # Logging
    level = getattr(logging, cfg.get("log_level", "INFO").upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    if args.daemon and os.getpid() != 1:
        # Double-fork daemonise (simple version)
        if os.fork() > 0:
            sys.exit(0)
        os.setsid()
        if os.fork() > 0:
            sys.exit(0)
        # Redirect stdio to /dev/null
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)

    bridge = Bridge(cfg)
    bridge.start()

    # Run until interrupted
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()


if __name__ == "__main__":
    main()
