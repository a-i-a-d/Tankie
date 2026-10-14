"""Offline unit tests for the brain operator CLI (issue #82, T6; A2).

Run from the repo root:  ``pytest tests/brain/ -q``

No tank, no Pi, no LocalAI, no real network. The CLI is a separate process
that talks to the brain **through the task file** (Q5/A5) and reads the tank
state file (Q6/A6). These tests exercise the pure functions and the command
dispatch (``main``) with temp file paths, plus the ``--once`` entry point.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from brain import brain_cli
from brain.brain_cli import build_state, cmd_state, cmd_task, main as cli_main
from brain.config import BrainConfig


def _cfg(tmp_path) -> BrainConfig:
    return BrainConfig(
        **{
            **BrainConfig().__dict__,
            "task_file": str(tmp_path / "task.json"),
            "state_file": str(tmp_path / "state.json"),
        }
    )


# ===========================================================================
# task command (Q5/A5)
# ===========================================================================
def test_cmd_task_set_writes_file(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    rc = cmd_task("go find the red ball", cfg)
    assert rc == 0
    data = json.loads((tmp_path / "task.json").read_text())
    assert data["task"] == "go find the red ball"
    out = capsys.readouterr().out
    assert "task set" in out


def test_cmd_task_clear_writes_null(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    cmd_task("something", cfg)
    rc = cmd_task(None, cfg)
    assert rc == 0
    data = json.loads((tmp_path / "task.json").read_text())
    assert data["task"] is None
    assert "task cleared" in capsys.readouterr().out


def test_cli_main_task_set(tmp_path, monkeypatch):
    # main() dispatch: `task <words...>` joins the words.
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(brain_cli, "_config", lambda: cfg)
    rc = cli_main(["task", "go", "find", "the", "red", "ball"])
    assert rc == 0
    data = json.loads((tmp_path / "task.json").read_text())
    assert data["task"] == "go find the red ball"


def test_cli_main_task_clear(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(brain_cli, "_config", lambda: cfg)
    rc = cli_main(["task", "clear"])
    assert rc == 0
    data = json.loads((tmp_path / "task.json").read_text())
    assert data["task"] is None


def test_cli_main_task_bare_clears(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(brain_cli, "_config", lambda: cfg)
    rc = cli_main(["task"])
    assert rc == 0
    data = json.loads((tmp_path / "task.json").read_text())
    assert data["task"] is None


# ===========================================================================
# state command (A3 / Q7)
# ===========================================================================
def test_cmd_state_json_default(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    # Seed the tank state file (Q6/A6) and the task file (Q5/A5).
    (tmp_path / "state.json").write_text(json.dumps({
        "link_up": True,
        "last_state": {"battery": 12.97, "pan": 90, "tilt": 90,
                       "net_mode": "sta", "net_ip": "192.0.2.42"},
    }))
    (tmp_path / "task.json").write_text(json.dumps({"task": "find the ball"}))

    rc = cmd_state(cfg)
    assert rc == 0
    out = capsys.readouterr().out
    state = json.loads(out)
    assert state["task"] == "find the ball"
    assert state["tank"]["battery_v"] == 12.97
    assert state["tank"]["pan"] == 90
    for key in ("tank", "task", "last_reflex", "last_deliberation", "liveness", "config"):
        assert key in state


def test_cmd_state_pretty(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    (tmp_path / "state.json").write_text(json.dumps({
        "link_up": True,
        "last_state": {"battery": 12.97, "pan": 90},
    }))
    (tmp_path / "task.json").write_text(json.dumps({"task": "find the ball"}))
    rc = cmd_state(cfg, pretty=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Tank state" in out
    assert "battery_v: 12.97" in out
    assert "Task: find the ball" in out


def test_cli_main_state(tmp_path, capsys, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(brain_cli, "_config", lambda: cfg)
    rc = cli_main(["state"])
    assert rc == 0
    state = json.loads(capsys.readouterr().out)
    assert "config" in state


def test_build_state_no_files(tmp_path):
    """With no state/task files, state() still returns the full shape."""
    cfg = _cfg(tmp_path)
    state = build_state(cfg)
    assert state["tank"] is None
    assert state["task"] is None
    assert state["last_reflex"] is None
    assert state["last_deliberation"] is None
    for key in ("tank", "task", "last_reflex", "last_deliberation", "liveness", "config"):
        assert key in state


# ===========================================================================
# __main__ --once (A1 / A8): one bounded cycle, clean stop, exit 0
# ===========================================================================
def test_main_once_runs_and_exits_zero(tmp_path, monkeypatch):
    """``python3 -m brain --once`` starts, settles one cycle, stops, exits 0."""
    from brain import __main__ as brain_main

    cfg = _cfg(tmp_path)
    monkeypatch.setattr(brain_main, "load_config", lambda: cfg)

    # Inject fakes so no real component is constructed.
    class FB:
        def send(self, c): return {"ok": True}
    class FG:
        def start(self): pass
        def stop(self): pass
        def read_latest(self): return None
        def read_sequence(self, n=4): return []
    class FL:
        def close(self): pass
    class FR:
        def start(self): pass
        def stop(self): pass
        ticks = 0
        stops_sent = 0
        @property
        def last_result(self): return None
    class FD:
        def start(self): pass
        def stop(self): pass
        calls = 0
        errors = 0
        valid_actions = 0

    def fake_brain(config, **kw):
        from brain.brain import Brain
        return Brain(config, bridge=FB(), grabber=FG(), localai=FL(),
                     reflex_loop=FR(), deliberation_loop=FD())

    monkeypatch.setattr(brain_main, "Brain", fake_brain)

    rc = brain_main.main(["--once"])
    assert rc == 0
