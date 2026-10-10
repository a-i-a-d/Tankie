"""Tankie AI brain (issue #69, Phase 1 — T1 scaffolding).

This package is the "brain" of the tank: the deliberation (System 2) and
reflex (System 1) loops that turn camera frames + voice into drive
commands, speaking through the LocalAI server (A1) and the serial bridge
(#33).

Phase 1 (this issue, #72) is scaffolding only: the Pydantic contract every
later task speaks in (`schemas`), the single config source (`config`), and
the dependency list (`requirements.txt`). No behaviour code yet — the
loops land in Phase 2.

Layout (D10):
    raspberry_pi/brain/     this package (the brain)
    raspberry_pi/conf/      single config source (brain.yaml)
    tests/brain/            offline unit tests (pytest)
"""

__version__ = "0.1.0"
