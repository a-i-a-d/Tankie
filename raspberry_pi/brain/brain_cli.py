"""Operator CLI for the Tankie brain (issue #82, T6; A2: a new brain_cli.py).

A separate, ESP-agnostic CLI (keeps ``tankie-serial.py`` focused on the ESP
link) for the cross-process task channel (Q5/A5) and a read-only state view.
It talks to the brain **through the task file** (the established
cross-process pattern, like ``state.json``) — never directly to the motors
(invariant 3).

Commands:
    python3 brain_cli.py task "go find the red ball"   # set the task
    python3 brain_cli.py task clear                    # clear the task
    python3 brain_cli.py state [--pretty]              # print current state

``state`` reads the tank state file (via :func:`read_tank_state`, Q6), the
task file, and the config, and prints a ``Brain.state()``-shaped JSON
snapshot (default) or a human table (``--pretty``). The live reflex /
deliberation counters are in-memory in the running brain process, so the
CLI reports them as ``null`` (they are only populated by ``Brain.state()``
in-process / via the MCP ``state()`` tool, T8).

No IP addresses or model names in this module (A4); the task/state file
paths come from the config source.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, List, Optional

from .brain import read_task_file, write_task_file
from .config import BrainConfig, load_config
from .deliberate import read_tank_state

__all__ = ["build_state", "cmd_task", "cmd_state", "main"]


def _config() -> BrainConfig:
    return load_config()


def build_state(config: BrainConfig) -> dict:
    """Build a ``Brain.state()``-shaped snapshot from the files + config.

    The CLI is a separate process, so it can only see what is on disk:
    the tank state file (Q6) and the task file (Q5). The live reflex /
    deliberation counters are in-memory in the running brain process and are
    reported as ``None`` here (populated by ``Brain.state()`` in-process).
    Never raises.
    """
    tank: Optional[dict] = None
    try:
        state = read_tank_state(config.state_file) if config.state_file else None
        if state is not None:
            tank = state.model_dump(mode="json")
    except Exception:  # noqa: BLE001
        tank = None

    task: Optional[str] = None
    try:
        task = read_task_file(config.task_file) if config.task_file else None
    except Exception:  # noqa: BLE001
        task = None

    return {
        "tank": tank,
        "task": task,
        "last_reflex": None,
        "last_deliberation": None,
        "liveness": {
            "reflex_ticks": None,
            "reflex_stops_sent": None,
            "deliberation_calls": None,
            "deliberation_errors": None,
            "deliberation_valid_actions": None,
        },
        "config": {
            "vlm_model": config.vlm_model,
            "detection_model": config.detection_model,
            "reflex_hz": config.reflex_hz,
            "deliberate_hz": config.deliberate_hz,
            "deliberate_frames": config.deliberate_frames,
            "bridge_socket": config.bridge_socket,
            "state_file": config.state_file,
            "task_file": config.task_file,
        },
    }


def cmd_task(task: Optional[str], config: BrainConfig) -> int:
    """Set (or clear, when ``task`` is None) the task file (Q5/A5)."""
    ok = write_task_file(config.task_file, task)
    if not ok:
        print(f"error: could not write task file {config.task_file}", file=sys.stderr)
        return 1
    if task is None:
        print("task cleared")
    else:
        print(f"task set: {task}")
    return 0


def _render_pretty(state: dict) -> str:
    lines: List[str] = []
    tank = state.get("tank") or {}
    lines.append("Tank state")
    for key in ("battery_v", "speed", "steer", "pan", "tilt", "net_mode", "net_ip"):
        if tank.get(key) is not None:
            lines.append(f"  {key}: {tank.get(key)}")
    if tank.get("link_up") is not None:
        lines.append(f"  link_up: {tank.get('link_up')}")
    if tank.get("watchdog_fired") is not None:
        lines.append(f"  watchdog_fired: {tank.get('watchdog_fired')}")
    if not tank:
        lines.append("  (no tank state available)")
    lines.append("")
    lines.append(f"Task: {state.get('task') or '(none)'}")
    cfg = state.get("config") or {}
    lines.append("")
    lines.append("Config")
    for key in ("vlm_model", "detection_model", "reflex_hz", "deliberate_hz",
                "deliberate_frames", "bridge_socket", "state_file", "task_file"):
        if key in cfg:
            lines.append(f"  {key}: {cfg.get(key)}")
    return "\n".join(lines)


def cmd_state(config: BrainConfig, *, pretty: bool = False) -> int:
    """Print the current state (JSON by default, ``--pretty`` for a table)."""
    state = build_state(config)
    if pretty:
        print(_render_pretty(state))
    else:
        print(json.dumps(state, indent=2, sort_keys=False))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="brain_cli",
        description="Operator CLI for the Tankie brain (task + state).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    task_p = sub.add_parser("task", help="set or clear the current task")
    task_p.add_argument(
        "value",
        nargs="*",
        help="the task text; use 'clear' (or no value) to clear the task",
    )

    state_p = sub.add_parser("state", help="print the current state")
    state_p.add_argument(
        "--pretty",
        action="store_true",
        help="print a human-readable table instead of JSON",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    config = _config()
    if args.command == "task":
        value = list(args.value)
        if not value or value == ["clear"]:
            return cmd_task(None, config)
        # Join multi-word tasks (the shell may split them).
        return cmd_task(" ".join(value), config)
    if args.command == "state":
        return cmd_state(config, pretty=args.pretty)
    return 2


if __name__ == "__main__":
    sys.exit(main())
