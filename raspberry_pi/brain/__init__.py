"""Tankie AI brain (issue #69, Phase 1 — T1 scaffolding).

This package is the "brain" of the tank: the deliberation (System 2) and
reflex (System 1) loops that turn camera frames + voice into drive
commands, speaking through the LocalAI server (A1) and the serial bridge
(#33).

Phase 1 builds the brain incrementally:
  * T1 (#72) — the Pydantic contract (`schemas`), the single config source
    (`config`), and the dependency list (`requirements.txt`).
  * T2 (#73) — the LocalAI client (`localai_client`): one typed, config-driven
    surface for every LocalAI endpoint (chat / detect / depth / transcribe /
    speak / health) with a uniform `{"ok": ...}` contract that never raises.

The reflex/deliberation loops (Phase 2) are the only callers of the client.

Layout (D10):
    raspberry_pi/brain/     this package (the brain)
    raspberry_pi/conf/      single config source (brain.yaml)
    tests/brain/            offline unit tests (pytest)
"""

__version__ = "0.1.0"
