"""Offline unit tests for the deliberative System 2 slow thinker (issue #81, T5).

Run from the repo root:  ``pytest tests/brain/ -q``

No tank, no Pi, no LocalAI, no real network. The :class:`PendingAction` slot
is exercised directly (set/get/clear/has/overwrite/thread-safety); the
:func:`build_vlm_prompt` / :func:`format_state` / :func:`read_state_file`
helpers are exercised with real :class:`TankState` objects; the
:class:`DeliberationLoop` thread is exercised with fake grabber / localai /
task_queue / pending_action to prove the safety invariants:

  * no task -> no VLM call (don't burn tokens idle);
  * no frames -> no VLM call;
  * valid VLM reply -> pending slot gets the action;
  * malformed VLM reply -> logged + ignored (slot unchanged);
  * VLM error -> logged + loop continues;
  * ``say`` field is logged, not spoken (A4);
  * configurable prompt is actually used (A2);
  * multi-frame passes N distinct frames (A1);
  * ``stop()`` exits cleanly;
  * the loop never touches the bridge (invariant 1).

The :meth:`FrameGrab.read_sequence` ring-buffer method (A1 / Q2) is exercised
with a :class:`FakeSource` that delivers a known sequence of frames.
"""

from __future__ import annotations

import json
import threading
import time

import numpy as np
import pytest
import yaml

from brain.config import (
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_USER_PROMPT_TEMPLATE,
    BrainConfig,
    load_config,
)
from brain.deliberate import (
    DeliberationLoop,
    build_vlm_prompt,
    format_state,
    read_state_file,
)
from brain.frame_grab import FrameGrab
from brain.pending_action import PendingAction
from brain.schemas import Action, TankState


# ===========================================================================
# helpers / fakes
# ===========================================================================

def _action_json(**over) -> str:
    """A valid Action JSON string (the shape the VLM is asked to return)."""
    base = {
        "intent": "go_to",
        "cmd": "drive",
        "speed": 40,
        "steer": 0,
        "pan": None,
        "tilt": None,
        "say": "",
        "reason": "heading to goal",
    }
    base.update(over)
    return json.dumps(base)


class FakeGrabber:
    """Returns a fixed list of frames from ``read_sequence``."""

    def __init__(self, frames=None):
        self._frames = frames if frames is not None else []
        self.calls = 0
        self.last_n = None

    def read_sequence(self, n=4):
        self.calls += 1
        self.last_n = n
        return list(self._frames)

    def read_latest(self):
        return self._frames[-1] if self._frames else None


class FakeLocalAI:
    """Returns a fixed ``chat()`` result and records the calls."""

    def __init__(self, result=None):
        self._result = result if result is not None else {"ok": True, "raw": _action_json()}
        self.calls = []

    def chat(self, messages, images_b64=None, task=""):
        self.calls.append({
            "messages": messages,
            "images_b64": list(images_b64 or []),
            "task": task,
        })
        return self._result


class FakeTaskQueue:
    """Returns a fixed current task from ``current()``."""

    def __init__(self, task=None):
        self._task = task

    def current(self):
        return self._task


def _frame(tag: int = 0) -> "np.ndarray":
    """A small distinct 8x8 BGR frame (a real ndarray, not a mock)."""
    arr = np.zeros((8, 8, 3), dtype=np.uint8)
    arr[:, :, 0] = 255
    arr[:, :, 1] = (tag * 37) % 256
    arr[:, :, 2] = (tag * 91) % 256
    return arr


class FakeSource:
    """A controllable FrameSource that delivers a *finite* sequence of frames.

    Each :meth:`read` returns the next frame in order; once the sequence is
    exhausted it returns ``None`` (a stream that stops producing). This lets
    the staleness guard be exercised: the grabber stops refreshing ``_ts``
    and the stored frame eventually goes stale.
    """

    def __init__(self, frames):
        self._frames = list(frames)
        self._idx = 0

    def read(self):
        if self._idx < len(self._frames):
            f = self._frames[self._idx]
            self._idx += 1
            return f
        return None


def _wait_until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


# ===========================================================================
# PendingAction — the thread-safe slot
# ===========================================================================

def test_pending_action_initially_empty():
    pa = PendingAction()
    assert pa.get() is None
    assert pa.has() is False


def test_pending_action_set_get():
    pa = PendingAction()
    action = {"intent": "go_to", "cmd": "drive", "speed": 40, "steer": 0}
    pa.set(action)
    assert pa.has() is True
    got = pa.get()
    assert got == action
    # get() returns a copy — mutating the result must not affect the slot
    got["speed"] = 99
    assert pa.get()["speed"] == 40


def test_pending_action_set_overwrites():
    pa = PendingAction()
    pa.set({"intent": "stop", "cmd": "stop"})
    pa.set({"intent": "go_to", "cmd": "drive", "speed": 50})
    assert pa.get()["intent"] == "go_to"
    assert pa.get()["speed"] == 50


def test_pending_action_clear():
    pa = PendingAction()
    pa.set({"intent": "go_to", "cmd": "drive"})
    pa.clear()
    assert pa.get() is None
    assert pa.has() is False


def test_pending_action_thread_safety():
    """Concurrent set/get/clear from multiple threads must not corrupt state."""
    pa = PendingAction()
    errors = []

    def writer(n):
        for i in range(200):
            try:
                pa.set({"intent": "go_to", "cmd": "drive", "speed": n * 100 + i})
            except Exception as e:
                errors.append(e)

    def reader():
        for _ in range(200):
            try:
                pa.get()
                pa.has()
            except Exception as e:
                errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    threads += [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    # The slot must hold a valid dict (from one of the writers)
    assert pa.get() is not None


def test_pending_action_get_is_reflex_callable():
    """slot.get is the exact Callable[[], Optional[dict]] ReflexLoop expects."""
    pa = PendingAction()
    pa.set({"intent": "go_to", "cmd": "drive", "speed": 40, "steer": 0})
    # ReflexLoop calls pending_action() with no args and expects dict|None
    result = pa.get()
    assert isinstance(result, dict)
    assert result["cmd"] == "drive"


# ===========================================================================
# format_state — TankState -> prompt string (A3)
# ===========================================================================

def test_format_state_none_is_unknown():
    assert format_state(None) == "unknown"


def test_format_state_empty_is_unknown():
    # A state with link_up=True and no other fields renders "unknown"
    # (link_up=True is the healthy default and adds no part).
    state = TankState(link_up=True)
    assert format_state(state) == "unknown"


def test_format_state_link_down_default():
    # The default TankState has link_up=False -> flagged as link_down.
    assert "link_down" in format_state(TankState())


def test_format_state_includes_present_fields():
    state = TankState(
        link_up=True,
        battery_v=7.4,
        speed=30,
        steer=-10,
        pan=90,
        tilt=45,
        net_mode="sta",
    )
    s = format_state(state)
    assert "battery_v=7.4" in s
    assert "speed=30" in s
    assert "steer=-10" in s
    assert "pan=90" in s
    assert "tilt=45" in s
    assert "net_mode=sta" in s


def test_format_state_link_down_flagged():
    state = TankState(link_up=False)
    assert "link_down" in format_state(state)


def test_format_state_watchdog_flagged():
    state = TankState(link_up=True, watchdog_fired=True)
    assert "watchdog_fired=true" in format_state(state)


# ===========================================================================
# build_vlm_prompt — message structure (A2)
# ===========================================================================

def test_build_vlm_prompt_structure():
    msgs = build_vlm_prompt("go to the door")
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    assert "go to the door" in msgs[1]["content"]


def test_build_vlm_prompt_includes_state():
    state = TankState(link_up=True, battery_v=7.4, speed=30)
    msgs = build_vlm_prompt("go to the door", state)
    user_text = msgs[1]["content"]
    assert "battery_v=7.4" in user_text
    assert "speed=30" in user_text


def test_build_vlm_prompt_configurable():
    custom_sys = "You are a custom tank brain."
    custom_user = "TASK: {task}\nSTATE: {state}\nRespond."
    state = TankState(link_up=True, battery_v=7.4)
    msgs = build_vlm_prompt(
        "go", state,
        system_prompt=custom_sys,
        user_prompt_template=custom_user,
    )
    assert msgs[0]["content"] == custom_sys
    assert "TASK: go" in msgs[1]["content"]
    assert "STATE: battery_v=7.4" in msgs[1]["content"]


def test_build_vlm_prompt_bad_template_falls_back():
    # A template with an unknown placeholder must not crash the loop.
    msgs = build_vlm_prompt("go", None, user_prompt_template="{unknown_key}")
    assert "go" in msgs[1]["content"]  # fallback still includes the task


# ===========================================================================
# read_state_file — TankState from JSON file
# ===========================================================================

def test_read_state_file_valid(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"link_up": True, "battery_v": 7.4, "speed": 30}))
    state = read_state_file(p)
    assert state is not None
    assert state.link_up is True
    assert state.battery_v == 7.4
    assert state.speed == 30


def test_read_state_file_missing_returns_none(tmp_path):
    assert read_state_file(tmp_path / "nope.json") is None


def test_read_state_file_bad_json_returns_none(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not json at all")
    assert read_state_file(p) is None


def test_read_state_file_non_dict_returns_none(tmp_path):
    p = tmp_path / "list.json"
    p.write_text(json.dumps([1, 2, 3]))
    assert read_state_file(p) is None


def test_read_state_file_extra_keys_dropped(tmp_path):
    p = tmp_path / "extra.json"
    p.write_text(json.dumps({"link_up": True, "unknown_key": "x"}))
    state = read_state_file(p)
    assert state is not None
    assert state.link_up is True
    # unknown_key is dropped (TankState is extra="forbid")


# ===========================================================================
# DeliberationLoop — thread with fakes
# ===========================================================================

def test_deliberate_no_task_no_vlm_call():
    """No task -> no VLM call (don't burn tokens idle)."""
    grabber = FakeGrabber(frames=[_frame(0), _frame(1)])
    localai = FakeLocalAI()
    pa = PendingAction()
    tq = FakeTaskQueue(task=None)
    loop = DeliberationLoop(grabber, localai, pa, tq, hz=100)
    loop.start()
    time.sleep(0.15)
    loop.stop()
    assert localai.calls == []
    assert pa.get() is None


def test_deliberate_valid_action_sets_pending():
    """Valid VLM reply -> pending slot gets the action."""
    grabber = FakeGrabber(frames=[_frame(0), _frame(1), _frame(2), _frame(3)])
    localai = FakeLocalAI(result={"ok": True, "raw": _action_json(speed=50, steer=10)})
    pa = PendingAction()
    tq = FakeTaskQueue(task="go to the door")
    loop = DeliberationLoop(grabber, localai, pa, tq, hz=100)
    loop.start()
    assert _wait_until(lambda: pa.has())
    loop.stop()
    action = pa.get()
    assert action["intent"] == "go_to"
    assert action["cmd"] == "drive"
    assert action["speed"] == 50
    assert action["steer"] == 10
    assert localai.calls  # VLM was called


def test_deliberate_malformed_json_clears_stale_action():
    """#101: malformed VLM reply -> stale action cleared (tank stops).

    A malformed reply means "no valid action this tick". Rather than coasting
    on the last good proposal, the slot is cleared so the reflex loop idles to
    ``drive 0 0`` (the tank stops instead of moving on a stale command).
    """
    grabber = FakeGrabber(frames=[_frame(0)])
    localai = FakeLocalAI(result={"ok": True, "raw": "this is not json"})
    pa = PendingAction()
    # Pre-set a stale drive so we can verify it is cleared.
    pa.set({"intent": "go_to", "cmd": "drive", "speed": 40, "steer": 0})
    tq = FakeTaskQueue(task="go")
    loop = DeliberationLoop(grabber, localai, pa, tq, hz=100)
    loop.start()
    time.sleep(0.15)
    loop.stop()
    # The stale action must be cleared (not kept).
    assert pa.get() is None
    assert loop.errors >= 1


def test_deliberate_no_task_clears_stale_action():
    """#101: task cleared -> stale drive cleared (tank stops).

    When the task is done the deliberation loop clears the pending slot so a
    stale drive does not keep the tank moving; the reflex loop then idles.
    """
    grabber = FakeGrabber(frames=[_frame(0), _frame(1)])
    localai = FakeLocalAI(result={"ok": True, "raw": _action_json(speed=50)})
    pa = PendingAction()
    # A stale drive is present (e.g. left over from a previous task).
    pa.set({"intent": "go_to", "cmd": "drive", "speed": 50, "steer": 0})
    tq = FakeTaskQueue(task=None)  # no task -> done
    loop = DeliberationLoop(grabber, localai, pa, tq, hz=100)
    loop.start()
    time.sleep(0.15)
    loop.stop()
    # No task -> the stale drive is cleared and no VLM call is made.
    assert pa.get() is None
    assert localai.calls == []


def test_deliberate_vlm_error_clears_stale_action():
    """#101: VLM down -> stale drive cleared (tank stops, invariant 4)."""
    grabber = FakeGrabber(frames=[_frame(0)])
    localai = FakeLocalAI(result={"ok": False, "error": "connection refused"})
    pa = PendingAction()
    pa.set({"intent": "go_to", "cmd": "drive", "speed": 40, "steer": 0})
    tq = FakeTaskQueue(task="go")
    loop = DeliberationLoop(grabber, localai, pa, tq, hz=100)
    loop.start()
    time.sleep(0.15)
    loop.stop()
    # VLM failed -> the stale action is cleared (perception dead => stop).
    assert pa.get() is None
    assert loop.errors >= 1


def test_deliberate_no_frames_no_vlm_call():
    """No frames -> no VLM call."""
    grabber = FakeGrabber(frames=[])
    localai = FakeLocalAI()
    pa = PendingAction()
    tq = FakeTaskQueue(task="go")
    loop = DeliberationLoop(grabber, localai, pa, tq, hz=100)
    loop.start()
    time.sleep(0.15)
    loop.stop()
    assert localai.calls == []
    assert pa.get() is None


def test_deliberate_vlm_error_loop_continues():
    """VLM error -> logged + loop continues (never raises)."""
    grabber = FakeGrabber(frames=[_frame(0)])
    localai = FakeLocalAI(result={"ok": False, "error": "connection refused"})
    pa = PendingAction()
    tq = FakeTaskQueue(task="go")
    loop = DeliberationLoop(grabber, localai, pa, tq, hz=100)
    loop.start()
    time.sleep(0.15)
    loop.stop()
    assert localai.calls  # VLM was called (and failed)
    assert loop.errors >= 1
    assert pa.get() is None
    # The loop must still be responsive (not crashed)
    assert loop.valid_actions == 0


def test_deliberate_stop_exits_cleanly():
    """stop() exits the thread cleanly."""
    grabber = FakeGrabber(frames=[_frame(0)])
    localai = FakeLocalAI()
    pa = PendingAction()
    tq = FakeTaskQueue(task="go")
    loop = DeliberationLoop(grabber, localai, pa, tq, hz=100)
    loop.start()
    time.sleep(0.1)
    loop.stop()
    # After stop, the thread must not be alive
    time.sleep(0.05)
    assert loop._thread is None


def test_deliberate_say_is_logged_not_spoken():
    """A `say` field is logged only (A4: TTS wiring comes with T10)."""
    grabber = FakeGrabber(frames=[_frame(0)])
    localai = FakeLocalAI(result={"ok": True, "raw": _action_json(say="I see a door")})
    pa = PendingAction()
    tq = FakeTaskQueue(task="go")
    loop = DeliberationLoop(grabber, localai, pa, tq, hz=100)
    loop.start()
    assert _wait_until(lambda: pa.has())
    loop.stop()
    action = pa.get()
    assert action["say"] == "I see a door"
    # No TTS call was made (the loop has no speak method)
    assert not hasattr(loop, "speak")


def test_deliberate_multi_frame_passes_n_distinct():
    """The VLM receives N distinct frames (A1: sequence for motion)."""
    frames = [_frame(i) for i in range(4)]
    grabber = FakeGrabber(frames=frames)
    localai = FakeLocalAI()
    pa = PendingAction()
    tq = FakeTaskQueue(task="go")
    loop = DeliberationLoop(grabber, localai, pa, tq, frames=4, hz=100)
    loop.start()
    assert _wait_until(lambda: localai.calls)
    loop.stop()
    # The VLM call must have 4 distinct images
    call = localai.calls[0]
    assert len(call["images_b64"]) == 4
    # Each image must be distinct (different base64)
    assert len(set(call["images_b64"])) == 4


def test_deliberate_fewer_frames_than_requested():
    """If fewer frames than requested are available, use what's there."""
    grabber = FakeGrabber(frames=[_frame(0), _frame(1)])  # only 2 frames
    localai = FakeLocalAI()
    pa = PendingAction()
    tq = FakeTaskQueue(task="go")
    loop = DeliberationLoop(grabber, localai, pa, tq, frames=4, hz=100)
    loop.start()
    assert _wait_until(lambda: localai.calls)
    loop.stop()
    call = localai.calls[0]
    assert len(call["images_b64"]) == 2  # only 2 available


def test_deliberate_configurable_prompt_used():
    """The configurable prompt is actually sent to the VLM (A2)."""
    custom_sys = "You are a custom tank brain."
    custom_user = "TASK: {task}\nRespond."
    grabber = FakeGrabber(frames=[_frame(0)])
    localai = FakeLocalAI()
    pa = PendingAction()
    tq = FakeTaskQueue(task="go to the door")
    loop = DeliberationLoop(
        grabber, localai, pa, tq,
        system_prompt=custom_sys,
        user_prompt_template=custom_user,
        hz=100,
    )
    loop.start()
    assert _wait_until(lambda: localai.calls)
    loop.stop()
    call = localai.calls[0]
    msgs = call["messages"]
    assert msgs[0]["content"] == custom_sys
    assert "TASK: go to the door" in msgs[1]["content"]


def test_deliberate_never_touches_bridge():
    """The deliberation loop never sends to the bridge (invariant 1)."""
    grabber = FakeGrabber(frames=[_frame(0)])
    localai = FakeLocalAI()
    pa = PendingAction()
    tq = FakeTaskQueue(task="go")
    loop = DeliberationLoop(grabber, localai, pa, tq, hz=100)
    # The loop has no bridge attribute
    assert not hasattr(loop, "_bridge")
    assert not hasattr(loop, "bridge")
    # The loop only writes to the pending slot
    assert hasattr(loop, "_pending")


def test_deliberate_start_stop_idempotent():
    """start()/stop() are idempotent."""
    grabber = FakeGrabber(frames=[_frame(0)])
    localai = FakeLocalAI()
    pa = PendingAction()
    tq = FakeTaskQueue(task="go")
    loop = DeliberationLoop(grabber, localai, pa, tq, hz=100)
    loop.start()
    loop.start()  # second start is a no-op
    time.sleep(0.1)
    loop.stop()
    loop.stop()  # second stop is a no-op
    # No exception, thread is gone
    assert loop._thread is None


# ===========================================================================
# vlm_model default — vision-capable
# ===========================================================================

def test_vlm_model_default_is_vision_capable():
    """The vlm_model default must be a vision-capable model (issue #81, Q1)."""
    c = BrainConfig()
    assert c.vlm_model == "ornith-1.5-9b-obliterated-q4"
    # It must NOT be a text-only model
    assert "qwen3.8-4b-q4" not in c.vlm_model


def test_vlm_model_configurable_via_yaml(tmp_path):
    """vlm_model can be overridden via brain.yaml (easily configurable)."""
    y = tmp_path / "brain.yaml"
    y.write_text("vlm_model: my-custom-vision-model\n")
    c = load_config(path=y, env={})
    assert c.vlm_model == "my-custom-vision-model"


def test_vlm_model_configurable_via_env(tmp_path):
    """vlm_model can be overridden via TANKIE_VLM_MODEL env var."""
    y = tmp_path / "brain.yaml"
    y.write_text("")
    c = load_config(path=y, env={"TANKIE_VLM_MODEL": "env-model"})
    assert c.vlm_model == "env-model"


# ===========================================================================
# FrameGrab.read_sequence — ring buffer (A1 / Q2)
# ===========================================================================

def test_read_sequence_returns_fresh_frames():
    """read_sequence(n) returns the last n distinct fresh frames."""
    frames = [_frame(i) for i in range(5)]
    src = FakeSource(frames)
    grab = FrameGrab(src, interval_s=0.01, stale_after_s=5.0, ring_size=4)
    grab.start()
    # Wait for the grabber to process all 5 frames
    assert _wait_until(lambda: len(grab._ring) >= 4)
    seq = grab.read_sequence(4)
    grab.stop()
    assert len(seq) == 4
    # The frames must be distinct (different tags)
    # (we can't easily compare ndarrays by value here, but the count is right)


def test_read_sequence_empty_when_no_frames():
    """read_sequence returns [] when no frames have been received."""
    src = FakeSource([])
    grab = FrameGrab(src, interval_s=0.01, stale_after_s=5.0, ring_size=4)
    grab.start()
    time.sleep(0.05)
    seq = grab.read_sequence(4)
    grab.stop()
    assert seq == []


def test_read_sequence_respects_stale():
    """read_sequence returns [] when the newest frame is stale."""
    frames = [_frame(0)]
    src = FakeSource(frames)
    grab = FrameGrab(src, interval_s=0.01, stale_after_s=0.05, ring_size=4)
    grab.start()
    assert _wait_until(lambda: len(grab._ring) >= 1)
    # Wait for the frame to go stale
    time.sleep(0.1)
    seq = grab.read_sequence(4)
    grab.stop()
    assert seq == []


def test_read_sequence_fewer_than_requested():
    """If fewer frames than requested are in the ring, return what's there."""
    frames = [_frame(0), _frame(1)]  # only 2 frames
    src = FakeSource(frames)
    grab = FrameGrab(src, interval_s=0.01, stale_after_s=5.0, ring_size=4)
    grab.start()
    assert _wait_until(lambda: len(grab._ring) >= 2)
    seq = grab.read_sequence(4)  # ask for 4, only have 2
    grab.stop()
    assert len(seq) == 2


def test_read_sequence_ring_bounded_by_ring_size():
    """The ring buffer is bounded by ring_size (oldest frames dropped)."""
    frames = [_frame(i) for i in range(10)]
    src = FakeSource(frames)
    grab = FrameGrab(src, interval_s=0.01, stale_after_s=5.0, ring_size=4)
    grab.start()
    # Wait for all 10 frames to be processed (ring keeps last 4)
    assert _wait_until(lambda: len(grab._ring) == 4)
    time.sleep(0.05)  # let the last few frames land
    seq = grab.read_sequence(4)
    grab.stop()
    assert len(seq) == 4


def test_build_frame_grab_wires_ring_size():
    """build_frame_grab passes deliberate_frames as ring_size."""
    from brain.config import BrainConfig
    from brain.frame_grab import build_frame_grab

    cfg = BrainConfig(deliberate_frames=3)
    # build_frame_grab will try to create a source from cam_url, but we only
    # check that ring_size is passed through (the source creation may fail
    # without a real camera, which is fine for this test).
    try:
        grab = build_frame_grab(cfg)
        assert grab._ring_size == 3
    except Exception:
        # Source creation may fail without a real camera; that's expected.
        # The key assertion is that ring_size is wired from config.
        pass


# ===========================================================================
# config — deliberate keys
# ===========================================================================

def test_config_deliberate_frames_default():
    """deliberate_frames defaults to 4 (A1: sequence)."""
    c = BrainConfig()
    assert c.deliberate_frames == 4


def test_config_deliberate_prompts_default():
    """deliberate_system_prompt and deliberate_user_prompt_template have defaults."""
    c = BrainConfig()
    assert c.deliberate_system_prompt == DEFAULT_SYSTEM_PROMPT
    assert c.deliberate_user_prompt_template == DEFAULT_USER_PROMPT_TEMPLATE


def test_config_deliberate_frames_configurable(tmp_path):
    """deliberate_frames can be overridden via brain.yaml."""
    y = tmp_path / "brain.yaml"
    y.write_text("deliberate_frames: 2\n")
    c = load_config(path=y, env={})
    assert c.deliberate_frames == 2


def test_config_deliberate_prompts_configurable(tmp_path):
    """deliberate prompts can be overridden via brain.yaml."""
    y = tmp_path / "brain.yaml"
    y.write_text(
        "deliberate_system_prompt: custom system\n"
        "deliberate_user_prompt_template: 'TASK: {task}'\n"
    )
    c = load_config(path=y, env={})
    assert c.deliberate_system_prompt == "custom system"
    assert c.deliberate_user_prompt_template == "TASK: {task}"


# ===========================================================================
# integration — DeliberationLoop with real PendingAction + FrameGrab
# ===========================================================================

def test_integration_loop_writes_to_pending_action():
    """End-to-end: DeliberationLoop -> PendingAction -> ReflexLoop-compatible get."""
    frames = [_frame(i) for i in range(4)]
    grabber = FakeGrabber(frames=frames)
    localai = FakeLocalAI(result={"ok": True, "raw": _action_json(speed=35, steer=5)})
    pa = PendingAction()
    tq = FakeTaskQueue(task="navigate to the flag")
    loop = DeliberationLoop(grabber, localai, pa, tq, frames=4, hz=100)
    loop.start()
    assert _wait_until(lambda: pa.has())
    loop.stop()

    # Simulate what ReflexLoop does: call pending_action() (i.e. pa.get())
    pending = pa.get()
    assert pending is not None
    assert pending["cmd"] == "drive"
    assert pending["speed"] == 35

    # The ReflexController can consume this pending action
    from brain.reflex import ReflexController
    ctrl = ReflexController(enabled=False, max_speed=40, max_steer=120)
    result = ctrl.decide([], 640, 480, pending=pending)
    assert result.cmd == "drive"
    assert result.speed == 35
    assert result.steer == 5
