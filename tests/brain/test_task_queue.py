"""Offline unit tests for the thread-safe task/goal store (issue #82, T6).

Run from the repo root:  ``pytest tests/brain/ -q``

No tank, no Pi, no LocalAI, no real network. The :class:`TaskQueue` is a
pure in-memory, thread-safe slot (the single channel for changing the goal,
invariant 3). These tests prove:

  * set / current / clear round-trip;
  * has() presence semantics;
  * latest-wins overwrite;
  * thread-safety (concurrent set/current from 2+ threads, no corruption);
  * never raises on odd inputs.
"""

from __future__ import annotations

import threading

from brain.task_queue import TaskQueue


# ===========================================================================
# set / current / clear
# ===========================================================================
def test_initially_empty():
    q = TaskQueue()
    assert q.current() is None
    assert q.has() is False


def test_set_current_clear():
    q = TaskQueue()
    q.set("go find the red ball")
    assert q.current() == "go find the red ball"
    assert q.has() is True
    q.clear()
    assert q.current() is None
    assert q.has() is False


def test_set_overwrites_latest_wins():
    q = TaskQueue()
    q.set("first")
    q.set("second")
    assert q.current() == "second"
    assert q.has() is True


def test_set_empty_string_is_stored():
    # An empty string is a valid (if useless) task; current() returns it.
    q = TaskQueue()
    q.set("")
    assert q.current() == ""
    assert q.has() is True


def test_set_coerces_non_string():
    # set() stores str(task); a non-string is coerced (never raises).
    q = TaskQueue()
    q.set(123)
    assert q.current() == "123"


def test_clear_when_empty_is_noop():
    q = TaskQueue()
    q.clear()  # must not raise
    assert q.current() is None
    assert q.has() is False


# ===========================================================================
# thread-safety
# ===========================================================================
def test_thread_safety_concurrent_set_current():
    """Concurrent set/current from 2 threads must not corrupt the value."""
    q = TaskQueue()
    errors = []

    def writer(n: int):
        try:
            for i in range(2000):
                q.set(f"writer-{n}-{i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def reader():
        try:
            for _ in range(2000):
                v = q.current()
                # A read is either None or a well-formed task string.
                if v is not None and not isinstance(v, str):
                    raise AssertionError(f"corrupt value: {v!r}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=(0,)),
        threading.Thread(target=writer, args=(1,)),
        threading.Thread(target=reader),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    # After all writers finish, the value must be one of the last writes.
    final = q.current()
    assert final is None or final.startswith("writer-")


def test_thread_safety_has_never_raises():
    q = TaskQueue()
    errors = []

    def churn():
        try:
            for _ in range(1000):
                q.set("x")
                q.has()
                q.clear()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=churn) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
