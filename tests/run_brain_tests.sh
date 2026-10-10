#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Test runner for the Tankie AI brain package (issue #75, T4).
#
# Single source of truth for the brain test command — CI (the `brain-tests`
# job in .github/workflows/tests.yml) and a human on a dev machine run the
# EXACT same line.
#
#   bash tests/run_brain_tests.sh
#
# Fully offline: no tank, no Pi, no LocalAI, no real network. The suite uses
# httpx.MockTransport, a fake frame source, and in-memory dicts (see
# tests/brain/README.md).
#
# Prerequisite: a venv (or the system Python) with the brain deps installed:
#   pip install -r raspberry_pi/brain/requirements.txt
#
# `--strict-markers` (issue #75, Q2) fails on any @pytest.mark.* that is not
# registered — today a no-op (no custom markers), tomorrow a guard against a
# stray @pytest.mark.slow / .integration added without a config.
set -euo pipefail

cd "$(dirname "$0")/.."   # repo root

PY="${PYTHON:-python3}"

echo "[brain-tests] running: ${PY} -m pytest tests/brain/ -q --strict-markers"
"${PY}" -m pytest tests/brain/ -q --strict-markers
