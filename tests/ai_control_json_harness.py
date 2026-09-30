#!/usr/bin/env python3
# Host test for ai-control/LocalAI/ai_control.py — issues #38 / #32.
#
# Stubs the third-party deps (websocket, cv2, openai, termcolor) and loads
# the REAL ai_control.py, then asserts:
#   1. every outgoing websocket message is valid JSON (no key=value strings)
#   2. drive/camera commands are exactly the protocol-contract shapes
#      ({"cmd":"drive","speed":N,"steer":M}, {"cmd":"pan","angle":N},
#       {"cmd":"tilt","angle":N})
#   3. the control loop re-issues the combined drive object (watchdog)
#   4. the state/watchdog broadcasts are parsed without crashing
#
# Usage: python3 tests/ai_control_json_harness.py
import importlib.util
import json
import os
import sys
import types

failures = []


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        failures.append(label)


# --- stubs (must exist before ai_control.py is imported) ---------------------
sent = []


class StubWS:
    def send(self, payload):
        sent.append(payload)


websocket_stub = types.ModuleType("websocket")
websocket_stub.enableTrace = lambda *a, **k: None
websocket_stub.create_connection = lambda *a, **k: StubWS()

cv2_stub = types.ModuleType("cv2")
cv2_stub.FONT_HERSHEY_SIMPLEX = 0
cv2_stub.VideoCapture = object
cv2_stub.imencode = lambda *a, **k: (None, b"")
cv2_stub.imshow = lambda *a, **k: None
cv2_stub.waitKey = lambda *a, **k: 0

openai_stub = types.ModuleType("openai")
openai_stub.OpenAI = lambda *a, **k: None

termcolor_stub = types.ModuleType("termcolor")
termcolor_stub.colored = lambda s, *a, **k: s

sys.modules.setdefault("websocket", websocket_stub)
sys.modules.setdefault("cv2", cv2_stub)
sys.modules.setdefault("openai", openai_stub)
sys.modules.setdefault("termcolor", termcolor_stub)

# --- load the real module -----------------------------------------------------
root = os.path.join(os.path.dirname(__file__), "..")
path = os.path.join(root, "ai-control", "LocalAI", "ai_control.py")
spec = importlib.util.spec_from_file_location("ai_control", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.ws = StubWS()


def last_json(n=1):
    out = []
    for m in sent[-n:]:
        try:
            out.append(json.loads(m))
        except (TypeError, ValueError):
            out.append(None)
    return out


def is_contract(m):
    """Valid JSON object with a string 'cmd' field (the contract)."""
    return (isinstance(m, dict) and isinstance(m.get("cmd"), str))


def reset_state():
    """Reset the module's global tank/camera state between test sections."""
    mod.tank["speed"] = 0
    mod.tank["steer"] = 0
    mod.camera_position["pan"] = 90
    mod.camera_position["tilt"] = 90
    sent.clear()


# --- T1: drive forward -> contract drive object -------------------------------
print("T1: drive_tank forward -> contract drive object")
reset_state()
mod.drive_tank(40, "forward")
msgs = last_json(3)
check(all(is_contract(m) for m in msgs), "all 3 messages are contract JSON: " + repr(msgs))
check(msgs[0] == {"cmd": "pan", "angle": 90}, "pan object: " + repr(msgs[0]))
check(msgs[1] == {"cmd": "tilt", "angle": 90}, "tilt object: " + repr(msgs[1]))
check(msgs[2] == {"cmd": "drive", "speed": 40, "steer": 0}, "drive object: " + repr(msgs[2]))

# --- T2: drive left -> clamped speed + steer in ONE object --------------------
print("T2: drive_tank left -> clamped speed + steer, one object")
reset_state()
mod.drive_tank(80, "left")
msgs = last_json(3)
check(msgs[2] == {"cmd": "drive", "speed": 40, "steer": -90},
      "drive object (clamped to 40): " + repr(msgs[2]))

# --- T3: reverse + stop --------------------------------------------------------
print("T3: reverse / stop")
reset_state()
mod.drive_tank(20, "reverse")
check(last_json(3)[2] == {"cmd": "drive", "speed": -20, "steer": 0},
      "reverse object: " + repr(last_json(3)[2]))
reset_state()
mod.drive_tank(0, "stop")
check(last_json(3)[2] == {"cmd": "drive", "speed": 0, "steer": 0},
      "stop object: " + repr(last_json(3)[2]))

# --- T4: camera moves -> pan/tilt angle objects --------------------------------
print("T4: camera_control up/left/center -> angle objects")
reset_state()
mod.camera_control(30, "up")
check(last_json(3)[0] == {"cmd": "pan", "angle": 90} and
      last_json(3)[1] == {"cmd": "tilt", "angle": 120},
      "tilt up 30 -> tilt=120: " + repr(last_json(3)))
reset_state()
mod.camera_control(30, "left")
check(last_json(3)[0] == {"cmd": "pan", "angle": 60} and
      last_json(3)[1] == {"cmd": "tilt", "angle": 90},
      "pan left 30 -> pan=60: " + repr(last_json(3)))
reset_state()
mod.camera_control(0, "center")
check(last_json(3)[0] == {"cmd": "pan", "angle": 90} and
      last_json(3)[1] == {"cmd": "tilt", "angle": 90},
      "center -> pan=90,tilt=90: " + repr(last_json(3)))

# --- T5: control loop re-issues the combined drive object ----------------------
print("T5: control_loop_once -> combined drive object (watchdog re-arm)")
reset_state()
mod.tank["speed"] = 40
mod.tank["steer"] = -90
mod.control_loop_once()
check(len(sent) == 1, "exactly one re-issue: " + repr(sent))
check(json.loads(sent[0]) == {"cmd": "drive", "speed": 40, "steer": -90},
      "re-issue object: " + repr(sent[0]))

print("T5b: control_loop_once idle -> no send")
reset_state()
mod.control_loop_once()
check(len(sent) == 0, "no message when idle: " + repr(sent))

# --- T6: state / watchdog broadcasts are parsed --------------------------------
print("T6: on_message parses state + watchdog broadcasts")
try:
    mod.on_message(None, '{"type":"state","seq":1,"battery":7.42,"speed":40,"steer":0,"pan":90,"tilt":90}')
    mod.on_message(None, '{"type":"watchdog"}')
    mod.on_message(None, "not json at all")
    check(True, "no exception on state/watchdog/plain messages")
except Exception as e:
    check(False, "on_message raised: %r" % e)

# --- T7: no key=value dialect anywhere -----------------------------------------
print("T7: no key=value strings were sent")
bad = [m for m in sent if isinstance(m, str) and "=" in m and not m.strip().startswith("{")]
check(not bad, "no key=value messages: " + repr(bad))

print()
if failures:
    print("FAILED: %d check(s) failed" % len(failures))
    sys.exit(1)
print("OK: all ai_control.py JSON-protocol checks passed")
