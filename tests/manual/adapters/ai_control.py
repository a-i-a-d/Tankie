"""AI control adapter (legacy ``ai_control.py``, issue #33) — Stage 5.

Runs the legacy LocalAI control client as a **bounded** subprocess with an
injected instruction (issue #87 Q2: ``TANKIE_INSTRUCTION`` env var), lets it
observe + act for ``duration_s``, then stops it and returns the captured
stdout as evidence. The operator PASS/FAILs on the tank's real behaviour; the
auto-checks look for the tool-call / bridge-reply signatures in the output.

``ai_control.py`` is an infinite observe->decide->act loop, so we bound it by
wall-clock and kill it (it degrades gracefully on a dead link, so a kill is
safe). Requires the client's deps (``ai-control/LocalAI/requirements.txt``).

Config keys:
    repo_root   — default: auto-detected from this file's location
    script      — default ``ai-control/LocalAI/ai_control.py`` (repo-relative)
    python      — interpreter, default ``python3``
    instruction — default instruction (overridable per-test via ``cmd``)
    duration_s  — how long to let it run, default 10.0
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Any, List, Mapping, Tuple

from .base import Adapter

DEFAULT_DURATION_S = 10.0
DEFAULT_SCRIPT = "ai-control/LocalAI/ai_control.py"


def _detect_repo_root() -> str:
    # This file lives at <repo>/tests/manual/adapters/ai_control.py
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "..", ".."))


class AIControlAdapter(Adapter):
    name = "ai_control"

    def __init__(self, cfg: Mapping[str, Any] | None = None):
        super().__init__(cfg)
        self.repo_root = self.cfg.get("repo_root") or _detect_repo_root()
        self.script = self.cfg.get("script", DEFAULT_SCRIPT)
        self.python = self.cfg.get("python", "python3")
        self.instruction = self.cfg.get("instruction")
        self.duration_s = float(self.cfg.get("duration_s", DEFAULT_DURATION_S))

    def _script_path(self) -> str:
        p = self.script
        if not os.path.isabs(p):
            p = os.path.join(self.repo_root, p)
        return p

    def run(self, cmd: Mapping[str, Any]) -> dict:
        instruction = cmd.get("instruction") or self.instruction
        script = self._script_path()
        if not os.path.exists(script):
            return {"reply": {"ok": False, "error": f"ai_control.py not found: {script}"}}

        env = os.environ.copy()
        if instruction:
            env["TANKIE_INSTRUCTION"] = instruction

        start = time.time()
        proc = subprocess.Popen(
            [self.python, script],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env, cwd=self.repo_root,
            start_new_session=True,  # so we can kill the whole process group
        )
        out = ""
        try:
            # Let it run for the bounded duration, then drain what it printed.
            time.sleep(self.duration_s)
        finally:
            try:
                out = proc.stdout.read() if proc.stdout else ""
            except Exception:  # noqa: BLE001
                out = ""
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                proc.kill()

        elapsed = round(time.time() - start, 2)
        # Signature checks the operator / auto-checks can rely on.
        has_tool_call = ("drive_tank" in out or "camera_control" in out
                         or "camera_sweep" in out or "bridge" in out.lower())
        has_generated = "Generated Text" in out or "response_message" in out
        crashed = ("Traceback (most recent call last)" in out)
        return {
            "reply": {
                "ok": (not crashed) and (has_generated or has_tool_call),
                "instruction": instruction,
                "elapsed_s": elapsed,
                "saw_tool_call": has_tool_call,
                "saw_generated_text": has_generated,
                "crashed": crashed,
            },
            "raw": out[-20000:],  # cap evidence size
        }

    def preflight(self) -> List[Tuple[str, bool, str]]:
        script = self._script_path()
        ok = os.path.exists(script)
        return [("ai_control.py present", ok,
                 f"{script} {'found' if ok else 'MISSING'}")]

    def describe(self) -> str:
        return ("legacy LocalAI control client (ai_control.py) — one bounded "
                "observe->decide->act cycle with an injected instruction")
