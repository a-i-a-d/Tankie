"""Typed config loader for the Tankie brain (issue #72, Phase 1 T1).

Single config source (D10/D19), mirroring the #16 pattern re-landed in PR
#66. Precedence, highest wins:

    1. environment variable   (TANKIE_LOCALAI_BASE_URL / TANKIE_VLM_MODEL /
                               TANKIE_CAM_URL)
    2. brain.yaml             (raspberry_pi/conf/brain.yaml — the one file
                               an operator edits)
    3. built-in defaults      (the values in this module)

The brain never crashes on config: a missing file, a corrupt file, or a
non-dict YAML all fall back to the built-in defaults (no exception).

No IP addresses are required in code (A4): the code default for the LocalAI
server is the dnsmasq hostname ``localai.local``. ``brain.yaml`` may carry
the static IP as a local override until dnsmasq is set up on the Pi
(#69 A4 follow-up).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

import yaml

# --------------------------------------------------------------------------
# env-var overrides (highest precedence). Only the three the #72 plan names.
# --------------------------------------------------------------------------
_ENV_OVERRIDES = {
    "TANKIE_LOCALAI_BASE_URL": "localai_base_url",
    "TANKIE_VLM_MODEL": "vlm_model",
    "TANKIE_CAM_URL": "cam_url",
    "TANKIE_GRAB_INTERVAL_S": "grab_interval_s",
    "TANKIE_GRAB_STALE_S": "grab_stale_after_s",
}

# Scalar BrainConfig fields (the nested AutoProfile/Timeouts are handled
# separately). Kept in declaration order for a stable to_dict().
_SCALAR_FIELDS = (
    "localai_base_url",
    "localai_api_key",
    "vlm_model",
    "detection_model",
    "stt_model",
    "tts_model",
    "reflex_hz",
    "deliberate_hz",
    "cam_url",
    "grab_interval_s",
    "grab_stale_after_s",
    "grab_http_poll_s",
    "grab_http_timeout_s",
    "bridge_socket",
    "state_file",
    "data_log_dir",
    "data_log_max_mb",
    "data_log_keep",
    "mcp_port",
)


@dataclass(frozen=True)
class AutoProfile:
    """AI drive limits (issue #31). The bridge is the enforcement layer;
    these are the values the brain clamps to as a UX mirror."""

    enabled: bool = True
    max_speed: int = 40
    max_steer: int = 120


@dataclass(frozen=True)
class Timeouts:
    """Per-call timeouts (seconds) for the LocalAI client (T2)."""

    chat_s: float = 5.0
    detect_s: float = 1.0
    transcribe_s: float = 10.0
    speak_s: float = 10.0


@dataclass(frozen=True)
class BrainConfig:
    """Resolved brain configuration (defaults < brain.yaml < env)."""

    localai_base_url: str = "http://localai.local:8080/v1"
    localai_api_key: str = "sk-0123456789"  # LocalAI accepts any key
    vlm_model: str = "qwen3.8-4b-q4"        # OPEN Q2 (#73) — confirm vision
    detection_model: str = "rfdetr-base"    # Q1 (#73) — detector (config-driven)
    stt_model: str = "whisper-1"        # Q3 (#73) — STT (confirmed deployed)
    tts_model: str = "qwen3-tts-0.6b-custom-voice"  # Q3 (#73) — TTS default (configurable)
    reflex_hz: int = 10                     # D3
    deliberate_hz: int = 1                  # conservative start (1-5 Hz)
    cam_url: str = "http://tankie.local:8888/cam_low/index.m3u8"  # D5 (LL-HLS)
    grab_interval_s: float = 0.2        # T3: how often FrameGrab polls the source
    grab_stale_after_s: float = 3.0     # T3: read_latest() -> None once a frame is this old
    grab_http_poll_s: float = 0.5       # T3: HTTPHLSSource playlist poll interval
    grab_http_timeout_s: float = 2.0    # T3: per-request timeout for the HTTP source
    bridge_socket: str = "/run/tankie/bridge.sock"
    state_file: str = "/var/lib/tankie/state.json"
    data_log_dir: str = "/var/lib/tankie/brain"
    data_log_max_mb: int = 10
    data_log_keep: int = 5
    mcp_port: int = 9000
    auto_profile: AutoProfile = field(default_factory=AutoProfile)
    timeouts: Timeouts = field(default_factory=Timeouts)

    def to_dict(self) -> dict:
        return {
            **{name: getattr(self, name) for name in _SCALAR_FIELDS},
            "auto_profile": self.auto_profile.__dict__,
            "timeouts": self.timeouts.__dict__,
        }


def default_config_path() -> Path:
    """The repo-relative brain.yaml (raspberry_pi/conf/brain.yaml)."""
    return Path(__file__).resolve().parent.parent / "conf" / "brain.yaml"


def _read_yaml(path: Path) -> dict:
    """Load a YAML mapping. Missing/corrupt/non-dict -> {} (never crash)."""
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _coerce_sub(data: dict, key: str, cls):
    """Build a nested dataclass from a YAML sub-dict, else defaults.

    Unknown keys inside the sub-dict are dropped (the loader is permissive;
    strictness lives in the Pydantic schemas, not the config).
    """
    sub = data.get(key)
    if not isinstance(sub, dict):
        return cls()
    valid = {f.name for f in cls.__dataclass_fields__.values()}
    return cls(**{k: v for k, v in sub.items() if k in valid})


def _coerce_scalar(name: str, value):
    """Coerce a string value (from env or YAML) to the field's declared type.

    Env vars are always strings; a numeric field (``grab_interval_s`` etc.)
    must end up as a number so comparisons like ``age > stale_after_s`` work.
    Non-strings and unparseable values are returned unchanged (the Pydantic
    schemas are the strict layer, not the config loader).
    """
    if not isinstance(value, str):
        return value
    target = type(getattr(BrainConfig, name, None))
    if target in (int, float):
        try:
            return target(value)
        except (TypeError, ValueError):
            return value
    return value


def load_config(path: Optional[Path] = None, *, env: Optional[Mapping] = None) -> BrainConfig:
    """Resolve the brain config.

    ``path`` is the brain.yaml to read (defaults to the repo-relative one).
    ``env`` is the environment mapping for overrides (defaults to
    ``os.environ``; inject a dict in tests to avoid touching the real env).
    """
    if env is None:
        env = os.environ
    if path is None:
        path = default_config_path()
    data = _read_yaml(Path(path))

    base = BrainConfig()  # 3) built-in defaults
    values = {name: getattr(base, name) for name in _SCALAR_FIELDS}

    # 2) brain.yaml overrides (only present, non-None keys)
    for name in _SCALAR_FIELDS:
        if name in data and data[name] is not None:
            values[name] = data[name]

    # 1) env vars override both (coerced to the field's type)
    for env_name, field_name in _ENV_OVERRIDES.items():
        val = env.get(env_name)
        if val is not None and val != "":
            values[field_name] = _coerce_scalar(field_name, val)

    return BrainConfig(
        **values,
        auto_profile=_coerce_sub(data, "auto_profile", AutoProfile),
        timeouts=_coerce_sub(data, "timeouts", Timeouts),
    )
