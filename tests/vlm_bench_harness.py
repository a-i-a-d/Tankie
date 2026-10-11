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

    # 9. prompt in records + HTML report (audit trail)
    print("9. prompt in records + HTML")
    src_bench = open(os.path.join(TOOLS, "vlm_bench.py"), encoding="utf-8").read()
    check("vlm_bench record stores 'prompt'", '"prompt": _prompt_for(task)' in src_bench)
    check("vlm_bench record keeps 'prompt_sha'", '"prompt_sha":' in src_bench)

    import tempfile
    rec_new = {
        "ts": "2026-01-01T00:00:00+00:00", "base": "http://x/v1", "model": "m-new",
        "task": "detect", "variant": "", "n_images": 1, "max_tokens": 500,
        "prompt": "Look at the image. PROMPT_MARKER_NEW.", "prompt_sha": "abc123def456",
        "http": 200, "elapsed_s": 1.0, "content": '{"objects": []}', "reasoning": None,
        "parsed": {"objects": []}, "error": None,
        "grade": {"valid_json": True, "required_keys": False, "semantic": False},
    }
    rec_legacy = dict(rec_new, model="m-legacy", prompt=None, prompt_sha="76447cbbf065")
    rec_run1 = dict(rec_new, model="m-runs", run=1)
    rec_run2 = dict(rec_new, model="m-runs", run=2)
    tmp = tempfile.mkdtemp()
    jl = os.path.join(tmp, "records.jsonl")
    with open(jl, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(rec_new) + "\n")
        fh.write(json.dumps(rec_legacy) + "\n")
        fh.write(json.dumps(rec_run1) + "\n")
        fh.write(json.dumps(rec_run2) + "\n")
    html_path = os.path.join(tmp, "report.html")
    r = run([sys.executable, os.path.join(TOOLS, "report.py"), jl, "--html", html_path])
    check("--html (synthetic records) exit 0", r.returncode == 0, r.stderr[:200])
    if os.path.isfile(html_path):
        h_txt = open(html_path, encoding="utf-8").read()
        check("html shows full prompt for new records", "PROMPT_MARKER_NEW" in h_txt)
        check("html has prompt label", 'class="lbl">prompt' in h_txt)
        check("html prompt block styled", 'class="prompt"' in h_txt)
        check("html falls back to sha for legacy records",
              "prompt not stored" in h_txt and "76447cbbf065" in h_txt)
        # 10. cold/warm labels in the per-model detail section
        check("html has cold badge css", '.temp.cold' in h_txt)
        check("html has warm badge css", '.temp.warm' in h_txt)
        check("html run 1 labeled cold", '<span class="temp cold">cold</span>' in h_txt)
        check("html run 2 labeled warm", '<span class="temp warm">warm</span>' in h_txt)
        check("html records without run number labeled cold (m-new)",
              h_txt.count('<span class="temp cold">cold</span>') >= 2)
    else:
        check("html (synthetic) created", False, f"missing: {html_path}")

    # 11. drive grading — direction-aware steer (C1/C2 fix)
    #     wheels.h: steer > 0 = LEFT, steer < 0 = RIGHT.
    #     "left" scene = obstacle on the LEFT -> correct = steer RIGHT (negative).
    print("11. drive grading (direction-aware)")
    try:
        def _d(variant, parsed):
            rec = {"error": None, "parsed": parsed, "content": json.dumps(parsed)}
            return vlm_bench._grade("drive", variant, rec)

        # C1: steer INTO the obstacle (left, +) must FAIL; AWAY (right, -) must PASS.
        check("drive/left steer=-50 forward -> PASS (away)",
              _d("left", {"action": "forward", "steer": -50})["semantic"] is True)
        check("drive/left steer=+50 forward -> FAIL (into)",
              _d("left", {"action": "forward", "steer": 50})["semantic"] is False)
        # C2: action "right" is a valid "steer right" answer and must be accepted.
        check("drive/left steer=-50 action=right -> PASS",
              _d("left", {"action": "right", "steer": -50})["semantic"] is True)
        check("drive/left steer=+50 action=right -> FAIL (into)",
              _d("left", {"action": "right", "steer": 50})["semantic"] is False)
        # magnitude floor: |steer| < 20 is too weak to be a real avoidance.
        check("drive/left steer=0 forward -> FAIL (no steer)",
              _d("left", {"action": "forward", "steer": 0})["semantic"] is False)
        check("drive/left steer=-10 forward -> FAIL (too weak)",
              _d("left", {"action": "forward", "steer": -10})["semantic"] is False)
        check("drive/left steer=-20 forward -> PASS (at threshold)",
              _d("left", {"action": "forward", "steer": -20})["semantic"] is True)
        # center: a center-lane obstacle must yield stop.
        check("drive/center action=stop -> PASS",
              _d("center", {"action": "stop"})["semantic"] is True)
        check("drive/center action=forward -> FAIL",
              _d("center", {"action": "forward", "throttle": 50})["semantic"] is False)
        # clear: forward with enough throttle.
        check("drive/clear forward throttle=50 -> PASS",
              _d("clear", {"action": "forward", "throttle": 50})["semantic"] is True)
        check("drive/clear forward throttle=10 -> FAIL (too slow)",
              _d("clear", {"action": "forward", "throttle": 10})["semantic"] is False)
    except Exception as e:
        check("drive grading import+run", False, f"{type(e).__name__}: {e}")

    # 12. high-severity input-validation fixes (H1-H5)
    #     A typo'd task/variant used to crash mid-run (after partial results
    #     were written). A bare --out filename crashed makedirs. A list-type
    #     `content` crashed _extract_answer. A malformed --list entry crashed.
    print("12. high-severity input validation (H1-H5)")
    import tempfile
    sys.path.insert(0, TOOLS)
    try:
        import importlib
        import vlm_bench
        importlib.reload(vlm_bench)

        # H4: list-type `content` (valid OpenAI variant) must not crash.
        check("H4 _content_text list-of-parts",
              vlm_bench._content_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a b")
        check("H4 _content_text list-no-text -> ''",
              vlm_bench._content_text([{"type": "image_url"}]) == "")
        check("H4 _content_text non-str/non-list -> ''",
              vlm_bench._content_text(12345) == "")
        check("H4 _extract_answer list-content",
              vlm_bench._extract_answer({"content": [{"type": "text", "text": "hi"}]}) == "hi")
        check("H4 _extract_answer list-reasoning fallback",
              vlm_bench._extract_answer({"content": None, "reasoning": [{"type": "text", "text": "R"}]}) == "R")

        # H1: unknown task -> ap.error (SystemExit code 2), NOT a mid-run ValueError.
        try:
            vlm_bench.main(["--models", "foo", "--tasks", "detct"])
            check("H1 unknown task rejected", False, "no SystemExit raised")
        except SystemExit as e:
            check("H1 unknown task rejected (exit 2)", e.code == 2, f"exit={e.code}")

        # H2: unknown drive variant -> ap.error (SystemExit code 2), NOT a mid-run KeyError.
        try:
            vlm_bench.main(["--models", "foo", "--drive-variants", "lef"])
            check("H2 unknown variant rejected", False, "no SystemExit raised")
        except SystemExit as e:
            check("H2 unknown variant rejected (exit 2)", e.code == 2, f"exit={e.code}")

        # H1/H2: valid tasks + variants still pass validation (dry-run exits 0).
        r = run([sys.executable, os.path.join(TOOLS, "vlm_bench.py"),
                 "--dry-run", "--tasks", "detect,sequence,drive",
                 "--drive-variants", "center,left,clear"])
        check("H1/H2 valid tasks+variants pass validation (dry-run 0)",
              r.returncode == 0, r.stderr[:200])

        # H3: bare --out filename (no directory part) must not crash makedirs.
        #     Run a real (failing) call against an unreachable host; the point is
        #     the file is created and the run completes (exit 0), not a crash.
        tmp = tempfile.mkdtemp()
        bare_out = os.path.join(tmp, "bare-out.jsonl")
        r = run([sys.executable, os.path.join(TOOLS, "vlm_bench.py"),
                 "--models", "nonexistent-model", "--tasks", "detect",
                 "--out", bare_out, "--timeout", "3",
                 "--base", "http://unreachable.invalid/v1"])
        check("H3 bare --out: run completes (exit 0)", r.returncode == 0, r.stderr[:300])
        check("H3 bare --out: results file created", os.path.isfile(bare_out),
              f"missing: {bare_out}")
        if os.path.isfile(bare_out):
            with open(bare_out, encoding="utf-8") as fh:
                rec = json.loads(fh.readline())
            check("H3 bare --out: record has an error (not a crash)",
                  rec.get("error") is not None, str(rec.get("error")))
    except Exception as e:
        check("H1-H5 import+run", False, f"{type(e).__name__}: {e}")

    # summary
    # 13. M1: auto_models heuristic + --probe plumbing
    print("13. auto_models heuristic (M1) + --probe plumbing")
    sys.path.insert(0, TOOLS)
    try:
        import importlib
        import vlm_bench as vb
        importlib.reload(vb)
        import scene_gen as sg
        importlib.reload(sg)

        good = ["mistralai_magistral-small-2509-multimodal", "qwen3-vl-4b-vllm-cpp",
                "smolvlm-500m-instruct", "moondream2", "winnow-e4b", "gemma-4-e4b-it",
                "arex-turbo", "spark-s1-4b-v6", "ui-mate-9b",
                "GLM-4.7-Flash-Derestricted-GGUF", "fara1.5-4b"]
        bad = ["qwen3.8-9b-q4", "ternary-bonsai-2-27b", "mimo-v2.6-distill-qwen-9b",
               "ornith-1.5-9b-q4", "qwen3-asr-0.6b", "qwen3-tts-0.6b-custom-voice",
               "whisper-1", "silero-vad", "granite-embedding-107m-multilingual",
               "ace-step-turbo", "voxcpm-1.5", "flux.2-klein-4b", "vibevoice",
               "localvqe-v1.3-4.8m", "supertonic-3",
               "parakeet-cpp-moondream-ultra-q8_0", "gpt-4", "test"]
        auto = set(vb.auto_models(good + bad))
        check("M1 all expected candidates kept", all(m in auto for m in good),
              f"missing: {[m for m in good if m not in auto]}")
        check("M1 no false positives", all(m not in auto for m in bad),
              f"unexpected: {[m for m in bad if m in auto]}")
        check("M1 vllm matched (old 'vlm' missed it)", "qwen3-vl-4b-vllm-cpp" in auto)
        check("M1 text-only qwen3.8 excluded", "qwen3.8-9b-q4" not in auto)
        check("M1 ASR/TTS/VAD/embedding excluded",
              all(m not in auto for m in ["qwen3-asr-0.6b", "qwen3-tts-0.6b-custom-voice",
                                          "whisper-1", "silero-vad",
                                          "granite-embedding-107m-multilingual"]))

        # probe_image is a valid PNG
        png = sg.probe_image()
        check("probe_image PNG magic", png[:8] == b"\x89PNG\r\n\x1a\n")
        import struct as _st
        w, h = _st.unpack(">II", png[16:24])
        check("probe_image 64x64", (w, h) == (64, 64), f"got {w}x{h}")

        # probe_filter keep/drop logic (injected fake _probe_one, no network)
        calls = []
        def fake_probe(base, key, model, timeout_s=45.0):
            calls.append(model)
            return (model == "vision-ok"), ("ok" if model == "vision-ok" else "HTTP 500")
        orig = vb._probe_one
        vb._probe_one = fake_probe
        try:
            kept = vb.probe_filter("http://x", "k", ["vision-ok", "text-only", "broken"])
        finally:
            vb._probe_one = orig
        check("probe_filter keeps capable only", kept == ["vision-ok"], f"got {kept}")
        check("probe_filter probes every candidate",
              calls == ["vision-ok", "text-only", "broken"], f"got {calls}")
        # all-drop -> empty list (fake still injected)
        vb._probe_one = lambda base, key, model, timeout_s=45.0: (False, "HTTP 500")
        try:
            check("probe_filter empty on all-drop",
                  vb.probe_filter("http://x", "k", ["a", "b"]) == [])
        finally:
            vb._probe_one = orig

        # --probe flag exists and is wired to auto
        r = run([sys.executable, os.path.join(TOOLS, "vlm_bench.py"), "--help"])
        check("--probe flag in --help", r.returncode == 0 and "--probe" in r.stdout,
              r.stderr[:120])
        src = open(os.path.join(TOOLS, "vlm_bench.py")).read()
        check("--probe wired into --models auto branch",
              'if args.probe:' in src and "probe_filter(" in src)
    except Exception as e:
        check("M1 section ran", False, f"{type(e).__name__}: {e}")

    print()
    print(f"{'=' * 40}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'=' * 40}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
