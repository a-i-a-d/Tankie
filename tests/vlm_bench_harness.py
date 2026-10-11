#!/usr/bin/env python3
"""CI harness for the VLM benchmark tool (tools/vlm-benchmark/).

Fully offline — no LocalAI server, no network. Verifies:
  1. All three scripts py_compile cleanly.
  2. vlm_bench.py --dry-run exits 0 (scene generation works).
  3. report.py produces a valid recommendation from the committed results.
  4. report.py --json emits valid JSON with the expected top-level keys.
  5. scene_gen.py produces valid PNGs (magic bytes + non-trivial size).

Usage:  python3 tests/vlm_bench_harness.py
Exit 0 = all pass, 1 = failure.
"""

from __future__ import annotations

import json
import os
import py_compile
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(REPO, "tools", "vlm-benchmark")
SCRIPTS = ["vlm_bench.py", "scene_gen.py", "report.py"]
RESULTS = os.path.join(TOOLS, "results")

passed = 0
failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}: {detail}")


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=60)


def main() -> int:
    print("VLM benchmark harness (offline)")
    print(f"  repo: {REPO}")
    print(f"  tools: {TOOLS}")
    print()

    # 1. py_compile all scripts
    print("1. py_compile")
    for s in SCRIPTS:
        path = os.path.join(TOOLS, s)
        if not os.path.isfile(path):
            check(f"py_compile {s}", False, f"missing: {path}")
            continue
        try:
            py_compile.compile(path, doraise=True)
            check(f"py_compile {s}", True)
        except py_compile.PyCompileError as e:
            check(f"py_compile {s}", False, str(e))

    # 2. dry-run
    print("2. vlm_bench.py --dry-run")
    r = run([sys.executable, os.path.join(TOOLS, "vlm_bench.py"), "--dry-run"])
    check("dry-run exit 0", r.returncode == 0, r.stderr[:200])
    check("dry-run prints scene sizes", "DRY-RUN" in r.stdout, r.stdout[:200])

    # 3. report.py on committed results
    print("3. report.py (committed results)")
    result_files = sorted(
        os.path.join(RESULTS, f) for f in os.listdir(RESULTS) if f.endswith(".jsonl")
    ) if os.path.isdir(RESULTS) else []
    if not result_files:
        check("results file present", False, f"no .jsonl in {RESULTS}")
    else:
        check("results file present", True, result_files[0])
        r = run([sys.executable, os.path.join(TOOLS, "report.py"), result_files[0]])
        check("report exit 0", r.returncode == 0, r.stderr[:200])
        check("report has RECOMMENDED", "RECOMMENDED" in r.stdout, r.stdout[:200])
        check("report has ranking", "Overall ranking" in r.stdout, r.stdout[:200])

    # 4. report.py --json
    print("4. report.py --json")
    if result_files:
        r = run([sys.executable, os.path.join(TOOLS, "report.py"), result_files[0], "--json"])
        check("--json exit 0", r.returncode == 0, r.stderr[:200])
        try:
            data = json.loads(r.stdout)
            check("--json valid JSON", True)
            for key in ("records", "models", "recommended", "summary"):
                check(f"--json has '{key}'", key in data, str(list(data.keys())))
        except json.JSONDecodeError as e:
            check("--json valid JSON", False, str(e))

    # 5. scene_gen produces valid PNGs
    print("5. scene_gen PNG validity")
    sys.path.insert(0, TOOLS)
    try:
        import scene_gen
        png = scene_gen.detect_scene()
        check("detect_scene is PNG", png[:8] == b"\x89PNG\r\n\x1a\n", repr(png[:8]))
        check("detect_scene > 1KB", len(png) > 1024, f"{len(png)} bytes")
        frames = scene_gen.sequence_frames(4)
        check("sequence_frames returns 4", len(frames) == 4, f"{len(frames)}")
        check("all frames are PNG", all(f[:8] == b"\x89PNG\r\n\x1a\n" for f in frames))
        for v in ("center", "left", "clear"):
            p = scene_gen.drive_scene(v)
            check(f"drive_scene({v}) is PNG", p[:8] == b"\x89PNG\r\n\x1a\n", repr(p[:8]))
    except Exception as e:
        check("scene_gen import+run", False, f"{type(e).__name__}: {e}")

    # summary
    print()
    print(f"{'=' * 40}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'=' * 40}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
