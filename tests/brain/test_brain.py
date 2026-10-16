"""Offline unit tests for the Brain orchestrator (issue #82, T6).

Run from the repo root:  ``pytest tests/brain/ -q``

No tank, no Pi, no LocalAI, no real network. Every component is a fake
(injected via the Brain constructor — the T2/T3 injection pattern), so the
suite proves the **wiring and lifecycle**, not the loops:

  * start/stop call order (bridge → grabber → localai → reflex → deliberation;
    stop is the reverse, reflex sends a final ``drive 0 0``);
  * ``set_task`` / ``clear_task`` delegate to the TaskQueue **and** the
    task file (Q5/A5);
  * ``run(once=True)`` settles for one bounded cycle then stops cleanly;
  * ``run()`` blocks until ``stop()``;
  * ``state()`` returns the full snapshot (A3/Q7);
  * partial failure — one component that raises on start is skipped, the
    brain continues (invariant 4);
  * the external task file is bridged into the in-memory TaskQueue (Q5/A5);
  * ``read_tank_state`` reads the nested ``last_state`` (Q6/A6);
  * ``read_task_file`` / ``write_task_file`` round-trip.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from brain.brain import Brain, read_task_file, write_task_file
from brain.config import BrainConfig
from brain.deliberate import read_tank_state
from brain.pending_action import PendingAction
from brain.task_queue import TaskQueue


# ===========================================================================
# fakes
# ===========================================================================
class CallLog:
    """A shared, thread-safe ordered log of lifecycle calls."""

    def __init__(self):
        self._lock = threading.Lock()
        self.calls = []

    def record(self, name: str) -> None:
        with self._lock:
            self.calls.append(name)

    def names(self) -> list:
        with self._lock:
            return list(self.calls)


class FakeBridge:
    def __init__(self, log: CallLog):
        self._log = log
        self.sent = []

    def send(self, cmd):
        self._log.record(f"bridge.send:{cmd.get('cmd')}")
        self.sent.append(cmd)
        return {"ok": True}


class FakeGrabber:
    def __init__(self, log: CallLog):
        self._log = log
        self.started = False

    def start(self):
        self._log.record("grabber.start")
        self.started = True

    def stop(self):
        self._log.record("grabber.stop")
        self.started = False

    def read_latest(self):
        return None

    def read_sequence(self, n=4):
        return []


class FakeLocalAI:
    def __init__(self, log: CallLog):
        self._log = log
        self.closed = False

    def close(self):
        self._log.record("localai.close")
        self.closed = True


class FakeReflex:
    def __init__(self, log: CallLog, *, fail_start: bool = False):
        self._log = log
        self.started = False
        self._fail = fail_start
        self.ticks = 0
        self.stops_sent = 0
        self._last = None

    @property
    def last_result(self):
        return self._last

    def start(self):
        if self._fail:
            raise RuntimeError("reflex start failed (injected)")
        self._log.record("reflex.start")
        self.started = True

    def stop(self):
        self._log.record("reflex.stop")
        self.started = False
        # A real ReflexLoop.stop() sends a final drive 0 0 (invariant 2).
        self.stops_sent += 1
        self._last = {"cmd": "drive", "speed": 0, "steer": 0,
                      "preempted": False, "reason": "idle"}


class FakeDeliberation:
    def __init__(self, log: CallLog, *, fail_start: bool = False):
        self._log = log
        self.started = False
        self._fail = fail_start
        self.calls = 0
        self.errors = 0
        self.valid_actions = 0

    def start(self):
        if self._fail:
            raise RuntimeError("deliberation start failed (injected)")
        self._log.record("deliberation.start")
        self.started = True

    def stop(self):
        self._log.record("deliberation.stop")
        self.started = False


def _cfg(tmp_path, *, task_file=None, state_file=None) -> BrainConfig:
    cfg = BrainConfig()
    # Point the file-backed paths at temp locations (never the real /var/lib).
    cfg = BrainConfig(
        **{
            **cfg.__dict__,
            "task_file": (str(tmp_path / "task.json") if task_file is not None
                          else str(tmp_path / "task.json")),
            "state_file": (str(tmp_path / "state.json") if state_file is not None
                           else str(tmp_path / "state.json")),
        }
    )
    return cfg


def _build_brain(tmp_path, log: CallLog, *, reflex_fail=False, deliberation_fail=False):
    cfg = _cfg(tmp_path)
    bridge = FakeBridge(log)
    grabber = FakeGrabber(log)
    localai = FakeLocalAI(log)
    reflex = FakeReflex(log, fail_start=reflex_fail)
    deliberation = FakeDeliberation(log, fail_start=deliberation_fail)
    brain = Brain(
        cfg,
        bridge=bridge,
        grabber=grabber,
        localai=localai,
        reflex_loop=reflex,
        deliberation_loop=deliberation,
        task_queue=TaskQueue(),
        pending_action=PendingAction(),
    )
    return brain, bridge, grabber, localai, reflex, deliberation


def _wait_until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


# ===========================================================================
# start / stop order
# ===========================================================================
def test_brain_start_stop_order(tmp_path):
    log = CallLog()
    brain, bridge, grabber, localai, reflex, deliberation = _build_brain(tmp_path, log)

    brain.start()
    # Start order: grabber -> reflex -> deliberation (bridge/localai have no start()).
    assert log.names() == ["grabber.start", "reflex.start", "deliberation.start"]

    brain.stop()
    # Stop order (reverse): deliberation -> reflex -> grabber, then localai.close().
    assert log.names() == [
        "grabber.start", "reflex.start", "deliberation.start",
        "deliberation.stop", "reflex.stop", "grabber.stop", "localai.close",
    ]
    # The reflex loop sent a final stop (invariant 2) — the tank is not coasting.
    assert reflex.stops_sent == 1


def test_brain_start_is_idempotent(tmp_path):
    log = CallLog()
    brain, *_ = _build_brain(tmp_path, log)
    brain.start()
    brain.start()  # second start is a no-op
    assert log.names().count("grabber.start") == 1
    brain.stop()


def test_brain_stop_is_idempotent(tmp_path):
    log = CallLog()
    brain, *_ = _build_brain(tmp_path, log)
    brain.start()
    brain.stop()
    brain.stop()  # second stop is a no-op
    assert log.names().count("deliberation.stop") == 1


# ===========================================================================
# set_task / clear_task (invariant 3 + Q5/A5 task file)
# ===========================================================================
def test_brain_set_task_delegates_to_queue_and_file(tmp_path):
    log = CallLog()
    brain, *_ = _build_brain(tmp_path, log)
    brain.set_task("go find the red ball")

    # In-memory TaskQueue (the deliberation loop's source).
    assert brain.task_queue.current() == "go find the red ball"
    assert brain.task_queue.has() is True

    # And the cross-process task file (Q5/A5).
    task_file = tmp_path / "task.json"
    assert task_file.exists()
    data = json.loads(task_file.read_text())
    assert data["task"] == "go find the red ball"
    assert "updated_at" in data


def test_brain_clear_task_clears_queue_and_file(tmp_path):
    log = CallLog()
    brain, *_ = _build_brain(tmp_path, log)
    brain.set_task("something")
    brain.clear_task()

    assert brain.task_queue.current() is None
    assert brain.task_queue.has() is False
    data = json.loads((tmp_path / "task.json").read_text())
    assert data["task"] is None


# ===========================================================================
# run() / run(once=True)
# ===========================================================================
def test_brain_run_once_settles_and_stops(tmp_path):
    log = CallLog()
    brain, *_ = _build_brain(tmp_path, log)
    brain.start()

    started = time.monotonic()
    brain.run(once=True)
    elapsed = time.monotonic() - started

    # once=True settles for a bounded cycle (>= the settle window) then stops.
    assert elapsed >= 1.0
    # The reflex loop stopped (final drive 0 0).
    assert log.names().count("reflex.stop") == 1
    assert brain._started is False


def test_brain_run_blocks_until_stop(tmp_path):
    log = CallLog()
    brain, *_ = _build_brain(tmp_path, log)
    brain.start()

    stopped = []

    def runner():
        brain.run()  # blocks until stop()
        stopped.append(True)

    t = threading.Thread(target=runner, daemon=True)
    t.start()

    # It must still be running after a short wait (it blocks).
    time.sleep(0.5)
    assert stopped == []

    brain.stop()
    t.join(timeout=3.0)
    assert stopped == [True]


def test_brain_run_signal_triggers_clean_stop(tmp_path):
    """SIGTERM (as the T12 systemd service sends) triggers a clean stop."""
    import os
    import signal

    log = CallLog()
    brain, *_ = _build_brain(tmp_path, log)
    brain.start()

    brain._install_signal_handlers()
    assert brain._signal_handlers_installed is True

    # Simulate the systemd SIGTERM.
    os.kill(os.getpid(), signal.SIGTERM)
    assert _wait_until(lambda: brain._stop_event.is_set(), timeout=2.0)
    brain.stop()


# ===========================================================================
# state() (A3 / Q7)
# ===========================================================================
def test_brain_state_returns_full_dict(tmp_path):
    log = CallLog()
    brain, *_ = _build_brain(tmp_path, log)
    brain.start()
    brain.set_task("find the ball")

    state = brain.state()
    assert isinstance(state, dict)
    # The documented shape (Q7).
    for key in ("tank", "task", "last_reflex", "last_deliberation", "liveness", "config"):
        assert key in state
    assert state["task"] == "find the ball"
    assert isinstance(state["liveness"], dict)
    assert isinstance(state["config"], dict)
    assert state["config"]["vlm_model"]
    brain.stop()


def test_brain_state_tank_reads_nested_last_state(tmp_path):
    """state().tank uses read_tank_state (Q6/A6) — the nested last_state."""
    log = CallLog()
    brain, *_ = _build_brain(tmp_path, log)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "link_up": True,
        "watchdog_fired": False,
        "updated_at": "2026-10-14T15:11:47+00:00",
        "last_state": {
            "type": "state", "seq": 3, "battery": 12.97, "speed": 0, "steer": 0,
            "pan": 90, "tilt": 90, "net_mode": "sta", "net_ip": "192.0.2.42",
        },
    }))

    state = brain.state()
    tank = state["tank"]
    assert tank is not None
    # The nested broadcast is surfaced (Q6/A6).
    assert tank["battery_v"] == 12.97
    assert tank["pan"] == 90
    assert tank["net_mode"] == "sta"
    assert tank["link_up"] is True


# ===========================================================================
# partial failure (invariant 4)
# ===========================================================================
def test_brain_start_partial_failure_reflex(tmp_path):
    """A reflex loop that fails to start is skipped; the brain continues."""
    log = CallLog()
    brain, bridge, grabber, localai, reflex, deliberation = _build_brain(
        tmp_path, log, reflex_fail=True
    )
    # Must not raise.
    brain.start()

    # The reflex loop did NOT start (it raised); the others did.
    assert reflex.started is False
    assert grabber.started is True
    assert deliberation.started is True
    # The reflex loop was skipped, so it is not in the stop order.
    assert log.names().count("reflex.stop") == 0
    brain.stop()
    # The components that did start are stopped.
    assert log.names().count("deliberation.stop") == 1
    assert log.names().count("grabber.stop") == 1


def test_brain_start_partial_failure_deliberation(tmp_path):
    log = CallLog()
    brain, bridge, grabber, localai, reflex, deliberation = _build_brain(
        tmp_path, log, deliberation_fail=True
    )
    brain.start()
    assert deliberation.started is False
    assert reflex.started is True
    assert grabber.started is True
    brain.stop()
    assert log.names().count("deliberation.stop") == 0
    assert log.names().count("reflex.stop") == 1


# ===========================================================================
# task-file poller (Q5/A5: the cross-process channel)
# ===========================================================================
def test_brain_picks_up_external_task_file(tmp_path):
    """A task written to the file (by brain_cli / MCP) is bridged in-memory."""
    log = CallLog()
    brain, *_ = _build_brain(tmp_path, log)
    brain.start()

    # Simulate brain_cli.py / the MCP server writing the task file.
    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps({"task": "external task", "updated_at": "now"}))

    # The poller (0.5 s interval) bridges it into the in-memory TaskQueue.
    assert _wait_until(lambda: brain.task_queue.current() == "external task", timeout=3.0)
    brain.stop()


def test_brain_honours_external_task_clear(tmp_path):
    log = CallLog()
    brain, *_ = _build_brain(tmp_path, log)
    brain.start()
    brain.set_task("in-process task")
    assert brain.task_queue.current() == "in-process task"

    # An external clear (task file -> null) is honoured.
    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps({"task": None, "updated_at": "now"}))
    assert _wait_until(lambda: brain.task_queue.current() is None, timeout=3.0)
    brain.stop()


# ===========================================================================
# read_tank_state (Q6/A6)
# ===========================================================================
def test_read_tank_state_nested_last_state(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({
        "link_up": True, "watchdog_fired": False,
        "updated_at": "2026-10-14T15:11:47+00:00",
        "last_state": {"battery": 12.97, "speed": 5, "steer": -3, "pan": 90,
                       "tilt": 45, "net_mode": "sta", "net_ip": "192.0.2.42"},
    }))
    state = read_tank_state(p)
    assert state is not None
    assert state.battery_v == 12.97
    assert state.speed == 5
    assert state.steer == -3
    assert state.pan == 90
    assert state.tilt == 45
    assert state.net_mode == "sta"
    assert state.link_up is True


def test_read_tank_state_top_level_wins(tmp_path):
    """Top-level keys (the bridge's own summary) take precedence."""
    p = tmp_path / "state.json"
    p.write_text(json.dumps({
        "link_up": True,
        "battery_v": 99.0,  # top-level override
        "last_state": {"battery": 12.97},
    }))
    state = read_tank_state(p)
    assert state is not None
    assert state.battery_v == 99.0
    assert state.link_up is True


def test_read_tank_state_missing_returns_none(tmp_path):
    assert read_tank_state(tmp_path / "nope.json") is None


def test_read_tank_state_bad_json_returns_none(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not json")
    assert read_tank_state(p) is None


def test_read_tank_state_non_dict_returns_none(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("[1, 2, 3]")
    assert read_tank_state(p) is None


# ===========================================================================
# task file helpers (Q5/A5)
# ===========================================================================
def test_write_then_read_task_file_roundtrip(tmp_path):
    p = tmp_path / "task.json"
    assert write_task_file(p, "go find the red ball") is True
    assert read_task_file(p) == "go find the red ball"


def test_write_task_file_clear(tmp_path):
    p = tmp_path / "task.json"
    write_task_file(p, "a task")
    assert write_task_file(p, None) is True
    assert read_task_file(p) is None


def test_read_task_file_missing_returns_none(tmp_path):
    assert read_task_file(tmp_path / "nope.json") is None


def test_read_task_file_bad_json_returns_none(tmp_path):
    p = tmp_path / "task.json"
    p.write_text("not json")
    assert read_task_file(p) is None


def test_read_task_file_non_dict_returns_none(tmp_path):
    p = tmp_path / "task.json"
    p.write_text("[1,2,3]")
    assert read_task_file(p) is None


def test_read_task_file_empty_task_returns_none(tmp_path):
    p = tmp_path / "task.json"
    p.write_text(json.dumps({"task": "   "}))
    assert read_task_file(p) is None


def test_write_task_file_creates_parent_dir(tmp_path):
    p = tmp_path / "nested" / "dir" / "task.json"
    assert write_task_file(p, "task") is True
    assert p.exists()
    assert read_task_file(p) == "task"


def test_brain_state_liveness_includes_frame_freshness(tmp_path):
    """state().liveness surfaces the frame age (issue #109)."""
    log = CallLog()
    brain, *_ = _build_brain(tmp_path, log)
    state = brain.state()
    liveness = state["liveness"]
    assert "frame_age_s" in liveness
    assert "frame_frozen_s" in liveness
    # No camera in this offline test: the grabber has no frame yet.
    assert liveness["frame_age_s"] is None
    assert liveness["frame_frozen_s"] is None
