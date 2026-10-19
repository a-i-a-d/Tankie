#!/usr/bin/env python3
"""CI harness for the VLM benchmark tool (tools/vlm-benchmark/).

Fully offline — no LocalAI server, no network. Verifies:
  1. All three scripts py_compile cleanly.
  2. vlm_bench.py --dry-run exits 0 (scene generation works).
  3. report.py produces a valid recommendation from the committed results.
  4. report.py --json emits valid JSON with the expected top-level keys.
  5. scene_gen.py produces valid PNGs (magic bytes + non-trivial size).
  6. _extract_answer falls back to `reasoning` when `content` is empty.
  (plus later sections: the dynamic UI core logic, runner --tasks-file,
  and submit_test.py -- the git_submit_test helper, incl. an end-to-end
  commit in a throwaway git repo.)

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
RESULTS = os.path.join(TOOLS, "results", "old")

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


def run(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=60, cwd=cwd)


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
    check("vlm_bench record stores 'prompt'", '"prompt": prompt' in src_bench)
    check("vlm_bench record keeps 'prompt_sha'", '"prompt_sha":' in src_bench)
    check("vlm_bench record stores 'phase'", '"phase": phase' in src_bench)

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

    # 11. drive grading — direction-aware steer (C1/C2 fix, issue #85)
    #     wheels.h: steer > 0 = RIGHT, steer < 0 = LEFT.
    #     "left" scene = obstacle on the LEFT -> correct = steer RIGHT (positive).
    print("11. drive grading (direction-aware)")
    try:
        def _d(variant, parsed):
            rec = {"error": None, "parsed": parsed, "content": json.dumps(parsed)}
            return vlm_bench._grade("drive", variant, rec)

        # C1: steer INTO the obstacle (left, -) must FAIL; AWAY (right, +) must PASS.
        check("drive/left steer=+50 forward -> PASS (away)",
              _d("left", {"action": "forward", "steer": 50})["semantic"] is True)
        check("drive/left steer=-50 forward -> FAIL (into)",
              _d("left", {"action": "forward", "steer": -50})["semantic"] is False)
        # C2: action "right" is a valid "steer right" answer and must be accepted.
        check("drive/left steer=+50 action=right -> PASS",
              _d("left", {"action": "right", "steer": 50})["semantic"] is True)
        check("drive/left steer=-50 action=right -> FAIL (into)",
              _d("left", {"action": "right", "steer": -50})["semantic"] is False)
        # signed floor: steer < +20 is too weak to be a real avoidance.
        check("drive/left steer=0 forward -> FAIL (no steer)",
              _d("left", {"action": "forward", "steer": 0})["semantic"] is False)
        check("drive/left steer=+10 forward -> FAIL (too weak)",
              _d("left", {"action": "forward", "steer": 10})["semantic"] is False)
        check("drive/left steer=+20 forward -> PASS (at threshold)",
              _d("left", {"action": "forward", "steer": 20})["semantic"] is True)
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


    # 14. L1 + L2: latency over successful calls only; ranking tie-break by error count
    print("14. L1 latency split + L2 error-aware ranking")
    import report as rp
    importlib.reload(rp)
    try:
        def _rec(model, ok, lat, pass_=True):
            return {
                "model": model, "task": "detect", "variant": "",
                "elapsed_s": lat, "error": None if ok else "HTTP 500: boom",
                "grade": {"valid_json": pass_, "required_keys": pass_, "semantic": pass_},
            }
        # M1: 1 success (10s) + 1 fast error (0.1s). M2: 2 successes (12s, 14s), 0 errors.
        recs = [
            _rec("M1", True, 10.0), _rec("M1", False, 0.1),
            _rec("M2", True, 12.0), _rec("M2", True, 14.0),
        ]
        summary = rp.aggregate(recs, None, None)
        st1 = summary["M1"]["overall"]
        st2 = summary["M2"]["overall"]
        check("L1 M1 lat_med = 10.0 (success only, not 5.05)", st1["lat_med"] == 10.0, f"got {st1['lat_med']}")
        check("L1 M1 lat_err_med = 0.1 (error latency kept separately)", st1["lat_err_med"] == 0.1, f"got {st1['lat_err_med']}")
        check("L1 M1 errors = 1", st1["errors"] == 1, f"got {st1['errors']}")
        check("L1 M2 lat_med = 13.0 (median of 12,14)", st2["lat_med"] == 13.0, f"got {st2['lat_med']}")
        check("L1 M2 lat_err_med = None (no errors)", st2["lat_err_med"] is None, f"got {st2['lat_err_med']}")
        check("L1 all-error group -> lat_med None", rp._stats([_rec("M3", False, 0.1)])["lat_med"] is None)

        # L2: M1 and M2 have the SAME pass rate (1/2 = 50%). M2 has 0 errors, M1 has 1.
        check("L2 setup: equal pass rates", st1["pass_rate"] == st2["pass_rate"], f"{st1['pass_rate']} vs {st2['pass_rate']}")
        check("L2 setup: M1 errors(1) > M2 errors(0)", st1["errors"] > st2["errors"])
        best, _ = rp.recommend(summary)
        check("L2 recommend picks M2 (fewer errors) despite M1's lower median", best == "M2", f"got {best}")

        # L2: with equal pass rate AND equal errors, latency is the tie-break.
        recs2 = [
            _rec("A", True, 5.0), _rec("A", False, 0.1),
            _rec("B", True, 3.0), _rec("B", False, 0.1),
        ]
        summary2 = rp.aggregate(recs2, None, None)
        check("L2 setup2: equal pass rate and error count",
              summary2["A"]["overall"]["pass_rate"] == summary2["B"]["overall"]["pass_rate"]
              and summary2["A"]["overall"]["errors"] == summary2["B"]["overall"]["errors"])
        best2, _ = rp.recommend(summary2)
        check("L2 equal errors -> lowest median latency wins (B)", best2 == "B", f"got {best2}")

        # L2: no-success model (all errors) sorts after a model with successes.
        recs3 = [
            _rec("S", True, 50.0),   # 100% pass, slow
            _rec("E", False, 0.1), _rec("E", False, 0.1),  # 0% pass, fast errors
        ]
        summary3 = rp.aggregate(recs3, None, None)
        best3, _ = rp.recommend(summary3)
        check("L2 pass rate dominates (S over E)", best3 == "S", f"got {best3}")
    except Exception as e:
        check("L1/L2 section ran", False, f"{type(e).__name__}: {e}")

    # 15. L3 retry + L4 json-mode + L6 dead branch + \bvlm\b fix
    print("15. L3 retry / L4 json-mode / L6 dead branch / \\bvlm\\b fix")
    try:
        import vlm_bench as vb15
        importlib.reload(vb15)

        # L3: _call retries a transient 503, then succeeds (injected _attempt).
        seq = [
            {"http": 503, "error": "HTTP 503: load cooldown"},
            {"http": 200, "error": None, "content": "{}"},
        ]
        calls = {"n": 0}
        def fake_attempt_503(*a, **k):
            r = dict(seq[min(calls["n"], len(seq) - 1)])
            calls["n"] += 1
            return r
        orig_attempt = vb15._attempt
        vb15._attempt = fake_attempt_503
        orig_sleep = vb15.time.sleep
        vb15.time.sleep = lambda s: None
        try:
            rec = vb15._call("http://x", "k", "m", "detect", "", 1.0, 100, max_retries=1)
        finally:
            vb15._attempt = orig_attempt
            vb15.time.sleep = orig_sleep
        check("L3 503 then 200 -> final record is the success", rec["http"] == 200 and rec["error"] is None, f"got {rec['http']} {rec['error']}")
        check("L3 _attempt called twice", calls["n"] == 2, f"got {calls['n']}")
        check("L3 retries recorded (1)", rec["retries"] == 1, f"got {rec['retries']}")

        # L3: deterministic 500 is NOT retried.
        calls2 = {"n": 0}
        def fake_attempt_500(*a, **k):
            calls2["n"] += 1
            return {"http": 500, "error": "HTTP 500: could not load model"}
        vb15._attempt = fake_attempt_500
        try:
            rec = vb15._call("http://x", "k", "m", "detect", "", 1.0, 100, max_retries=2)
        finally:
            vb15._attempt = orig_attempt
        check("L3 500 not retried (1 attempt)", calls2["n"] == 1, f"got {calls2['n']}")
        check("L3 500 recorded as error", rec["error"] is not None and rec["http"] == 500)
        check("L3 retries recorded (0)", rec["retries"] == 0, f"got {rec['retries']}")

        # L3: two 503s with max_retries=1 -> gives up after the retry.
        calls3 = {"n": 0}
        def fake_attempt_503x(*a, **k):
            calls3["n"] += 1
            return {"http": 503, "error": "HTTP 503: cooldown"}
        vb15._attempt = fake_attempt_503x
        try:
            rec = vb15._call("http://x", "k", "m", "detect", "", 1.0, 100, max_retries=1)
        finally:
            vb15._attempt = orig_attempt
        check("L3 503x2 with 1 retry -> 2 attempts total", calls3["n"] == 2, f"got {calls3['n']}")
        check("L3 final record is the 503", rec["http"] == 503 and rec["retries"] == 1)

        # L3: _TRANSIENT_HTTP is only 503.
        check("L3 _TRANSIENT_HTTP == (503,)", tuple(vb15._TRANSIENT_HTTP) == (503,), f"got {vb15._TRANSIENT_HTTP}")

        # L4: _attempt only adds response_format when use_json_mode is True.
        captured = {}
        def fake_urlopen(req, timeout=None):
            import urllib.request as _ur
            captured["body"] = json.loads(req.data.decode())
            raise _ur.HTTPError(req.full_url, 400, "response_format not supported", {}, None)
        import urllib.request as _ur2
        orig_urlopen = _ur2.urlopen
        _ur2.urlopen = fake_urlopen
        try:
            vb15._attempt("http://x", "k", "m", "detect", "", 1.0, 100, use_json_mode=True)
            with_rf = "response_format" in captured["body"]
            vb15._attempt("http://x", "k", "m", "detect", "", 1.0, 100, use_json_mode=False)
            without_rf = "response_format" not in captured["body"]
        finally:
            _ur2.urlopen = orig_urlopen
        check("L4 use_json_mode=True sends response_format", with_rf)
        check("L4 use_json_mode=False omits response_format", without_rf)

        # L4: --no-json-mode flag exists.
        r = run([sys.executable, os.path.join(TOOLS, "vlm_bench.py"), "--help"])
        check("L4 --no-json-mode in --help", r.returncode == 0 and "--no-json-mode" in r.stdout, r.stderr[:120])

        # L6: dead `if False` branch removed from scene_gen.
        sg_src = open(os.path.join(TOOLS, "scene_gen.py"), encoding="utf-8").read()
        check("L6 no 'if False' in scene_gen", "if False" not in sg_src)

        # L-fix: \bvlm\b is a real word boundary now (a bare 'vlm' model matches).
        check("L-fix \\bvlm\\b matches a bare 'vlm' model name",
              vb15._VISION_HINTS.search("my-vlm-model") is not None)
        check("L-fix \\bvlm\\b still matches vllm",
              vb15._VISION_HINTS.search("qwen3-vl-4b-vllm-cpp") is not None)
        check("L-fix \\bvlm\\b still matches smolvlm",
              vb15._VISION_HINTS.search("smolvlm-500m-instruct") is not None)
    except Exception as e:
        check("L3/L4/L6 section ran", False, f"{type(e).__name__}: {e}")

    # 16. --settle: configurable pause between models (server recovery window)
    print("16. --settle (pause between models)")
    try:
        import vlm_bench as vb16
        importlib.reload(vb16)

        # 16a: DEFAULT_SETTLE_S is 30.0 (the requested default).
        check("settle DEFAULT_SETTLE_S == 30.0",
              getattr(vb16, "DEFAULT_SETTLE_S", None) == 30.0,
              f"got {getattr(vb16, 'DEFAULT_SETTLE_S', None)!r}")

        # 16b: --settle flag exists and defaults to 30.
        r = run([sys.executable, os.path.join(TOOLS, "vlm_bench.py"), "--help"])
        help_out = r.stdout
        check("settle --settle in --help", r.returncode == 0 and "--settle" in help_out, help_out[:120])
        check("settle --settle default 30 shown in help",
              "default: 30" in help_out, help_out[:200])

        # 16c: settle actually sleeps BETWEEN models (not before the first,
        # not between tasks of the same model). Fake _attempt records the model
        # order; fake time.sleep records the pause durations.
        order = []
        sleeps = []
        def fake_attempt_settle(base, key, model, task, variant,
                                timeout_s, max_tokens, use_json_mode=True,
                                phase="test", **kwargs):
            if phase == "test":  # ignore the warmup call for the order check
                order.append(model)
            return {"http": 200, "elapsed_s": 0.01, "error": None,
                    "content": "{}", "parsed": {},
                    "grade": {"valid_json": True, "required_keys": True, "semantic": True},
                    "model": model, "task": task, "variant": variant,
                    "phase": phase}
        def fake_sleep_settle(dur):
            sleeps.append(dur)
        import tempfile
        tmp = os.path.join(tempfile.mkdtemp(), "settle-test.jsonl")
        orig_attempt = vb16._attempt
        orig_sleep = vb16.time.sleep
        vb16._attempt = fake_attempt_settle
        vb16.time.sleep = fake_sleep_settle
        try:
            rc = vb16.main(["--models", "m1,m2", "--tasks", "detect",
                            "--settle", "0.05", "--out", tmp])
        finally:
            vb16._attempt = orig_attempt
            vb16.time.sleep = orig_sleep
        check("settle main() exit 0 (2 models)", rc == 0, f"rc={rc}")
        check("settle both models ran in order", order == ["m1", "m2"], f"got {order}")
        # Exactly one pause, between m1 and m2 (not before m1).
        check("settle paused exactly once (between models)",
              sleeps.count(0.05) == 1, f"sleeps={sleeps}")
        check("settle pause used the configured value (0.05)",
              0.05 in sleeps, f"sleeps={sleeps}")

        # 16d: --settle 0 disables the pause entirely.
        sleeps0 = []
        def fake_sleep0(dur):
            sleeps0.append(dur)
        tmp0 = os.path.join(tempfile.mkdtemp(), "settle-zero.jsonl")
        vb16._attempt = fake_attempt_settle
        vb16.time.sleep = fake_sleep0
        try:
            rc0 = vb16.main(["--models", "m1,m2", "--tasks", "detect",
                             "--settle", "0", "--out", tmp0])
        finally:
            vb16._attempt = orig_attempt
            vb16.time.sleep = orig_sleep
        check("settle 0 -> main() exit 0", rc0 == 0, f"rc={rc0}")
        check("settle 0 -> no pause between models",
              not any(d > 0 for d in sleeps0), f"sleeps={sleeps0}")
    except Exception as e:
        check("settle section ran", False, f"{type(e).__name__}: {e}")

    # 17. warmup pass (issue #89): one model-load call per model before the
    #     graded test calls; phase tagging; warm-only stats + ranking;
    #     phase-driven cold/warm badges; "Load (cold)" column.
    print("17. warmup pass (issue #89)")
    try:
        import importlib
        import vlm_bench as vb17
        importlib.reload(vb17)
        import report as rp17
        importlib.reload(rp17)
        import tempfile

        # 17a: --no-warmup flag exists in --help.
        r = run([sys.executable, os.path.join(TOOLS, "vlm_bench.py"), "--help"])
        help17 = r.stdout
        check("warmup --no-warmup in --help",
              r.returncode == 0 and "--no-warmup" in help17, help17[:120])

        # 17b: exactly ONE warmup call per model, BEFORE its test calls;
        # warmup record tagged phase="warmup", test records phase="test".
        order17 = []
        def fake_attempt17(base, key, model, task, variant,
                           timeout_s, max_tokens, use_json_mode=True,
                           phase="test", **kwargs):
            order17.append((model, task, variant, phase))
            return {"http": 200, "elapsed_s": 0.01, "error": None,
                    "content": "{}", "parsed": {},
                    "grade": {"valid_json": True, "required_keys": True, "semantic": True},
                    "model": model, "task": task, "variant": variant,
                    "phase": phase,
                    "n_images": 1, "max_tokens": max_tokens,
                    "prompt": vb17.WARMUP_PROMPT if phase == "warmup" else "TASK PROMPT"}
        tmp17 = os.path.join(tempfile.mkdtemp(), "warmup-test.jsonl")
        orig_attempt17 = vb17._attempt
        orig_sleep17 = vb17.time.sleep
        vb17._attempt = fake_attempt17
        vb17.time.sleep = lambda s: None
        try:
            rc17 = vb17.main(["--models", "m1,m2", "--tasks", "detect",
                              "--settle", "0", "--out", tmp17])
        finally:
            vb17._attempt = orig_attempt17
            vb17.time.sleep = orig_sleep17
        check("warmup main() exit 0 (2 models)", rc17 == 0, f"rc={rc17}")
        check("warmup exactly one extra call per model (4 calls total)",
              len(order17) == 4, f"got {order17}")
        check("warmup call precedes the test call per model",
              order17 == [("m1", "detect", "", "warmup"),
                          ("m1", "detect", "", "test"),
                          ("m2", "detect", "", "warmup"),
                          ("m2", "detect", "", "test")], f"got {order17}")
        with open(tmp17, encoding="utf-8") as fh:
            recs17 = [json.loads(l) for l in fh if l.strip()]
        check("warmup JSONL: 2 records phase=warmup",
              sum(1 for r in recs17 if r.get("phase") == "warmup") == 2,
              f"phases={[r.get('phase') for r in recs17]}")
        check("warmup JSONL: 2 records phase=test",
              sum(1 for r in recs17 if r.get("phase") == "test") == 2,
              f"phases={[r.get('phase') for r in recs17]}")
        wrec = [r for r in recs17 if r.get("phase") == "warmup"][0]
        check("warmup record uses the warmup prompt",
              wrec.get("prompt") == vb17.WARMUP_PROMPT, str(wrec.get("prompt"))[:80])
        check("warmup record max_tokens capped (64)",
              wrec.get("max_tokens") == 64, f"got {wrec.get('max_tokens')}")
        check("warmup record sends an image (mmproj loads)",
              wrec.get("n_images") == 1, f"got {wrec.get('n_images')}")

        # 17c: --no-warmup disables the warmup call entirely.
        order17n = []
        def fake_attempt17n(base, key, model, task, variant,
                            timeout_s, max_tokens, use_json_mode=True,
                            phase="test", **kwargs):
            order17n.append((model, phase))
            return {"http": 200, "elapsed_s": 0.01, "error": None,
                    "content": "{}", "parsed": {},
                    "grade": {"valid_json": True, "required_keys": True, "semantic": True},
                    "model": model, "task": task, "variant": variant, "phase": phase}
        tmp17n = os.path.join(tempfile.mkdtemp(), "warmup-noff.jsonl")
        vb17._attempt = fake_attempt17n
        try:
            rc17n = vb17.main(["--models", "m1,m2", "--tasks", "detect",
                               "--no-warmup", "--settle", "0", "--out", tmp17n])
        finally:
            vb17._attempt = orig_attempt17
        check("warmup --no-warmup exit 0", rc17n == 0, f"rc={rc17n}")
        check("warmup --no-warmup: no warmup calls",
              order17n == [("m1", "test"), ("m2", "test")], f"got {order17n}")

        # 17d: warmup error is NOT fatal -- test calls still run.
        def fake_attempt17e(base, key, model, task, variant,
                            timeout_s, max_tokens, use_json_mode=True,
                            phase="test", **kwargs):
            if phase == "warmup":
                return {"http": 500, "elapsed_s": 2.0,
                        "error": "HTTP 500: could not load model", "content": None,
                        "parsed": None,
                        "grade": {"valid_json": False, "required_keys": False,
                                  "semantic": False},
                        "model": model, "task": task, "variant": variant,
                        "phase": "warmup"}
            return {"http": 200, "elapsed_s": 0.01, "error": None,
                    "content": "{}", "parsed": {},
                    "grade": {"valid_json": True, "required_keys": True, "semantic": True},
                    "model": model, "task": task, "variant": variant, "phase": "test"}
        tmp17e = os.path.join(tempfile.mkdtemp(), "warmup-err.jsonl")
        vb17._attempt = fake_attempt17e
        try:
            rc17e = vb17.main(["--models", "m1", "--tasks", "detect",
                               "--settle", "0", "--out", tmp17e])
        finally:
            vb17._attempt = orig_attempt17
        check("warmup error not fatal (exit 0)", rc17e == 0, f"rc={rc17e}")
        with open(tmp17e, encoding="utf-8") as fh:
            recs17e = [json.loads(l) for l in fh if l.strip()]
        check("warmup error: warmup + test records both written",
              [r.get("phase") for r in recs17e] == ["warmup", "test"],
              f"got {[r.get('phase') for r in recs17e]}")

        # 17e: report.py stats are warm-only; cold load surfaced separately.
        def _wrec(model, phase, lat, ok=True, semantic=True, run=None):
            rec = {"model": model, "task": "detect", "variant": "",
                   "phase": phase, "elapsed_s": lat,
                   "error": None if ok else "HTTP 500: boom",
                   "grade": {"valid_json": semantic, "required_keys": semantic,
                             "semantic": semantic}}
            if run is not None:
                rec["run"] = run
            return rec
        # M-cold: huge cold load (100s) + fast warm calls (2s, 4s).
        # M-fast: small cold load (5s) + slow warm calls (50s, 60s).
        recs17 = [
            _wrec("M-cold", "warmup", 100.0),
            _wrec("M-cold", "test", 2.0), _wrec("M-cold", "test", 4.0),
            _wrec("M-fast", "warmup", 5.0),
            _wrec("M-fast", "test", 50.0), _wrec("M-fast", "test", 60.0),
        ]
        summary17 = rp17.aggregate(recs17, None, None)
        sc = summary17["M-cold"]["overall"]
        sf = summary17["M-fast"]["overall"]
        check("warm stats: n counts test calls only (M-cold n=2)", sc["n"] == 2, f"got {sc['n']}")
        check("warm stats: lat_med is warm median (M-cold 3.0)", sc["lat_med"] == 3.0, f"got {sc['lat_med']}")
        check("warm stats: load_cold_s = warmup latency (M-cold 100.0)",
              summary17["M-cold"]["load_cold_s"] == 100.0,
              f"got {summary17['M-cold']['load_cold_s']}")
        check("warm stats: load_cold_s surfaced (M-fast 5.0)",
              summary17["M-fast"]["load_cold_s"] == 5.0,
              f"got {summary17['M-fast']['load_cold_s']}")
        check("warm stats: warmup not in pass-rate denominator",
              sc["pass_rate"] == 1.0, f"got {sc['pass_rate']}")
        # Ranking: equal pass rate + errors -> the WARM median decides.
        best17, _ = rp17.recommend(summary17)
        check("warm ranking: huge-cold/fast-warm beats small-cold/slow-warm",
              best17 == "M-cold", f"got {best17}")
        # Warmup error is tracked, not mixed into test errors.
        recs17w = [_wrec("W", "warmup", 3.0, ok=False, semantic=False),
                   _wrec("W", "test", 2.0)]
        wsum = rp17.aggregate(recs17w, None, None)["W"]
        check("warm stats: warmup error tracked separately (errors=0, warmup_errors=1)",
              wsum["overall"]["errors"] == 0 and wsum["warmup_errors"] == 1,
              f"got errors={wsum['overall']['errors']} warmup_errors={wsum['warmup_errors']}")

        # 17f: legacy records (no phase) still aggregate as before.
        legacy = [{"model": "L", "task": "detect", "variant": "",
                   "elapsed_s": 7.0, "error": None,
                   "grade": {"valid_json": True, "required_keys": True, "semantic": True}}]
        st17l = rp17._stats(legacy)
        check("legacy (no phase): n=1, lat_med=7.0",
              st17l["n"] == 1 and st17l["lat_med"] == 7.0, f"got {st17l}")
        check("legacy (no phase): no cold stats", st17l["lat_cold_med"] is None)

        # 17g: HTML -- Load (cold) column + phase-driven badges.
        tmp17h = tempfile.mkdtemp()
        jl17 = os.path.join(tmp17h, "phase.jsonl")
        with open(jl17, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(_wrec("P1", "warmup", 12.0)) + "\n")
            fh.write(json.dumps(_wrec("P1", "test", 1.0)) + "\n")
            legacy1 = _wrec("P2", "test", 2.0, run=1); del legacy1["phase"]
            legacy2 = _wrec("P2", "test", 3.0, run=2); del legacy2["phase"]
            fh.write(json.dumps(legacy1) + "\n")
            fh.write(json.dumps(legacy2) + "\n")
        html17 = os.path.join(tmp17h, "report.html")
        r = run([sys.executable, os.path.join(TOOLS, "report.py"), jl17, "--html", html17])
        check("warmup --html exit 0", r.returncode == 0, r.stderr[:200])
        if os.path.isfile(html17):
            h17 = open(html17, encoding="utf-8").read()
            check("html has 'Load (cold)' column header", ">Load (cold)</th>" in h17)
            check("html cold cell shows load time (12.0s)",
              '<td class="coldload"' in h17 and ">12.0s<" in h17, h17[:200])
            check("html phase=warmup record labeled cold",
              h17.count('<span class="temp cold">cold</span>') >= 1)
            check("html phase=test record labeled warm",
              h17.count('<span class="temp warm">warm</span>') >= 1)
            # P1: warmup(12s) + test(1s) -> cold badge 1, warm badge 1
            # P2: legacy run1 -> cold, run2 -> warm (fallback intact)
            cold_n = h17.count('<span class="temp cold">cold</span>')
            warm_n = h17.count('<span class="temp warm">warm</span>')
            check("html legacy run-number fallback still works (2 cold, 2 warm)",
                  cold_n == 2 and warm_n == 2,
                  "cold=%d warm=%d" % (cold_n, warm_n))
            check("html ranking header says 'warm median'", ">warm median<" in h17)
        else:
            check("warmup html created", False, f"missing: {html17}")

        # 17h: text + markdown renderers carry the Load (cold) column.
        r_txt = run([sys.executable, os.path.join(TOOLS, "report.py"), jl17])
        check("text report has 'Load (cold)' column",
              r_txt.returncode == 0 and "Load (cold)" in r_txt.stdout, r_txt.stdout[:200])
        md17 = os.path.join(tmp17h, "report.md")
        r_md = run([sys.executable, os.path.join(TOOLS, "report.py"), jl17, "--md", md17])
        if r_md.returncode == 0 and os.path.isfile(md17):
            m17 = open(md17, encoding="utf-8").read()
            check("markdown has 'Load (cold)' column", "| Load (cold) |" in m17, m17[:200])
        else:
            check("markdown report generated", False, r_md.stderr[:200])
    except Exception as e:
        import traceback
        check("warmup section ran", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-800:]}")


    # 18. benchmark-ui.html — dynamic frontend core logic (issue #90).
    #     Runs the REAL inline <script> from the self-contained HTML in a Node
    #     VM sandbox (no DOM, no network) and asserts the pure logic:
    #     JSONL parsing, stats/aggregate/ranking (mirrors report.py),
    #     run comparison ("only changed"), grading (mirrors vlm_bench._grade),
    #     and the LocalAI chat request/response/record shapes.
    print("18. benchmark-ui.html (dynamic UI core logic, issue #90)")
    import shutil
    import tempfile as _tmp18
    ui_html = os.path.join(TOOLS, "benchmark-ui.html")
    if not os.path.isfile(ui_html):
        check("benchmark-ui.html present", False, f"missing: {ui_html}")
    else:
        check("benchmark-ui.html present", True)
        node = shutil.which("node")
        if not node:
            check("node available (UI harness)", False, "node not found on PATH")
        else:
            js_runner = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');
const htmlPath = process.argv[2];
const html = fs.readFileSync(htmlPath, 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const sandbox = { console: console };
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
for (const s of scripts) vm.runInContext(s, sandbox, { filename: 'inline.js' });
const M = sandbox.TankieBenchUI;
if (!M) { console.error('NO_MODULE'); process.exit(3); }
const out = {};
const p = M.parseJsonl('{"a":1}\n\nnot-json\n{"b":2}\n');
out.parse = { records: p.records.length, malformed: p.malformed };
const statsRecs = [
  { phase: 'warmup', elapsed_s: 100, error: null },
  { phase: 'test', elapsed_s: 2, error: null, grade: { semantic: true } },
  { phase: 'test', elapsed_s: 4, error: null, grade: { semantic: true } },
  { phase: 'test', elapsed_s: 0.1, error: 'HTTP 500', grade: { semantic: false } },
];
out.stats = M.stats(statsRecs);
out.cell = {
  empty: M.cellStatus({ n: 0 }),
  pass: M.cellStatus({ n: 2, pass: 2 }),
  partial: M.cellStatus({ n: 2, pass: 1 }),
  error: M.cellStatus({ n: 2, pass: 0, errors: 2 }),
  fail: M.cellStatus({ n: 2, pass: 0, errors: 0 }),
};
function mkRun(model, passN, latMed) {
  const recs = [];
  for (let i = 0; i < 2; i++) {
    const ok = i < passN;
    recs.push({ model: model, task: 'detect', variant: '', phase: 'test',
      elapsed_s: latMed, error: ok ? null : 'HTTP 500',
      grade: { valid_json: ok, required_keys: ok, semantic: ok } });
  }
  return { name: model, records: recs };
}
const cmp = M.compareRuns(mkRun('M', 1, 10), mkRun('M', 2, 8));
out.compare = { changed: cmp.changed.length, improved: cmp.improved, regressed: cmp.regressed,
  unchanged: cmp.unchanged, passDelta: cmp.rows[0].pass_rate_delta,
  latDelta: cmp.rows[0].lat_delta, errDelta: cmp.rows[0].errors_delta };
const g = (t, v, x) => M.grade(t, v, x);
out.grade = {
  drive_left_50: g('drive', 'left', { error: null, parsed: { action: 'forward', steer: 50 } }),
  drive_left_neg50: g('drive', 'left', { error: null, parsed: { action: 'forward', steer: -50 } }),
  drive_left_10: g('drive', 'left', { error: null, parsed: { action: 'forward', steer: 10 } }),
  drive_left_20: g('drive', 'left', { error: null, parsed: { action: 'forward', steer: 20 } }),
  drive_center_stop: g('drive', 'center', { error: null, parsed: { action: 'stop' } }),
  drive_center_fwd: g('drive', 'center', { error: null, parsed: { action: 'forward', throttle: 50 } }),
  drive_clear_50: g('drive', 'clear', { error: null, parsed: { action: 'forward', throttle: 50 } }),
  drive_clear_10: g('drive', 'clear', { error: null, parsed: { action: 'forward', throttle: 10 } }),
  detect_ok: g('detect', '', { error: null, parsed: { objects: [{ label: 'red circle' }, { label: 'green square' }] } }),
  detect_missing: g('detect', '', { error: null, parsed: { objects: [{ label: 'red circle' }] } }),
  seq_ok: g('sequence', '', { error: null, parsed: { moving: 'square', direction: 'right' } }),
  seq_wrong: g('sequence', '', { error: null, parsed: { moving: 'square', direction: 'left' } }),
  err: g('detect', '', { error: 'HTTP 500', parsed: { objects: [] } }),
};
const req = M.buildChatRequest({ base: 'http://x/v1/', model: 'm', prompt: 'P',
  images: ['data:image/png;base64,AAA'], max_tokens: 100, json_mode: true });
out.chat = { url: req.url, model: req.body.model, text: req.body.messages[1].content[0].text,
  max_tokens: req.body.max_tokens, temp: req.body.temperature,
  rf: req.body.response_format ? req.body.response_format.type : null,
  img: req.body.messages[1].content[1].image_url.url };
const reqOff = M.buildChatRequest({ base: 'http://x/v1', model: 'm', prompt: 'P', images: [],
  max_tokens: 100, json_mode: false });
out.chat.rf_off = reqOff.body.response_format ? 'present' : 'absent';
out.parseResp = {
  c1: M.parseChatResponse({ choices: [{ message: { content: '{"a":1}' } }] }),
  c2: M.parseChatResponse({ choices: [{ message: { content: '', reasoning: '{"b":2}' } }] }),
  c3: M.parseChatResponse({ choices: [{ message: { content: [{ type: 'text', text: 'hi' }] } }] }),
};
const rec = M.buildRecord({ base: 'http://x', model: 'm', task: 'detect', variant: '', images: [],
  max_tokens: 500, prompt: 'P', http: 200, content: '{}', reasoning: null, parsed: {}, error: null,
  grade: { valid_json: false, required_keys: false, semantic: false }, t0: Date.now() - 1500 });
out.record = { phase: rec.phase, task: rec.task, hasTs: !!rec.ts,
  hasElapsed: typeof rec.elapsed_s === 'number', n_images: rec.n_images };
const recs = [
  { model: 'M1', task: 'detect', variant: '', phase: 'test', elapsed_s: 10, error: null,
    grade: { valid_json: true, required_keys: true, semantic: true } },
  { model: 'M1', task: 'detect', variant: '', phase: 'test', elapsed_s: 0.1, error: 'HTTP 500',
    grade: { valid_json: false, required_keys: false, semantic: false } },
  { model: 'M2', task: 'detect', variant: '', phase: 'test', elapsed_s: 12, error: null,
    grade: { valid_json: true, required_keys: true, semantic: true } },
  { model: 'M2', task: 'detect', variant: '', phase: 'test', elapsed_s: 14, error: null,
    grade: { valid_json: true, required_keys: true, semantic: true } },
];
const agg = M.aggregate(recs);
out.recommend = M.recommend(agg);
out.agg = { models: Object.keys(agg).sort(), m1_pr: agg.M1.overall.pass_rate,
  m1_lat: agg.M1.overall.lat_med, m1_err: agg.M1.overall.errors,
  m2_pr: agg.M2.overall.pass_rate, m2_lat: agg.M2.overall.lat_med, m2_err: agg.M2.overall.errors };
// --- issue #125: task images + task file + expected override ---
out.sceneKeys = Object.keys(M.SCENES).sort();
out.sceneLens = {};
for (const k of Object.keys(M.SCENES)) out.sceneLens[k] = M.SCENES[k].length;
out.sceneFor = {
  detect: M.sceneFor('detect','').length,
  sequence: M.sceneFor('sequence','').length,
  drive_left: M.sceneFor('drive','left').length,
  custom: M.sceneFor('custom',''),
};
out.recInline = M.recordImages({ task:'custom', images:['data:image/png;base64,AAA'] });
out.recScene = M.recordImages({ task:'detect', variant:'' }).length;
out.recNone = M.recordImages({ task:'custom', variant:'' });
out.maxBytes = M.MAX_IMAGE_BYTES;
function mkDataUrl(nBytes){ const pad = Math.ceil(nBytes/3)*4; return 'data:image/png;base64,' + 'A'.repeat(pad); }
out.chk = {
  ok: M.checkImageSize(mkDataUrl(100*1024)),
  over: M.checkImageSize(mkDataUrl(300*1024)),
  bad: M.checkImageSize('not-a-dataurl'),
};
const rec125 = M.buildRecord({ base:'http://x', model:'m', task:'custom', variant:'',
  images:['data:image/png;base64,AAA','data:image/png;base64,BBB'], max_tokens:500, prompt:'P',
  http:200, content:'{}', reasoning:null, parsed:{}, error:null,
  grade:{valid_json:false,required_keys:false,semantic:false}, t0:Date.now()-1000 });
out.rec125 = { images: rec125.images, n_images: rec125.n_images };
const task125 = { name:'red-circle-detect', model:'m', task:'detect', variant:'', prompt:'P',
  images:['data:image/png;base64,AAA'], max_tokens:500, json_mode:true,
  expected:{objects:['circle','square']}, ts:'2026-10-18T00:00:00Z' };
out.fileLine = M.taskToFile(task125);
const pt125 = M.parseTaskFile(out.fileLine + '\n\nnot-json\n');
out.parse125 = { tasks: pt125.tasks.length, malformed: pt125.malformed,
  name: pt125.tasks[0] ? pt125.tasks[0].name : null,
  exp: pt125.tasks[0] ? pt125.tasks[0].expected : null };
const base125 = { error:null, parsed:{objects:[{label:'red circle'},{label:'blue square'}]} };
out.ovr = {
  detect_def: M.grade('detect','',base125).semantic,
  detect_ovr_bad: M.grade('detect','',base125, {detect:{objects:['circle','triangle']}}).semantic,
  detect_ovr_ok: M.grade('detect','',base125, {detect:{objects:['circle','square']}}).semantic,
  drive_ovr_ok: M.grade('drive','left',{error:null,parsed:{action:'forward',steer:5}}, {drive:{left:{action:['forward'],steer_min:0}}}).semantic,
  drive_def_fail: M.grade('drive','left',{error:null,parsed:{action:'forward',steer:5}}).semantic,
};
// issue #126: a record that STORES its expected (task-file sourced) is graded
// against it without an explicit override -- the path the runner relies on.
out.ovrRec = {
  detect_rec_bad: M.grade('detect','', {error:null, parsed:{objects:[{label:'red circle'},{label:'blue square'}]}, expected:{detect:{objects:['circle','triangle']}}}).semantic,
  detect_rec_ok: M.grade('detect','', {error:null, parsed:{objects:[{label:'red circle'},{label:'blue square'}]}, expected:{detect:{objects:['circle','square']}}}).semantic,
  drive_rec_ok: M.grade('drive','left', {error:null, parsed:{action:'forward',steer:5}, expected:{drive:{left:{action:['forward'],steer_min:0}}}}).semantic,
  drive_rec_fail: M.grade('drive','left', {error:null, parsed:{action:'forward',steer:5}, expected:{drive:{left:{action:['forward'],steer_min:20}}}}).semantic,
  seq_rec_ok: M.grade('sequence','', {error:null, parsed:{moving:'square',direction:'right'}, expected:{sequence:{moving:'square',direction:'right'}}}).semantic,
};
console.log(JSON.stringify(out));
"""
            js_path = os.path.join(_tmp18.mkdtemp(), "ui_runner.js")
            with open(js_path, "w", encoding="utf-8") as fh:
                fh.write(js_runner)
            r = run([node, js_path, ui_html])
            if r.returncode != 0:
                check("UI core loads in Node", False,
                      f"node exit {r.returncode}: {r.stderr[:300]}")
            else:
                try:
                    out = json.loads(r.stdout.strip().splitlines()[-1])
                except Exception as e:
                    check("UI core JSON output", False, f"{e}: {r.stdout[:200]}")
                    out = None
                if out is not None:
                    # parse
                    check("18 parseJsonl records=2", out["parse"]["records"] == 2, str(out["parse"]))
                    check("18 parseJsonl malformed=1", out["parse"]["malformed"] == 1, str(out["parse"]))
                    # stats (mirrors report.py._stats)
                    st = out["stats"]
                    check("18 stats n=3 (test only)", st["n"] == 3, str(st))
                    check("18 stats pass=2", st["pass"] == 2, str(st))
                    check("18 stats pass_rate=0.667", st["pass_rate"] == 0.667, str(st))
                    check("18 stats errors=1", st["errors"] == 1, str(st))
                    check("18 stats lat_med=3.0 (warm success only)", st["lat_med"] == 3.0, str(st))
                    check("18 stats lat_err_med=0.1", st["lat_err_med"] == 0.1, str(st))
                    check("18 stats lat_cold_med=100 (warmup)", st["lat_cold_med"] == 100, str(st))
                    # cellStatus
                    check("18 cell empty", out["cell"]["empty"] == "empty", str(out["cell"]))
                    check("18 cell pass", out["cell"]["pass"] == "pass", str(out["cell"]))
                    check("18 cell partial", out["cell"]["partial"] == "partial", str(out["cell"]))
                    check("18 cell error", out["cell"]["error"] == "error", str(out["cell"]))
                    check("18 cell fail", out["cell"]["fail"] == "fail", str(out["cell"]))
                    # compare (issue #90 A4)
                    c = out["compare"]
                    check("18 compare changed=1", c["changed"] == 1, str(c))
                    check("18 compare improved=1", c["improved"] == 1, str(c))
                    check("18 compare regressed=0", c["regressed"] == 0, str(c))
                    check("18 compare pass_delta=+0.5", c["passDelta"] == 0.5, str(c))
                    check("18 compare lat_delta=-2", c["latDelta"] == -2, str(c))
                    check("18 compare err_delta=-1", c["errDelta"] == -1, str(c))
                    # chat request shape
                    ch = out["chat"]
                    check("18 chat url", ch["url"] == "http://x/v1/chat/completions", str(ch["url"]))
                    check("18 chat model", ch["model"] == "m")
                    check("18 chat prompt text", ch["text"] == "P")
                    check("18 chat max_tokens", ch["max_tokens"] == 100)
                    check("18 chat temperature=0", ch["temp"] == 0)
                    check("18 chat json mode on", ch["rf"] == "json_object")
                    check("18 chat image attached", ch["img"] == "data:image/png;base64,AAA")
                    check("18 chat json mode off omits response_format", ch["rf_off"] == "absent")
                    # parseChatResponse
                    pr = out["parseResp"]
                    check("18 parseResp content", pr["c1"]["content"] == '{"a":1}' and pr["c1"]["parsed"] == {"a": 1}, str(pr["c1"]))
                    check("18 parseResp reasoning fallback", pr["c2"]["content"] == "" and pr["c2"]["parsed"] == {"b": 2}, str(pr["c2"]))
                    check("18 parseResp list content", pr["c3"]["content"] == "hi", str(pr["c3"]))
                    # buildRecord
                    rc = out["record"]
                    check("18 record phase=test", rc["phase"] == "test", str(rc))
                    check("18 record has ts + elapsed", rc["hasTs"] and rc["hasElapsed"], str(rc))
                    check("18 record n_images=0", rc["n_images"] == 0, str(rc))
                    # aggregate + recommend (cross-checked vs report.py below)
                    check("18 aggregate models", out["agg"]["models"] == ["M1", "M2"], str(out["agg"]))
                    check("18 agg M1 pass_rate=0.5", out["agg"]["m1_pr"] == 0.5, str(out["agg"]))
                    check("18 agg M1 lat_med=10", out["agg"]["m1_lat"] == 10, str(out["agg"]))
                    check("18 agg M2 pass_rate=1.0", out["agg"]["m2_pr"] == 1.0, str(out["agg"]))

                    # --- cross-check: JS grade == Python vlm_bench._grade ---
                    sys.path.insert(0, TOOLS)
                    import vlm_bench as _vb18
                    importlib.reload(_vb18)
                    cases = {
                        "drive_left_50": ("drive", "left", {"action": "forward", "steer": 50}),
                        "drive_left_neg50": ("drive", "left", {"action": "forward", "steer": -50}),
                        "drive_left_10": ("drive", "left", {"action": "forward", "steer": 10}),
                        "drive_left_20": ("drive", "left", {"action": "forward", "steer": 20}),
                        "drive_center_stop": ("drive", "center", {"action": "stop"}),
                        "drive_center_fwd": ("drive", "center", {"action": "forward", "throttle": 50}),
                        "drive_clear_50": ("drive", "clear", {"action": "forward", "throttle": 50}),
                        "drive_clear_10": ("drive", "clear", {"action": "forward", "throttle": 10}),
                        "detect_ok": ("detect", "", {"objects": [{"label": "red circle"}, {"label": "green square"}]}),
                        "detect_missing": ("detect", "", {"objects": [{"label": "red circle"}]}),
                        "seq_ok": ("sequence", "", {"moving": "square", "direction": "right"}),
                        "seq_wrong": ("sequence", "", {"moving": "square", "direction": "left"}),
                    }
                    all_match = True
                    mismatch = []
                    for cid, (t, v, parsed) in cases.items():
                        py = _vb18._grade(t, v, {"error": None, "parsed": parsed, "content": json.dumps(parsed)})
                        js = out["grade"][cid]
                        if py != js:
                            all_match = False
                            mismatch.append(f"{cid}: py={py} js={js}")
                    check("18 JS grade == Python vlm_bench._grade (12 cases)", all_match, "; ".join(mismatch))
                    # error case: both all-False
                    check("18 JS grade error -> all False",
                          out["grade"]["err"] == {"valid_json": False, "required_keys": False, "semantic": False},
                          str(out["grade"]["err"]))

                    # --- cross-check: JS recommend == Python report.recommend ---
                    import report as _rp18
                    importlib.reload(_rp18)
                    recs18 = [
                        {"model": "M1", "task": "detect", "variant": "", "phase": "test", "elapsed_s": 10, "error": None,
                         "grade": {"valid_json": True, "required_keys": True, "semantic": True}},
                        {"model": "M1", "task": "detect", "variant": "", "phase": "test", "elapsed_s": 0.1, "error": "HTTP 500",
                         "grade": {"valid_json": False, "required_keys": False, "semantic": False}},
                        {"model": "M2", "task": "detect", "variant": "", "phase": "test", "elapsed_s": 12, "error": None,
                         "grade": {"valid_json": True, "required_keys": True, "semantic": True}},
                        {"model": "M2", "task": "detect", "variant": "", "phase": "test", "elapsed_s": 14, "error": None,
                         "grade": {"valid_json": True, "required_keys": True, "semantic": True}},
                    ]
                    py_sum = _rp18.aggregate(recs18, None, None)
                    py_best, _ = _rp18.recommend(py_sum)
                    check("18 JS recommend == Python recommend (M2)",
                          out["recommend"]["model"] == py_best == "M2",
                          f"js={out['recommend']} py={py_best}")
                    # JS aggregate stats match Python aggregate stats
                    py_m1 = py_sum["M1"]["overall"]
                    check("18 JS aggregate M1 == Python (pr/lat/err)",
                          out["agg"]["m1_pr"] == py_m1["pass_rate"]
                          and out["agg"]["m1_lat"] == py_m1["lat_med"]
                          and out["agg"]["m1_err"] == py_m1["errors"],
                          f"js={out['agg']} py={py_m1}")

                    # --- issue #125: task images + task file + expected override ---
                    check("18 SCENES has 5 keys",
                          out["sceneKeys"] == ["detect", "drive/center", "drive/clear", "drive/left", "sequence"],
                          str(out["sceneKeys"]))
                    check("18 SCENES detect=1 img", out["sceneLens"]["detect"] == 1, str(out["sceneLens"]))
                    check("18 SCENES sequence=4 imgs", out["sceneLens"]["sequence"] == 4, str(out["sceneLens"]))
                    check("18 SCENES drive/center=1 img", out["sceneLens"]["drive/center"] == 1, str(out["sceneLens"]))
                    check("18 sceneFor detect=1", out["sceneFor"]["detect"] == 1, str(out["sceneFor"]))
                    check("18 sceneFor sequence=4", out["sceneFor"]["sequence"] == 4, str(out["sceneFor"]))
                    check("18 sceneFor drive/left=1", out["sceneFor"]["drive_left"] == 1, str(out["sceneFor"]))
                    check("18 sceneFor custom=[]", out["sceneFor"]["custom"] == [], str(out["sceneFor"]["custom"]))
                    check("18 recordImages inline",
                          out["recInline"] == ["data:image/png;base64,AAA"], str(out["recInline"]))
                    check("18 recordImages scene fallback=1", out["recScene"] == 1, str(out["recScene"]))
                    check("18 recordImages none=[]", out["recNone"] == [], str(out["recNone"]))
                    check("18 MAX_IMAGE_BYTES=200KB", out["maxBytes"] == 200 * 1024, str(out["maxBytes"]))
                    chk = out["chk"]
                    check("18 checkImageSize ok (100KB)",
                          chk["ok"]["ok"] is True and chk["ok"]["bytes"] > 100 * 1024, str(chk["ok"]))
                    check("18 checkImageSize over (300KB)", chk["over"]["ok"] is False, str(chk["over"]))
                    check("18 checkImageSize bad url", chk["bad"]["ok"] is False, str(chk["bad"]))
                    r125 = out["rec125"]
                    check("18 buildRecord stores images (2)",
                          r125["images"] == ["data:image/png;base64,AAA", "data:image/png;base64,BBB"], str(r125))
                    check("18 buildRecord n_images=2", r125["n_images"] == 2, str(r125))
                    check("18 taskToFile has name+expected",
                          '"red-circle-detect"' in out["fileLine"] and '"expected"' in out["fileLine"],
                          out["fileLine"][:120])
                    p125 = out["parse125"]
                    check("18 parseTaskFile tasks=1", p125["tasks"] == 1, str(p125))
                    check("18 parseTaskFile malformed=1", p125["malformed"] == 1, str(p125))
                    check("18 parseTaskFile name round-trip", p125["name"] == "red-circle-detect", str(p125))
                    check("18 parseTaskFile expected round-trip",
                          p125["exp"] == {"objects": ["circle", "square"]}, str(p125))
                    # --- issue #126: JS grade(rec.expected) == Python _grade(expected) ---
                    ovrRec = out["ovrRec"]
                    _py = _vb18._grade
                    check("18 JS rec.expected detect (triangle) == Python False",
                          ovrRec["detect_rec_bad"] is False
                          and _py("detect", "", {"error": None, "parsed": {"objects": [{"label": "red circle"}, {"label": "blue square"}]}},
                                 {"detect": {"objects": ["circle", "triangle"]}})["semantic"] is False, str(ovrRec))
                    check("18 JS rec.expected detect (circle+square) == Python True",
                          ovrRec["detect_rec_ok"] is True
                          and _py("detect", "", {"error": None, "parsed": {"objects": [{"label": "red circle"}, {"label": "blue square"}]}},
                                 {"detect": {"objects": ["circle", "square"]}})["semantic"] is True, str(ovrRec))
                    check("18 JS rec.expected drive (steer_min=0) == Python True",
                          ovrRec["drive_rec_ok"] is True
                          and _py("drive", "left", {"error": None, "parsed": {"action": "forward", "steer": 5}},
                                 {"drive": {"left": {"action": ["forward"], "steer_min": 0}}})["semantic"] is True, str(ovrRec))
                    check("18 JS rec.expected drive (steer_min=20) == Python False",
                          ovrRec["drive_rec_fail"] is False
                          and _py("drive", "left", {"error": None, "parsed": {"action": "forward", "steer": 5}},
                                 {"drive": {"left": {"action": ["forward"], "steer_min": 20}}})["semantic"] is False, str(ovrRec))
                    check("18 JS rec.expected sequence == Python True",
                          ovrRec["seq_rec_ok"] is True
                          and _py("sequence", "", {"error": None, "parsed": {"moving": "square", "direction": "right"}},
                                 {"sequence": {"moving": "square", "direction": "right"}})["semantic"] is True, str(ovrRec))
                    ovr = out["ovr"]
                    check("18 grade detect default semantic=true", ovr["detect_def"] is True, str(ovr))
                    check("18 grade detect override (triangle) semantic=false", ovr["detect_ovr_bad"] is False, str(ovr))
                    check("18 grade detect override (circle+square) semantic=true", ovr["detect_ovr_ok"] is True, str(ovr))
                    check("18 grade drive override (steer_min=0) semantic=true", ovr["drive_ovr_ok"] is True, str(ovr))
                    check("18 grade drive default (steer_min=20) semantic=false", ovr["drive_def_fail"] is False, str(ovr))

    # 19. runner --tasks-file (issue #126): load + validate a JSONL task file,
    #     grade against its expected (authoritative), --dry-run offline, and
    #     the JS==Python expected-override cross-check.
    print("19. runner --tasks-file (issue #126)")
    try:
        import importlib
        import tempfile
        import vlm_bench as vb19
        importlib.reload(vb19)

        # 19a: --tasks-file flag is present in --help.
        r = run([sys.executable, os.path.join(TOOLS, "vlm_bench.py"), "--help"])
        help19 = r.stdout
        check("19 --tasks-file in --help", r.returncode == 0 and "--tasks-file" in help19, help19[:120])

        # 19b: _expected_for returns the task-shaped expected for built-in
        # tasks, None for custom.
        check("19 _expected_for detect", vb19._expected_for("detect", "") == {"detect": {"objects": ["circle", "square"]}}, str(vb19._expected_for("detect", "")))
        check("19 _expected_for sequence", vb19._expected_for("sequence", "") == {"sequence": {"moving": "square", "direction": "right"}}, str(vb19._expected_for("sequence", "")))
        check("19 _expected_for drive/left", vb19._expected_for("drive", "left") == {"drive": {"left": {"action": ["forward", "right"], "steer_min": 20}}}, str(vb19._expected_for("drive", "left")))
        check("19 _expected_for custom -> None", vb19._expected_for("custom", "") is None, str(vb19._expected_for("custom", "")))

        # 19c: _grade honours an expected override (authoritative) and falls
        # back to the built-in defaults when absent.
        rec_det = {"error": None, "parsed": {"objects": [{"label": "red circle"}, {"label": "green square"}]}}
        check("19 _grade detect default semantic", vb19._grade("detect", "", rec_det)["semantic"] is True, str(vb19._grade("detect", "", rec_det)))
        check("19 _grade detect override (circle+square) semantic",
              vb19._grade("detect", "", rec_det, {"detect": {"objects": ["circle", "square"]}})["semantic"] is True, "")
        check("19 _grade detect override (circle+triangle) NOT semantic",
              vb19._grade("detect", "", rec_det, {"detect": {"objects": ["circle", "triangle"]}})["semantic"] is False, "")
        rec_drv = {"error": None, "parsed": {"action": "forward", "steer": 5}}
        check("19 _grade drive default (steer_min=20) NOT semantic", vb19._grade("drive", "left", rec_drv)["semantic"] is False, "")
        check("19 _grade drive override (steer_min=0) semantic",
              vb19._grade("drive", "left", rec_drv, {"drive": {"left": {"action": ["forward"], "steer_min": 0}}})["semantic"] is True, "")
        rec_seq = {"error": None, "parsed": {"moving": "square", "direction": "right"}}
        check("19 _grade sequence default semantic", vb19._grade("sequence", "", rec_seq)["semantic"] is True, "")
        check("19 _grade sequence override (direction=left) NOT semantic",
              vb19._grade("sequence", "", rec_seq, {"sequence": {"moving": "square", "direction": "left"}})["semantic"] is False, "")

        # 19d: _task_payloads builds image_url payloads from data-URLs.
        pl = vb19._task_payloads(["data:image/png;base64,AAA"])
        check("19 _task_payloads shape", pl == [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}], str(pl))
        check("19 _task_payloads empty", vb19._task_payloads([]) == [], str(vb19._task_payloads([])))

        # 19e: _load_tasks normalizes + validates a JSONL task file.
        tmp19 = tempfile.mkdtemp()
        tf = os.path.join(tmp19, "tasks.jsonl")
        with open(tf, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"name": "t1", "task": "detect", "prompt": "P1",
                                 "images": ["data:image/png;base64,AAA"],
                                 "expected": {"detect": {"objects": ["circle"]}},
                                 "max_tokens": 100, "json_mode": True}) + "\n")
            fh.write("\n")  # blank -> skipped
            fh.write("not-json\n")  # malformed
            fh.write(json.dumps({"prompt": "   "}) + "\n")  # empty prompt -> malformed
            fh.write(json.dumps({"name": "t2", "task": "custom", "prompt": "P2"}) + "\n")
            fh.write(json.dumps({"name": "t3", "task": "bogus", "prompt": "P3"}) + "\n")  # unknown -> custom
        tasks19, malformed19 = vb19._load_tasks(tf)
        check("19 _load_tasks 3 usable", len(tasks19) == 3, f"got {len(tasks19)}")
        check("19 _load_tasks 2 malformed", malformed19 == 2, f"got {malformed19}")
        check("19 task t1 kind=detect", tasks19[0]["kind"] == "detect", str(tasks19[0]))
        check("19 task t1 images preserved", tasks19[0]["images"] == ["data:image/png;base64,AAA"], str(tasks19[0]["images"]))
        check("19 task t1 expected preserved", tasks19[0]["expected"] == {"detect": {"objects": ["circle"]}}, str(tasks19[0]["expected"]))
        check("19 task t1 max_tokens=100", tasks19[0]["max_tokens"] == 100, str(tasks19[0]["max_tokens"]))
        check("19 task t2 kind=custom expected=None", tasks19[1]["kind"] == "custom" and tasks19[1]["expected"] is None, str(tasks19[1]))
        check("19 task t3 unknown kind -> custom", tasks19[2]["kind"] == "custom", str(tasks19[2]))

        # 19f: --dry-run --tasks-file works OFFLINE (no --models, no server).
        r = run([sys.executable, os.path.join(TOOLS, "vlm_bench.py"),
                 "--dry-run", "--tasks-file", tf])
        check("19 dry-run --tasks-file exit 0", r.returncode == 0, r.stderr[:200])
        check("19 dry-run prints task names", "t1" in r.stdout and "t2" in r.stdout and "t3" in r.stdout, r.stdout[:200])
        check("19 dry-run reports image count", "1 image(s)" in r.stdout, r.stdout[:200])
        check("19 dry-run warns on malformed", "malformed task line" in r.stdout, r.stdout[:200])

        # 19g: missing / empty task file -> fail fast (exit 2), no silent no-op.
        r = run([sys.executable, os.path.join(TOOLS, "vlm_bench.py"),
                 "--dry-run", "--tasks-file", os.path.join(tmp19, "nope.jsonl")])
        check("19 missing task file -> exit 2", r.returncode == 2, f"rc={r.returncode} {r.stderr[:120]}")
        ef = os.path.join(tmp19, "empty.jsonl")
        with open(ef, "w", encoding="utf-8") as fh:
            fh.write("not-json\n\n")
        r = run([sys.executable, os.path.join(TOOLS, "vlm_bench.py"),
                 "--dry-run", "--tasks-file", ef])
        check("19 empty task file -> exit 2", r.returncode == 2, f"rc={r.returncode} {r.stderr[:120]}")

        # 19h: built-in --dry-run is unchanged (no task file).
        r = run([sys.executable, os.path.join(TOOLS, "vlm_bench.py"), "--dry-run"])
        check("19 built-in dry-run unchanged", r.returncode == 0 and "DRY-RUN detect" in r.stdout, r.stdout[:120])

        # 19i: execution path uses the task's prompt/images/expected and stores
        # them on the record (verified by faking _attempt to capture the args).
        import vlm_bench as vb19x
        importlib.reload(vb19x)
        captured = {}
        def fake_attempt19(base, key, model, task, variant,
                           timeout_s, max_tokens, use_json_mode=True,
                           phase="test", prompt=None, payloads=None,
                           expected=None, name=None, **kwargs):
            if phase == "test" and "prompt" not in captured:  # capture the FIRST task (t1)
                captured["prompt"] = prompt
                captured["payloads"] = payloads
                captured["expected"] = expected
                captured["name"] = name
                captured["max_tokens"] = max_tokens
                captured["use_json_mode"] = use_json_mode
                captured["task"] = task
                captured["variant"] = variant
            return {"http": 200, "elapsed_s": 0.01, "error": None,
                    "content": "{}", "parsed": {},
                    "grade": {"valid_json": True, "required_keys": True, "semantic": True},
                    "model": model, "task": task, "variant": variant, "phase": phase,
                    "name": name, "expected": expected, "prompt": prompt}
        tmp19r = os.path.join(tmp19, "results.jsonl")
        orig19 = vb19x._attempt
        vb19x._attempt = fake_attempt19
        try:
            rc19 = vb19x.main(["--models", "m1", "--tasks-file", tf,
                               "--no-warmup", "--settle", "0", "--out", tmp19r])
        finally:
            vb19x._attempt = orig19
        check("19 run --tasks-file exit 0", rc19 == 0, f"rc={rc19}")
        check("19 used task prompt", captured.get("prompt") == "P1", str(captured.get("prompt")))
        check("19 used task image payload",
              captured.get("payloads") == [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}],
              str(captured.get("payloads")))
        check("19 used task expected (authoritative)",
              captured.get("expected") == {"detect": {"objects": ["circle"]}}, str(captured.get("expected")))
        check("19 used task max_tokens", captured.get("max_tokens") == 100, str(captured.get("max_tokens")))
        check("19 record stores name", captured.get("name") == "t1", str(captured.get("name")))
        with open(tmp19r, encoding="utf-8") as fh:
            recs19 = [json.loads(l) for l in fh if l.strip()]
        # 3 tasks (t1 detect, t2 custom, t3 custom) x 1 model, no warmup.
        check("19 wrote 3 test records", len(recs19) == 3, f"got {len(recs19)}")
        check("19 records tagged phase=test", all(r.get("phase") == "test" for r in recs19), str([r.get("phase") for r in recs19]))
        first = recs19[0]
        check("19 first record task=detect name=t1", first.get("task") == "detect" and first.get("name") == "t1", str({k: first.get(k) for k in ("task", "name")}))
        check("19 first record expected stored", first.get("expected") == {"detect": {"objects": ["circle"]}}, str(first.get("expected")))
        check("19 first record prompt stored", first.get("prompt") == "P1", str(first.get("prompt")))
    except Exception as e:
        import traceback
        check("19 section ran", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-800:]}")

    # 20. submit_test.py — the git_submit_test helper (issue #127).
    #     Validates with vlm_bench._load_tasks(), copies to
    #     tools/vlm-benchmark/tasks/<name>.jsonl, git add + commit (optionally
    #     push). End-to-end tested in a THROWAWAY git repo (no network).
    print("20. submit_test.py (git_submit_test, issue #127)")
    import importlib as _il20
    import shutil as _sh20
    import tempfile as _tmp20

    SUB = os.path.join(TOOLS, "submit_test.py")
    check("20 submit_test.py present", os.path.isfile(SUB), SUB)
    try:
        py_compile.compile(SUB, doraise=True)
        check("20 py_compile submit_test.py", True)
    except py_compile.PyCompileError as e:
        check("20 py_compile submit_test.py", False, str(e))

    # 20a: --help lists the flags.
    r = run([sys.executable, SUB, "--help"])
    check("20 --help exit 0", r.returncode == 0, r.stderr[:120])
    for flag in ("--message", "--push", "--dry-run"):
        check(f"20 --help has {flag}", flag in r.stdout, r.stdout[:120])

    def _taskfile(d, name, prompt):
        import json as _j20
        p20 = os.path.join(d, "tasks-" + name + ".jsonl")
        with open(p20, "w", encoding="utf-8") as fh20:
            fh20.write(_j20.dumps({"name": name, "task": "custom", "prompt": prompt,
                                   "images": [], "expected": None}) + "\n")
        return p20

    # 20b: unknown task -> exit 2, with the tried paths on stderr.
    tmp20 = _tmp20.mkdtemp()
    r = run([sys.executable, SUB, "no-such-task-xyz"], cwd=tmp20)
    check("20 unknown task -> exit 2", r.returncode == 2, f"rc={r.returncode} {r.stderr[:120]}")
    check("20 unknown task stderr lists tried paths", "no task file found" in r.stderr, r.stderr[:200])

    # 20c: file with no usable tasks -> exit 1 (validated via _load_tasks).
    bad = os.path.join(tmp20, "tasks-bad.jsonl")
    with open(bad, "w", encoding="utf-8") as fh20:
        fh20.write("not-json\n\n")
    r = run([sys.executable, SUB, bad])
    check("20 no usable tasks -> exit 1", r.returncode == 1, f"rc={r.returncode} {r.stderr[:120]}")
    check("20 no usable tasks message", "no usable tasks" in r.stderr, r.stderr[:200])

    # 20d: --dry-run validates + prints the git commands, changes nothing.
    _taskfile(tmp20, "dry-task", "What do you see?")
    r = run([sys.executable, SUB, "dry-task", "--dry-run"], cwd=tmp20)
    check("20 dry-run exit 0", r.returncode == 0, f"rc={r.returncode} {r.stderr[:120]}")
    check("20 dry-run prints git add", "git add tools/vlm-benchmark/tasks/dry-task.jsonl" in r.stdout, r.stdout[:200])
    check("20 dry-run prints git commit", "vlm-bench: add task dry-task" in r.stdout, r.stdout[:200])
    check("20 dry-run creates nothing", not os.path.isdir(os.path.join(TOOLS, "tasks")), "tasks/ dir appeared")

    # 20e: end-to-end in a THROWAWAY git repo (add -> idempotent -> update).
    gitbin = _sh20.which("git")
    check("20 git available", gitbin is not None, "git not on PATH")
    if gitbin:
        repo = _tmp20.mkdtemp()
        tdir = os.path.join(repo, "tools", "vlm-benchmark")
        os.makedirs(tdir)
        for f20 in ("submit_test.py", "vlm_bench.py", "scene_gen.py"):
            _sh20.copyfile(os.path.join(TOOLS, f20), os.path.join(tdir, f20))
        env20 = dict(os.environ)
        env20.update({"GIT_AUTHOR_NAME": "harness", "GIT_AUTHOR_EMAIL": "h@h",
                      "GIT_COMMITTER_NAME": "harness", "GIT_COMMITTER_EMAIL": "h@h"})
        def _git(*a):
            subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True, env=env20)
        _git("init", "-q", "-b", "main")
        _git("config", "user.name", "harness")
        _git("config", "user.email", "h@h")
        _taskfile(repo, "e2e-task", "What do you see?")
        SUB20 = os.path.join(tdir, "submit_test.py")

        r = run([sys.executable, SUB20, "e2e-task"], cwd=repo)
        check("20 e2e add exit 0", r.returncode == 0, f"rc={r.returncode} {r.stderr[:200]}")
        dest20 = os.path.join(tdir, "tasks", "e2e-task.jsonl")
        check("20 e2e file in tasks/", os.path.isfile(dest20), dest20)
        log20 = subprocess.run(["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True, env=env20).stdout
        check("20 e2e commit message (add)", "vlm-bench: add task e2e-task" in log20, log20)

        r = run([sys.executable, SUB20, "e2e-task"], cwd=repo)
        check("20 e2e idempotent exit 0", r.returncode == 0, f"rc={r.returncode} {r.stderr[:200]}")
        check("20 e2e idempotent no-op", "nothing to do" in r.stdout, r.stdout[:200])
        log20 = subprocess.run(["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True, env=env20).stdout
        check("20 e2e idempotent no new commit", log20.count("vlm-bench") == 1, log20)

        # update: change the prompt -> commit with a custom message.
        with open(os.path.join(repo, "tasks-e2e-task.jsonl"), "r+", encoding="utf-8") as fh20:
            import json as _j20
            t20 = _j20.loads(fh20.readline())
            t20["prompt"] = "What do you see? (v2)"
            fh20.seek(0); fh20.truncate()
            fh20.write(_j20.dumps(t20) + "\n")
        r = run([sys.executable, SUB20, "e2e-task", "--message", "my custom message"], cwd=repo)
        check("20 e2e update exit 0", r.returncode == 0, f"rc={r.returncode} {r.stderr[:200]}")
        log20 = subprocess.run(["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True, env=env20).stdout
        check("20 e2e custom commit message", "my custom message" in log20, log20)
        check("20 e2e two commits", log20.count("vlm-bench") == 1 and log20.count("my custom message") == 1, log20)

        # --push without a remote: commit happens, then push fails (exit 1).
        with open(os.path.join(repo, "tasks-e2e-task.jsonl"), "r+", encoding="utf-8") as fh20:
            import json as _j20
            t20 = _j20.loads(fh20.readline())
            t20["prompt"] = "What do you see? (v3)"
            fh20.seek(0); fh20.truncate()
            fh20.write(_j20.dumps(t20) + "\n")
        r = run([sys.executable, SUB20, "e2e-task", "--push"], cwd=repo)
        check("20 e2e --push without remote -> exit 1", r.returncode == 1, f"rc={r.returncode} {r.stderr[:120]}")

    # 20f: every committed tasks/*.jsonl must pass _load_tasks (CI guard).
    sys.path.insert(0, TOOLS)
    import vlm_bench as vb20
    importlib.reload(vb20)
    committed = []
    if os.path.isdir(os.path.join(TOOLS, "tasks")):
        committed = sorted(f20 for f20 in os.listdir(os.path.join(TOOLS, "tasks")) if f20.endswith(".jsonl"))
    check("20 committed tasks all loadable",
          all(len(vb20._load_tasks(os.path.join(TOOLS, "tasks", f20))[0]) >= 1 for f20 in committed),
          str(committed))

    # 20g: the UI exposes taskSubmitCommand + a commit button (Node VM).
    node20 = _sh20.which("node")
    ui20 = os.path.join(TOOLS, "benchmark-ui.html")
    if node20 and os.path.isfile(ui20):
        js20 = r'''
'use strict';
const fs = require('fs');
const vm = require('vm');
const html = fs.readFileSync(process.argv[2], 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const sandbox = { console: console };
sandbox.globalThis = sandbox;
const ctx = vm.createContext(sandbox);
for (const sc of scripts) {
  try { vm.runInContext(sc, ctx); } catch (e) { /* DOM glue: expected in Node */ }
}
const M = sandbox.TankieBenchUI;
const out = {
  cmd1: M.taskSubmitCommand('red circle detect!'),
  cmd2: M.taskSubmitCommand(''),
  hasButton: /data-saved-commit/.test(html),
  hasSubmitRef: /submit_test\.py/.test(html),
};
console.log(JSON.stringify(out));
'''
        js20p = os.path.join(_tmp20.mkdtemp(), "ui20.js")
        with open(js20p, "w", encoding="utf-8") as fh20:
            fh20.write(js20)
        r = run([node20, js20p, ui20])
        if r.returncode != 0:
            check("20 UI Node run", False, f"node exit {r.returncode}: {r.stderr[:200]}")
        else:
            o20 = json.loads(r.stdout.strip().splitlines()[-1])
            check("20 UI taskSubmitCommand sanitizes",
                  o20["cmd1"] == "python3 tools/vlm-benchmark/submit_test.py red-circle-detect", str(o20))
            check("20 UI taskSubmitCommand empty name",
                  o20["cmd2"] == "python3 tools/vlm-benchmark/submit_test.py task", str(o20))
            check("20 UI has commit button", o20["hasButton"] is True, str(o20))
            check("20 UI references submit_test.py", o20["hasSubmitRef"] is True, str(o20))

    print()

    print()
    print(f"{'=' * 40}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'=' * 40}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
