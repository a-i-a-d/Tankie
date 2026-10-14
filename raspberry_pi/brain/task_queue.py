"""Thread-safe task/goal store — the single channel for changing the goal (issue #82, T6).

The :class:`TaskQueue` holds the current task/goal the tank is working on.
It is the **only** way to change the goal (invariant 3): voice/text never
touch the motors directly; they change the task, and the deliberation loop
(System 2) reads the task each tick and proposes an action.

Semantics (from the #82 spec):
  * ``set(task)``   — set the current task (str). Overwrites the previous.
  * ``current()``   — return the current task (str) or ``None``.
  * ``clear()``     — clear the current task.
  * ``has()``       — is there a current task?

The deliberation loop reads ``current()`` each tick. The CLI/voice/MCP call
``set()``. All methods are thread-safe (a single lock) and never raise.

The cross-process channel (``brain_cli.py`` / the MCP server writing a task
from a *separate* process) is a **task file** (issue #82, Q5/A5) — see
:mod:`brain.brain` for the polling that bridges the file into this in-memory
store. This module stays pure and in-memory (matching its spec + tests).
"""

from __future__ import annotations

import threading
from typing import Optional

__all__ = ["TaskQueue"]


class TaskQueue:
    """A thread-safe single slot holding the current task/goal string.

    ``set()`` overwrites (latest task wins); ``current()`` returns the task
    or ``None``; ``clear()`` empties it; ``has()`` reports presence. This is
    the single channel for changing the goal (invariant 3) — the
    deliberation loop (T5) reads ``current()`` each tick and the
    CLI/voice/MCP call ``set()``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._task: Optional[str] = None

    def set(self, task: str) -> None:
        """Store ``task`` (overwriting any previous value). Never raises."""
        with self._lock:
            self._task = str(task)

    def current(self) -> Optional[str]:
        """Return the current task string, or ``None`` if none is set.

        This is the callable the deliberation loop consumes
        (``task_queue.current()``).
        """
        with self._lock:
            return self._task

    def clear(self) -> None:
        """Empty the slot. Never raises."""
        with self._lock:
            self._task = None

    def has(self) -> bool:
        """``True`` if a task is currently set. Never raises."""
        with self._lock:
            return self._task is not None
