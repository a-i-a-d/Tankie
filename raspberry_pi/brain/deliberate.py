"""Deliberative System 2 — the slow thinker (issue #81, Phase 2 T5).

The deliberation loop is the *proposer*: at ``deliberate_hz`` (1 Hz,
configurable) it takes a short sequence of camera frames (A1: a real
multi-frame sequence so the VLM can infer motion), the current task/goal,
and the tank state (A3), asks the VLM (via the T2 ``LocalAIClient.chat()``)
for the next :class:`~brain.schemas.Action`, validates the reply with
Pydantic, and writes it into the thread-safe
:class:`~brain.pending_action.PendingAction` slot that the reflex loop (T4)
reads each tick.

Safety invariants enforced here:
  * **Invariant 1** (single writer) — the deliberation loop *never* talks to
    the bridge. It only writes to the pending-action slot; the reflex loop is
    the sole writer of drive commands.
  * **Never raises** (invariant 4) — a dead VLM, a missing frame, an empty
    task, or a malformed reply is *data*, not an exception: it is logged and
    the loop continues. A malformed reply leaves the pending slot unchanged
    (the last good deliberation stays in force).
  * **A4** — a ``say`` field in the Action is *logged only* for now; TTS
    wiring lands with T10.

The VLM prompt is fully **configurable** (A2): ``deliberate_system_prompt``
and ``deliberate_user_prompt_template`` come from the config source
(``brain.yaml``), so the phrasing can be changed without touching code.

No IP addresses or model names in this module (A4): the model is read from
``BrainConfig.vlm_model`` by the injected client.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from pydantic import ValidationError

from .config import DEFAULT_SYSTEM_PROMPT, DEFAULT_USER_PROMPT_TEMPLATE
from .reflex import frame_to_b64
from .schemas import Action, TankState

__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "DEFAULT_USER_PROMPT_TEMPLATE",
    "build_vlm_prompt",
    "format_state",
    "read_state_file",
    "read_tank_state",
    "DeliberationLoop",
]

logger = logging.getLogger("brain.deliberate")

# The default prompts (``DEFAULT_SYSTEM_PROMPT`` /
# ``DEFAULT_USER_PROMPT_TEMPLATE``) live in ``brain.config`` — the single
# config source (D10/D19) — so the loop and the config agree and the
# phrasing stays editable in ``brain.yaml`` without a code edit (A2).


def format_state(state: Optional[TankState]) -> str:
    """Render a :class:`TankState` as a compact, prompt-friendly string (A3).

    Only the fields that are present are included; an unknown/empty state
    renders as ``"unknown"`` so the VLM is never fed a misleading value.
    Never raises.
    """
    if state is None:
        return "unknown"
    parts = []
    if state.battery_v is not None:
        parts.append(f"battery_v={state.battery_v}")
    if state.speed is not None:
        parts.append(f"speed={state.speed}")
    if state.steer is not None:
        parts.append(f"steer={state.steer}")
    if state.pan is not None:
        parts.append(f"pan={state.pan}")
    if state.tilt is not None:
        parts.append(f"tilt={state.tilt}")
    if state.net_mode:
        parts.append(f"net_mode={state.net_mode}")
    if state.watchdog_fired:
        parts.append("watchdog_fired=true")
    if not state.link_up:
        parts.append("link_down")
    return ", ".join(parts) if parts else "unknown"


def build_vlm_prompt(
    task: str,
    state: Optional[TankState] = None,
    *,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    user_prompt_template: str = DEFAULT_USER_PROMPT_TEMPLATE,
) -> list:
    """Build the ``[system, user]`` message list for the VLM call (A2/A3).

    ``task`` is the current goal; ``state`` is the tank state (rendered via
    :func:`format_state`). Both prompt strings are configurable so the
    phrasing can change without a code edit. The user message carries the
    task + state text; the T2 client appends the image parts to it.
    Never raises.
    """
    state_str = format_state(state)
    try:
        user_text = user_prompt_template.format(task=task, state=state_str)
    except (KeyError, IndexError, ValueError) as exc:
        # A bad template must not kill the loop: fall back to a safe prompt.
        logger.warning("deliberate: bad user prompt template (%s); using fallback", exc)
        user_text = f"Current task: {task}\nTank state: {state_str}"
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_text},
    ]


def read_state_file(path: Any) -> Optional[TankState]:
    """Load the bridge's tank-state file into a :class:`TankState` (A3).

    Missing file, unreadable file, bad JSON, or a non-dict payload all return
    ``None`` (the loop proceeds with an "unknown" state rather than crashing).
    Extra keys in the file are dropped (``TankState`` is ``extra="forbid"``).
    Never raises.
    """
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        valid = set(TankState.model_fields)
        filtered = {k: v for k, v in data.items() if k in valid}
        return TankState.model_validate(filtered)
    except Exception as exc:  # noqa: BLE001 - the loop must never crash on state
        logger.debug("deliberate: could not read state file %r (%s)", path, exc)
        return None


# The bridge's state file nests the ESP's state broadcast under ``last_state``
# (issue #33 StateStore._on_state). The mapping from that nested broadcast to
# the flat :class:`TankState` fields (``battery`` -> ``battery_v``).
_STATE_KEY_MAP = {
    "battery": "battery_v",
    "speed": "speed",
    "steer": "steer",
    "pan": "pan",
    "tilt": "tilt",
    "net_mode": "net_mode",
    "net_ip": "net_ip",
}


def read_tank_state(path: Any) -> Optional[TankState]:
    """Robustly load the bridge's tank-state file into a :class:`TankState`.

    Unlike :func:`read_state_file` (which only reads top-level keys), this
    also reads the **nested ``last_state``** broadcast the bridge stores
    (issue #82, Q6/A6): the bridge writes ``link_up`` / ``watchdog_fired`` /
    ``updated_at`` at the top level but ``battery`` / ``speed`` / ``steer`` /
    ``pan`` / ``tilt`` / ``net_mode`` / ``net_ip`` under ``last_state``.

    Precedence per field: top-level wins (it is the bridge's own summary);
    the nested broadcast fills in whatever is missing. Missing file,
    unreadable file, bad JSON, or a non-dict payload all return ``None``.
    Never raises.
    """
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        nested = data.get("last_state")
        if not isinstance(nested, dict):
            nested = {}
        # Merge: nested broadcast first, then top-level overrides (top-level wins).
        merged: dict = {}
        for k, v in nested.items():
            if k in _STATE_KEY_MAP:
                merged[_STATE_KEY_MAP[k]] = v
        for k, v in data.items():
            if k in TankState.model_fields:
                merged[k] = v
        valid = set(TankState.model_fields)
        filtered = {k: v for k, v in merged.items() if k in valid}
        return TankState.model_validate(filtered)
    except Exception as exc:  # noqa: BLE001 - the loop must never crash on state
        logger.debug("deliberate: could not read tank state %r (%s)", path, exc)
        return None


class DeliberationLoop:
    """Run the VLM at ``hz``: frames + task + state -> proposed :class:`Action`.

    Each tick:
      1. ``task = task_queue.current()`` — **no task -> sleep, no VLM call**
         (don't burn tokens idle).
      2. ``frames = grabber.read_sequence(n)`` — **0 frames -> skip**.
      3. ``state = read_state_file(state_file)`` (A3).
      4. ``messages = build_vlm_prompt(task, state)`` (A2: configurable).
      5. ``localai.chat(messages, images_b64=[...], task=task)``.
      6. ``res["ok"]`` False -> **log + continue** (never raises).
      7. ``Action.model_validate_json(res["raw"])`` -> **ValidationError ->
         log + ignore** (the pending slot keeps its previous value).
      8. success -> ``pending_action.set(action.model_dump())``.

    ``start()`` / ``stop()`` are idempotent. The loop never raises and never
    touches the bridge (invariant 1 — the reflex loop is the sole writer).
    """

    def __init__(
        self,
        grabber: Any,
        localai: Any,
        pending_action: Any,
        task_queue: Any,
        state_file: Optional[str] = None,
        *,
        hz: int = 1,
        frames: int = 4,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        user_prompt_template: str = DEFAULT_USER_PROMPT_TEMPLATE,
    ) -> None:
        self._grabber = grabber
        self._localai = localai
        self._pending = pending_action
        self._task_queue = task_queue
        self._state_file = state_file
        self._hz = max(1, int(hz))
        self._frames = max(1, int(frames))
        self._system_prompt = system_prompt
        self._user_template = user_prompt_template
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        # Observability counters (the data log / T7 will use these later).
        self.calls = 0
        self.errors = 0
        self.valid_actions = 0

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="deliberation-loop", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            t = self._thread
            self._thread = None
        if t is not None:
            t.join(timeout=2.0)

    # -- internals ----------------------------------------------------------
    def _current_task(self) -> Optional[str]:
        try:
            task = self._task_queue.current()
        except Exception:
            return None
        if isinstance(task, str) and task.strip():
            return task.strip()
        return None

    def _read_frames(self) -> list:
        try:
            frames = self._grabber.read_sequence(self._frames)
        except Exception as exc:  # noqa: BLE001
            logger.warning("deliberate: grabber.read_sequence failed: %s", exc)
            return []
        if not isinstance(frames, (list, tuple)):
            return []
        return list(frames)

    def _tick(self) -> None:
        # 1) task — no task, don't burn tokens.
        task = self._current_task()
        if task is None:
            return

        # 2) frames — a real sequence (A1); empty -> skip.
        frames = self._read_frames()
        if not frames:
            return

        # 3) tank state (A3) — None is fine (renders as "unknown").
        #    read_tank_state also reads the nested last_state broadcast
        #    (issue #82, Q6/A6) so the VLM sees the real tank state.
        state = read_tank_state(self._state_file) if self._state_file else None

        # 4) prompt (A2: configurable).
        messages = build_vlm_prompt(
            task, state,
            system_prompt=self._system_prompt,
            user_prompt_template=self._user_template,
        )

        # 5) VLM call (the T2 client applies timeouts.chat_s internally).
        images_b64 = [b for b in (frame_to_b64(f) for f in frames) if b is not None]
        if not images_b64:
            logger.warning("deliberate: no encodable frames; skipping VLM call")
            return
        res = self._localai.chat(messages, images_b64=images_b64, task=task)
        self.calls += 1
        if not isinstance(res, Mapping) or not res.get("ok"):
            self.errors += 1
            err = res.get("error") if isinstance(res, Mapping) else "no result"
            logger.warning("deliberate: VLM call failed: %s", err)
            return

        # 6) validate — malformed reply is logged + ignored (slot unchanged).
        raw = res.get("raw", "")
        try:
            action = Action.model_validate_json(raw)
        except (ValidationError, ValueError) as exc:
            self.errors += 1
            logger.warning(
                "deliberate: malformed VLM reply ignored: %s (raw=%r)", exc, raw[:200]
            )
            return

        # 7) write to the pending slot (latest deliberation wins).
        self._pending.set(action.model_dump())
        self.valid_actions += 1

        # 8) A4: a `say` field is logged only for now (TTS lands with T10).
        if action.say:
            logger.info("deliberate: VLM says: %s", action.say)

    def _loop(self) -> None:
        period = 1.0 / self._hz
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 - a tick must never kill the loop
                logger.error("deliberate: tick raised (suppressed): %s", exc)
            elapsed = time.monotonic() - t0
            self._stop.wait(max(0.001, period - elapsed))
