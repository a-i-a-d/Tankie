"""Layer adapters for the Tankie manual integration test harness (issue #87).

Each adapter wraps one "signal stage" of the tank and exposes a uniform
interface so the runner (``run_manual.py``) can drive every stage the same way:

    adapter = get_adapter("bridge", cfg)
    evidence = adapter.run({"cmd": "drive", "speed": 50, "steer": 0})
    # evidence = {"reply": {...}, "state": {...}, "raw": "..."}

Adapters:
    bridge      — the Pi bridge daemon (Unix socket + state store), issue #29/#33
    raw_serial  — direct pyserial NDJSON to the ESP8266, issue #29
    camera      — mediamtx WebRTC / HLS / RTSP / API probes
    localai     — LocalAI health + one chat/detect call
    ai_control  — run ai_control.py with an injected instruction (issue #87 Q2)
    brain       — run the real reflex loop and record its drive commands (issue #80 T4)

The ``base`` module holds the shared evidence shape, the dotted-path resolver,
and the auto-assertion engine (a small pure, offline-testable core).
"""

from .base import (
    Adapter,
    AssertionResult,
    evaluate_assertions,
    resolve_path,
)

__all__ = [
    "Adapter",
    "AssertionResult",
    "evaluate_assertions",
    "resolve_path",
    "get_adapter",
]


def get_adapter(name: str, cfg: dict) -> "Adapter":
    """Instantiate a layer adapter by name (lazy imports keep deps optional)."""
    name = (name or "").strip().lower()
    if name == "bridge":
        from .bridge import BridgeAdapter
        return BridgeAdapter(cfg)
    if name == "raw_serial":
        from .raw_serial import RawSerialAdapter
        return RawSerialAdapter(cfg)
    if name == "camera":
        from .camera import CameraAdapter
        return CameraAdapter(cfg)
    if name == "localai":
        from .localai import LocalAIAdapter
        return LocalAIAdapter(cfg)
    if name == "ai_control":
        from .ai_control import AIControlAdapter
        return AIControlAdapter(cfg)
    if name == "brain":
        from .brain import BrainAdapter
        return BrainAdapter(cfg)
    raise ValueError(f"unknown adapter layer: {name!r}")
