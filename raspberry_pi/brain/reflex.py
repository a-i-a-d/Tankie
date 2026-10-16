"""Reflex System 1 — the safety net (issue #80, Phase 2 T4).

The reflex loop **owns stop** (invariant 2): every tick it maps the latest
frame + detections + the deliberation loop's pending action into one safe
drive command, and it is the **sole writer** of drive commands to the bridge
(invariant 1). The deliberation loop (T5) only *proposes* via a
thread-safe pending-action slot; the reflex loop decides.

Two pieces:

* :class:`ReflexController` — a **pure** decision function (no I/O, no
  threads, no config-file reads). All parameters are explicit so the full
  test matrix runs in milliseconds.
* :class:`ReflexLoop` — a daemon thread running ``grab -> detect -> decide ->
  bridge`` at ``reflex_hz`` (D3: 10 Hz). ``start()``/``stop()`` are
  idempotent; ``stop()`` sends one final ``drive 0 0`` before exiting.

Safety invariants enforced here:
  * **Invariant 2** — the reflex loop can always pre-empt the VLM: a
    center-lane obstacle always yields ``drive 0 0`` regardless of any
    pending action.
  * **Invariant 4** — LocalAI unreachable / no frame / detect() failed
    => ``drive 0 0`` (a dead perception stack is a stop, never a free drive).
  * **A2 (#80)** — detection-only obstacle avoidance is implemented but
    **disabled by default** (``reflex_obstacle_enabled: false``) until depth
    (T15) lands; the switch is config-driven for testing.
  * **A4 (#80)** — idle keep-alive ``drive 0 0`` every tick is the default
    (a stale drive command can't persist); set ``reflex_idle_keepalive:
    false`` to go quiet when there is nothing to do, so a human can take
    over the tank over the bridge while the ESP stays reachable.

Steer sign convention (tankie/wheels.h, issue #85):
  ``steer > 0 = turn RIGHT``, ``steer < 0 = turn LEFT``. Therefore an
  obstacle on the **left** of the lane is avoided by steering **right**
  (``+steer_around``), and one on the **right** by steering **left**
  (``-steer_around``) — away from the obstacle, never into it.

No IP addresses or model names in this module (A4); the LocalAI client and
the bridge client are injected.
"""

from __future__ import annotations

import base64
import threading
import time
from typing import Any, Callable, Mapping, Optional, Sequence

from .schemas import ReflexResult

__all__ = ["ReflexController", "ReflexLoop", "frame_to_b64"]

# The idle keep-alive command (A4): sent every tick when there is nothing to
# do, so a stale drive command on the bridge/ESP can never persist.
IDLE_CMD = {"cmd": "drive", "speed": 0, "steer": 0}


def frame_to_b64(frame: Any) -> Optional[str]:
    """Encode a grabbed frame as a JPEG base64 string for ``detect()``.

    Accepts ``bytes``/``bytearray`` (already-encoded), a ``cv2``-decodable
    image (``numpy`` array), or anything exposing ``tobytes()``. Returns
    ``None`` when the frame cannot be encoded (the loop treats that as
    "no frame" and stops — invariant 4). Never raises.
    """
    if frame is None:
        return None
    try:
        if isinstance(frame, (bytes, bytearray)):
            raw = bytes(frame)
        else:
            try:
                import cv2  # lazy: keeps the module importable without cv2

                ok, buf = cv2.imencode(".jpg", frame)
                if not ok:
                    return None
                raw = buf.tobytes()
            except ImportError:
                # No cv2: fall back to raw bytes (tests use this path).
                raw = frame.tobytes() if hasattr(frame, "tobytes") else bytes(frame)
        return base64.b64encode(raw).decode("ascii")
    except Exception:
        return None


class ReflexController:
    """Pure decision function: detections + pending action -> drive command.

    No I/O, no threads, no config file reads — all parameters are explicit
    so the full test matrix can run in milliseconds.

    Parameters
    ----------
    enabled:
        A2 (#80): when ``False`` (the default until depth lands, T15),
        detections are ignored entirely — the controller only passes the
        pending action through (or idles). ``True`` = detection-only
        obstacle avoidance.
    block_classes:
        A1 (#80): COCO class names that threaten the tank (people, vehicles,
        animals). Configurable via ``brain.yaml`` (``reflex_block_classes``).
    lane_half_width:
        A2 (#80): a box whose **center x** is within
        ``[img_w/2 ± lane_half_width * img_w/2]`` is "in the lane".
    min_bottom_ratio:
        A2 (#80): a box whose **bottom edge** is below
        ``min_bottom_ratio * img_h`` is "close".
    steer_around:
        A3 (#80): steer magnitude (±) when avoiding a side-lane obstacle.
        The tank moves forward at ``max_speed`` while steering this much
        (issue #105: an arc around the obstacle, not a spin-in-place).
    max_speed / max_steer:
        AUTO_PROFILE limits (issue #31) — the UX mirror clamp; the bridge is
        the real enforcement layer.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        block_classes: Sequence[str] = ("person", "car", "truck", "bus",
                                        "bicycle", "motorcycle", "dog", "cat",
                                        "horse"),
        lane_half_width: float = 0.30,
        min_bottom_ratio: float = 0.70,
        steer_around: int = 60,
        max_speed: int = 40,
        max_steer: int = 255,
    ) -> None:
        self.enabled = bool(enabled)
        self.block_classes = tuple(str(c) for c in block_classes)
        self.lane_half_width = float(lane_half_width)
        self.min_bottom_ratio = float(min_bottom_ratio)
        self.steer_around = int(steer_around)
        self.max_speed = int(max_speed)
        self.max_steer = int(max_steer)

    # ------------------------------------------------------------------
    # geometry helpers (pure)
    # ------------------------------------------------------------------
    def _lane_bounds(self, img_w: int) -> tuple:
        """(left, right) center-x bounds of the drivable lane."""
        center = img_w / 2.0
        half = self.lane_half_width * (img_w / 2.0)
        return (center - half, center + half)

    def _is_close(self, det: Mapping[str, Any], img_h: int) -> bool:
        """Box bottom edge below ``min_bottom_ratio * img_h`` = close."""
        bottom = float(det.get("y", 0.0)) + float(det.get("h", 0.0))
        return bottom > self.min_bottom_ratio * img_h

    def _is_in_lane(self, det: Mapping[str, Any], img_w: int) -> bool:
        """Box center x within the lane bounds."""
        cx = float(det.get("x", 0.0)) + float(det.get("w", 0.0)) / 2.0
        left, right = self._lane_bounds(img_w)
        return left <= cx <= right

    def _most_threatening(
        self, detections: Sequence[Mapping[str, Any]], img_w: int, img_h: int
    ) -> Optional[Mapping[str, Any]]:
        """Closest (lowest box bottom) blocking + close detection, else None."""
        best: Optional[Mapping[str, Any]] = None
        best_bottom = float("-inf")
        for det in detections:
            if not isinstance(det, Mapping):
                continue
            if str(det.get("class_name", "")) not in self.block_classes:
                continue
            if not self._is_close(det, img_h):
                continue
            bottom = float(det.get("y", 0.0)) + float(det.get("h", 0.0))
            if bottom > best_bottom:
                best_bottom = bottom
                best = det
        return best

    # ------------------------------------------------------------------
    # the decision
    # ------------------------------------------------------------------
    def decide(
        self,
        detections: Optional[Sequence[Mapping[str, Any]]],
        img_w: int,
        img_h: int,
        pending: Optional[Mapping[str, Any]] = None,
    ) -> ReflexResult:
        """Map detections + the pending deliberation action to a drive command.

        Logic (in priority order):
          1. **Center-lane obstacle is close** (and enabled) ->
             ``drive 0 0`` (stop), ``preempted=True`` when a pending action
             was overridden (invariant 2).
          2. **Side-lane obstacle is close** (and enabled) -> **arc around**:
             move forward at ``max_speed`` while steering **away** (issue #105:
             a side obstacle implies *moving around* it, not spinning in place).
             obstacle left  -> ``speed=max_speed, steer = +steer_around`` (turn right),
             obstacle right -> ``speed=max_speed, steer = -steer_around`` (turn left).
          3. **Clear** -> pass the pending action through (drive clamped to
             the AUTO_PROFILE; pan/tilt with its angle), or ``drive 0 0``
             idle keep-alive when there is no pending action.
        """
        pending = pending if isinstance(pending, Mapping) else None
        preempted = pending is not None

        if self.enabled and detections:
            obstacle = self._most_threatening(detections, img_w, img_h)
            if obstacle is not None:
                cls = str(obstacle.get("class_name", "obstacle"))
                if self._is_in_lane(obstacle, img_w):
                    return ReflexResult(
                        cmd="drive", speed=0, steer=0,
                        preempted=preempted,
                        reason=f"stop: {cls} in lane",
                    )
                # Side-lane: steer away from the obstacle (wheels.h, issue #85:
                # steer > 0 = right, steer < 0 = left).
                cx = float(obstacle.get("x", 0.0)) + float(obstacle.get("w", 0.0)) / 2.0
                if cx < img_w / 2.0:
                    return ReflexResult(
                        cmd="drive", speed=self.max_speed, steer=self.steer_around,
                        preempted=preempted,
                        reason=f"arc-around: {cls} left",
                    )
                return ReflexResult(
                    cmd="drive", speed=self.max_speed, steer=-self.steer_around,
                    preempted=preempted,
                    reason=f"arc-around: {cls} right",
                )

        # --- clear (or obstacle avoidance disabled) ---
        if pending is not None:
            cmd = str(pending.get("cmd", ""))
            if cmd == "drive":
                # Clamp to the AUTO_PROFILE (issue #31), then to the
                # ReflexResult schema bounds (±255). The bridge is the real
                # enforcement layer (±255); we are always within it.
                speed = self._clamp(int(pending.get("speed", 0)), self.max_speed)
                steer = self._clamp(int(pending.get("steer", 0)), self.max_steer)
                return ReflexResult(
                    cmd="drive", speed=speed, steer=steer,
                    preempted=False,
                    reason=str(pending.get("reason", "pending drive")),
                )
            if cmd == "pan":
                return ReflexResult(
                    cmd="pan", preempted=False,
                    reason=str(pending.get("reason", "pending pan")),
                )
            if cmd == "tilt":
                return ReflexResult(
                    cmd="tilt", preempted=False,
                    reason=str(pending.get("reason", "pending tilt")),
                )
            # Unknown/invalid pending cmd -> treat as no pending action.
            return ReflexResult(
                cmd="drive", speed=0, steer=0,
                preempted=False,
                reason=f"pending action ignored (cmd={cmd!r})",
            )

        return ReflexResult(
            cmd="drive", speed=0, steer=0, preempted=False, reason="idle"
        )

    def to_bridge_cmd(self, result: ReflexResult, pending: Optional[Mapping[str, Any]]) -> dict:
        """Translate a :class:`ReflexResult` into the bridge socket command.

        ``drive`` -> ``{"cmd":"drive","speed":...,"steer":...}`` (the values
        the reflex decided — a stop is ``0 0``, a passthrough is the clamped
        pending drive); ``pan``/``tilt`` -> ``{"cmd":...,"angle":...}`` (the
        angle comes from the pending action that produced the result). The
        bridge is the real enforcement layer (issue #31); this is the UX
        mirror. A stop and a passthrough are both ``drive`` here, so this is
        a pure passthrough of ``result`` — it never re-introduces a pending
        value the reflex overrode.
        """
        if result.cmd == "pan":
            angle = int(pending.get("pan", 90)) if isinstance(pending, Mapping) else 90
            return {"cmd": "pan", "angle": angle}
        if result.cmd == "tilt":
            angle = int(pending.get("tilt", 90)) if isinstance(pending, Mapping) else 90
            return {"cmd": "tilt", "angle": angle}
        return {"cmd": "drive", "speed": int(result.speed), "steer": int(result.steer)}

    @staticmethod
    def _clamp(value: int, limit: int) -> int:
        """Clamp ``value`` to ``±limit`` and to the ReflexResult schema (±255)."""
        bound = min(int(limit), 255)
        return max(-bound, min(bound, int(value)))


class ReflexLoop:
    """Run ``grab -> detect -> decide -> bridge`` at ``hz`` (D3: 10 Hz).

    The deliberation loop (T5) writes its proposed Action to a thread-safe
    pending-action slot; the reflex loop reads it each tick via the
    ``pending_action`` callable. This is the single-writer path to the bridge
    (invariant 1).

    * ``grabber.read_latest()`` -> ``None`` => ``drive 0 0`` (invariant 4).
    * ``localai.detect()`` not ok => ``drive 0 0`` (invariant 4) —
      only when ``controller.enabled`` is True (issue #99); when disabled
      the detector is skipped entirely and the pending action passes through.
    * ``bridge.send()`` not ok => keep sending ``drive 0 0`` (the stop is
      retried until the bridge is back; never raises).
    * ``start()`` / ``stop()`` are idempotent; ``stop()`` sends one final
      ``drive 0 0`` before the thread exits.
    """

    def __init__(
        self,
        grabber: Any,
        localai: Any,
        controller: ReflexController,
        bridge: Any,
        pending_action: Optional[Callable[[], Optional[dict]]] = None,
        pending_clear: Optional[Callable[[], None]] = None,
        *,
        hz: int = 10,
        min_confidence: float = 0.5,
        idle_keepalive: bool = True,
    ) -> None:
        self._grabber = grabber
        self._localai = localai
        self._controller = controller
        self._bridge = bridge
        self._pending_action = pending_action or (lambda: None)
        # #101: consume-once hook — clears the pending slot after a one-shot
        # pan/tilt is applied (so it is not re-sent every tick).
        self._pending_clear = pending_clear or (lambda: None)
        self._hz = max(1, int(hz))
        self._min_confidence = float(min_confidence)
        self._idle_keepalive = bool(idle_keepalive)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.ticks = 0
        self.stops_sent = 0
        # T6 (#82): the last decision, for Brain.state() observability.
        # Written under _lock each tick; read as a copy (never mutated).
        self._last_result: Optional[ReflexResult] = None

    # -- lifecycle ------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="reflex-loop", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            t = self._thread
            self._thread = None
        if t is not None:
            t.join(timeout=2.0)
        # One final stop so the tank is never left coasting (invariant 2).
        self._send_idle()

    # -- internals ------------------------------------------------------
    @property
    def last_result(self) -> Optional[dict]:
        """The last :class:`ReflexResult` as a dict, or ``None`` before
        the first tick. Thread-safe (T6: feeds ``Brain.state()``)."""
        with self._lock:
            r = self._last_result
            return r.model_dump() if r is not None else None

    def _send_idle(self) -> None:
        try:
            self._bridge.send(IDLE_CMD)
            self.stops_sent += 1
        except Exception:
            pass  # the bridge client never raises; belt and braces.

    def _pending(self) -> Optional[dict]:
        try:
            return self._pending_action()
        except Exception:
            return None

    def _consume_pending(self) -> None:
        """Clear the pending slot after applying a one-shot pan/tilt (#101).

        Never raises (a failing clear must not kill the loop).
        """
        try:
            self._pending_clear()
        except Exception:
            pass

    def _loop(self) -> None:
        period = 1.0 / self._hz
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self._tick()
            except Exception:
                # A tick must never kill the loop: fall back to stop.
                self._send_idle()
            self.ticks += 1
            elapsed = time.monotonic() - t0
            self._stop.wait(max(0.001, period - elapsed))

    def _tick(self) -> None:
        frame = self._grabber.read_latest()
        img_b64 = frame_to_b64(frame)
        if img_b64 is None:
            self._send_idle()  # invariant 4: no frame => stop
            return

        # #99: only call detect() when obstacle avoidance is actually enabled.
        # When disabled (the default until depth/T15 lands), a dead or missing
        # detector must not block the VLM path — the pending action passes
        # through. When enabled, a detect() failure is still a hard stop
        # (invariant 4: perception dead => stop).
        if self._controller.enabled:
            det = self._localai.detect(img_b64, min_confidence=self._min_confidence)
            if not isinstance(det, Mapping) or not det.get("ok"):
                self._send_idle()  # invariant 4: server unreachable => stop
                return
            detections = det.get("detections", [])
        else:
            detections = []

        pending = self._pending()
        # Image size: the detection payload carries it when the server
        # provides it; otherwise the controller's geometry is relative to
        # the frame the grabber produced. We pass the frame's own size when
        # available (numpy shape), else 1x1 (which degenerates geometry to
        # "nothing is close" — safe, and only reached if the server omits
        # size metadata).
        img_w, img_h = _frame_size(frame)
        result = self._controller.decide(
            detections, img_w, img_h, pending
        )
        with self._lock:
            self._last_result = result
        # #101: consume-once. A one-shot pan/tilt is applied once; clear the
        # pending slot so the reflex loop stops re-sending it every tick.
        # (drive is a *level* command — it stays until re-proposed or the
        # deliberation loop clears the slot when the task ends.)
        if result.cmd in ("pan", "tilt"):
            self._consume_pending()
        cmd = self._controller.to_bridge_cmd(result, pending)
        if not self._idle_keepalive and cmd == IDLE_CMD:
            return  # A4: quiet idle (human can take over)
        self._bridge.send(cmd)


def _frame_size(frame: Any) -> tuple:
    """(w, h) of a numpy-style frame, else (1, 1)."""
    shape = getattr(frame, "shape", None)
    if isinstance(shape, tuple) and len(shape) >= 2:
        try:
            return (int(shape[1]), int(shape[0]))
        except (TypeError, ValueError):
            pass
    return (1, 1)
