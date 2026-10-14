"""Shared core for the manual test adapters (issue #87).

This module is deliberately dependency-free (stdlib only) and pure, so the
assertion engine and path resolver can be unit-tested offline and reused by a
future web UI (the "design for that" requirement in issue #87).

Key pieces:
    * :class:`Adapter`        — the uniform interface every layer adapter implements
    * :func:`resolve_path`    — dotted-path lookup into nested evidence (dicts + lists)
    * :func:`evaluate_assertions` — turn a list of YAML assertions into pass/fail results
    * :class:`AssertionResult` — one evaluated assertion (serialisable to the report)
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, asdict
from typing import Any, List, Mapping, Optional, Tuple

__all__ = [
    "Adapter",
    "AssertionResult",
    "evaluate_assertions",
    "resolve_path",
    "SUPPORTED_OPS",
]


# ---------------------------------------------------------------------------
# Dotted-path resolution
# ---------------------------------------------------------------------------
def resolve_path(obj: Any, dotted: str) -> Tuple[bool, Any]:
    """Resolve a dotted path (e.g. ``reply.clamped.speed``) into a nested value.

    Supports dicts (string keys) and lists/tuples (integer keys, e.g.
    ``evidence.0.name``). Returns ``(found, value)`` where ``found`` is False
    when any segment is missing or the type does not support the key. Never
    raises — a bad path simply resolves to ``(False, None)``.
    """
    if dotted is None or dotted == "":
        return True, obj
    cur = obj
    for part in str(dotted).split("."):
        if isinstance(cur, dict):
            if part in cur:
                cur = cur[part]
            else:
                return False, None
        elif isinstance(cur, (list, tuple)):
            try:
                idx = int(part)
            except (TypeError, ValueError):
                return False, None
            if 0 <= idx < len(cur):
                cur = cur[idx]
            else:
                return False, None
        else:
            return False, None
    return True, cur


# ---------------------------------------------------------------------------
# Auto-assertion engine
# ---------------------------------------------------------------------------
@dataclass
class AssertionResult:
    """One evaluated auto-assertion (JSON-serialisable for the report)."""

    path: str
    op: str
    expected: Any
    actual: Any
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# Comparison operators. Each takes (actual, expected) and returns a bool.
# They are total: a missing/None actual yields False (the assertion fails),
# which is the safe behaviour for a test harness.
def _cmp_eq(a, e):
    return a == e


def _cmp_ne(a, e):
    return a is not None and a != e


def _cmp_gt(a, e):
    try:
        return a is not None and e is not None and a > e
    except TypeError:
        return False


def _cmp_lt(a, e):
    try:
        return a is not None and e is not None and a < e
    except TypeError:
        return False


def _cmp_gte(a, e):
    try:
        return a is not None and e is not None and a >= e
    except TypeError:
        return False


def _cmp_lte(a, e):
    try:
        return a is not None and e is not None and a <= e
    except TypeError:
        return False


def _cmp_contains(a, e):
    try:
        return e in a
    except TypeError:
        return False


SUPPORTED_OPS = {
    "eq": _cmp_eq,
    "ne": _cmp_ne,
    "gt": _cmp_gt,
    "lt": _cmp_lt,
    "gte": _cmp_gte,
    "lte": _cmp_lte,
    "contains": _cmp_contains,
}


def evaluate_assertions(
    evidence: Mapping[str, Any],
    assertions: Optional[List[Mapping[str, Any]]],
) -> List[AssertionResult]:
    """Evaluate a list of YAML assertions against an evidence dict.

    Each assertion is a mapping with a required ``path`` and exactly one
    operator key from :data:`SUPPORTED_OPS` (or the special ``exists`` bool).
    Example::

        - path: reply.ok
          eq: true
        - path: reply.clamped.speed
          eq: 40
        - path: raw
          contains: "hello"
        - path: state.link_up
          exists: true

    A missing path is a **failure** (not a skip), because a test that cannot
    find the evidence it asserts on is not passing. Returns one
    :class:`AssertionResult` per assertion, in order.
    """
    results: List[AssertionResult] = []
    for a in (assertions or []):
        if not isinstance(a, dict) or "path" not in a:
            results.append(AssertionResult(
                path=str(a), op="malformed", expected=None, actual=None,
                passed=False, detail="assertion must be a mapping with a 'path'",
            ))
            continue
        path = str(a["path"])
        found, actual = resolve_path(evidence, path)

        # Special op: existence check (no comparison value).
        if "exists" in a:
            expected = bool(a["exists"])
            passed = found == expected
            results.append(AssertionResult(
                path=path, op="exists", expected=expected,
                actual=("<present>" if found else "<missing>"),
                passed=passed,
                detail="" if passed else f"path {path!r} {'present' if found else 'missing'}",
            ))
            continue

        # Standard comparison op.
        op_key = next((k for k in SUPPORTED_OPS if k in a), None)
        if op_key is None:
            results.append(AssertionResult(
                path=path, op="malformed", expected=None, actual=actual,
                passed=False,
                detail="no operator (eq/ne/gt/lt/gte/lte/contains/exists) given",
            ))
            continue
        expected = a[op_key]
        if not found:
            passed = False
            detail = f"path {path!r} not found in evidence"
        else:
            try:
                passed = bool(SUPPORTED_OPS[op_key](actual, expected))
            except Exception as exc:  # noqa: BLE001 - report, don't crash the harness
                passed = False
                detail = f"comparison raised {type(exc).__name__}: {exc}"
            else:
                detail = "" if passed else f"actual={actual!r} expected {op_key} {expected!r}"
        results.append(AssertionResult(
            path=path, op=op_key, expected=expected, actual=actual,
            passed=passed, detail=detail,
        ))
    return results


# ---------------------------------------------------------------------------
# Adapter interface
# ---------------------------------------------------------------------------
class Adapter(abc.ABC):
    """Uniform interface for a layer adapter.

    Subclasses implement :meth:`run` (execute one action and return evidence)
    and :meth:`preflight` (check that the dependencies for this layer are
    present, without touching the tank). :meth:`name` is used for logging and
    the report.
    """

    #: adapter name (override in subclasses)
    name: str = "base"

    def __init__(self, cfg: Optional[Mapping[str, Any]] = None):
        self.cfg = dict(cfg or {})

    # -- required -----------------------------------------------------------
    @abc.abstractmethod
    def run(self, cmd: Mapping[str, Any]) -> dict:
        """Execute one action and return an evidence dict.

        Evidence shape (all keys optional, but ``reply`` is the canonical one)::

            {
              "reply": {...},   # the layer's direct response to the action
              "state": {...},   # the bridge state store snapshot (if relevant)
              "raw":   "...",   # raw text (serial log, HTTP body, subprocess stdout)
            }

        Must never raise on a tank/network failure — degrade to a
        ``{"reply": {"ok": False, "error": "..."}}`` evidence instead, so the
        operator always gets a PASS/FAIL/SKIP prompt.
        """

    # -- optional -----------------------------------------------------------
    def preflight(self) -> List[Tuple[str, bool, str]]:
        """Return a list of ``(check_name, ok, detail)`` tuples.

        Used by the runner to warn the operator before a suite starts. Default
        is a single "present" check so every adapter has a preflight.
        """
        return [(f"{self.name} adapter present", True, "ok")]

    def describe(self) -> str:
        """One-line human description of what this layer tests."""
        return f"layer adapter: {self.name}"
