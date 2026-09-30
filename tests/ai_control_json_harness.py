#!/usr/bin/env python3
# Host test for ai-control/LocalAI/ai_control.py — issue #33 (serial bridge).
#
# Stubs the third-party deps (cv2, openai, termcolor) and the `socket`
# module, then loads the REAL ai_control.py so the real bridge client code
# path runs (no websocket stub anymore — the transport is gone). Asserts:
#   1. every command handed to the bridge is valid JSON with the exact
#      contract shape ({"cmd":"drive","speed":N,"steer":M},
#      {"cmd":"pan","angle":N}, {"cmd":"tilt","angle":N})
#   2. the AUTO_PROFILE clamp (max 40) is applied
#   3. tank state is read from the bridge state store (read_state /
#      sync_from_state)
#   4. watchdog / link feedback is surfaced into the tool results
#   5. bridge down -> graceful {"ok":False,"error":...}, no crash
#   6. no WebSocket plumbing / no key=value strings anywhere
#
# Usage: python3 tests/ai_control_json_harness.py
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


# --- sandbox: temp socket path + state store ---------------------------------
sandbox = tempfile.mkdtemp(prefix="tankie_ai_harness_")
sock_path = os.path.join(sandbox, "bridge.sock")
state_path = os.path.join(sandbox, "state.json")


def write_state(**kw):
    with open(state_path, "w") as f:
        json.dump(kw, f)


def read_state_file():
    try:
        with open(state_path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# --- stubs (must be in place before ai_control.py is imported) ----------------
sent = []          # commands handed to the bridge socket
reply_mode = "ok"  # "ok" | "error" | "empty" | "badjson"


class StubSocket:
    def __init__(self, family, type_):
        self.family = family
        self.timeout = None

    def settimeout(self, t):
        self.timeout = t

    def connect(self, path):
        if not os.path.exists(path):
            raise OSError(f"no such file: {path}")  # bridge down

    def sendall(self, payload):
        line = payload.decode()
        if not line.endswith("\n"):
            raise ValueError("bridge protocol is one JSON line in")
        sent.append(json.loads(line.rstrip("\n")))

    def recv(self, n):
        if reply_mode == "empty":
            return b""
        if reply_mode == "badjson":
            return b"not json at all\n"
        if reply_mode == "error":
            return b'{"ok": false, "error": "boom"}\n'
        return b'{"ok": true}\n'

    def close(self):
        pass


socket_stub = types.ModuleType("socket")
socket_stub.AF_UNIX = 1
socket_stub.SOCK_STREAM = 2
socket_stub.timeout = OSError  # bridge_command catches (OSError, socket.timeout)
socket_stub.socket = lambda family, type_: StubSocket(family, type_)

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

sys.modules["socket"] = socket_stub
sys.modules.setdefault("cv2", cv2_stub)
sys.modules.setdefault("openai", openai_stub)
sys.modules.setdefault("termcolor", termcolor_stub)

# --- load the real module -----------------------------------------------------
root = os.path.join(os.path.dirname(__file__), "..")
path = os.path.join(root, "ai-control", "LocalAI", "ai_control.py")
spec = importlib.util.spec_from_file_location("ai_control", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# Point the client at the sandbox (the module reads these globals at call time).
mod.BRIDGE_SOCKET = sock_path
mod.STATE_FILE = state_path


def reset(reply="ok"):
    """Reset bridge + state between test sections."""
    global reply_mode
    reply_mode = reply
    sent.clear()
    mod.tank["speed"] = 0
    mod.tank["steer"] = 0
    mod.camera_position["pan"] = 90
    mod.camera_position["tilt"] = 90
    try:
        os.unlink(sock_path)
    except OSError:
        pass
    if reply != "down":
        touch = open(sock_path, "w")
        touch.close()
    try:
        os.unlink(state_path)
    except OSError:
        pass


def is_contract(m):
    """Valid JSON object with a string 'cmd' field (the contract)."""
    return isinstance(m, dict) and isinstance(m.get("cmd"), str)


# --- T1: drive forward -> ONE contract drive object ---------------------------
print("T1: drive_tank forward -> contract drive object over the bridge")
reset()
res = mod.drive_tank(40, "forward")
check(len(sent) == 1, "exactly one bridge command: " + repr(sent))
check(all(is_contract(m) for m in sent), "all commands are contract JSON: " + repr(sent))
check(sent and sent[0] == {"cmd": "drive", "speed": 40, "steer": 0},
      "drive object: " + repr(sent))
check(isinstance(res, str) and res.startswith("ok"), "tool result ok: " + repr(res))

# --- T2: drive left -> clamped speed + steer in ONE object --------------------
print("T2: drive_tank left -> clamped speed + steer, one object")
reset()
mod.drive_tank(80, "left")
check(sent and sent[0] == {"cmd": "drive", "speed": 40, "steer": -90},
      "drive object (clamped to 40): " + repr(sent))

# --- T3: reverse + stop --------------------------------------------------------
print("T3: reverse / stop")
reset()
mod.drive_tank(20, "reverse")
check(sent and sent[0] == {"cmd": "drive", "speed": -20, "steer": 0},
      "reverse object: " + repr(sent))
reset()
mod.drive_tank(0, "stop")
check(sent and sent[0] == {"cmd": "drive", "speed": 0, "steer": 0},
      "stop object: " + repr(sent))

# --- T4: camera moves -> pan/tilt angle objects --------------------------------
print("T4: camera_control up/left/center -> pan+tilt angle objects")
reset()
mod.camera_control(30, "up")
check(sent[-2:] == [{"cmd": "pan", "angle": 90}, {"cmd": "tilt", "angle": 120}],
      "tilt up 30 -> pan=90,tilt=120: " + repr(sent))
reset()
mod.camera_control(30, "left")
check(sent[-2:] == [{"cmd": "pan", "angle": 60}, {"cmd": "tilt", "angle": 90}],
      "pan left 30 -> pan=60,tilt=90: " + repr(sent))
reset()
res = mod.camera_control(0, "center")
check(sent[-2:] == [{"cmd": "pan", "angle": 90}, {"cmd": "tilt", "angle": 90}],
      "center -> pan=90,tilt=90: " + repr(sent))
check(isinstance(res, str) and res.startswith("ok"), "camera result ok: " + repr(res))

# --- T5: bridge down -> graceful error, no crash -------------------------------
print("T5: bridge down -> graceful error result")
reset(reply="down")
try:
    res = mod.drive_tank(20, "forward")
    check(isinstance(res, str) and res.startswith("error") and "bridge" in res,
          "error result mentions the bridge: " + repr(res))
except Exception as e:
    check(False, "drive_tank raised with bridge down: %r" % e)
reset(reply="down")
try:
    res = mod.camera_control(30, "up")
    check(isinstance(res, str) and res.startswith("error"),
          "camera error result: " + repr(res))
except Exception as e:
    check(False, "camera_control raised with bridge down: %r" % e)

# --- T6: bad bridge replies -> graceful ----------------------------------------
print("T6: empty / non-JSON bridge replies -> graceful error")
reset(reply="empty")
res = mod.drive_tank(20, "forward")
check(isinstance(res, str) and res.startswith("error"), "empty reply -> error: " + repr(res))
reset(reply="badjson")
res = mod.drive_tank(20, "forward")
check(isinstance(res, str) and res.startswith("error"), "bad JSON reply -> error: " + repr(res))
reset(reply="error")
res = mod.drive_tank(20, "forward")
check(isinstance(res, str) and "boom" in res, "bridge error surfaced: " + repr(res))

# --- T7: state store is the source of truth ------------------------------------
print("T7: read_state / sync_from_state pull from the bridge state store")
reset()
write_state(link_up=True, watchdog_fired=False, last_seq=7,
            last_state={"type": "state", "seq": 7, "battery": 7.42,
                        "speed": 50, "steer": -90, "pan": 30, "tilt": 60})
st = mod.read_state()
check(st.get("link_up") is True and st.get("last_seq") == 7, "read_state: " + repr(st))
mod.sync_from_state(st)
check(mod.tank["speed"] == 50 and mod.tank["steer"] == -90,
      "tank synced from last_state: " + repr(mod.tank))
check(mod.camera_position["pan"] == 30 and mod.camera_position["tilt"] == 60,
      "camera synced from last_state: " + repr(mod.camera_position))
check(mod.read_state() is not None, "read_state default path works")

# --- T8: watchdog / link feedback ----------------------------------------------
print("T8: surface_feedback reports watchdog + link state")
write_state(link_up=True, watchdog_fired=True)
fb = mod.surface_feedback()
check("watchdog" in fb, "watchdog feedback: " + repr(fb))
check(read_state_file().get("watchdog_fired") is False,
      "watchdog flag cleared after surfacing: " + repr(read_state_file()))
write_state(link_up=False, watchdog_fired=False)
fb = mod.surface_feedback()
check("link" in fb, "link-down feedback: " + repr(fb))
write_state(link_up=True, watchdog_fired=False)
check(mod.surface_feedback() == "", "quiet when all is well")

# --- T9: feedback rides along in the tool result --------------------------------
print("T9: tool result carries the bridge feedback")
reset()
write_state(link_up=True, watchdog_fired=True)
res = mod.drive_tank(20, "forward")
check(isinstance(res, str) and res.startswith("ok") and "watchdog" in res,
      "drive result carries watchdog feedback: " + repr(res))

# --- T10: no WebSocket plumbing / no key=value dialect --------------------------
print("T10: no WebSocket plumbing, no key=value strings")
src = open(path).read()
for needle in ("websocket", "WS_HOST", "on_message", "on_error",
               "on_close", "create_connection", "control_loop"):
    check(needle not in src, "no %r in ai_control.py" % needle)
check("websocket" not in sys.modules, "websocket module never imported")
bad = [m for m in sent if not is_contract(m)]
check(not bad, "every sent command is contract JSON: " + repr(bad))
keyval = [m for m in sent if isinstance(m, str) and "=" in m and not m.strip().startswith("{")]
check(not keyval, "no key=value messages: " + repr(keyval))

print()
if failures:
    print("FAILED: %d check(s) failed" % len(failures))
    sys.exit(1)
print("OK: all ai_control.py serial-bridge checks passed (%d commands captured)" % len(sent))
