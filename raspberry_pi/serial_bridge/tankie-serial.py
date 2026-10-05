#!/usr/bin/env python3
"""
Tankie serial CLI — issue #29.

Drives the tank through the bridge daemon (Unix socket) or directly over
the serial port in raw mode.

Usage:
  tankie-serial.py drive --speed 50 --steer 0
  tankie-serial.py pan 90
  tankie-serial.py tilt 30
  tankie-serial.py stop
  tankie-serial.py state
  tankie-serial.py watchdog-test
  tankie-serial.py raw --port /dev/ttyS0          # direct, no daemon

  --raw    Talk directly to the serial port (bypass the daemon).
  --config path to config.yaml (default: next to this script)
"""

import argparse
import json
import os
import socket
import sys

try:
    import yaml
except ImportError:
    yaml = None

DEFAULTS = {
    "serial_port": "/dev/ttyS0",
    "baud": 921600,
    "socket_path": "/run/tankie/bridge.sock",
    "state_file": "/var/lib/tankie/state.json",
}


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path) and yaml:
        with open(path) as f:
            cfg.update(yaml.safe_load(f) or {})
    return cfg


def send_via_socket(cfg, cmd):
    """Send a command to the bridge daemon over the Unix socket."""
    sock_path = cfg["socket_path"]
    if not os.path.exists(sock_path):
        sys.exit(f"bridge socket not found at {sock_path} — is tankie-serial running?")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3.0)
    s.connect(sock_path)
    s.sendall((json.dumps(cmd) + "\n").encode())
    data = b""
    while b"\n" not in data:
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
    s.close()
    return json.loads(data.decode().strip()) if data else {}


def send_raw(cfg, cmd):
    """Send a command directly over the serial port (bypass the daemon)."""
    try:
        import serial
    except ImportError:
        sys.exit("pyserial required for --raw: pip3 install pyserial")
    port = cfg["serial_port"]
    if not os.path.exists(port):
        sys.exit(f"serial port {port} not found")
    s = serial.Serial(port, cfg["baud"], timeout=1.0)
    s.reset_input_buffer()
    line = json.dumps(cmd, separators=(",", ":")) + "\n"
    s.write(line.encode())
    s.flush()
    # Read the response (ack / error / state) for ~1 s.
    resp = b""
    import time
    end = time.time() + 1.0
    while time.time() < end:
        d = s.read(256)
        if d:
            resp += d
            if b"\n" in resp:
                break
    s.close()
    out = []
    for ln in resp.decode("utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if ln.startswith("{"):
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
    return out


def main():
    ap = argparse.ArgumentParser(description="Tankie serial CLI")
    ap.add_argument("--raw", action="store_true", help="talk directly to the port")
    ap.add_argument("--config", default=None, help="path to config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("drive", help="drive the tank")
    d.add_argument("--speed", type=int, default=0, help="-255..255")
    d.add_argument("--steer", type=int, default=0, help="-255..255")

    p = sub.add_parser("pan", help="set pan angle (0-180)")
    p.add_argument("angle", type=int)

    t = sub.add_parser("tilt", help="set tilt angle (0-180)")
    t.add_argument("angle", type=int)

    sub.add_parser("stop", help="stop the motors")
    sub.add_parser("state", help="show the bridge state")
    sub.add_parser("watchdog-test", help="disable keep-alive (ESP watchdog will fire)")

    args = ap.parse_args()

    # Resolve config
    cfg_path = args.config
    if cfg_path is None:
        here = os.path.dirname(os.path.abspath(__file__))
        candidate = os.path.join(here, "..", "conf", "serial_bridge.yaml")
        if os.path.exists(candidate):
            cfg_path = candidate
    cfg = load_config(cfg_path)

    if args.cmd == "drive":
        cmd = {"cmd": "drive", "speed": args.speed, "steer": args.steer}
    elif args.cmd == "pan":
        cmd = {"cmd": "pan", "angle": args.angle}
    elif args.cmd == "tilt":
        cmd = {"cmd": "tilt", "angle": args.angle}
    elif args.cmd == "stop":
        cmd = {"cmd": "stop"}
    elif args.cmd == "state":
        cmd = {"cmd": "state"}
    elif args.cmd == "watchdog-test":
        cmd = {"cmd": "watchdog-test"}
    else:
        ap.error(f"unknown command {args.cmd}")

    if args.raw:
        result = send_raw(cfg, cmd)
    else:
        result = send_via_socket(cfg, cmd)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
