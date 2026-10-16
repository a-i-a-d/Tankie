"""Pydantic contract for the Tankie brain (issue #72, Phase 1 T1).

These models are the single vocabulary every later task speaks in: the
deliberation loop emits an :class:`Action`, the reflex loop emits a
:class:`ReflexResult`, the bridge reports a :class:`BridgeResult`, and one
full cycle is captured as a :class:`DataLogRecord` (the data-log line the
LoRA/benchmark work in Phase 6 trains on).

Rules (from the #72 plan):
  * Pydantic v2, ``model_config = ConfigDict(extra="forbid")`` — a stray key
    is a bug, not a silent pass-through. **Exception:** the *external*
    (model-produced) :class:`Action` uses ``extra="ignore"`` — a small VLM
    routinely adds fields (``confidence``, trailing noise), and one stray key
    must not discard an otherwise-valid action (issue #103). Every *internal*
    schema stays ``forbid``.
  * ``speed``/``steer`` are bounded to the ESP8266 serial range (±255 here;
    the bridge clamps the AI path to the AUTO_PROFILE of ±40/±255 — see
    ``Action.clamp`` for the UX mirror). The steer cap is a *configurable*
    behavioural default (``auto_profile.max_steer``), not a safety bound —
    issue #104 raised it to the full human range so the AI can turn as
    sharply as a joystick can.
  * ``pan``/``tilt`` are camera angles in degrees [0..180], optional (None =
    "leave as is").
  * The brain never crashes on a bad model: validation errors are caught by
    the caller and turned into a logged ``error``.

No IP addresses, no network, no hardware — pure data.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# --- enums-as-Literals (kept as module-level for reuse in prompts/tests) ---

Intent = Literal["go_to", "find", "explore", "stop", "say", "wait"]
Cmd = Literal["drive", "pan", "tilt", "stop", "none"]


class _Strict(BaseModel):
    """Base for **internal** brain models: reject unknown keys.

    The one exception is the external, model-produced :class:`Action`, which
    opts into ``extra="ignore"`` (see issue #103).
    """

    model_config = ConfigDict(extra="forbid")


class Action(BaseModel):
    """One deliberation decision: what to do and why.

    ``intent`` is the high-level goal (for the LLM and the data log);
    ``cmd`` is the concrete actuator command. ``speed``/``steer`` are only
    meaningful when ``cmd == "drive"``; ``pan``/``tilt`` only when
    ``cmd`` is ``"pan"``/``"tilt"``.

    This is the **only** brain model parsed from external (VLM) output, so it
    is permissive about *unknown* keys (``extra="ignore"``): a small VLM
    routinely emits extra fields (``confidence``, ``confidence_score``,
    trailing noise) and one stray key must not discard an otherwise-valid
    action (issue #103). Required fields and all range/literal checks are
    still enforced — only unknown keys are dropped.
    """

    model_config = ConfigDict(extra="ignore")

    intent: Intent
    cmd: Cmd
    speed: int = Field(0, ge=-100, le=100)
    steer: int = Field(0, ge=-255, le=255)
    pan: Optional[int] = Field(None, ge=0, le=180)
    tilt: Optional[int] = Field(None, ge=0, le=180)
    say: str = ""
    reason: str = ""

    def clamp(self, max_speed: int, max_steer: int) -> dict:
        """Return a dict with speed/steer bounded to the AUTO_PROFILE limits.

        The bridge is the real enforcement layer (it is the only component
        that can tell AI traffic from manual traffic); this is a UX mirror
        so a prompt can show the brain the values it will actually get.

        Returns a plain ``dict`` (not a re-validated ``Action``): it is a
        display/logging mirror of the values the brain will actually get, so
        it is not re-validated (issue #104 aligned the AUTO_PROFILE steer
        limit to the ``Action`` steer field bound of ±255).
        """
        data = self.model_dump()
        data["speed"] = max(-max_speed, min(max_speed, data["speed"]))
        data["steer"] = max(-max_steer, min(max_steer, data["steer"]))
        return data


class Detection(_Strict):
    """One object detection (from the detection model, Phase 2 T5)."""

    class_name: str
    x: float
    y: float
    w: float
    h: float
    confidence: float = Field(0.0, ge=0.0, le=1.0)


class DepthSummary(_Strict):
    """Coarse depth summary (T9, later). Optional until depth lands."""

    min_m: Optional[float] = None
    center_m: Optional[float] = None


class VLMResult(_Strict):
    """One vision-language-model call (Phase 2 T4)."""

    model: str
    raw: str = ""
    action: Optional[Action] = None
    error: Optional[str] = None
    ok: bool = False


class ReflexResult(_Strict):
    """One reflex-loop tick (Phase 2 T5).

    ``preempted`` is True when the reflex overrode a pending deliberation
    action (the safety invariant: reflex always wins).
    """

    cmd: Cmd
    speed: int = Field(0, ge=-100, le=100)
    steer: int = Field(0, ge=-255, le=255)
    preempted: bool = False
    reason: str = ""


class BridgeResult(_Strict):
    """Outcome of sending an action to the serial bridge (Phase 2 T6)."""

    cmd: Cmd
    speed: Optional[int] = None
    steer: Optional[int] = None
    ok: bool = False
    error: Optional[str] = None


class TankState(_Strict):
    """Snapshot of the tank state (from the bridge state file, #33).

    Mirrors the ``state`` broadcast the ESP sends over the serial link and
    the bridge stores at ``/var/lib/tankie/state.json``. All fields are
    optional because the bridge may not have received every field yet.
    """

    link_up: bool = False
    battery_v: Optional[float] = None
    speed: Optional[int] = None
    steer: Optional[int] = None
    pan: Optional[int] = None
    tilt: Optional[int] = None
    net_mode: Optional[str] = None
    net_ip: Optional[str] = None
    watchdog_fired: bool = False
    updated_at: Optional[datetime] = None


class DataLogRecord(_Strict):
    """One full deliberation+reflex cycle — a single data-log line.

    This is the training/benchmark artifact for Phase 6 (LoRA, VLA). The
    brain appends one of these per cycle to ``data_log_dir`` (rotated,
    ``data_log_max_mb``/``data_log_keep``).
    """

    ts: datetime
    frame_id: str
    task: str
    vlm: Optional[VLMResult] = None
    detections: list[Detection] = Field(default_factory=list)
    depth: Optional[DepthSummary] = None
    reflex: ReflexResult
    bridge: BridgeResult
    state: TankState
    watchdog: bool = False
