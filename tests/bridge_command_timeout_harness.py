#!/usr/bin/env python3
# Host test for raspberry_pi/serial_bridge/bridge.py — issue #98 (safety).
#
# Stubs the third-party deps (serial, yaml), then loads the REAL bridge.py
# and exercises the client-silence safety net:
#
#   The brain's reflex loop is the primary safety net, but it sits ABOVE the
#   bridge. If the brain process dies (crash / OOM / SIGKILL) while the tank
#   is driving, the bridge would otherwise keep re-sending the last non-zero
#   `drive` forever (re-arming the ESP 1000 ms watchdog on every send) and
#   the tank would drive indefinitely.
#
#   The fix: if no new `drive` command arrives within `command_timeout_ms`,
#   the bridge clears the active drive and sends one `drive 0 0` stop.
#
# Scenarios covered:
#   1. no active drive            -> no stop sent
#   2. non-zero drive, fresh       -> no timeout yet (client alive)
#   3. non-zero drive, stale       -> clears drive + sends `drive 0 0` + flag
#   4. zero (0,0) drive, stale     -> no timeout (already stopped)
#   5. after timeout               -> _active_drive is None (writer won't re-send)
#   6. _dispatch drive             -> refreshes _last_cmd_ms (freshness)
#   7. fresh drive after timeout   -> command_timeout_fired reset to False
#   8. command_timeout_ms          -> configurable (small window trips faster)
#   9. DEFAULTS                    -> command_timeout_ms present (5000)
#  10. StateStore                  -> command_timeout_fired initialized False
#  11. _writer_loop (real thread)  -> stale drive self-stops within one tick
#
# Usage: python3 tests/bridge_command_timeout_harness.py
import importlib.util
import json
import os
import sys
import tempfile
import threading
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

sandbox = tempfile.mkdtemp(prefix="tankie_bridge_timeout_harness_")
sent = []


def make_bridge(**overrides):
    cfg = dict(mod.DEFAULTS)
    cfg["state_file"] = os.path.join(sandbox, "state.json")
    cfg["socket_path"] = os.path.join(sandbox, "bridge.sock")
    cfg.update(overrides)
    b = mod.Bridge(cfg)
    b._send_line = lambda obj: sent.append(obj)  # capture, no real serial
    return b


def now_ms():
    return int(time.time() * 1000)


def stale_ms(seconds_ago):
    return now_ms() - int(seconds_ago * 1000)


# --- T1: no active drive -> no stop -----------------------------------------
print("T1: no active drive -> no timeout stop")
b = make_bridge()
sent.clear()
b._check_command_timeout()
check(not sent, "no drive => no stop sent: %r" % sent)

# --- T2: non-zero drive, fresh -> no timeout yet -----------------------------
print("T2: non-zero drive, fresh timestamp -> no timeout yet")
b = make_bridge()
sent.clear()
with b._active_drive_lock:
    b._active_drive = {"speed": 40, "steer": 0}
    b._last_cmd_ms = now_ms()  # just dispatched
b._check_command_timeout()
check(not sent, "fresh drive => no stop: %r" % sent)
with b._active_drive_lock:
    still = dict(b._active_drive)
check(still == {"speed": 40, "steer": 0}, "fresh drive retained: %r" % still)

# --- T3: non-zero drive, stale -> clears + stops + flag ----------------------
print("T3: non-zero drive, stale timestamp -> clear + stop + flag")
b = make_bridge()
sent.clear()
with b._active_drive_lock:
    b._active_drive = {"speed": 40, "steer": 0}
    b._last_cmd_ms = stale_ms(6)  # 6 s ago > 5000 ms default
b._check_command_timeout()
check(sent == [{"cmd": "drive", "speed": 0, "steer": 0}],
      "stale drive => one stop sent: %r" % sent)
with b._active_drive_lock:
    cleared = b._active_drive
check(cleared is None, "active drive cleared after timeout: %r" % cleared)
state = b.state.read()
check(state.get("command_timeout_fired") is True,
      "command_timeout_fired flag set: %r" % state.get("command_timeout_fired"))

# --- T4: zero (0,0) drive, stale -> no timeout (already stopped) -------------
print("T4: zero (0,0) drive, stale -> no timeout stop")
b = make_bridge()
sent.clear()
with b._active_drive_lock:
    b._active_drive = {"speed": 0, "steer": 0}
    b._last_cmd_ms = stale_ms(6)
b._check_command_timeout()
check(not sent, "already-stopped drive => no redundant stop: %r" % sent)

# --- T5: after timeout the writer loop reads None (no re-send) ---------------
print("T5: after timeout, _writer_loop's drive read is None (no re-send)")
b = make_bridge()
sent.clear()
with b._active_drive_lock:
    b._active_drive = {"speed": 30, "steer": 10}
    b._last_cmd_ms = stale_ms(6)
b._check_command_timeout()
# Mirror exactly what _writer_loop does after the timeout check.
with b._active_drive_lock:
    drive = dict(b._active_drive) if b._active_drive else None
check(drive is None, "writer loop would read None (no stale re-send): %r" % drive)

# --- T6: _dispatch drive refreshes _last_cmd_ms ------------------------------
print("T6: _dispatch drive refreshes _last_cmd_ms (freshness)")
b = make_bridge()
with b._active_drive_lock:
    b._last_cmd_ms = stale_ms(60)  # very stale
b._dispatch({"cmd": "drive", "speed": 20, "steer": 0})
with b._active_drive_lock:
    fresh = b._last_cmd_ms
check(abs(fresh - now_ms()) < 2000,
      "dispatch refreshed _last_cmd_ms to ~now: %d vs %d" % (fresh, now_ms()))
# And a stale-then-dispatch must NOT time out now.
sent.clear()
b._check_command_timeout()
check(not sent, "after fresh dispatch, no timeout stop: %r" % sent)

# --- T7: fresh drive resets command_timeout_fired ----------------------------
print("T7: fresh drive resets command_timeout_fired to False")
b = make_bridge()
b.state.update(command_timeout_fired=True)  # simulate a prior fire
b._dispatch({"cmd": "drive", "speed": 20, "steer": 0})
check(b.state.read().get("command_timeout_fired") is False,
      "fresh drive clears the flag: %r" % b.state.read().get("command_timeout_fired"))

# --- T8: command_timeout_ms is configurable ----------------------------------
print("T8: command_timeout_ms is configurable (small window trips faster)")
b = make_bridge(command_timeout_ms=1000)  # 1 s window
sent.clear()
with b._active_drive_lock:
    b._active_drive = {"speed": 40, "steer": 0}
    b._last_cmd_ms = stale_ms(2)  # 2 s ago > 1 s window
b._check_command_timeout()
check(sent == [{"cmd": "drive", "speed": 0, "steer": 0}],
      "1 s window: 2 s stale => stop: %r" % sent)
# And 0.5 s stale is INSIDE a 1 s window -> no stop.
b2 = make_bridge(command_timeout_ms=1000)
sent.clear()
with b2._active_drive_lock:
    b2._active_drive = {"speed": 40, "steer": 0}
    b2._last_cmd_ms = stale_ms(0.5)
b2._check_command_timeout()
check(not sent, "1 s window: 0.5 s stale => no stop: %r" % sent)

# --- T9: DEFAULTS carry command_timeout_ms -----------------------------------
print("T9: DEFAULTS include command_timeout_ms (5000)")
check(mod.DEFAULTS.get("command_timeout_ms") == 5000,
      "DEFAULTS.command_timeout_ms == 5000: %r" % mod.DEFAULTS.get("command_timeout_ms"))

# --- T10: StateStore initializes command_timeout_fired False -----------------
print("T10: StateStore initializes command_timeout_fired False")
b = make_bridge()
check(b.state.read().get("command_timeout_fired") is False,
      "state store init flag False: %r" % b.state.read().get("command_timeout_fired"))

# --- T11: real _writer_loop thread self-stops a stale drive ------------------
print("T11: _writer_loop (real thread) self-stops a stale drive within one tick")
b = make_bridge(keepalive_ms=20)
sent.clear()
with b._active_drive_lock:
    b._active_drive = {"speed": 40, "steer": 0}
    b._last_cmd_ms = stale_ms(6)
t = threading.Thread(target=b._writer_loop, daemon=True)
t.start()
# Wait up to 1 s for the loop to fire the timeout stop.
deadline = time.time() + 1.0
while time.time() < deadline and not any(
        d.get("cmd") == "drive" and d.get("speed") == 0 and d.get("steer") == 0
        for d in sent):
    time.sleep(0.02)
b._stop.set()
t.join(timeout=1.0)
stops = [d for d in sent if d.get("cmd") == "drive" and d.get("speed") == 0
         and d.get("steer") == 0]
check(len(stops) >= 1, "writer loop sent a stop for the stale drive: %r" % sent)
with b._active_drive_lock:
    cleared = b._active_drive
check(cleared is None, "writer loop cleared the stale drive: %r" % cleared)

print()
if failures:
    print("FAILED: %d check(s) failed" % len(failures))
    sys.exit(1)
print("OK: all bridge command-timeout (issue #98) checks passed")
