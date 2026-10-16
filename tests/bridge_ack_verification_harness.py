#!/usr/bin/env python3
# Host test for raspberry_pi/serial_bridge/bridge.py — issue #106.
#
# The bridge used to return {"ok": true} as soon as the bytes hit the serial
# buffer (fire-and-forget): _send_line() was a silent no-op when the serial
# handle was None, and the ESP's ack/error replies were only logged, never
# correlated with the request. So a closed link or an ESP range error still
# looked like success to the CLI / brain.
#
# The fix: _send_line() reports whether the bytes were written, the reader
# thread publishes each ESP ack/error (_on_ack / _on_error), and _dispatch()
# waits (bounded, ack_timeout_ms) for one of them before replying — so
# "ok" means the ESP executed the command.
#
# Scenarios covered (serial + yaml stubbed, real bridge.py loaded):
#   1. _send_line with serial handle None  -> False (drop reported)
#   2. _send_line with a live handle       -> True, line written + flushed
#   3. DEFAULTS / config: ack_timeout_ms present (500)
#   4. _on_ack publishes the ack + state (last_ack_ms / last_seq)
#   5. _on_error publishes the error (code / field / seq)
#   6. ack supersedes a stale pending error
#   7. drive, serial closed                -> ok:false "serial link down"
#   8. drive, ESP acks                     -> ok:true (+seq)
#   9. drive, ESP range error              -> ok:false code=range field=speed
#  10. drive, no ack (ESP silent)          -> ok:false timeout (bounded wait)
#  11. drive clamped + acked               -> ok:true AND clamped reported
#  12. pan / tilt / pan-rel / tilt-rel / center / sweep / stop with ack
#      -> ok:true each (the full acked command set)
#  13. pan out-of-range (ESP error)        -> ok:false (surfaced)
#  14. get_wifi / set_wifi / reboot        -> still fire-and-forget ok:true
#  15. timeout is bounded (ack_timeout_ms honoured, ~10x faster to fail)
#
# Usage: python3 tests/bridge_ack_verification_harness.py
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import types

failures = []


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        failures.append(label)


# --- stubs (must be in place before bridge.py is imported) ------------------
serial_stub = types.ModuleType("serial")
serial_stub.Serial = object
sys.modules.setdefault("serial", serial_stub)

yaml_stub = types.ModuleType("yaml")
yaml_stub.safe_load = lambda *a, **k: {}
sys.modules.setdefault("yaml", yaml_stub)

# --- load the real module ---------------------------------------------------
root = os.path.join(os.path.dirname(__file__), "..")
path = os.path.join(root, "raspberry_pi", "serial_bridge", "bridge.py")
spec = importlib.util.spec_from_file_location("bridge", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

sandbox = tempfile.mkdtemp(prefix="tankie_bridge_ack_harness_")
sent = []


class FakeSerial:
    """In-memory stand-in for pyserial: records written bytes."""

    def __init__(self):
        self.buf = io.BytesIO()
        self.flushed = 0

    def write(self, data):
        self.buf.write(data)

    def flush(self):
        self.flushed += 1

    def lines(self):
        return [ln for ln in self.buf.getvalue().decode().splitlines() if ln]


def make_bridge(**overrides):
    cfg = dict(mod.DEFAULTS)
    cfg["state_file"] = os.path.join(sandbox, "state.json")
    cfg["socket_path"] = os.path.join(sandbox, "bridge.sock")
    cfg.update(overrides)
    b = mod.Bridge(cfg)
    b._serial = None  # start "closed" unless a test installs a FakeSerial
    return b


# --- T1: _send_line reports a dropped line ----------------------------------
print("T1: _send_line with serial handle None -> False (drop reported)")
b = make_bridge()
check(b._send_line({"cmd": "drive", "speed": 10}) is False,
      "closed handle -> False")

# --- T2: _send_line writes + flushes when live -------------------------------
print("T2: _send_line with a live handle -> True, line written + flushed")
b = make_bridge()
fs = FakeSerial()
with b._serial_lock:
    b._serial = fs
check(b._send_line({"cmd": "drive", "speed": 10, "steer": 0}) is True,
      "live handle -> True")
check(fs.lines() == ['{"cmd":"drive","speed":10,"steer":0}'],
      "exact NDJSON line written: %r" % fs.lines())
check(fs.flushed >= 1, "flushed")

# --- T3: ack_timeout_ms present in DEFAULTS ----------------------------------
print("T3: DEFAULTS include ack_timeout_ms (500)")
check(mod.DEFAULTS.get("ack_timeout_ms") == 500,
      "DEFAULTS.ack_timeout_ms == 500: %r" % mod.DEFAULTS.get("ack_timeout_ms"))

# --- T4: _on_ack publishes the ack + state -----------------------------------
print("T4: _on_ack publishes the ack and updates state")
b = make_bridge()
b._on_ack({"seq": 7})
st = b.state.read()
check(st.get("last_seq") == 7, "state.last_seq == 7: %r" % st.get("last_seq"))
check(int(st.get("last_ack_ms", 0)) > 0, "state.last_ack_ms set")

# --- T5: _on_error publishes the error ---------------------------------------
print("T5: _on_error publishes code/field/seq")
b = make_bridge()
b._on_error({"seq": 8, "code": "range", "field": "speed"})
st = b.state.read()
check(st.get("last_seq") == 8, "state.last_seq == 8: %r" % st.get("last_seq"))

# --- T6: ack supersedes a stale pending error --------------------------------
print("T6: an ack supersedes a stale pending error")
b = make_bridge()
b._on_error({"seq": 1, "code": "range", "field": "speed"})
b._on_ack({"seq": 2})
with b._ack_lock:
    check(b._pending_error is None, "stale error cleared")
    check(b._pending_ack == {"seq": 2}, "ack retained: %r" % b._pending_ack)

# --- T7: drive while serial closed -> ok:false (acceptance #1) ---------------
print("T7: drive with serial closed -> ok:false (not ok:true)")
b = make_bridge()
res = b._dispatch({"cmd": "drive", "speed": 40, "steer": 0})
check(res.get("ok") is False, "ok is False: %r" % res)
check("serial link down" in res.get("error", ""), "error names the drop: %r" % res)

# --- T8: drive acked by the ESP -> ok:true -----------------------------------
print("T8: drive acked by the ESP -> ok:true (+seq)")
b = make_bridge()
with b._serial_lock:
    b._serial = FakeSerial()
b._on_ack({"seq": 1})  # the ESP (reader thread) confirms
res = b._dispatch({"cmd": "drive", "speed": 40, "steer": 0})
check(res.get("ok") is True, "ok is True: %r" % res)
check(res.get("seq") == 1, "seq reported: %r" % res)

# --- T9: drive rejected by the ESP -> ok:false (acceptance #2) ---------------
print("T9: drive with an ESP range error -> ok:false (error surfaced)")
b = make_bridge()
with b._serial_lock:
    b._serial = FakeSerial()
b._on_error({"seq": 2, "code": "range", "field": "speed"})
res = b._dispatch({"cmd": "drive", "speed": 999, "steer": 0})
check(res.get("ok") is False, "ok is False: %r" % res)
check("range" in res.get("error", "") and "speed" in res.get("error", ""),
      "code+field surfaced: %r" % res)

# --- T10: ESP silent -> bounded timeout -> ok:false --------------------------
print("T10: drive with no ack (ESP silent) -> ok:false timeout, bounded wait")
b = make_bridge()
with b._serial_lock:
    b._serial = FakeSerial()
t0 = time.monotonic()
res = b._dispatch({"cmd": "drive", "speed": 10, "steer": 0})
elapsed = time.monotonic() - t0
check(res.get("ok") is False, "ok is False: %r" % res)
check("ack" in res.get("error", ""), "timeout error named: %r" % res)
check(0.4 <= elapsed <= 1.0, "waited ~ack_timeout_ms (0.5s): %.3fs" % elapsed)

# --- T11: clamped drive still reports ok + clamped ---------------------------
print("T11: clamped drive, acked -> ok:true AND clamped reported")
b = make_bridge(auto_profile={"enabled": True, "max_speed": 40, "max_steer": 255})
with b._serial_lock:
    b._serial = FakeSerial()
b._on_ack({"seq": 3})
res = b._dispatch({"cmd": "drive", "speed": 100, "steer": 0})
check(res.get("ok") is True, "ok is True: %r" % res)
check(res.get("clamped") == {"speed": 40, "steer": 0}, "clamped kept: %r" % res)
check(b.state.read().get("last_command") ==
      {"cmd": "drive", "speed": 40, "steer": 0}, "clamped value stored")

# --- T12: the full acked command set -----------------------------------------
print("T12: pan / tilt / pan-rel / tilt-rel / center / sweep / stop -> ok:true")
cmds = [
    {"cmd": "pan", "angle": 90},
    {"cmd": "tilt", "angle": 30},
    {"cmd": "pan-rel", "delta": 20},
    {"cmd": "tilt-rel", "delta": -10},
    {"cmd": "center"},
    {"cmd": "sweep", "axis": "pan", "from": 0, "to": 180, "steps": 20},
    {"cmd": "stop"},
]
for c in cmds:
    b = make_bridge()
    with b._serial_lock:
        b._serial = FakeSerial()
    b._on_ack({"seq": 4})
    res = b._dispatch(c)
    check(res.get("ok") is True, "%s -> ok: %r" % (c["cmd"], res))

# --- T13: pan out-of-range (ESP error) -> ok:false ----------------------------
print("T13: pan out-of-range (ESP error) -> ok:false")
b = make_bridge()
with b._serial_lock:
    b._serial = FakeSerial()
b._on_error({"seq": 5, "code": "range", "field": "pan"})
res = b._dispatch({"cmd": "pan", "angle": 199})
check(res.get("ok") is False, "ok is False: %r" % res)
check("pan" in res.get("error", ""), "field surfaced: %r" % res)

# --- T14: unacked commands stay fire-and-forget ok:true -----------------------
print("T14: get_wifi / set_wifi / reboot -> ok:true (unacked by firmware)")
b = make_bridge()
with b._serial_lock:
    b._serial = FakeSerial()
res = b._dispatch({"cmd": "get_wifi"})
check(res.get("ok") is True, "get_wifi ok: %r" % res)
res = b._dispatch({"cmd": "set_wifi", "ssid": "tankie-lan", "pass": "hunter2"})
check(res.get("ok") is True, "set_wifi ok: %r" % res)
res = b._dispatch({"cmd": "reboot"})
check(res.get("ok") is True, "reboot ok: %r" % res)

# --- T15: timeout is configurable (bounded) ----------------------------------
print("T15: ack_timeout_ms honoured (small window fails fast)")
b = make_bridge(ack_timeout_ms=100)
with b._serial_lock:
    b._serial = FakeSerial()
t0 = time.monotonic()
res = b._dispatch({"cmd": "drive", "speed": 10, "steer": 0})
elapsed = time.monotonic() - t0
check(res.get("ok") is False, "ok is False: %r" % res)
check(elapsed < 0.5, "failed fast (~100ms): %.3fs" % elapsed)

print()
if failures:
    print("FAILED: %d check(s) failed" % len(failures))
    sys.exit(1)
print("OK: all bridge ack-verification (issue #106) checks passed")
