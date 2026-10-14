"""AI brain reflex-loop adapter (issue #80, Phase 2 T4) — Stage 6.

Wires up the **real** brain components (``FrameGrab`` + ``LocalAIClient`` +
``ReflexController`` + a bridge client) from ``brain.yaml`` and runs the
:class:`~brain.reflex.ReflexLoop` for a bounded duration, recording the drive
commands it sends. This makes the reflex **safety invariants** machine-
verifiable without needing the tank to be in a particular spot:

    * invariant 2 — stop pre-empts (a pending drive never overrides a stop)
    * invariant 4 — no frame / detect failure / dead bridge => ``drive 0 0``
    * A4          — idle keep-alive ``drive 0 0`` every tick
    * stop()      — one final ``drive 0 0`` before the loop exits

Two bridge modes (selected per-test via ``cmd``):
    record (default) — a **recording** bridge captures the exact drive commands
                       the reflex loop emits (no tank needed; authoritative for
                       the stop/idle invariants).
    tank             — the real ``BridgeClient`` (the tank actually receives the
                       commands; the operator observes it).

Requires the brain deps (``raspberry_pi/brain/requirements.txt``). Degrades to
a ``{"ok": False, "error": ...}`` reply if the brain package or a component is
unavailable, so the operator always gets a verdict prompt.

Config keys (all optional; defaults come from ``BrainConfig`` / ``brain.yaml``):
    brain_yaml   — path to brain.yaml (default: raspberry_pi/conf/brain.yaml)
    duration_s   — how long to run the loop, default 3.0
    cam_url      — override the camera URL (to force a "no frame" test)
    localai_url  — override the LocalAI URL (to force a "detect failure" test)
    obstacle     — reflex_obstacle_enabled override (for the avoidance tests)
"""

from __future__ import annotations

import os
import time
from typing import Any, List, Mapping, Optional, Tuple

from .base import Adapter

DEFAULT_DURATION_S = 3.0


class _RecordingBridge:
    """A bridge double that records every drive command the loop sends."""

    def __init__(self):
        self.commands: List[dict] = []

    def send(self, cmd: Mapping[str, Any]) -> dict:
        self.commands.append(dict(cmd))
        return {"ok": True}


def _idle(cmd: Mapping[str, Any]) -> bool:
    return (cmd.get("cmd") == "drive" and cmd.get("speed") == 0
            and cmd.get("steer") == 0)


class BrainAdapter(Adapter):
    name = "brain"

    def __init__(self, cfg: Mapping[str, Any] | None = None):
        super().__init__(cfg)
        self.repo_root = self.cfg.get("repo_root") or _detect_repo_root()
        self.brain_yaml = self.cfg.get("brain_yaml")
        self.duration_s = float(self.cfg.get("duration_s", DEFAULT_DURATION_S))

    def _brain_pkg(self) -> str:
        return os.path.join(self.repo_root, "raspberry_pi")

    def _run_loop(self, cmd: Mapping[str, Any]) -> dict:
        """Run the reflex loop with the real components and record its output."""
        mode = (cmd.get("mode") or "record").lower()
        cam_url = cmd.get("cam_url")
        localai_url = cmd.get("localai_url")
        obstacle = cmd.get("obstacle")

        # Make the brain package importable.
        brain_pkg_dir = self._brain_pkg()
        import sys
        if brain_pkg_dir not in sys.path:
            sys.path.insert(0, brain_pkg_dir)

        try:
            from brain import config as brain_config
            from brain import reflex as brain_reflex
            from brain import frame_grab as brain_frame
            from brain import localai_client as brain_localai
            from brain import bridge_client as brain_bridge
        except Exception as exc:  # noqa: BLE001 - degrade, don't crash
            return {"reply": {"ok": False,
                              "error": f"brain package import failed: {exc}"}}

        # Load config (defaults < brain.yaml < env), then apply per-test overrides.
        try:
            cfg = brain_config.load_config()
        except Exception as exc:  # noqa: BLE001
            return {"reply": {"ok": False, "error": f"load_config failed: {exc}"}}

        # Apply overrides by constructing a fresh config via env (the supported
        # precedence path) — simplest is to rebuild with env vars set.
        env = dict(os.environ)
        if cam_url:
            env["TANKIE_CAM_URL"] = cam_url
        if localai_url:
            env["TANKIE_LOCALAI_BASE_URL"] = localai_url
        if obstacle is not None:
            env["TANKIE_REFLEX_OBSTACLE_ENABLED"] = "true" if obstacle else "false"
        try:
            cfg = brain_config.load_config(env=env)
        except TypeError:
            # Older load_config without an env kwarg: fall back to defaults + manual
            cfg = brain_config.load_config()
            if cam_url:
                cfg = _replace(cfg, "cam_url", cam_url)
            if localai_url:
                cfg = _replace(cfg, "localai_base_url", localai_url)
            if obstacle is not None:
                cfg = _replace(cfg, "reflex_obstacle_enabled", bool(obstacle))

        # Build the components.
        try:
            grabber = brain_frame.build_frame_grab(cfg)
            localai = brain_localai.LocalAIClient(cfg)
            controller = brain_reflex.ReflexController(
                enabled=bool(cfg.reflex_obstacle_enabled),
                block_classes=tuple(cfg.reflex_block_classes),
                lane_half_width=float(cfg.reflex_lane_half_width),
                min_bottom_ratio=float(cfg.reflex_min_bottom_ratio),
                steer_around=int(cfg.reflex_steer_around),
                max_speed=int(cfg.auto_profile.max_speed),
                max_steer=int(cfg.auto_profile.max_steer),
            )
            if mode == "tank":
                bridge = brain_bridge.BridgeClient(cfg.bridge_socket)
            else:
                bridge = _RecordingBridge()
        except Exception as exc:  # noqa: BLE001
            return {"reply": {"ok": False, "error": f"component build failed: {exc}"}}

        # Start the grabber (it polls the source in a thread).
        try:
            grabber.start()
        except Exception:  # noqa: BLE001 - some sources have no start()
            pass

        loop = brain_reflex.ReflexLoop(
            grabber=grabber, localai=localai, controller=controller, bridge=bridge,
            hz=int(cfg.reflex_hz),
            min_confidence=0.5,
            idle_keepalive=bool(getattr(cfg, "reflex_idle_keepalive", True)),
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

    # -- Adapter interface --------------------------------------------------
    def run(self, cmd: Mapping[str, Any]) -> dict:
        return self._run_loop(cmd)

    def preflight(self) -> List[Tuple[str, bool, str]]:
        brain_pkg_dir = self._brain_pkg()
        ok = os.path.isdir(os.path.join(brain_pkg_dir, "brain"))
        return [("brain package present", ok,
                 f"{brain_pkg_dir}/brain {'found' if ok else 'MISSING'}")]

    def describe(self) -> str:
        return ("AI brain reflex loop (issue #80 T4) — run the real reflex loop "
                "and record its drive commands to verify the safety invariants")


def _detect_repo_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "..", ".."))


def _replace(frozen_cfg, field: str, value):
    """Return a copy of a frozen dataclass with one field replaced."""
    try:
        import dataclasses
        return dataclasses.replace(frozen_cfg, **{field: value})
    except Exception:  # noqa: BLE001
        return frozen_cfg
