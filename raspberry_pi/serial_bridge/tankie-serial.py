#!/usr/bin/env python3
"""
Tankie serial CLI — issue #29.

Drives the tank through the bridge daemon (Unix socket) or directly over
the serial port in raw mode.

Usage:
  tankie-serial.py drive --speed 50 --steer 0
  tankie-serial.py pan 90
  tankie-serial.py tilt 30
  tankie-serial.py pan-rel 20                 # relative from current pan (issue #31)
  tankie-serial.py tilt-rel -10               # relative from current tilt (issue #31)
  tankie-serial.py center                     # pan=90 + tilt=90 (issue #31)
  tankie-serial.py sweep --axis pan --from 0 --to 180 [--steps 20]  (issue #31)
  tankie-serial.py stop
  tankie-serial.py state
  tankie-serial.py watchdog-test
  tankie-serial.py wifi --ssid tankie-lan --pass hunter2 [--ip 192.168.178.126 --gateway 192.168.178.1]   (issue #56)
  tankie-serial.py wifi --reset          # wipe stored WiFi config (issue #56)
  tankie-serial.py wifi                  # query current ssid/mode/ip (issue #56)
  tankie-serial.py reboot                # reboot the ESP on demand (issue #56)
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

    pr = sub.add_parser("pan-rel", help="pan by a delta from the current angle (issue #31)")
    pr.add_argument("delta", type=int, help="-180..180")

    tr = sub.add_parser("tilt-rel", help="tilt by a delta from the current angle (issue #31)")
    tr.add_argument("delta", type=int, help="-180..180")

    sub.add_parser("center", help="center the camera: pan=90 + tilt=90 (issue #31)")

    sw = sub.add_parser("sweep", help="sweep the camera across a range (issue #31)")
    sw.add_argument("--axis", choices=["pan", "tilt"], default="pan")
    sw.add_argument("--from", dest="frm", type=int, default=0, help="start angle (0-180)")
    sw.add_argument("--to", type=int, default=180, help="end angle (0-180)")
    sw.add_argument("--steps", type=int, default=20, help="1..50")

    sub.add_parser("stop", help="stop the motors")
    sub.add_parser("state", help="show the bridge state")
    sub.add_parser("watchdog-test", help="disable keep-alive (ESP watchdog will fire)")

    wf = sub.add_parser("wifi", help="configure/query WiFi over the serial link (issue #56)")
    wf.add_argument("--ssid", default=None, help="WiFi SSID (required unless --reset)")
    wf.add_argument("--pass", dest="passw", default=None, help="WiFi password")
    wf.add_argument("--ip", default=None, help="static IP (empty/dhcp = DHCP)")
    wf.add_argument("--gateway", default=None, help="gateway (optional; derived x.x.x.1 if omitted)")
    wf.add_argument("--reset", action="store_true", help="wipe the stored WiFi config")

    sub.add_parser("reboot", help="reboot the ESP on demand (issue #56)")

    args = ap.parse_args()

    # Resolve config
    cfg_path = args.config
    if cfg_path is None:
        here = os.path.dirname(os.path.abspath(__file__))
        candidate = os.path.join(here, "..", "conf", "serial_bridge.yaml")
        if not os.path.exists(candidate):
            candidate = "/etc/tankie/serial_bridge.yaml"
        if os.path.exists(candidate):
            cfg_path = candidate
    cfg = load_config(cfg_path)

    if args.cmd == "drive":
        cmd = {"cmd": "drive", "speed": args.speed, "steer": args.steer}
    elif args.cmd == "pan":
        cmd = {"cmd": "pan", "angle": args.angle}
    elif args.cmd == "tilt":
        cmd = {"cmd": "tilt", "angle": args.angle}
    elif args.cmd == "pan-rel":
        cmd = {"cmd": "pan-rel", "delta": args.delta}
    elif args.cmd == "tilt-rel":
        cmd = {"cmd": "tilt-rel", "delta": args.delta}
    elif args.cmd == "center":
        cmd = {"cmd": "center"}
    elif args.cmd == "sweep":
        cmd = {"cmd": "sweep", "axis": args.axis, "from": args.frm,
               "to": args.to, "steps": args.steps}
    elif args.cmd == "stop":
        cmd = {"cmd": "stop"}
    elif args.cmd == "state":
        cmd = {"cmd": "state"}
    elif args.cmd == "watchdog-test":
        cmd = {"cmd": "watchdog-test"}
    elif args.cmd == "wifi":
        if args.reset:
            cmd = {"cmd": "set_wifi", "reset": True}
        elif args.ssid:
            cmd = {"cmd": "set_wifi", "ssid": args.ssid}
            if args.passw is not None:
                cmd["pass"] = args.passw
            if args.ip is not None:
                cmd["ip"] = args.ip
            if args.gateway is not None:
                cmd["gateway"] = args.gateway
        else:
            cmd = {"cmd": "get_wifi"}
    elif args.cmd == "reboot":
        cmd = {"cmd": "reboot"}
    else:
        ap.error(f"unknown command {args.cmd}")

    if args.raw:
        result = send_raw(cfg, cmd)
    else:
        result = send_via_socket(cfg, cmd)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
