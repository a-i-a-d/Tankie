#!/usr/bin/env python3
"""CI harness for the VLM benchmark tool (tools/vlm-benchmark/).

Fully offline — no LocalAI server, no network. Verifies:
  1. All three scripts py_compile cleanly.
  2. vlm_bench.py --dry-run exits 0 (scene generation works).
  3. report.py produces a valid recommendation from the committed results.
  4. report.py --json emits valid JSON with the expected top-level keys.
  5. scene_gen.py produces valid PNGs (magic bytes + non-trivial size).
  6. _extract_answer falls back to `reasoning` when `content` is empty.

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

    # 6. _extract_answer: reasoning-model fallback (content empty -> reasoning)
    print("6. _extract_answer (reasoning fallback)")
    sys.path.insert(0, TOOLS)
    try:
        import vlm_bench
        # standard model: content wins
        check("content preferred", vlm_bench._extract_answer({"content": "{\"a\":1}", "reasoning": "{\"b\":2}"}) == "{\"a\":1}")
        # reasoning model: content empty -> reasoning
        check("empty content -> reasoning", vlm_bench._extract_answer({"content": "", "reasoning": "{\"a\":1}"}) == "{\"a\":1}")
        # reasoning model: content None -> reasoning
        check("None content -> reasoning", vlm_bench._extract_answer({"content": None, "reasoning": "{\"a\":1}"}) == "{\"a\":1}")
        # whitespace-only content -> reasoning
        check("whitespace content -> reasoning", vlm_bench._extract_answer({"content": "   ", "reasoning": "{\"a\":1}"}) == "{\"a\":1}")
        # neither present -> empty string
        check("no answer -> empty", vlm_bench._extract_answer({"content": None, "reasoning": None}) == "")
        check("empty dict -> empty", vlm_bench._extract_answer({}) == "")
    except Exception as e:
        check("_extract_answer import+run", False, f"{type(e).__name__}: {e}")


    # 7. detect scene shape correctness (regression: the "triangle" was a square)
    print("7. detect scene shape correctness")
    sys.path.insert(0, TOOLS)
    try:
        import scene_gen
        import vlm_bench
        # DETECT_EXPECTED must not include a shape that isn't drawn
        check("DETECT_EXPECTED has no 'triangle'",
              "triangle" not in vlm_bench.DETECT_EXPECTED,
              str(vlm_bench.DETECT_EXPECTED))
        check("DETECT_EXPECTED has 'circle'", "circle" in vlm_bench.DETECT_EXPECTED, str(vlm_bench.DETECT_EXPECTED))
        check("DETECT_EXPECTED has 'square'", "square" in vlm_bench.DETECT_EXPECTED, str(vlm_bench.DETECT_EXPECTED))

        # The blue region must be a SOLID rectangle (a square), not a triangle.
        # Decode the PNG (pure stdlib) and check the blue region's fill ratio.
        import zlib, struct
        png = scene_gen.detect_scene()
        d = png
        i = 8; idat = b""; W = H = None
        while i < len(d):
            ln = struct.unpack(">I", d[i:i+4])[0]; typ = d[i+4:i+8]; data = d[i+8:i+8+ln]; i += 12 + ln
            if typ == b"IHDR":
                W, H = struct.unpack(">II", data[:8])
            elif typ == b"IDAT":
                idat += data
        raw = zlib.decompress(idat); bpp = 3; stride = W * bpp
        out = bytearray(); prev = bytearray(stride); pos = 0
        for y in range(H):
            f = raw[pos]; pos += 1
            line = bytearray(raw[pos:pos+stride]); pos += stride
            for x in range(stride):
                a = line[x-bpp] if x >= bpp else 0
                b = prev[x]
                c = prev[x-bpp] if x >= bpp else 0
                if f == 1: line[x] = (line[x]+a) & 255
                elif f == 2: line[x] = (line[x]+b) & 255
                elif f == 3: line[x] = (line[x]+((a+b)>>1)) & 255
                elif f == 4:
                    p = a+b-c; pa = abs(p-a); pb = abs(p-b); pc = abs(p-c)
                    pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                    line[x] = (line[x]+pr) & 255
            out += line; prev = line
        px = bytes(out)
        def pixel(x, y):
            o = (y*W+x)*3
            return (px[o], px[o+1], px[o+2])
        blue = (40, 80, 220)
        xs = [x for y in range(H) for x in range(W) if pixel(x, y) == blue]
        ys = [y for y in range(H) for x in range(W) if pixel(x, y) == blue]
        minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
        total = (maxx-minx+1) * (maxy-miny+1)
        filled = sum(1 for y in range(miny, maxy+1) for x in range(minx, maxx+1) if pixel(x, y) == blue)
        ratio = filled / total
        check("blue region is a solid square (fill ratio >= 0.95)",
              ratio >= 0.95, f"fill ratio {ratio:.2f} (1.0=square, ~0.5=triangle)")
    except Exception as e:
        check("detect scene shape check", False, f"{type(e).__name__}: {e}")

    # 8. report.py --html (self-contained HTML report)
    print("8. report.py --html")
    if result_files:
        import tempfile, os as _os
        html_path = _os.path.join(tempfile.gettempdir(), "vlm_bench_test.html")
        r = run([sys.executable, _os.path.join(TOOLS, "report.py"), result_files[0], "--html", html_path])
        check("--html exit 0", r.returncode == 0, r.stderr[:200])
        if _os.path.isfile(html_path):
            h_txt = open(html_path, encoding="utf-8").read()
            check("html starts with doctype", h_txt.startswith("<!doctype html>"), h_txt[:60])
            check("html has inline <style>", "<style>" in h_txt and "</style>" in h_txt)
            check("html has no external CSS", '<link rel="stylesheet"' not in h_txt)
            check("html has no <script>", "<script" not in h_txt)
            check("html has per-task table", "Per-task results" in h_txt)
            check("html has ranking table", "Overall ranking" in h_txt)
            check("html has per-model detail", "Per-model detail" in h_txt)
            check("html has <details> elements", "<details>" in h_txt)
            check("html has summary cards", 'class="cards"' in h_txt)
            check("html has legend", "hover any cell" in h_txt)
            check("html has pass cells", 'class="pass"' in h_txt)
            check("html has fail cells", 'class="fail"' in h_txt)
            check("html file size > 10KB", len(h_txt) > 10_000, f"{len(h_txt)} bytes")
            _os.unlink(html_path)
        else:
            check("html file created", False, f"missing: {html_path}")
    else:
        check("--html (no results)", False, "no result files to test with")

    # summary
    print()
    print(f"{'=' * 40}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'=' * 40}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
