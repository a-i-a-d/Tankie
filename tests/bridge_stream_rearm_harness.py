#!/usr/bin/env python3
# Host test for raspberry_pi/serial_bridge/bridge.py — issue #57.
#
# Stubs the third-party deps (serial, yaml), then loads the REAL bridge.py
# and exercises the two self-healing changes that restore the "Start video
# stream" button after an ESP reboot:
#
#   1. _extract_json — robust line extraction (the root cause): the ESP
#      emits a burst of non-newline boot noise right before its first
#      protocol line after a reset, so garbage and a real message (e.g. the
#      hello handshake) share one serial line. The old startswith("{") gate
#      dropped those. Covers clean / garbage-prefixed / no-brace /
#      stray-brace lines.
#
#   2. _on_state + _maybe_rearm_stream — the 30 s re-arm tick: track whether
#      the ESP holds a stream endpoint (from its own state broadcasts) and
#      force-push set_stream once when it does not (but we have a LAN IP to
#      offer). Covers no IP / ESP-has-stream / ESP-lacks-stream / after-confirm.
#
# Usage: python3 tests/bridge_stream_rearm_harness.py
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
sandbox = tempfile.mkdtemp(prefix="tankie_bridge_rearm_")
sent = []


def make_bridge():
    cfg = dict(mod.DEFAULTS)
    cfg["state_file"] = os.path.join(sandbox, "state.json")
    cfg["socket_path"] = os.path.join(sandbox, "bridge.sock")
    b = mod.Bridge(cfg)
    b._send_line = lambda obj: sent.append(obj)  # capture, no real serial
    return b


extract = mod.Bridge._extract_json

# --- T1: _extract_json on a clean line --------------------------------------
print("T1: _extract_json parses a clean line")
line = '{"type":"hello","proto":1,"fw":"v0.1-serial"}'
msg = extract(line)
check(isinstance(msg, dict) and msg.get("type") == "hello",
      "clean hello parsed: " + repr(msg))

# --- T2: _extract_json on a garbage-prefixed line (the issue #57 case) ------
print("T2: _extract_json recovers a garbage-prefixed hello (issue #57)")
garbage = "\x00\x00\xff\xfe\x01"  # non-newline boot noise before the hello
line = garbage + '{"type":"hello","proto":1,"fw":"v0.1-serial"}'
msg = extract(line)
check(isinstance(msg, dict) and msg.get("type") == "hello",
      "garbage-prefixed hello recovered: " + repr(msg))

# the old startswith("{") gate would have dropped this line entirely
check(not line.startswith("{"), "precondition: line does NOT start with {")

# --- T3: _extract_json on a no-brace line -----------------------------------
print("T3: _extract_json returns None for a line with no JSON object")
check(extract("[WiFiManager] WiFi connected") is None,
      "plain debug text -> None")
check(extract("") is None, "empty line -> None")
check(extract("   ") is None, "whitespace-only -> None")

# --- T4: _extract_json on a stray-brace line --------------------------------
print("T4: _extract_json falls through a stray brace to the real object")
# A stray '{' inside the garbage makes the first span unparseable; the
# scanner must retry at the next '{' and find the real message.
line = "noise { broken " + '{"type":"state","seq":7,"battery":12.6}'
msg = extract(line)
check(isinstance(msg, dict) and msg.get("type") == "state" and msg.get("seq") == 7,
      "stray-brace line recovers the real state: " + repr(msg))

# --- T5: _on_state tracks the ESP's stream state ----------------------------
print("T5: _on_state sets _esp_has_stream from the stream_url presence")
b = make_bridge()
b._on_state({"type": "state", "seq": 1, "battery": 12.6,
             "stream_url": "http://192.168.1.42:8889/cam/"})
check(b._esp_has_stream is True, "state WITH stream_url -> has_stream True")
b._on_state({"type": "state", "seq": 2, "battery": 12.6})
check(b._esp_has_stream is False, "state WITHOUT stream_url -> has_stream False")

# --- T6: re-arm tick — no LAN IP -> no push ---------------------------------
print("T6: _maybe_rearm_stream does nothing without a LAN IP")
b = make_bridge()
sent.clear()
b._esp_has_stream = False
mod.detect_lan_ip = lambda: None
b._maybe_rearm_stream()
check(len(sent) == 0, "no IP -> no set_stream push: " + repr(sent))

# --- T7: re-arm tick — ESP has a stream -> no push --------------------------
print("T7: _maybe_rearm_stream does nothing when the ESP has a stream")
b = make_bridge()
sent.clear()
b._esp_has_stream = True
mod.detect_lan_ip = lambda: "192.168.1.42"
b._maybe_rearm_stream()
check(len(sent) == 0, "ESP has stream -> no push: " + repr(sent))

# --- T8: re-arm tick — ESP lacks stream + we have IP -> one forced push -----
print("T8: _maybe_rearm_stream force-pushes set_stream when the ESP lacks one")
b = make_bridge()
sent.clear()
b._esp_has_stream = False
mod.detect_lan_ip = lambda: "192.168.1.42"
b._maybe_rearm_stream()
check(len(sent) == 1, "exactly one push: " + repr(sent))
check(sent and sent[0].get("cmd") == "set_stream"
      and sent[0].get("ip") == "192.168.1.42"
      and sent[0].get("port") == 8889
      and sent[0].get("path") == "/cam/",
      "pushed set_stream with endpoint: " + repr(sent))

# --- T9: after the ESP confirms, no further pushes --------------------------
print("T9: once the ESP confirms the stream, the tick stops pushing")
b = make_bridge()
sent.clear()
b._esp_has_stream = False
mod.detect_lan_ip = lambda: "192.168.1.42"
b._maybe_rearm_stream()  # first tick: pushes
first = len(sent)
# ESP applies it and reports it back in its next state broadcast:
b._on_state({"type": "state", "seq": 9, "stream_url":
             "http://192.168.1.42:8889/cam/"})
b._maybe_rearm_stream()  # second tick: must be a no-op
check(first == 1, "first tick pushed once: " + repr(sent))
check(len(sent) == 1, "second tick after confirm did not re-push: " + repr(sent))

# --- T10: the re-arm push is gated by the flag, not the IP dedup ------------
print("T10: re-arm force-pushes even when the IP is unchanged (force path)")
b = make_bridge()
sent.clear()
b._esp_has_stream = False
mod.detect_lan_ip = lambda: "192.168.1.42"
b._last_stream_ip = "192.168.1.42"  # pretend we already pushed this IP
b._maybe_rearm_stream()
check(len(sent) == 1, "force bypasses IP dedup: " + repr(sent))

print()
if failures:
    print("FAILED: %d check(s) failed" % len(failures))
    sys.exit(1)
print("OK: all bridge stream re-arm checks passed")
