"""Thread-safe pending-action slot — the System 2 -> System 1 hand-off (issue #81, T5).

The deliberation loop (T5) *proposes* an :class:`~brain.schemas.Action` and
writes it here; the reflex loop (T4) *decides* by reading it each tick. This
is the single point of contact between the two loops and enforces the two
core invariants:

* **Invariant 1** (single writer) — the deliberation loop never talks to the
  bridge directly; it only writes to this slot. The reflex loop is the sole
  writer of drive commands to the bridge.
* **Invariant 2** (reflex wins) — the reflex loop reads the slot each tick and
  may pre-empt the pending action (e.g. a center-lane obstacle always stops).

Semantics:
  * ``set(action_dict)`` overwrites the previous value — **latest
    deliberation wins** (the VLM's newest proposal replaces the older one).
  * ``get()`` returns the current dict or ``None``. It is the exact
    ``Callable[[], Optional[dict]]`` the merged :class:`~brain.reflex.ReflexLoop`
    expects as its ``pending_action`` argument, so the T6 orchestrator wires
    ``ReflexLoop(..., pending_action=slot.get)`` with zero changes to T4.
  * ``clear()`` empties the slot (e.g. after the reflex loop consumes a
    one-shot ``pan``/``tilt``/``say`` action).
  * ``has()`` reports whether a value is present.

All methods are thread-safe (a single lock) and never raise.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

__all__ = ["PendingAction"]


class PendingAction:
    """A thread-safe single slot holding the latest proposed ``Action`` dict.

    ``set()`` overwrites (latest deliberation wins); ``get()`` returns the
    current value or ``None``; ``clear()`` empties the slot; ``has()`` reports
    presence. The slot is the only channel between the deliberation loop
    (writer) and the reflex loop (reader) — the reflex loop is the sole writer
    to the bridge (invariant 1).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: Optional[dict] = None

    def set(self, action: dict) -> None:
        """Store ``action`` (overwriting any previous value). Never raises."""
        with self._lock:
            self._value = dict(action)  # copy: decouple from the caller's dict

    def get(self) -> Optional[dict]:
        """Return the current action dict, or ``None`` if the slot is empty.

        This is the callable the reflex loop consumes
        (``ReflexLoop(pending_action=slot.get)``).
        """
        with self._lock:
            if self._value is None:
                return None
            return dict(self._value)  # copy: the reader can't mutate the slot

    def clear(self) -> None:
        """Empty the slot. Never raises."""
        with self._lock:
            self._value = None

    def has(self) -> bool:
        """``True`` if an action is currently stored. Never raises."""
        with self._lock:
            return self._value is not None
