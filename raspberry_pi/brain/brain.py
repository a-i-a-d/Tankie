"""Brain orchestrator — the glue that makes the brain a runnable program (issue #82, T6).

The :class:`Brain` owns all brain components and starts/stops them in a safe
order. It is the single entry point for ``python3 -m brain`` (the T12
systemd target) and the data source for ``Brain.state()`` (the MCP
``state()`` tool, T8).

Wiring (the single-writer invariant, invariant 1): the Brain creates **one**
:class:`~brain.pending_action.PendingAction` slot and **one**
:class:`~brain.task_queue.TaskQueue`, then wires
``ReflexLoop(pending_action=slot.get)`` and
``DeliberationLoop(pending_action=slot, task_queue=tq)``. The deliberation
loop (T5) only *proposes* into the slot; the reflex loop (T4) is the *sole*
bridge writer.

Safety invariants enforced here:
  * **Invariant 1** (single writer) — the Brain never talks to the bridge
    directly; only the reflex loop writes drive commands.
  * **Invariant 4** (never crash) — a component that fails to construct or
    start is *logged and skipped*; the brain continues with what it has
    (a dead perception stack is a stop, never a free drive).
  * **Keep-alive** (issue #82 "decided context") — the reflex loop sends an
    idle ``drive 0 0`` every tick (10 Hz) and one final ``drive 0 0`` in
    ``stop()``; the Brain "owns keep-alive" by keeping the reflex loop alive
    and guaranteeing ``stop()`` runs it (a stale command can't persist).

Start order (each step idempotent; a failure is logged and the brain
continues with what it has):
  1. BridgeClient      (the sole bridge writer — one-shot per command)
  2. FrameGrab         (perception)
  3. LocalAIClient     (inference)
  4. ReflexLoop        (safety net — starts first among the loops)
  5. DeliberationLoop  (slow thinker)

Stop order (reverse):
  1. DeliberationLoop.stop()
  2. ReflexLoop.stop()          (sends final ``drive 0 0``)
  3. FrameGrab.stop()
  4. LocalAIClient.close()

No IP addresses or model names in this module (A4); all paths and rates come
from :class:`~brain.config.BrainConfig`.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from .config import BrainConfig
from .deliberate import DeliberationLoop, read_tank_state
from .pending_action import PendingAction
from .reflex import ReflexController, ReflexLoop
from .task_queue import TaskQueue

__all__ = ["Brain", "read_task_file", "write_task_file"]

logger = logging.getLogger("brain.brain")

# How often the task-file poller checks for an external task change (Q5/A5:
# the cross-process channel). One tiny read per interval — negligible.
_TASK_POLL_S = 0.5

# How long ``run(once=True)`` lets the loops tick before the clean stop
# (A8: one bounded reflex idle cycle + one deliberation tick if a task is set).
_ONCE_SETTLE_S = 1.2


# ---------------------------------------------------------------------------
# task file (Q5/A5: the cross-process task channel)
# ---------------------------------------------------------------------------
def _parse_task_file(data: Any) -> Optional[str]:
    """Extract the task string from a parsed task-file payload (or None)."""
    if not isinstance(data, dict):
        return None
    task = data.get("task")
    if task is None or not isinstance(task, str):
        return None
    task = task.strip()
    return task or None


def read_task_file(path: Any) -> Optional[str]:
    """Read the current task from the task file (Q5/A5 cross-process channel).

    Returns the task string, or ``None`` when the file is missing, unreadable,
    bad JSON, non-dict, or carries a null/empty task. Never raises.
    """
    try:
        with open(path) as f:
            data = json.load(f)
        return _parse_task_file(data)
    except Exception:  # noqa: BLE001 - a bad task file must not crash the brain
        return None


def write_task_file(path: Any, task: Optional[str]) -> bool:
    """Atomically write the task file (``{"task": str|null, "updated_at": iso}``).

    ``task=None`` clears the task (writes ``{"task": null, ...}``). Uses a
    temp file + rename so a concurrent reader never sees a partial write.
    Returns ``True`` on success, ``False`` on any failure (never raises).
    """
    try:
        payload = {
            "task": (task.strip() if isinstance(task, str) and task.strip() else None),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        p = Path(path)
        parent = str(p.parent)
        if parent and parent not in (".",):
            p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, p)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("brain: could not write task file %r: %s", path, exc)
        return False


def _safe_start(component: Any, name: str) -> bool:
    """Call ``component.start()`` if present; log + swallow any failure.

    Returns ``True`` when the component started (or had nothing to start),
    ``False`` when its ``start()`` raised (the brain skips it and continues).
    """
    if component is None:
        return True
    start = getattr(component, "start", None)
    if start is None:
        return True
    try:
        start()
        return True
    except Exception as exc:  # noqa: BLE001 - a failing component must not kill the brain
        logger.warning("brain: %s start() failed: %s", name, exc)
        return False


class Brain:
    """Owns all brain components; starts/stops them in a safe order.

    Every component is injectable (matching the T2/T3 injection pattern) so
    the offline test suite runs with fakes; the defaults construct the real
    ones from :class:`BrainConfig`.

    * ``start()`` — idempotent; start order bridge → grabber → localai →
      reflex → deliberation; a component that fails to start is logged +
      skipped (the brain continues with what it has).
    * ``stop()`` — idempotent; reverse order (deliberation → reflex (final
      ``drive 0 0``) → grabber → localai.close()); never raises.
    * ``run(once=False)`` — installs SIGINT/SIGTERM handlers (A4) then blocks
      on a stop-event until ``stop()``/a signal; ``once=True`` (the A1/A8
      ``--once`` path) settles for one bounded cycle, then stops cleanly.
    * ``set_task(task)`` / ``clear_task()`` — update the in-memory
      :class:`TaskQueue` **and** the task file (Q5), so in-process and
      CLI/MCP agree.
    * ``state()`` — the full snapshot (A3): ``tank`` (via
      :func:`read_tank_state`, Q6), ``task``, ``last_reflex``,
      ``last_deliberation``, ``liveness``, and a ``config`` summary.
    """

    def __init__(
        self,
        config: BrainConfig,
        *,
        bridge: Any = None,
        grabber: Any = None,
        localai: Any = None,
        controller: Any = None,
        reflex_loop: Any = None,
        deliberation_loop: Any = None,
        task_queue: Any = None,
        pending_action: Any = None,
    ) -> None:
        self._cfg = config
        self._task_queue = task_queue if task_queue is not None else TaskQueue()
        self._pending = pending_action if pending_action is not None else PendingAction()

        # -- construct (or adopt) the components, in start order ----------
        self._bridge = bridge if bridge is not None else self._build_bridge()
        self._grabber = grabber if grabber is not None else self._build_grabber()
        self._localai = localai if localai is not None else self._build_localai()
        self._controller = (
            controller if controller is not None else self._build_controller()
        )
        self._reflex = (
            reflex_loop if reflex_loop is not None else self._build_reflex()
        )
        self._deliberation = (
            deliberation_loop
            if deliberation_loop is not None
            else self._build_deliberation()
        )

        self._lock = threading.Lock()
        self._started = False
        self._started_order: List[Any] = []
        self._stop_event = threading.Event()
        self._poller_stop = threading.Event()
        self._task_poller: Optional[threading.Thread] = None
        self._last_written_task: Optional[str] = None
        self._signal_handlers_installed = False

    # -- construction helpers (each returns the component or None) --------
    def _build_bridge(self) -> Any:
        from .bridge_client import BridgeClient

        try:
            return BridgeClient(self._cfg.bridge_socket, timeout_s=1.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("brain: BridgeClient construction failed: %s", exc)
            return None

    def _build_grabber(self) -> Any:
        from .frame_grab import build_frame_grab

        try:
            return build_frame_grab(self._cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("brain: FrameGrab construction failed: %s", exc)
            return None

    def _build_localai(self) -> Any:
        from .localai_client import LocalAIClient

        try:
            return LocalAIClient(self._cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("brain: LocalAIClient construction failed: %s", exc)
            return None

    def _build_controller(self) -> ReflexController:
        ap = self._cfg.auto_profile
        try:
            return ReflexController(
                enabled=bool(self._cfg.reflex_obstacle_enabled),
                block_classes=self._cfg.reflex_block_classes,
                lane_half_width=self._cfg.reflex_lane_half_width,
                min_bottom_ratio=self._cfg.reflex_min_bottom_ratio,
                steer_around=self._cfg.reflex_steer_around,
                arc_speed=self._cfg.reflex_arc_speed,
                max_speed=ap.max_speed,
                max_steer=ap.max_steer,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("brain: ReflexController construction failed: %s", exc)
            return ReflexController()

    def _build_reflex(self) -> Any:
        if self._bridge is None or self._grabber is None or self._localai is None:
            logger.warning(
                "brain: reflex loop not constructed (a prerequisite component "
                "is unavailable) — the tank stays stopped (invariant 4)"
            )
            return None
        try:
            return ReflexLoop(
                self._grabber,
                self._localai,
                self._controller,
                self._bridge,
                pending_action=self._pending.get,
                # #101: consume-once — after the reflex loop applies a one-shot
                # pan/tilt it clears the slot so it is not re-sent every tick.
                pending_clear=self._pending.clear,
                hz=self._cfg.reflex_hz,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("brain: ReflexLoop construction failed: %s", exc)
            return None

    def _build_deliberation(self) -> Any:
        if self._grabber is None or self._localai is None:
            logger.warning(
                "brain: deliberation loop not constructed (a prerequisite "
                "component is unavailable)"
            )
            return None
        try:
            return DeliberationLoop(
                self._grabber,
                self._localai,
                pending_action=self._pending,
                task_queue=self._task_queue,
                state_file=self._cfg.state_file,
                hz=self._cfg.deliberate_hz,
                frames=self._cfg.deliberate_frames,
                system_prompt=self._cfg.deliberate_system_prompt,
                user_prompt_template=self._cfg.deliberate_user_prompt_template,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("brain: DeliberationLoop construction failed: %s", exc)
            return None

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        """Start all components in the safe order (idempotent).

        Start order: bridge (no start) -> grabber -> localai (no start) ->
        reflex -> deliberation. A component whose ``start()`` raises is
        logged + skipped; the brain continues with what it has (partial
        failure is data, not a crash).
        """
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stop_event.clear()
            self._poller_stop.clear()
            self._started_order = []

        if _safe_start(self._grabber, "FrameGrab"):
            self._started_order.append(self._grabber)
        if _safe_start(self._reflex, "ReflexLoop"):
            self._started_order.append(self._reflex)
        if _safe_start(self._deliberation, "DeliberationLoop"):
            self._started_order.append(self._deliberation)

        # Start the task-file poller (Q5/A5: the cross-process channel).
        self._start_task_poller()

    def stop(self) -> None:
        """Stop all components in the reverse order (idempotent, never raises).

        Stop order (reverse of start): deliberation -> reflex (final
        ``drive 0 0``) -> grabber -> localai.close(). The reflex loop's
        ``stop()`` sends one final ``drive 0 0`` (invariant 2) so the tank is
        never left coasting.
        """
        self._stop_event.set()  # unblock run() (A4: stop() is the exit path)
        with self._lock:
            if not self._started:
                self._stop_task_poller()
                return
            order = list(self._started_order)
            self._started = False
            self._started_order = []

        self._stop_task_poller()
        for comp in reversed(order):
            self._safe_stop(comp)
        # localai has no start(); close it (idempotent, never raises).
        if self._localai is not None:
            self._safe_stop(self._localai)

    @staticmethod
    def _safe_stop(comp: Any) -> None:
        try:
            stop = getattr(comp, "stop", None)
            if stop is not None:
                stop()
                return
            close = getattr(comp, "close", None)
            if close is not None:
                close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("brain: stopping %s failed: %s", type(comp).__name__, exc)

    def run(self, once: bool = False) -> None:
        """Block until ``stop()`` is called or SIGINT/SIGTERM arrives (A4).

        ``once=True`` (the A1/A8 ``--once`` smoke-test path): settle for one
        bounded cycle (one reflex idle tick + one deliberation tick if a task
        is set), then stop cleanly and return.
        """
        self._install_signal_handlers()
        if once:
            time.sleep(_ONCE_SETTLE_S)
            self.stop()
            return
        while not self._stop_event.is_set():
            self._stop_event.wait(0.2)

    # -- signals (A4: Brain owns SIGINT/SIGTERM) -------------------------
    def _install_signal_handlers(self) -> None:
        if self._signal_handlers_installed:
            return
        if threading.current_thread() is not threading.main_thread():
            return  # signal handlers can only be set in the main thread
        try:
            signal.signal(signal.SIGINT, self._on_signal)
            signal.signal(signal.SIGTERM, self._on_signal)
            self._signal_handlers_installed = True
        except (ValueError, OSError) as exc:
            logger.debug("brain: could not install signal handlers: %s", exc)

    def _on_signal(self, signum, frame) -> None:  # noqa: ANN001
        logger.info("brain: received signal %s — stopping", signum)
        self._stop_event.set()

    # -- task channel (invariant 3: the only way to change the goal) -----
    def set_task(self, task: str) -> None:
        """Set the current task (in-memory **and** task file, Q5)."""
        task = str(task)
        self._task_queue.set(task)
        self._last_written_task = task
        if self._cfg.task_file:
            write_task_file(self._cfg.task_file, task)

    def clear_task(self) -> None:
        """Clear the current task (in-memory **and** task file, Q5)."""
        self._task_queue.clear()
        self._last_written_task = None
        if self._cfg.task_file:
            write_task_file(self._cfg.task_file, None)

    @property
    def task_queue(self) -> TaskQueue:
        """The in-memory :class:`TaskQueue` (the deliberation loop's source)."""
        return self._task_queue

    # -- task-file poller (Q5/A5: the cross-process channel) -------------
    def _start_task_poller(self) -> None:
        if not self._cfg.task_file:
            return
        if self._task_poller is not None and self._task_poller.is_alive():
            return
        self._task_poller = threading.Thread(
            target=self._task_poll_loop, name="brain-task-poll", daemon=True
        )
        self._task_poller.start()

    def _stop_task_poller(self) -> None:
        self._poller_stop.set()
        t = self._task_poller
        self._task_poller = None
        if t is not None:
            t.join(timeout=2.0)

    def _task_poll_loop(self) -> None:
        while not self._poller_stop.is_set():
            try:
                self._sync_task_file()
            except Exception as exc:  # noqa: BLE001
                logger.debug("brain: task poll tick failed: %s", exc)
            self._poller_stop.wait(_TASK_POLL_S)

    def _sync_task_file(self) -> None:
        """Bridge the task file into the in-memory TaskQueue (one tiny read).

        The file is the **external instruction** channel (Q5/A5): a *missing*
        or unreadable file means "no external instruction" (the in-memory task
        is left alone — so an in-process ``set_task`` is never clobbered by a
        stale/absent file); a file that explicitly carries ``task: null`` is an
        external **clear**; a file carrying a task string is an external **set**.
        Only updates the in-memory task when the file differs from the current
        in-memory value (a CLI/MCP write is picked up; a no-op is skipped).
        """
        if not self._cfg.task_file:
            return
        try:
            with open(self._cfg.task_file) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return  # file missing / unreadable -> no external instruction
        if not isinstance(data, dict) or "task" not in data:
            return  # no task key -> no instruction
        file_task = data["task"]
        if file_task is None:
            # Explicit external clear (``brain_cli.py task clear``).
            if self._task_queue.current() is not None:
                self._task_queue.clear()
                self._last_written_task = None
            return
        if not isinstance(file_task, str) or not file_task.strip():
            return  # empty / non-string -> no instruction
        file_task = file_task.strip()
        if file_task != self._task_queue.current():
            self._task_queue.set(file_task)
            self._last_written_task = file_task

    # -- observability (A3: Brain.state() is the MCP state() data source) -
    def state(self) -> dict:
        """Return the full brain state snapshot (A3).

        Shape (Q7): ``tank`` (full :class:`TankState` via
        :func:`read_tank_state`, Q6), ``task``, ``last_reflex``,
        ``last_deliberation``, ``liveness``, and a ``config`` summary.
        Never raises.
        """
        return {
            "tank": self._tank_state(),
            "task": self._task_queue.current(),
            "last_reflex": self._last_reflex(),
            "last_deliberation": self._last_deliberation(),
            "liveness": {
                "reflex_ticks": self._safe_get(self._reflex, "ticks"),
                "reflex_stops_sent": self._safe_get(self._reflex, "stops_sent"),
                "deliberation_calls": self._safe_get(self._deliberation, "calls"),
                "deliberation_errors": self._safe_get(self._deliberation, "errors"),
                "deliberation_valid_actions": self._safe_get(
                    self._deliberation, "valid_actions"
                ),
            },
            "config": {
                "vlm_model": self._cfg.vlm_model,
                "detection_model": self._cfg.detection_model,
                "reflex_hz": self._cfg.reflex_hz,
                "deliberate_hz": self._cfg.deliberate_hz,
                "deliberate_frames": self._cfg.deliberate_frames,
                "bridge_socket": self._cfg.bridge_socket,
                "state_file": self._cfg.state_file,
                "task_file": self._cfg.task_file,
            },
        }

    def _tank_state(self) -> Optional[dict]:
        try:
            state = (
                read_tank_state(self._cfg.state_file)
                if self._cfg.state_file
                else None
            )
        except Exception:  # noqa: BLE001
            return None
        return state.model_dump(mode="json") if state is not None else None

    def _last_reflex(self) -> Optional[dict]:
        try:
            return self._reflex.last_result if self._reflex is not None else None
        except Exception:  # noqa: BLE001
            return None

    def _last_deliberation(self) -> dict:
        d = self._deliberation
        return {
            "calls": self._safe_get(d, "calls"),
            "errors": self._safe_get(d, "errors"),
            "valid_actions": self._safe_get(d, "valid_actions"),
        }

    @staticmethod
    def _safe_get(obj: Any, attr: str) -> Any:
        try:
            return getattr(obj, attr)
        except Exception:  # noqa: BLE001
            return None
