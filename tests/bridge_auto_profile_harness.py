#!/usr/bin/env python3
# Host test for raspberry_pi/serial_bridge/bridge.py — issue #31.
#
# Stubs the third-party deps (serial, yaml), then loads the REAL bridge.py
# and exercises:
#   1. _clamp_drive against the auto_profile (enabled/disabled, speed/steer)
#   2. _dispatch for the new pan-rel / tilt-rel / center / sweep commands
#      (asserts the exact NDJSON line handed to the serial port)
#   3. the `clamped` reply shape on drive
#
# Usage: python3 tests/bridge_auto_profile_harness.py
import importlib.util
import json
import os
import sys
import tempfile
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

# --- build a Bridge with a temp state store + a capture on _send_line -------
sandbox = tempfile.mkdtemp(prefix="tankie_bridge_harness_")
sent = []


def make_bridge(auto_profile):
    cfg = dict(mod.DEFAULTS)
    cfg["state_file"] = os.path.join(sandbox, "state.json")
    cfg["socket_path"] = os.path.join(sandbox, "bridge.sock")
    cfg["auto_profile"] = auto_profile
    b = mod.Bridge(cfg)
    b._send_line = lambda obj: sent.append(obj)  # capture, no real serial
    return b


# --- T1: _clamp_drive clamps speed + steer ----------------------------------
print("T1: _clamp_drive clamps to the auto profile")
b = make_bridge({"enabled": True, "max_speed": 40, "max_steer": 120})
check(b._clamp_drive(40, 0) == (40, 0, None), "at-limit speed not clamped")
check(b._clamp_drive(50, 0) == (40, 0, {"speed": 40, "steer": 0}),
      "over-limit speed clamped to 40")
check(b._clamp_drive(-50, 0) == (-40, 0, {"speed": -40, "steer": 0}),
      "negative over-limit clamped to -40")
check(b._clamp_drive(0, 120) == (0, 120, None), "at-limit steer not clamped")
check(b._clamp_drive(0, 200) == (0, 120, {"speed": 0, "steer": 120}),
      "over-limit steer clamped to 120")
check(b._clamp_drive(0, -200) == (0, -120, {"speed": 0, "steer": -120}),
      "negative over-limit steer clamped to -120")
check(b._clamp_drive(255, 255) == (40, 120, {"speed": 40, "steer": 120}),
      "full-range clamped to profile")

# --- T2: _clamp_drive disabled passes through --------------------------------
print("T2: _clamp_drive disabled -> no clamp")
b = make_bridge({"enabled": False, "max_speed": 40, "max_steer": 120})
check(b._clamp_drive(255, 255) == (255, 255, None), "disabled: full range passes")
b = make_bridge({})  # no enabled key
check(b._clamp_drive(255, 255) == (255, 255, None), "missing enabled: passes")

# --- T3: _dispatch drive returns clamped reply -------------------------------
print("T3: _dispatch drive clamps + reports clamped in reply")
b = make_bridge({"enabled": True, "max_speed": 40, "max_steer": 120})
sent.clear()
res = b._dispatch({"cmd": "drive", "speed": 100, "steer": 200})
check(res.get("ok") is True, "drive ok: " + repr(res))
check(res.get("clamped") == {"speed": 40, "steer": 120},
      "clamped reported: " + repr(res))
check(sent and sent[-1] == {"cmd": "drive", "speed": 40, "steer": 120},
      "clamped line sent: " + repr(sent))
# within-limit -> no clamped key
sent.clear()
res = b._dispatch({"cmd": "drive", "speed": 10, "steer": 0})
check(res.get("ok") is True and "clamped" not in res,
      "within-limit no clamped key: " + repr(res))
check(sent and sent[-1] == {"cmd": "drive", "speed": 10, "steer": 0},
      "within-limit line sent: " + repr(sent))

# --- T4: new pan/tilt command dispatches -------------------------------------
print("T4: pan-rel / tilt-rel / center / sweep dispatch")
b = make_bridge({"enabled": True, "max_speed": 40, "max_steer": 120})

sent.clear()
res = b._dispatch({"cmd": "pan-rel", "delta": 20})
check(res.get("ok") is True, "pan-rel ok: " + repr(res))
check(sent and sent[-1] == {"cmd": "pan-rel", "delta": 20},
      "pan-rel line: " + repr(sent))

sent.clear()
res = b._dispatch({"cmd": "tilt-rel", "delta": -10})
check(res.get("ok") is True, "tilt-rel ok: " + repr(res))
check(sent and sent[-1] == {"cmd": "tilt-rel", "delta": -10},
      "tilt-rel line: " + repr(sent))

sent.clear()
res = b._dispatch({"cmd": "center"})
check(res.get("ok") is True, "center ok: " + repr(res))
check(sent and sent[-1] == {"cmd": "center"}, "center line: " + repr(sent))

sent.clear()
res = b._dispatch({"cmd": "sweep", "axis": "pan", "from": 0, "to": 180, "steps": 20})
check(res.get("ok") is True, "sweep ok: " + repr(res))
check(sent and sent[-1] == {"cmd": "sweep", "axis": "pan", "from": 0,
                            "to": 180, "steps": 20},
      "sweep line: " + repr(sent))

# --- T5: sweep validation -----------------------------------------------------
print("T5: sweep validation rejects bad params")
b = make_bridge({"enabled": True, "max_speed": 40, "max_steer": 120})
res = b._dispatch({"cmd": "sweep", "axis": "yaw", "from": 0, "to": 180, "steps": 5})
check("error" in res, "bad axis rejected: " + repr(res))
res = b._dispatch({"cmd": "sweep", "axis": "pan", "from": 0, "to": 181, "steps": 5})
check("error" in res, "to>180 rejected: " + repr(res))
res = b._dispatch({"cmd": "sweep", "axis": "pan", "from": 0, "to": 180, "steps": 0})
check("error" in res, "steps=0 rejected: " + repr(res))
res = b._dispatch({"cmd": "sweep", "axis": "pan", "from": 0, "to": 180, "steps": 51})
check("error" in res, "steps=51 rejected: " + repr(res))
res = b._dispatch({"cmd": "sweep", "axis": "pan", "to": 180, "steps": 5})
check("error" in res, "missing from rejected: " + repr(res))

print()
if failures:
    print("FAILED: %d check(s) failed" % len(failures))
    sys.exit(1)
print("OK: all bridge auto-profile + new-command checks passed")
