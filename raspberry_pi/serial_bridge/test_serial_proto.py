#!/usr/bin/env python3
"""
Tankie serial protocol test — issue #29 (Phase 1 raw bring-up + T1-T7).

Talks DIRECTLY to the ESP8266 over the serial port (no daemon) and walks
through the protocol test matrix:

  T1  power up / open port        -> {"type":"hello","proto":1,"fw":...}
  T2  drive 50/0                  -> ack with seq; state reflects speed/steer
  T3  pan 90 / tilt 30            -> ack; state.pan/tilt updated
  T4  stop                        -> ack; state.speed=0
  T5  drive speed=999             -> error code=range field=speed
  T6  no keep-alive > watchdog    -> {"type":"watchdog"}
  T7  non-JSON garbage on the line-> ignored, link stays up

Usage:
  python3 test_serial_proto.py [--port /dev/ttyUSB0] [--baud 921600]
                               [--watchdog-ms 1000] [--verbose]

Exits 0 if every test passes, 1 otherwise.
"""

import argparse
import json
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("pyserial is required: pip3 install pyserial")


class Port:
    def __init__(self, port, baud):
        self.s = serial.Serial(port, baud, timeout=0.5)
        self.s.reset_input_buffer()

    def close(self):
        self.s.close()

    def send(self, obj):
        line = json.dumps(obj, separators=(",", ":")) + "\n"
        self.s.write(line.encode())
        self.s.flush()

    def send_raw_text(self, text):
        self.s.write(text.encode())
        self.s.flush()

    def read_lines(self, timeout=1.0, want=None):
        """Read until `timeout` elapses or `want` (a substring) is seen."""
        buf = b""
        end = time.time() + timeout
        while time.time() < end:
            d = self.s.read(256)
            if d:
                buf += d
                if want and want.encode() in buf:
                    break
        out = []
        for ln in buf.decode("utf-8", errors="replace").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            if ln.startswith("{"):
                try:
                    out.append(json.loads(ln))
                    continue
                except json.JSONDecodeError:
                    pass
            out.append(ln)  # keep non-JSON as a raw string
        return out

    def flush(self):
        self.s.reset_input_buffer()


def main():
    ap = argparse.ArgumentParser(description="Tankie serial protocol test")
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--watchdog-ms", type=int, default=1000)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    import os
    if not os.path.exists(args.port):
        sys.exit(f"serial port {args.port} not found")

    p = Port(args.port, args.baud)
    passed = 0
    failed = 0

    def check(name, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failed += 1
            print(f"  FAIL  {name}  {detail}")

    def verbose(msg):
        if args.verbose:
            print(f"  [v] {msg}")

    try:
        # ------------------------------------------------------------------
        # T1 — hello handshake (the ESP sends hello on (re)connect/reset)
        # ------------------------------------------------------------------
        print("T1: hello handshake")
        # Reset the ESP by toggling DTR/RTS (CH340 drives EN/IO0).
        p.s.dtr = False
        p.s.rts = False
        time.sleep(0.2)
        p.s.dtr = True
        p.s.rts = True
        time.sleep(0.5)
        p.flush()
        lines = p.read_lines(timeout=2.0, want='"hello"')
        hello = next((m for m in lines if isinstance(m, dict) and m.get("type") == "hello"), None)
        verbose(f"got: {lines}")
        check("hello received", hello is not None, f"lines={lines}")
        if hello:
            check("hello proto==1", hello.get("proto") == 1, f"proto={hello.get('proto')}")
            check("hello has fw", "fw" in hello, f"hello={hello}")

        # ------------------------------------------------------------------
        # T2 — drive
        # ------------------------------------------------------------------
        print("T2: drive 50/0")
        p.flush()
        p.send({"cmd": "drive", "speed": 50, "steer": 0})
        lines = p.read_lines(timeout=1.0, want='"ack"')
        verbose(f"got: {lines}")
        ack = next((m for m in lines if isinstance(m, dict) and m.get("type") == "ack"), None)
        state = next((m for m in lines if isinstance(m, dict) and m.get("type") == "state"), None)
        check("ack received", ack is not None, f"lines={lines}")
        check("state reflects speed=50", state is not None and state.get("speed") == 50, f"state={state}")

        # ------------------------------------------------------------------
        # T3 — pan / tilt
        # ------------------------------------------------------------------
        print("T3: pan 90 / tilt 30")
        p.flush()
        p.send({"cmd": "pan", "angle": 90})
        lines = p.read_lines(timeout=1.0, want='"ack"')
        ack = next((m for m in lines if isinstance(m, dict) and m.get("type") == "ack"), None)
        state = next((m for m in lines if isinstance(m, dict) and m.get("type") == "state"), None)
        check("pan ack", ack is not None, f"lines={lines}")
        check("state.pan==90", state is not None and state.get("pan") == 90, f"state={state}")

        p.flush()
        p.send({"cmd": "tilt", "angle": 30})
        lines = p.read_lines(timeout=1.0, want='"ack"')
        state = next((m for m in lines if isinstance(m, dict) and m.get("type") == "state"), None)
        check("state.tilt==30", state is not None and state.get("tilt") == 30, f"state={state}")

        # ------------------------------------------------------------------
        # T4 — stop
        # ------------------------------------------------------------------
        print("T4: stop")
        p.flush()
        p.send({"cmd": "stop"})
        lines = p.read_lines(timeout=1.0, want='"ack"')
        ack = next((m for m in lines if isinstance(m, dict) and m.get("type") == "ack"), None)
        state = next((m for m in lines if isinstance(m, dict) and m.get("type") == "state"), None)
        check("stop ack", ack is not None, f"lines={lines}")
        check("state.speed==0", state is not None and state.get("speed") == 0, f"state={state}")

        # ------------------------------------------------------------------
        # T5 — out-of-range
        # ------------------------------------------------------------------
        print("T5: drive speed=999")
        p.flush()
        p.send({"cmd": "drive", "speed": 999, "steer": 0})
        lines = p.read_lines(timeout=1.0, want='"error"')
        verbose(f"got: {lines}")
        err = next((m for m in lines if isinstance(m, dict) and m.get("type") == "error"), None)
        check("error received", err is not None, f"lines={lines}")
        if err:
            check("error code==range", err.get("code") == "range", f"err={err}")
            check("error field==speed", err.get("field") == "speed", f"err={err}")

        # ------------------------------------------------------------------
        # T6 — watchdog (no keep-alive for > SERIAL_WATCHDOG_MS)
        # ------------------------------------------------------------------
        print(f"T6: watchdog (wait {args.watchdog_ms + 500} ms)")
        p.flush()
        # Wait past the watchdog window with no commands.
        time.sleep(args.watchdog_ms / 1000.0 + 0.5)
        # The ESP should have emitted a watchdog line (it is emitted once).
        # Read whatever is buffered.
        lines = p.read_lines(timeout=0.5)
        verbose(f"buffered: {lines}")
        wd = next((m for m in lines if isinstance(m, dict) and m.get("type") == "watchdog"), None)
        # The watchdog line may have been emitted during the sleep; if the
        # buffer was flushed we may need to re-trigger. Try a drive + wait.
        if wd is None:
            p.send({"cmd": "drive", "speed": 10, "steer": 0})
            time.sleep(args.watchdog_ms / 1000.0 + 0.5)
            lines = p.read_lines(timeout=0.5)
            verbose(f"re-trigger buffered: {lines}")
            wd = next((m for m in lines if isinstance(m, dict) and m.get("type") == "watchdog"), None)
        check("watchdog emitted", wd is not None, f"lines={lines}")

        # ------------------------------------------------------------------
        # T7 — non-JSON garbage is ignored, link stays up
        # ------------------------------------------------------------------
        print("T7: garbage tolerance")
        p.flush()
        p.send_raw_text("this is not json\n")
        p.send_raw_text("Battery Voltage: 7.42\n")
        time.sleep(0.3)
        # Now a valid command should still be processed.
        p.send({"cmd": "drive", "speed": 5, "steer": 0})
        lines = p.read_lines(timeout=1.0, want='"ack"')
        ack = next((m for m in lines if isinstance(m, dict) and m.get("type") == "ack"), None)
        check("link still up after garbage", ack is not None, f"lines={lines}")

        # Clean up: stop the tank.
        p.send({"cmd": "stop"})
        time.sleep(0.2)

    finally:
        p.close()

    print(f"\n== {passed} passed, {failed} failed ==")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
