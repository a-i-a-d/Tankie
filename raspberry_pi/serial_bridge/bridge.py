#!/usr/bin/env python3
"""
Tankie serial bridge daemon — issue #29.

Holds the control-link serial port (default /dev/ttyS0, the Pi native
UART0 wired to the ESP8266; see conf/serial_bridge.yaml) exclusively and provides:
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
               {"cmd":"pan-rel","delta":+20}         (issue #31)
               {"cmd":"tilt-rel","delta":-10}        (issue #31)
               {"cmd":"center"}                      (issue #31)
               {"cmd":"sweep","axis":"pan","from":0,"to":180,"steps":20}
               {"cmd":"stop"}
               {"cmd":"set_stream","ip":"192.168.1.42","port":8889,"path":"/cam/"}
                (issue #51: the Pi pushes its own stream endpoint so the ESP
                 web UI can point the video iframe at the correct IP)
               {"cmd":"set_wifi","ssid":"tankie-lan","pass":"hunter2"}
               {"cmd":"set_wifi","ssid":"tankie-lan","pass":"hunter2","ip":"192.168.178.126","gateway":"192.168.178.1"}
               {"cmd":"set_wifi","reset":true}
               {"cmd":"get_wifi"}
               {"cmd":"reboot"}
                (issue #56: push WiFi config over the serial link, query it,
                 or reboot the ESP on demand)
  ESP → Pi:   {"type":"hello","proto":1,"fw":"v0.1-serial"}
               {"type":"ack","seq":1}
               {"type":"error","seq":2,"code":"range","field":"speed"}
               {"type":"watchdog"}
               {"type":"sweep","axis":"pan","done":true}   (issue #31)
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
    "serial_port": "/dev/ttyS0",
    "baud": 921600,
    "keepalive_ms": 250,
    "ack_timeout_ms": 500,
    "state_file": "/var/lib/tankie/state.json",
    "socket_path": "/run/tankie/bridge.sock",
    "log_level": "INFO",
    # Issue #51: the stream endpoint the bridge pushes to the ESP via
    # set_stream (the Pi owns the camera + mediamtx, so it knows its own IP).
    "stream_port": 8889,
    "stream_path": "/cam/",

    # Autonomous drive profile (issue #31): clamp AI-issued drive commands.
    # The ESP cannot tell AI from manual traffic, so the bridge enforces
    # the limit. Manual (CLI / Web UI) traffic is NOT clamped.
    "auto_profile": {
        "enabled": True,
        "max_speed": 40,
        "max_steer": 120,
    },
}


def load_config(path):
    """Load config.yaml, falling back to defaults for missing keys.

    Nested dicts (currently just `auto_profile`) are deep-merged so a
    partial override (e.g. only `max_speed`) keeps the other defaults.
    """
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path) and yaml:
        with open(path) as f:
            user_cfg = yaml.safe_load(f) or {}
        for key, val in user_cfg.items():
            if isinstance(val, dict) and isinstance(cfg.get(key), dict):
                merged = dict(cfg[key])
                merged.update(val)
                cfg[key] = merged
            else:
                cfg[key] = val
        log.info("loaded config from %s", path)
    elif path and os.path.exists(path) and not yaml:
        log.warning("pyyaml not installed — using defaults (config file %s ignored)", path)
    return cfg


# ---------------------------------------------------------------------------
# Stream endpoint (issue #51)
# ---------------------------------------------------------------------------
def detect_lan_ip():
    """Return this host's LAN IP (the one reachable from other devices).

    Uses the classic UDP ``connect()`` trick: the socket is never actually
    sent on, but the kernel picks the egress interface for the destination,
    and ``getsockname()`` then reports that interface's IP. No new
    dependencies (no netifaces), works on the Pi's default-route setup.
    Returns None if no route exists (e.g. no network yet).
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


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
        # Guards the self._serial reference (the stream thread may race stop()).
        self._serial_lock = threading.Lock()
        # Issue #51: the stream endpoint we push to the ESP (set_stream).
        self._last_stream_ip = None
        # Issue #57: does the ESP currently hold a stream endpoint? Set from
        # every `state` broadcast (the ESP only includes `stream_url` in a
        # state line when it has one). Gates the self-healing re-arm in
        # _maybe_rearm_stream() so we only force-push while the ESP is
        # actually missing its streamInfo (e.g. right after its reboot).
        self._esp_has_stream = False

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
        self._stream_thread = threading.Thread(target=self._stream_loop, daemon=True)
        self._reader_thread.start()
        self._writer_thread.start()
        self._socket_thread.start()
        self._stream_thread.start()
        log.info("bridge started")

    def stop(self):
        log.info("stopping bridge")
        self._stop.set()
        if self._serial:
            try:
                self._send_line({"cmd": "stop"})
            except Exception:
                pass
        with self._serial_lock:
            ser, self._serial = self._serial, None
        if ser:
            ser.close()
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
        with self._serial_lock:
            ser = self._serial
        if ser is None:
            return
        ser.write(line.encode())
        ser.flush()

    # -- autonomous drive profile (issue #31) --------------------------------
    #
    # The ESP8266 cannot distinguish AI-issued `drive` from manual `drive`.
    # The bridge is therefore the enforcement layer for the AI speed/steer
    # limit. Manual traffic (CLI / Web UI) is NOT clamped — only the AI path
    # (ai_control.py) goes through this. The 250 ms keep-alive re-sends the
    # CLAMPED values, so the ESP never sees an over-limit value from the AI
    # path.
    def _clamp_drive(self, speed, steer):
        """Clamp speed/steer to the auto profile. Returns (speed, steer, clamped)."""
        prof = self.cfg.get("auto_profile") or {}
        if not prof.get("enabled", False):
            return speed, steer, None
        max_speed = int(prof.get("max_speed", 40))
        max_steer = int(prof.get("max_steer", 120))
        clamped = None
        new_speed = max(-max_speed, min(max_speed, speed))
        new_steer = max(-max_steer, min(max_steer, steer))
        if new_speed != speed or new_steer != steer:
            clamped = {"speed": new_speed, "steer": new_steer}
        return new_speed, new_steer, clamped

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

    @staticmethod
    def _extract_json(line):
        """Pull the first parseable JSON object out of a serial line.

        Returns the parsed object, or None if the line carries no valid
        JSON object. After an ESP8266 reset the UART carries a burst of
        non-newline boot noise immediately before the first protocol line,
        so a single "line" (bytes up to the first newline) can look like
        b"\x00\xff...garbage...{\\"type\\":\\"hello\\",...}" — the hello and the
        garbage share one line (issue #57). The old startswith("{") gate
        dropped those, which silently lost the hello handshake and left the
        ESP without its stream endpoint after every reboot.

        Scan for "{" and try json.loads on the remainder; on a parse error
        (e.g. a stray brace inside the garbage broke the span) fall through
        to the next "{" and retry.
        """
        i = line.find("{")
        while i != -1:
            try:
                return json.loads(line[i:])
            except json.JSONDecodeError:
                pass
            i = line.find("{", i + 1)
        return None

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

            # issue #57: the ESP emits a burst of non-newline boot noise right
            # before its first protocol line after a reset, so garbage and a
            # real message (e.g. the hello handshake) can share one line.
            # Extract the first parseable JSON object instead of requiring
            # the line to start with "{" (which dropped the hello, and with
            # it the set_stream re-arm, after every ESP reboot).
            if not line:
                continue
            log.debug("rx: %r", line)

            msg = self._extract_json(line)
            if msg is None:
                continue  # non-JSON (debug output, etc.)

            mtype = msg.get("type")
            now_ms = int(time.time() * 1000)

            if mtype == "hello":
                self._on_hello(msg)

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
                self._on_state(msg)

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

    # -- stream thread (issue #51) -------------------------------------------

    def _stream_loop(self):
        """Detect our own LAN IP and push it to the ESP via set_stream.

        Runs on startup (after the hello handshake) and re-checks every 30 s
        so an IP change (DHCP renew, AP <-> STA switch) is picked up without
        a restart. The push is cheap: one short NDJSON line.
        """
        # Give the ESP a moment to boot + emit its hello before we talk.
        time.sleep(1.0)
        while not self._stop.is_set():
            self._push_stream()
            self._maybe_rearm_stream()
            for _ in range(30):
                if self._stop.is_set():
                    break
                time.sleep(1)

    def _on_hello(self, msg):
        """Handle the ESP's hello handshake (reader thread).

        issue #51 (follow-up): re-push set_stream on every hello. The bridge
        only re-sends when the IP changes, so after an ESP reboot (which loses
        its in-memory streamInfo) or a bridge restart the ESP would otherwise
        never learn the stream endpoint again. force=True bypasses the IP
        dedup so the endpoint is always re-sent.
        """
        log.info("hello: proto=%s fw=%s", msg.get("proto"), msg.get("fw"))
        self.state.update(link_up=True, watchdog_fired=False)
        self._last_stream_ip = None
        # Fresh handshake: we have no knowledge of the ESP's stream state
        # until its next state broadcast (issue #57).
        self._esp_has_stream = False
        self._push_stream(force=True)

    def _push_stream(self, force=False, _rearm=False):
        """Detect our LAN IP and push it to the ESP via set_stream.

        Normally a no-op while the IP is unchanged (``_last_stream_ip``).
        ``force=True`` bypasses that guard — used on the ESP's ``hello``
        handshake so a reconnected ESP always gets the endpoint re-sent, even
        when the IP is unchanged. (issue #51 follow-up)
        """
        ip = detect_lan_ip()
        if not ip:
            log.debug("stream: no LAN IP yet (no route?) — skipping set_stream")
            return
        if not force and ip == self._last_stream_ip:
            return
        port = int(self.cfg.get("stream_port", 8889))
        path = self.cfg.get("stream_path", "/cam/")
        try:
            self._send_line({"cmd": "set_stream", "ip": ip,
                             "port": port, "path": path})
            self._last_stream_ip = ip
            log.info("stream: pushed set_stream ip=%s port=%d path=%s%s",
                     ip, port, path, " (forced on hello)" if force else (" (re-arm)" if _rearm else ""))
        except Exception as e:
            log.warning("stream: set_stream push failed: %s", e)

    def _on_state(self, msg):
        """Handle a periodic state broadcast from the ESP (reader thread).

        issue #57: the ESP only includes `stream_url` in a state broadcast
        while it holds one (set_stream was applied). Track that here so the
        30 s stream tick can tell whether the ESP is missing its endpoint
        (e.g. right after its reboot) and needs a re-arm.
        """
        self._esp_has_stream = "stream_url" in msg
        log.debug("state: speed=%s steer=%s pan=%s tilt=%s battery=%s "
                  "net_mode=%s net_ip=%s stream_url=%s",
                  msg.get("speed"), msg.get("steer"),
                  msg.get("pan"), msg.get("tilt"), msg.get("battery"),
                  msg.get("net_mode"), msg.get("net_ip"),
                  msg.get("stream_url"))
        self.state.update(last_state=msg)

    def _maybe_rearm_stream(self):
        """Self-healing stream re-arm (issue #57).

        The ESP keeps its stream endpoint only in RAM: after an ESP reboot
        (brown-out, watchdog, power cycle) it has none, and the only
        re-arm path was the hello handshake — which can be lost when boot
        noise and the hello land on the same serial line. So: if we have a
        LAN IP to offer (we have pushed before) but the ESP's latest state
        broadcast carries no stream_url, force-push set_stream once. The
        `_esp_has_stream` flag (set by the ESP's own state feedback) gates
        the push, so once the ESP confirms the URL we stop — no spam.
        """
        if self._esp_has_stream:
            return
        ip = detect_lan_ip()
        if not ip:
            return
        log.info("stream: ESP reports no stream_url — re-arming set_stream")
        self._push_stream(force=True, _rearm=True)

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
            # Autonomous drive profile (issue #31): clamp before sending and
            # before storing (so the keep-alive re-sends the clamped values).
            speed, steer, clamped = self._clamp_drive(speed, steer)
            with self._active_drive_lock:
                self._active_drive = {"speed": speed, "steer": steer}
            self._send_line({"cmd": "drive", "speed": speed, "steer": steer})
            self._last_send_ms = int(time.time() * 1000)
            self.state.update(last_command={"cmd": "drive", "speed": speed, "steer": steer})
            result = {"ok": True}
            if clamped:
                result["clamped"] = clamped
            return result

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

        elif c == "pan-rel":
            delta = int(cmd.get("delta", 0))
            self._send_line({"cmd": "pan-rel", "delta": delta})
            self.state.update(last_command={"cmd": "pan-rel", "delta": delta})
            return {"ok": True}

        elif c == "tilt-rel":
            delta = int(cmd.get("delta", 0))
            self._send_line({"cmd": "tilt-rel", "delta": delta})
            self.state.update(last_command={"cmd": "tilt-rel", "delta": delta})
            return {"ok": True}

        elif c == "center":
            self._send_line({"cmd": "center"})
            self.state.update(last_command={"cmd": "center"})
            return {"ok": True}

        elif c == "sweep":
            axis = cmd.get("axis", "pan")
            if axis not in ("pan", "tilt"):
                return {"error": f"invalid axis: {axis} (must be pan|tilt)"}
            try:
                frm = int(cmd.get("from"))
                to = int(cmd.get("to"))
                steps = int(cmd.get("steps"))
            except (TypeError, ValueError):
                return {"error": "sweep requires integer from/to/steps"}
            if not (0 <= frm <= 180 and 0 <= to <= 180):
                return {"error": "sweep from/to must be in [0, 180]"}
            if not (1 <= steps <= 50):
                return {"error": "sweep steps must be in [1, 50]"}
            self._send_line({"cmd": "sweep", "axis": axis, "from": frm,
                             "to": to, "steps": steps})
            self.state.update(last_command={"cmd": "sweep", "axis": axis,
                                            "from": frm, "to": to, "steps": steps})
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

        elif c == "set_wifi":
            # Issue #56: push the WiFi config over the serial link. The ESP
            # validates + stores it in the EEPROM sector and reboots to apply.
            # Tolerate the expected link drop (the ESP reboots); the reader
            # re-establishes on the next hello handshake.
            payload = {"cmd": "set_wifi"}
            if cmd.get("reset") is True:
                payload["reset"] = True
            else:
                if not cmd.get("ssid"):
                    return {"error": "set_wifi requires a non-empty ssid (or reset:true)"}
                payload["ssid"] = cmd["ssid"]
                if cmd.get("pass") is not None:
                    payload["pass"] = cmd["pass"]
                if cmd.get("ip") is not None:
                    payload["ip"] = cmd["ip"]
                if cmd.get("gateway") is not None:
                    payload["gateway"] = cmd["gateway"]
            self._send_line(payload)
            self.state.update(last_command={"cmd": "set_wifi", "reset": cmd.get("reset", False)})
            return {"ok": True, "note": "config stored — ESP will reboot to apply (link drops briefly)"}

        elif c == "get_wifi":
            # Issue #56: query the current WiFi state (ssid/mode/ip, no password).
            self._send_line({"cmd": "get_wifi"})
            self.state.update(last_command={"cmd": "get_wifi"})
            return {"ok": True, "note": "response arrives as a wifi line in the state stream"}

        elif c == "reboot":
            # Issue #56: reboot the ESP on demand (a Pi-only agent can restart
            # the tank without power-cycling). Tolerate the expected link drop.
            self._send_line({"cmd": "reboot"})
            self.state.update(last_command={"cmd": "reboot"})
            return {"ok": True, "note": "ESP will reboot (link drops briefly)"}

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

    # Resolve config path: --config wins, then the installed system config
    # (/etc/tankie/serial_bridge.yaml, installed by setup.sh), then the checkout-relative
    # conf/ (dev execution from the repo), then built-in defaults.
    cfg_path = args.config
    if cfg_path is None:
        here = os.path.dirname(os.path.abspath(__file__))
        candidate = os.path.join(here, "..", "conf", "serial_bridge.yaml")
        if not os.path.exists(candidate):
            candidate = "/etc/tankie/serial_bridge.yaml"
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
