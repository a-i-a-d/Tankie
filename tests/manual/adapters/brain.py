"""AI brain adapter (issue #80 T4 reflex + issue #81 T5 deliberation + issue #82 T6 orchestrator) — Stage 6.

This adapter drives the **real** brain components so the Phase 1 + Phase 2
safety invariants are machine-verifiable — without needing the tank in a
particular spot, and (in ``full`` mode with fakes) without any hardware at all.

Two execution modes (selected per-test via ``cmd["mode"]``):

* ``reflex`` (a.k.a. ``record`` / ``tank``) — run the **reflex loop** (T4)
  with a recording bridge and assert the reflex safety invariants:
    * invariant 2 — stop pre-empts (a pending drive never overrides a stop)
    * invariant 4 — no frame / detect failure / dead bridge => ``drive 0 0``
    * A4          — idle keep-alive ``drive 0 0`` every tick
    * stop()      — one final ``drive 0 0`` before the loop exits
  This is the pre-Phase-2 behaviour (S6-01…S6-06).

* ``full`` — run the **full :class:`~brain.brain.Brain` orchestrator** (T6):
  reflex loop (T4) + deliberation loop (T5) + task queue (T6) + pending-action
  slot, started/stopped in the safe order. This is what Phase 2 added, and it
  is what the new deliberation / orchestrator / task-file / consume-once /
  frozen-camera tests exercise.

``full`` mode accepts a ``fakes`` flag:
  * ``fakes: true``  — inject a fresh-frame grabber, a scripted LocalAI client,
                       and a recording bridge. The whole pipeline (reflex +
                       deliberation + task + invariants) is then **verifiable
                       offline** (no tank, no camera, no LocalAI server).
  * ``fakes: false`` — the real components (bridge socket + camera + LocalAI),
                       i.e. the live on-tank run (gated by ``--i-confirm-tank-is-clear``).

Config keys (all optional; defaults come from ``BrainConfig`` / ``brain.yaml``):
    duration_s    — how long to run the loop(s), default 3.0
    task          — a task string to set (in-process, via ``Brain.set_task``)
    task_file_write — a task string to write to the task **file** (the
                      cross-process channel, i.e. what ``brain_cli.py`` does)
    fakes         — ``true`` = offline fakes (full mode), default ``true``
    frames        — fakes: deliver fresh frames (default ``true``); ``false`` =
                    no frame (invariant 4)
    chat_reply    — fakes: the raw VLM reply JSON (default a valid drive action)
    chat_ok       — fakes: VLM call succeeds (default ``true``); ``false`` =
                    VLM down (issue #101: stale action cleared)
    detect        — fakes: a detection list to return (for obstacle tests)
    obstacle      — reflex_obstacle_enabled override (True/False)
    cam_url       — override the camera URL (to force a "no frame" test)
    localai_url   — override the LocalAI URL (to force a "detect failure" test)

Requires the brain deps (``raspberry_pi/brain/requirements.txt``). Degrades to
a ``{"ok": False, "error": ...}`` reply if the brain package or a component is
unavailable, so the operator always gets a verdict prompt.
"""

from __future__ import annotations

import collections
import dataclasses
import os
import sys
import threading
import time
from typing import Any, List, Mapping, Optional, Tuple

from .base import Adapter

DEFAULT_DURATION_S = 3.0


# ---------------------------------------------------------------------------
# Offline fakes (full mode) — just enough surface for the brain to run.
# ---------------------------------------------------------------------------
class _RecordingBridge:
    """A bridge double that records every command the brain sends."""

    def __init__(self):
        self.commands: List[dict] = []

    def send(self, cmd: Mapping[str, Any]) -> dict:
        self.commands.append(dict(cmd))
        return {"ok": True}


class _FakeGrabber:
    """A fresh-frame source (or a dead one when ``frames=False``).

    Satisfies the grabber surface the brain uses: ``read_latest()`` (reflex),
    ``read_sequence(n)`` (deliberation), ``age_s()`` / ``frozen_s()`` (#109),
    ``start()`` / ``stop()``.
    """

    def __init__(self, frames: bool = True, interval_s: float = 0.1):
        import numpy as np  # lazy: keeps the adapter importable without cv2/numpy

        self._np = np
        self._frames = frames
        self._interval = interval_s
        self._lock = threading.Lock()
        self._latest = None
        self._ring = collections.deque(maxlen=8)
        self._ts_d: Optional[float] = None
        self._ts_dist: Optional[float] = None
        self._n = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            t = self._thread
            self._thread = None
        if t is not None:
            t.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._frames:
                self._n += 1
                f = self._np.full((48, 64, 3), self._n % 256, dtype=self._np.uint8)
                now = time.monotonic()
                with self._lock:
                    self._latest = f
                    self._ts_d = now
                    self._ts_dist = now
                    self._ring.append(f)
            self._stop.wait(self._interval)

    def read_latest(self):
        with self._lock:
            if self._latest is None or self._ts_d is None:
                return None
            if time.monotonic() - self._ts_d > 3.0:
                return None
            return self._latest

    def read_sequence(self, n: int = 4) -> list:
        with self._lock:
            if self._latest is None:
                return []
            return list(self._ring)[-max(1, int(n)):]

    def age_s(self) -> Optional[float]:
        with self._lock:
            return None if self._ts_d is None else max(0.0, time.monotonic() - self._ts_d)

    def frozen_s(self) -> Optional[float]:
        with self._lock:
            return None if self._ts_dist is None else max(0.0, time.monotonic() - self._ts_dist)


class _FakeLocalAI:
    """A scripted LocalAI client (chat + detect) for offline ``full`` runs.

    ``chat`` returns ``chat_reply`` (a raw JSON string) when ``chat_ok`` is
    True, else an error dict. ``detect`` returns the configured ``detections``
    (for the obstacle-avoidance tests).
    """

    def __init__(
        self,
        chat_reply: str = '{"intent": "explore", "cmd": "drive", "speed": 35, "steer": 0, "reason": "fake"}',
        chat_ok: bool = True,
        detections: Optional[List[dict]] = None,
    ):
        self._reply = chat_reply
        self._chat_ok = chat_ok
        self._detections = list(detections or [])
        self.chat_calls = 0
        self.detect_calls = 0

    def detect(self, image_b64: str, min_confidence: float = 0.5) -> dict:
        self.detect_calls += 1
        return {"ok": True, "detections": list(self._detections)}

    def chat(self, messages, images_b64=None, task: str = "") -> dict:
        self.chat_calls += 1
        if not self._chat_ok:
            return {"ok": False, "error": "fake VLM down"}
        return {"ok": True, "raw": self._reply, "model": "fake", "task": task}

    def health(self) -> dict:
        return {"ok": True, "models": ["fake"]}

    def close(self) -> None:
        pass


def _idle(cmd: Mapping[str, Any]) -> bool:
    return (cmd.get("cmd") == "drive" and cmd.get("speed") == 0
            and cmd.get("steer") == 0)


class BrainAdapter(Adapter):
    name = "brain"

    def __init__(self, cfg: Mapping[str, Any] | None = None):
        super().__init__(cfg)
        self.repo_root = self.cfg.get("repo_root") or _detect_repo_root()
        self.duration_s = float(self.cfg.get("duration_s", DEFAULT_DURATION_S))

    def _brain_pkg(self) -> str:
        return os.path.join(self.repo_root, "raspberry_pi")

    # -- shared helpers ---------------------------------------------------
    def _import_brain(self):
        """Import the brain package modules (raises on failure)."""
        brain_pkg_dir = self._brain_pkg()
        if brain_pkg_dir not in sys.path:
            sys.path.insert(0, brain_pkg_dir)
        import brain  # noqa: F401
        from brain import brain as brain_mod
        from brain import config as brain_config
        from brain import reflex as brain_reflex
        from brain import frame_grab as brain_frame
        from brain import localai_client as brain_localai
        from brain import bridge_client as brain_bridge
        return {
            "brain": brain_mod,
            "config": brain_config,
            "reflex": brain_reflex,
            "frame": brain_frame,
            "localai": brain_localai,
            "bridge": brain_bridge,
        }

    def _load_cfg(self, mods, cmd: Mapping[str, Any]):
        """Load the brain config, applying per-test overrides (env precedence)."""
        env = dict(os.environ)
        cam_url = cmd.get("cam_url")
        localai_url = cmd.get("localai_url")
        obstacle = cmd.get("obstacle")
        if cam_url:
            env["TANKIE_CAM_URL"] = cam_url
        if localai_url:
            env["TANKIE_LOCALAI_BASE_URL"] = localai_url
        if obstacle is not None:
            env["TANKIE_REFLEX_OBSTACLE_ENABLED"] = "true" if obstacle else "false"
        try:
            cfg = mods["config"].load_config(env=env)
        except TypeError:
            cfg = mods["config"].load_config()
            if cam_url:
                cfg = dataclasses.replace(cfg, cam_url=cam_url)
            if localai_url:
                cfg = dataclasses.replace(cfg, localai_base_url=localai_url)
            if obstacle is not None:
                cfg = dataclasses.replace(cfg, reflex_obstacle_enabled=bool(obstacle))
        return cfg

    # -- reflex mode (pre-Phase-2, S6-01…S6-06) ---------------------------
    def _run_reflex(self, cmd: Mapping[str, Any]) -> dict:
        mode = (cmd.get("mode") or "record").lower()
        cam_url = cmd.get("cam_url")
        localai_url = cmd.get("localai_url")
        obstacle = cmd.get("obstacle")

        try:
            mods = self._import_brain()
        except Exception as exc:  # noqa: BLE001 - degrade, don't crash
            return {"reply": {"ok": False, "error": f"brain package import failed: {exc}"}}

        try:
            cfg = self._load_cfg(mods, cmd)
        except Exception as exc:  # noqa: BLE001
            return {"reply": {"ok": False, "error": f"load_config failed: {exc}"}}

        try:
            grabber = mods["frame"].build_frame_grab(cfg)
            localai = mods["localai"].LocalAIClient(cfg)
            controller = mods["reflex"].ReflexController(
                enabled=bool(cfg.reflex_obstacle_enabled),
                block_classes=tuple(cfg.reflex_block_classes),
                lane_half_width=float(cfg.reflex_lane_half_width),
                min_bottom_ratio=float(cfg.reflex_min_bottom_ratio),
                steer_around=int(cfg.reflex_steer_around),
                arc_speed=int(cfg.reflex_arc_speed),
                max_speed=int(cfg.auto_profile.max_speed),
                max_steer=int(cfg.auto_profile.max_steer),
            )
            if mode == "tank":
                bridge = mods["bridge"].BridgeClient(cfg.bridge_socket)
            else:
                bridge = _RecordingBridge()
        except Exception as exc:  # noqa: BLE001
            return {"reply": {"ok": False, "error": f"component build failed: {exc}"}}

        try:
            grabber.start()
        except Exception:  # noqa: BLE001 - some sources have no start()
            pass

        loop = mods["reflex"].ReflexLoop(
            grabber=grabber, localai=localai, controller=controller, bridge=bridge,
            hz=int(cfg.reflex_hz),
            min_confidence=0.5,
            idle_keepalive=True,
        )
        loop.start()
        time.sleep(self.duration_s)
        ticks_before_stop = loop.ticks
        loop.stop()  # sends one final drive 0 0 (invariant: stop final command)

        commands = bridge.commands if isinstance(bridge, _RecordingBridge) else []
        all_idle = bool(commands) and all(_idle(c) for c in commands)
        last = commands[-1] if commands else None

        return {
            "reply": {
                "ok": True,
                "mode": mode,
                "ticks": ticks_before_stop,
                "commands_recorded": len(commands),
                "all_idle": all_idle,
                "last_command": last,
                "final_stop_sent": (last is not None and _idle(last)),
                "obstacle_enabled": bool(cfg.reflex_obstacle_enabled),
            },
            "commands": commands,
        }

    # -- full mode (Phase 2 T5 + T6 orchestrator) -------------------------
    def _run_full(self, cmd: Mapping[str, Any]) -> dict:
        fakes = bool(cmd.get("fakes", True))
        task = cmd.get("task")
        task_file_write = cmd.get("task_file_write")
        chat_reply = cmd.get(
            "chat_reply",
            '{"intent": "explore", "cmd": "drive", "speed": 35, "steer": 0, "reason": "fake"}',
        )
        chat_ok = bool(cmd.get("chat_ok", True))
        detections = cmd.get("detect")
        frames = bool(cmd.get("frames", True))

        try:
            mods = self._import_brain()
        except Exception as exc:  # noqa: BLE001
            return {"reply": {"ok": False, "error": f"brain package import failed: {exc}"}}

        try:
            cfg = self._load_cfg(mods, cmd)
        except Exception as exc:  # noqa: BLE001
            return {"reply": {"ok": False, "error": f"load_config failed: {exc}"}}

        # In offline (fakes) mode, keep the task file local so we never touch
        # the real /var/lib/tankie/task.json.
        if fakes:
            import tempfile
            cfg = dataclasses.replace(cfg, task_file=os.path.join(tempfile.gettempdir(),
                                                                  "tankie-manual-task.json"))

        bridge = None
        grabber = None
        localai = None
        if fakes:
            bridge = _RecordingBridge()
            grabber = _FakeGrabber(frames=frames)
            localai = _FakeLocalAI(chat_reply=chat_reply, chat_ok=chat_ok,
                                   detections=detections)
        # else: let Brain build the real components from cfg (live on-tank).

        try:
            brain = mods["brain"].Brain(
                cfg,
                bridge=bridge,
                grabber=grabber,
                localai=localai,
            )
        except Exception as exc:  # noqa: BLE001
            return {"reply": {"ok": False, "error": f"Brain construction failed: {exc}"}}

        try:
            brain.start()
        except Exception as exc:  # noqa: BLE001
            return {"reply": {"ok": False, "error": f"Brain.start() failed: {exc}"}}

        # Set the task (in-process) or write the task file (cross-process).
        if task_file_write:
            try:
                mods["brain"].write_task_file(cfg.task_file, task_file_write)
            except Exception as exc:  # noqa: BLE001
                return {"reply": {"ok": False, "error": f"write_task_file failed: {exc}"}}
        elif task:
            try:
                brain.set_task(task)
            except Exception as exc:  # noqa: BLE001
                return {"reply": {"ok": False, "error": f"set_task failed: {exc}"}}

        time.sleep(self.duration_s)

        state = None
        try:
            state = brain.state()
        except Exception:  # noqa: BLE001
            state = None

        try:
            brain.stop()
        except Exception:  # noqa: BLE001 - stop must not mask the result
            pass

        commands = bridge.commands if isinstance(bridge, _RecordingBridge) else []
        all_idle = bool(commands) and all(_idle(c) for c in commands)
        last = commands[-1] if commands else None
        liveness = (state or {}).get("liveness", {}) or {}

        return {
            "reply": {
                "ok": True,
                "mode": "full",
                "fakes": fakes,
                "task": (state or {}).get("task"),
                "state_keys": sorted((state or {}).keys()),
                "commands_recorded": len(commands),
                "all_idle": all_idle,
                "last_command": last,
                "final_stop_sent": (last is not None and _idle(last)),
                "reflex_ticks": liveness.get("reflex_ticks"),
                "reflex_stops_sent": liveness.get("reflex_stops_sent"),
                "deliberation_calls": liveness.get("deliberation_calls"),
                "deliberation_errors": liveness.get("deliberation_errors"),
                "deliberation_valid_actions": liveness.get("deliberation_valid_actions"),
                "frame_age_s": liveness.get("frame_age_s"),
                "frame_frozen_s": liveness.get("frame_frozen_s"),
                "obstacle_enabled": bool(cfg.reflex_obstacle_enabled),
            },
            "commands": commands,
            "state": state,
        }

    # -- Adapter interface --------------------------------------------------
    def run(self, cmd: Mapping[str, Any]) -> dict:
        mode = (cmd.get("mode") or "record").lower()
        if mode == "full":
            return self._run_full(cmd)
        return self._run_reflex(cmd)

    def preflight(self) -> List[Tuple[str, bool, str]]:
        brain_pkg_dir = self._brain_pkg()
        ok = os.path.isdir(os.path.join(brain_pkg_dir, "brain"))
        return [("brain package present", ok,
                 f"{brain_pkg_dir}/brain {'found' if ok else 'MISSING'}")]

    def describe(self) -> str:
        return ("AI brain (reflex T4 + deliberation T5 + orchestrator T6) — run "
                "the real loops and record their drive commands to verify the "
                "safety invariants")


def _detect_repo_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "..", ".."))
