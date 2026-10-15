"""Offline unit tests for the reflex System 1 safety net (issue #80, T4).

Run from the repo root:  ``pytest tests/brain/ -q``

No tank, no Pi, no LocalAI, no real bridge. The pure :class:`ReflexController`
is exercised directly (the full A1–A4 decision matrix); the
:class:`BridgeClient` is exercised against a real Unix socket served by a
fake in-process bridge (happy path / timeout / missing socket / malformed
response); the :class:`ReflexLoop` thread is exercised with fake
grabber/localai/bridge to prove the safety invariants:

  * invariant 2 — a center-lane obstacle always yields ``drive 0 0``
    (pre-empting any pending action);
  * invariant 4 — no frame / detect() failed => ``drive 0 0``;
  * A2 — obstacle avoidance is ON by default-OFF (``reflex_obstacle_enabled``)
    and the switch works both ways;
  * A4 — idle keep-alive ``drive 0 0`` every tick, or quiet idle when
    ``idle_keepalive=False``;
  * ``stop()`` always sends one final ``drive 0 0``.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time

import numpy as np
import pytest

from brain.bridge_client import BridgeClient
from brain.reflex import IDLE_CMD, ReflexController, ReflexLoop, frame_to_b64

IMG_W, IMG_H = 640, 480


def det(class_name, cx, cy, w=60, h=60, conf=0.9):
    """A detection dict centred at (cx, cy)."""
    return {
        "class_name": class_name,
        "x": cx - w / 2.0,
        "y": cy - h / 2.0,
        "w": w,
        "h": h,
        "confidence": conf,
    }


def controller(**over) -> ReflexController:
    base = dict(enabled=True, steer_around=60, max_speed=40, max_steer=120)
    base.update(over)
    return ReflexController(**base)


# ===========================================================================
# ReflexController — the pure decision matrix
# ===========================================================================

def test_clear_no_pending_is_idle():
    r = controller().decide([], IMG_W, IMG_H)
    assert (r.cmd, r.speed, r.steer) == ("drive", 0, 0)
    assert r.preempted is False
    assert r.reason == "idle"


def test_clear_pending_drive_passes_through_clamped():
    r = controller().decide([], IMG_W, IMG_H, pending={"cmd": "drive", "speed": 99, "steer": -150, "reason": "go"})
    assert r.cmd == "drive"
    assert r.speed == 40          # clamped to max_speed (40)
    assert r.steer == -100        # clamped to max_steer (120), then schema (±100)
    assert r.preempted is False
    assert r.reason == "go"


def test_to_bridge_cmd_is_pure_passthrough_of_result():
    # to_bridge_cmd() never re-introduces a pending value the reflex
    # overrode: it is a pure passthrough of the ReflexResult (a stop stays a
    # stop). The ReflexResult is already clamped to min(AUTO_PROFILE, ±100);
    # the bridge is the real enforcement layer (issue #31).
    c = controller()
    r = c.decide([], IMG_W, IMG_H, pending={"cmd": "drive", "speed": 50, "steer": 150})
    assert (r.speed, r.steer) == (40, 100)
    assert c.to_bridge_cmd(r, {"cmd": "drive", "speed": 50, "steer": 150}) == {
        "cmd": "drive", "speed": 40, "steer": 100
    }


def test_clear_pending_pan_passthrough():
    r = controller().decide([], IMG_W, IMG_H, pending={"cmd": "pan", "pan": 45, "reason": "look left"})
    assert r.cmd == "pan"
    assert r.preempted is False
    assert r.reason == "look left"


def test_clear_pending_tilt_passthrough():
    r = controller().decide([], IMG_W, IMG_H, pending={"cmd": "tilt", "tilt": 120})
    assert r.cmd == "tilt"


def test_clear_pending_invalid_cmd_ignored():
    r = controller().decide([], IMG_W, IMG_H, pending={"cmd": "warp"})
    assert (r.cmd, r.speed, r.steer) == ("drive", 0, 0)
    assert "ignored" in r.reason


def test_center_obstacle_stops_and_preempts():
    # box centred in the lane, bottom at 0.9*H (close)
    obstacle = det("person", IMG_W / 2, 0.85 * IMG_H)
    r = controller().decide([obstacle], IMG_W, IMG_H,
                            pending={"cmd": "drive", "speed": 40, "steer": 0})
    assert (r.cmd, r.speed, r.steer) == ("drive", 0, 0)
    assert r.preempted is True
    assert "person" in r.reason and "in lane" in r.reason


def test_left_obstacle_steers_right_away():
    # obstacle centred at 15% width (left of lane) -> steer RIGHT (positive)
    obstacle = det("car", 0.15 * IMG_W, 0.85 * IMG_H)
    r = controller().decide([obstacle], IMG_W, IMG_H)
    assert r.cmd == "drive" and r.speed == 0
    assert r.steer == 60
    assert "left" in r.reason


def test_right_obstacle_steers_left_away():
    # obstacle centred at 85% width (right of lane) -> steer LEFT (negative)
    obstacle = det("dog", 0.85 * IMG_W, 0.85 * IMG_H)
    r = controller().decide([obstacle], IMG_W, IMG_H)
    assert r.steer == -60
    assert "right" in r.reason


def test_non_blocking_class_is_clear():
    obstacle = det("cup", IMG_W / 2, 0.9 * IMG_H)
    r = controller().decide([obstacle], IMG_W, IMG_H)
    assert (r.speed, r.steer) == (0, 0) and r.reason == "idle"


def test_far_obstacle_is_clear():
    # bottom at 0.5*H < 0.7*H threshold -> not close
    obstacle = det("person", IMG_W / 2, 0.45 * IMG_H)
    r = controller().decide([obstacle], IMG_W, IMG_H)
    assert (r.speed, r.steer) == (0, 0) and r.reason == "idle"


def test_disabled_mode_ignores_obstacles():
    # A2: default-off switch — an in-lane obstacle is ignored when disabled
    obstacle = det("person", IMG_W / 2, 0.9 * IMG_H)
    r = controller(enabled=False).decide([obstacle], IMG_W, IMG_H)
    assert (r.cmd, r.speed, r.steer) == ("drive", 0, 0)
    assert r.reason == "idle"


def test_custom_block_classes():
    # A1: configurable list — "cup" blocks when added, "person" doesn't
    c = controller(block_classes=("cup",))
    assert c.decide([det("person", IMG_W / 2, 0.9 * IMG_H)], IMG_W, IMG_H).reason == "idle"
    assert "cup" in c.decide([det("cup", IMG_W / 2, 0.9 * IMG_H)], IMG_W, IMG_H).reason


def test_to_bridge_cmd_drive_and_pan():
    c = controller()
    r = c.decide([], IMG_W, IMG_H, pending={"cmd": "drive", "speed": 20, "steer": 5})
    assert c.to_bridge_cmd(r, {"cmd": "drive", "speed": 20, "steer": 5}) == {"cmd": "drive", "speed": 20, "steer": 5}
    rp = c.decide([], IMG_W, IMG_H, pending={"cmd": "pan", "pan": 45})
    assert c.to_bridge_cmd(rp, {"cmd": "pan", "pan": 45}) == {"cmd": "pan", "angle": 45}
    rt = c.decide([], IMG_W, IMG_H, pending={"cmd": "tilt", "tilt": 130})
    assert c.to_bridge_cmd(rt, {"cmd": "tilt", "tilt": 130}) == {"cmd": "tilt", "angle": 130}


# ===========================================================================
# BridgeClient — real Unix socket, fake in-process bridge
# ===========================================================================

class _FakeBridgeServer:
    """Serves one JSON line per connection on a real Unix socket."""

    def __init__(self, path, responder):
        self.path = path
        self.responder = responder
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(4)
        self.sock.settimeout(2.0)
        self.requests = []
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            self.requests.append(json.loads(data.decode().strip()))
            self.responder(conn)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        try:
            os.unlink(self.path)
        except OSError:
            pass


@pytest.fixture
def sock_path(tmp_path):
    p = str(tmp_path / "bridge.sock")
    yield p
    try:
        os.unlink(p)
    except OSError:
        pass


def test_bridge_client_send_happy(sock_path):
    server = _FakeBridgeServer(sock_path, lambda c: c.sendall(b'{"ok":true}\n'))
    try:
        r = BridgeClient(sock_path, timeout_s=1.0).send({"cmd": "drive", "speed": 10, "steer": 0})
        assert r == {"ok": True}
        assert server.requests[-1] == {"cmd": "drive", "speed": 10, "steer": 0}
    finally:
        server.close()


def test_bridge_client_timeout_is_data(sock_path):
    def hang(conn):
        time.sleep(1.0)  # longer than the client's 0.2s timeout
        conn.sendall(b'{"ok":true}\n')

    server = _FakeBridgeServer(sock_path, hang)
    try:
        r = BridgeClient(sock_path, timeout_s=0.2).send({"cmd": "drive", "speed": 0, "steer": 0})
        assert r["ok"] is False and "timeout" in r["error"]
    finally:
        server.close()


def test_bridge_client_missing_socket_is_data(tmp_path):
    missing = str(tmp_path / "nope.sock")
    r = BridgeClient(missing, timeout_s=0.2).send({"cmd": "drive", "speed": 0, "steer": 0})
    assert r["ok"] is False and "unreachable" in r["error"]


def test_bridge_client_malformed_response_is_data(sock_path):
    server = _FakeBridgeServer(sock_path, lambda c: c.sendall(b"not json at all\n"))
    try:
        r = BridgeClient(sock_path, timeout_s=1.0).send({"cmd": "drive", "speed": 0, "steer": 0})
        assert r["ok"] is False and "malformed" in r["error"]
    finally:
        server.close()


# ===========================================================================
# ReflexLoop — fakes for grabber / localai / bridge
# ===========================================================================

class FakeGrabber:
    def __init__(self, frame=None):
        self.frame = frame

    def read_latest(self):
        return self.frame


class FakeLocalAI:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def detect(self, image_b64, min_confidence=0.5):
        self.calls.append((image_b64, min_confidence))
        return self.result


class FakeBridge:
    def __init__(self, result=None):
        self.result = result if result is not None else {"ok": True}
        self.sent = []

    def send(self, cmd):
        self.sent.append(cmd)
        return self.result


def _frame():
    return np.zeros((IMG_H, IMG_W, 3), dtype=np.uint8)


def _wait_until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def test_loop_no_frame_sends_idle():
    bridge = FakeBridge()
    loop = ReflexLoop(FakeGrabber(None), FakeLocalAI({"ok": True, "detections": []}),
                      controller(), bridge, hz=100)
    loop.start()
    try:
        assert _wait_until(lambda: bridge.sent)
        assert bridge.sent[0] == IDLE_CMD
    finally:
        loop.stop()


def test_loop_detect_fail_sends_idle():
    # invariant 4: server unreachable => stop
    bridge = FakeBridge()
    loop = ReflexLoop(FakeGrabber(_frame()), FakeLocalAI({"ok": False, "error": "501"}),
                      controller(), bridge, hz=100)
    loop.start()
    try:
        assert _wait_until(lambda: bridge.sent)
        assert bridge.sent[0] == IDLE_CMD
    finally:
        loop.stop()


def test_loop_detect_ok_pending_drive_forwarded():
    bridge = FakeBridge()
    loop = ReflexLoop(FakeGrabber(_frame()), FakeLocalAI({"ok": True, "detections": []}),
                      controller(), bridge,
                      pending_action=lambda: {"cmd": "drive", "speed": 25, "steer": 0}, hz=100)
    loop.start()
    try:
        assert _wait_until(lambda: bridge.sent)
        assert bridge.sent[0] == {"cmd": "drive", "speed": 25, "steer": 0}
    finally:
        loop.stop()


def test_loop_enabled_center_obstacle_stops():
    bridge = FakeBridge()
    loop = ReflexLoop(
        FakeGrabber(_frame()),
        FakeLocalAI({"ok": True, "detections": [det("person", IMG_W / 2, 0.85 * IMG_H)]}),
        controller(), bridge,
        pending_action=lambda: {"cmd": "drive", "speed": 40, "steer": 0}, hz=100)
    loop.start()
    try:
        assert _wait_until(lambda: bridge.sent)
        assert bridge.sent[0] == IDLE_CMD  # stop pre-empts the pending drive
    finally:
        loop.stop()


def test_loop_disabled_obstacle_ignored():
    # A2: switch off -> the obstacle is ignored, pending drive passes through
    bridge = FakeBridge()
    loop = ReflexLoop(
        FakeGrabber(_frame()),
        FakeLocalAI({"ok": True, "detections": [det("person", IMG_W / 2, 0.85 * IMG_H)]}),
        controller(enabled=False), bridge,
        pending_action=lambda: {"cmd": "drive", "speed": 15, "steer": 0}, hz=100)
    loop.start()
    try:
        assert _wait_until(lambda: bridge.sent)
        assert bridge.sent[0] == {"cmd": "drive", "speed": 15, "steer": 0}
    finally:
        loop.stop()


# --- issue #99: detect() is only called when obstacle avoidance is enabled ---

def test_loop_disabled_detect_not_called():
    """#99: when controller.enabled=False, detect() must never be called —
    a dead/missing detector must not block the VLM path."""
    localai = FakeLocalAI({"ok": False, "error": "501"})
    bridge = FakeBridge()
    loop = ReflexLoop(
        FakeGrabber(_frame()), localai,
        controller(enabled=False), bridge,
        pending_action=lambda: {"cmd": "drive", "speed": 25, "steer": 0},
        hz=100,
    )
    loop.start()
    try:
        assert _wait_until(lambda: bridge.sent)
        assert bridge.sent[0] == {"cmd": "drive", "speed": 25, "steer": 0}
        assert localai.calls == []  # detect() was never called
    finally:
        loop.stop()


def test_loop_disabled_detect_fail_pending_drive_forwarded():
    """#99: disabled + detect() would fail + pending drive → drive forwarded.
    This is the exact scenario that was broken before the fix: the detector
    was required to succeed even when obstacle avoidance was off."""
    localai = FakeLocalAI({"ok": False, "error": "501"})
    bridge = FakeBridge()
    loop = ReflexLoop(
        FakeGrabber(_frame()), localai,
        controller(enabled=False), bridge,
        pending_action=lambda: {"cmd": "drive", "speed": 30, "steer": -10},
        hz=100,
    )
    loop.start()
    try:
        assert _wait_until(lambda: bridge.sent)
        # The pending drive passes through despite detect() failing
        assert bridge.sent[0] == {"cmd": "drive", "speed": 30, "steer": -10}
    finally:
        loop.stop()


def test_loop_disabled_no_pending_sends_idle():
    """#99: disabled + no pending action → idle keep-alive (no crash)."""
    localai = FakeLocalAI({"ok": False, "error": "501"})
    bridge = FakeBridge()
    loop = ReflexLoop(
        FakeGrabber(_frame()), localai,
        controller(enabled=False), bridge,
        hz=100,
    )
    loop.start()
    try:
        assert _wait_until(lambda: bridge.sent)
        assert bridge.sent[0] == IDLE_CMD
    finally:
        loop.stop()


def test_loop_enabled_detect_fail_stops():
    """#99 (invariant 4 preserved): enabled + detect() fails → drive 0 0.
    When obstacle avoidance IS enabled, a dead detector is a hard stop."""
    localai = FakeLocalAI({"ok": False, "error": "501"})
    bridge = FakeBridge()
    loop = ReflexLoop(
        FakeGrabber(_frame()), localai,
        controller(enabled=True), bridge,
        pending_action=lambda: {"cmd": "drive", "speed": 40, "steer": 0},
        hz=100,
    )
    loop.start()
    try:
        assert _wait_until(lambda: bridge.sent)
        assert bridge.sent[0] == IDLE_CMD  # stop pre-empts the pending drive
        assert len(localai.calls) >= 1     # detect() WAS called
    finally:
        loop.stop()


def test_loop_quiet_idle_when_keepalive_disabled():
    # A4: idle_keepalive=False + nothing to do => the loop stays silent
    bridge = FakeBridge()
    loop = ReflexLoop(FakeGrabber(_frame()), FakeLocalAI({"ok": True, "detections": []}),
                      controller(), bridge, hz=100, idle_keepalive=False)
    loop.start()
    try:
        time.sleep(0.15)
        assert bridge.sent == []
    finally:
        loop.stop()


def test_loop_stop_always_sends_final_idle():
    bridge = FakeBridge()
    loop = ReflexLoop(FakeGrabber(None), FakeLocalAI({"ok": True, "detections": []}),
                      controller(), bridge, hz=100)
    loop.start()
    loop.stop()
    assert IDLE_CMD in bridge.sent
    assert bridge.sent[-1] == IDLE_CMD


def test_loop_stop_without_start_sends_idle():
    bridge = FakeBridge()
    loop = ReflexLoop(FakeGrabber(None), FakeLocalAI({"ok": True, "detections": []}),
                      controller(), bridge, hz=100)
    loop.stop()
    assert bridge.sent == [IDLE_CMD]


def test_frame_to_b64_roundtrip_and_none():
    assert frame_to_b64(None) is None
    b = frame_to_b64(np.zeros((4, 4, 3), dtype=np.uint8))
    assert isinstance(b, str) and len(b) > 0
    import base64
    raw = base64.b64decode(b)
    assert raw[:3] == b"\xff\xd8\xff"  # JPEG magic


# ===========================================================================
# Config — the new reflex keys (A1/A2/A3/A4) load and coerce correctly
# ===========================================================================

def test_reflex_config_defaults():
    from brain.config import BrainConfig
    c = BrainConfig()
    assert c.reflex_obstacle_enabled is False          # A2: off until depth (T15)
    assert c.reflex_steer_around == 60                 # A3
    assert c.reflex_lane_half_width == 0.30            # A2
    assert c.reflex_min_bottom_ratio == 0.70           # A2
    assert "person" in c.reflex_block_classes          # A1


def test_reflex_config_yaml_overrides(tmp_path):
    from brain.config import load_config
    y = tmp_path / "brain.yaml"
    y.write_text(
        "reflex_obstacle_enabled: true\n"
        "reflex_steer_around: 40\n"
        "reflex_block_classes: [person, cat]\n"
    )
    c = load_config(path=y, env={})
    assert c.reflex_obstacle_enabled is True
    assert c.reflex_steer_around == 40
    assert c.reflex_block_classes == ("person", "cat")


def test_reflex_block_classes_env_string(tmp_path):
    from brain.config import load_config
    y = tmp_path / "brain.yaml"
    y.write_text("vlm_model: m\n")
    c = load_config(path=y, env={"TANKIE_REFLEX_BLOCK_CLASSES": "person, car, dog"})
    assert c.reflex_block_classes == ("person", "car", "dog")


def test_reflex_obstacle_enabled_env_bool(tmp_path):
    from brain.config import load_config
    y = tmp_path / "brain.yaml"
    y.write_text("vlm_model: m\n")
    c = load_config(path=y, env={"TANKIE_REFLEX_OBSTACLE_ENABLED": "true"})
    assert c.reflex_obstacle_enabled is True
    c2 = load_config(path=y, env={"TANKIE_REFLEX_OBSTACLE_ENABLED": "off"})
    assert c2.reflex_obstacle_enabled is False
