#!/usr/bin/env python3
# Host test for raspberry_pi/serial_bridge/bridge.py — issue #56.
#
# Stubs the third-party deps (serial, yaml), then loads the REAL bridge.py
# and exercises the new _dispatch() entries that let the Pi (or a Pi-only
# agent) re-network the tank over the serial link without a browser:
#
#   - set_wifi (save)  -> the exact NDJSON line handed to the serial port
#   - set_wifi (reset) -> the {"cmd":"set_wifi","reset":true} line
#   - set_wifi missing ssid -> an error (no line sent)
#   - get_wifi         -> the {"cmd":"get_wifi"} line
#   - reboot           -> the {"cmd":"reboot"} line
#
# Usage: python3 tests/bridge_wifi_harness.py
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

sandbox = tempfile.mkdtemp(prefix="tankie_bridge_wifi_")
sent = []


def make_bridge():
    cfg = dict(mod.DEFAULTS)
    cfg["state_file"] = os.path.join(sandbox, "state.json")
    cfg["socket_path"] = os.path.join(sandbox, "bridge.sock")
    b = mod.Bridge(cfg)
    b._send_line = lambda obj: sent.append(obj)  # capture, no real serial
    return b


b = make_bridge()

# --- T1: set_wifi (save) sends the exact line -------------------------------
print("T1: set_wifi (save) dispatches the exact NDJSON line")
sent.clear()
res = b._dispatch({"cmd": "set_wifi", "ssid": "tankie-lan", "pass": "hunter2",
                   "ip": "192.168.178.126", "gateway": "192.168.178.1"})
check(res.get("ok") is True, "set_wifi ok: " + repr(res))
check(sent and sent[-1] == {"cmd": "set_wifi", "ssid": "tankie-lan",
                            "pass": "hunter2", "ip": "192.168.178.126",
                            "gateway": "192.168.178.1"},
      "set_wifi line: " + repr(sent))

# --- T2: set_wifi with only ssid+pass ---------------------------------------
print("T2: set_wifi (ssid+pass only) omits ip/gateway")
sent.clear()
res = b._dispatch({"cmd": "set_wifi", "ssid": "tankie-lan", "pass": "hunter2"})
check(res.get("ok") is True, "ok: " + repr(res))
check(sent and sent[-1] == {"cmd": "set_wifi", "ssid": "tankie-lan", "pass": "hunter2"},
      "line omits ip/gateway: " + repr(sent))

# --- T3: set_wifi (reset) ----------------------------------------------------
print("T3: set_wifi (reset) sends the reset line")
sent.clear()
res = b._dispatch({"cmd": "set_wifi", "reset": True})
check(res.get("ok") is True, "ok: " + repr(res))
check(sent and sent[-1] == {"cmd": "set_wifi", "reset": True},
      "reset line: " + repr(sent))

# --- T4: set_wifi missing ssid -> error, no line ----------------------------
print("T4: set_wifi without ssid (and without reset) is an error")
sent.clear()
res = b._dispatch({"cmd": "set_wifi"})
check("error" in res, "error returned: " + repr(res))
check(len(sent) == 0, "no line sent: " + repr(sent))

# --- T5: get_wifi ------------------------------------------------------------
print("T5: get_wifi dispatches the query line")
sent.clear()
res = b._dispatch({"cmd": "get_wifi"})
check(res.get("ok") is True, "ok: " + repr(res))
check(sent and sent[-1] == {"cmd": "get_wifi"}, "get_wifi line: " + repr(sent))

# --- T6: reboot --------------------------------------------------------------
print("T6: reboot dispatches the reboot line")
sent.clear()
res = b._dispatch({"cmd": "reboot"})
check(res.get("ok") is True, "ok: " + repr(res))
check(sent and sent[-1] == {"cmd": "reboot"}, "reboot line: " + repr(sent))

# --- T7: unknown command still errors ---------------------------------------
print("T7: unknown command still errors")
res = b._dispatch({"cmd": "fly"})
check("error" in res, "unknown cmd error: " + repr(res))

print()
if failures:
    print("FAILED: %d check(s) failed" % len(failures))
    sys.exit(1)
print("OK: all bridge set_wifi/get_wifi/reboot checks passed")
