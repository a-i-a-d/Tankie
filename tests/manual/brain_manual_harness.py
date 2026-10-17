#!/usr/bin/env python3
"""Offline harness for the Phase 2 brain manual tests (issue #81 T5 + #82 T6).

Runs the **full-mode** brain test cases (S6-07…S6-16) from ``specs/brain.yaml``
through the real :class:`Brain` orchestrator with offline fakes, and evaluates
each case's ``observe.auto`` assertions with the shared assertion engine
(``adapters/base.py``). This proves the Phase 2 wiring + safety invariants are
correct **without any hardware** (no tank, no camera, no LocalAI server).

Usage:
    python3 tests/manual/brain_manual_harness.py            # run + exit 0/1
    python3 tests/manual/brain_manual_harness.py --list     # just list the cases

Exit code 0 when every assertion of every case passes, 1 otherwise.
"""

from __future__ import annotations

import os
import sys
from typing import Any, List

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import yaml  # noqa: E402
from adapters import get_adapter  # noqa: E402
from adapters.base import evaluate_assertions  # noqa: E402

SPEC = os.path.join(_HERE, "specs", "brain.yaml")
# The Phase 2 (full-mode) cases. S6-17 is the live on-tank case (needs hardware).
PHASE2_IDS = ["S6-07", "S6-08", "S6-09", "S6-10", "S6-11",
              "S6-12", "S6-13", "S6-14", "S6-15", "S6-16"]


def _load_cases() -> List[dict]:
    with open(SPEC) as f:
        data = yaml.safe_load(f)
    cases = data if isinstance(data, list) else (data.get("cases") if isinstance(data, dict) else [])
    return [c for c in cases if c.get("id") in PHASE2_IDS]


def main(argv: List[str]) -> int:
    cases = _load_cases()
    if not cases:
        print("error: no Phase 2 brain cases found in", SPEC, file=sys.stderr)
        return 1

    if "--list" in argv:
        for c in cases:
            print(f"{c['id']}  {c['title']}")
        return 0

    adapter = get_adapter("brain", {})
    total = 0
    failed = 0
    for case in cases:
        action = dict(case.get("action") or {})
        observe = case.get("observe") or {}
        auto = observe.get("auto")
        evidence = adapter.run(action)
        results = evaluate_assertions(evidence, auto)
        total += len(results)
        case_ok = all(r.passed for r in results)
        mark = "PASS" if case_ok else "FAIL"
        if not case_ok:
            failed += 1
        print(f"[{mark}] {case['id']}  {case['title']}")
        for r in results:
            if not r.passed:
                print(f"       - {r.path} {r.op} {r.expected!r} (actual={r.actual!r}) {r.detail}")
        if evidence.get("reply", {}).get("error"):
            print(f"       NOTE: {evidence['reply']['error']}")

    print()
    print(f"cases: {len(cases)}  assertions: {total}  "
          f"failed-cases: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
