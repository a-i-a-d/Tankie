#!/usr/bin/env python3
"""VLM benchmark for the Tankie brain -- repeatable model comparison.

Compares vision-language models on the LocalAI server across the three tasks
the deliberation/reflex loops actually need:

  detect    single frame  -> list objects as JSON
  sequence  N frames      -> identify the moving object + direction
  drive     single frame  -> driving decision (action/throttle/steer)

Every call is recorded (request metadata, HTTP status, timing, full response
content, parsed JSON, grade) to a JSONL results file so runs can be compared
and turned into markdown tables afterwards (``report.py``).

Pure stdlib (urllib) -- no third-party dependencies, runs on any machine that
can reach the LocalAI server. Scenes are generated on the fly (scene_gen.py).

Usage:
  python3 tools/vlm-benchmark/vlm_bench.py --list
  python3 tools/vlm-benchmark/vlm_bench.py --models a,b,c
  python3 tools/vlm-benchmark/vlm_bench.py --models auto --tasks detect
  python3 tools/vlm-benchmark/vlm_bench.py --dry-run          # offline, no server

Environment overrides (also used by the brain, so one config source):
  TANKIE_LOCALAI_BASE_URL   e.g. http://192.168.178.10:8080/v1
  TANKIE_LOCALAI_API_KEY    e.g. sk-0123456789
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scene_gen  # noqa: E402

DEFAULT_BASE = os.environ.get("TANKIE_LOCALAI_BASE_URL", "http://localai.local:8080/v1")
DEFAULT_KEY = os.environ.get("TANKIE_LOCALAI_API_KEY", "sk-0123456789")

# ---------------------------------------------------------------------------
# prompts (kept identical to the deliberation loop contract)
# ---------------------------------------------------------------------------
SYSTEM = "You are a small tank robot. Respond ONLY with a JSON object. No commentary."

DETECT_PROMPT = (
    "Look at the image. List every distinct object you see. Respond with JSON: "
    '{"objects":[{"label":str,"confidence":float,"bbox":[x,y,w,h]}]}. '
    "Coordinates are pixels in the 640x480 image."
)

SEQUENCE_PROMPT = (
    "These are consecutive camera frames of a scene (oldest first). "
    "Identify which object moves between the frames and its direction. "
    'Respond with JSON: {"moving":str,"direction":"left|right|up|down|none"}. '
    "If nothing moves, use moving='none' and direction='none'."
)

DRIVE_PROMPT = (
    "You are a small tank robot. Task: drive forward, but steer to avoid any red obstacle. "
    "If the path ahead is clear, drive forward (throttle 50). If an obstacle blocks the center, "
    "steer toward the clearer side. If fully blocked, stop. "
    "Tank state: battery_v=7.4, mode=autonomous. "
    'Respond with JSON: {"action":"forward|backward|left|right|stop","throttle":int(0-100),'
    '"steer":int(-100..100),"reason":str,"say":str}'
)

# expected semantics used for grading
DETECT_EXPECTED = ("circle", "square", "triangle")
SEQUENCE_MOVING = "square"
SEQUENCE_DIRECTION = "right"
DRIVE_EXPECTED = {
    "center": {"action": "stop"},
    "left": {"action": "forward", "steer_min": 20},
    "clear": {"action": "forward", "throttle_min": 40},
}

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def _b64(png: bytes) -> str:
    return base64.b64encode(png).decode()


def _scene_payloads(task: str, variant: str) -> tuple[list[dict], int]:
    """Build the image payloads (data URLs) for a task/variant. Returns (payloads, n)."""
    if task == "detect":
        return [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_b64(scene_gen.detect_scene())}"}}], 1
    if task == "sequence":
        frames = scene_gen.sequence_frames(4)
        return [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_b64(f)}"}}
            for f in frames
        ], len(frames)
    if task == "drive":
        return [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_b64(scene_gen.drive_scene(variant))}"}}], 1
    raise ValueError(f"unknown task {task}")


def _prompt_for(task: str) -> str:
    return {"detect": DETECT_PROMPT, "sequence": SEQUENCE_PROMPT, "drive": DRIVE_PROMPT}[task]


def _extract_answer(message: dict) -> str:
    """The model answer, with a reasoning-model fallback.

    Standard models put the answer in ``content``. Reasoning models (e.g.
    ``qwen3.8-heretic``) put it in ``reasoning`` and leave ``content``
    empty. Prefer a non-empty ``content``; otherwise fall back to
    ``reasoning`` so the benchmark grades the real answer, not a false
    negative. Returns "" when neither is present.
    """
    content = (message.get("content") or "").strip()
    if content:
        return content
    return (message.get("reasoning") or "").strip()


def _call(base: str, key: str, model: str, task: str, variant: str,
          timeout_s: float, max_tokens: int) -> dict:
    """One chat/completions call. Returns a result record (never raises)."""
    payloads, n_images = _scene_payloads(task, variant)
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": [{"type": "text", "text": _prompt_for(task)}] + payloads},
        ],
        "max_tokens": max_tokens,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "base": base, "model": model, "task": task, "variant": variant,
        "n_images": n_images, "max_tokens": max_tokens,
        "prompt_sha": hashlib.sha256(_prompt_for(task).encode()).hexdigest()[:12],
        "http": None, "elapsed_s": None, "content": None, "reasoning": None,
        "parsed": None,
        "error": None,
    }
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", "replace")
            rec["http"] = resp.status
            rec["elapsed_s"] = round(time.time() - t0, 2)
            data = json.loads(raw)
            message = data["choices"][0]["message"]
            rec["reasoning"] = message.get("reasoning")
            rec["content"] = _extract_answer(message)
            try:
                rec["parsed"] = json.loads(rec["content"])
            except (json.JSONDecodeError, TypeError):
                rec["parsed"] = None
    except urllib.error.HTTPError as e:
        rec["http"] = e.code
        rec["elapsed_s"] = round(time.time() - t0, 2)
        try:
            detail = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            detail = ""
        rec["error"] = f"HTTP {e.code}: {detail}"
    except Exception as e:  # timeout, DNS, connection refused, ...
        rec["elapsed_s"] = round(time.time() - t0, 2)
        rec["error"] = f"{type(e).__name__}: {e}"
    rec["grade"] = _grade(task, variant, rec)
    return rec


def _grade(task: str, variant: str, rec: dict) -> dict:
    """Grade a result record. All checks are data-driven and re-runnable."""
    g = {"valid_json": False, "required_keys": False, "semantic": False}
    if rec.get("error"):
        return g
    parsed = rec.get("parsed")
    if not isinstance(parsed, dict):
        return g
    g["valid_json"] = True
    if task == "detect":
        objs = parsed.get("objects")
        g["required_keys"] = isinstance(objs, list) and len(objs) > 0
        if g["required_keys"]:
            text = json.dumps(parsed).lower()
            g["semantic"] = all(k in text for k in DETECT_EXPECTED)
    elif task == "sequence":
        g["required_keys"] = "moving" in parsed and "direction" in parsed
        if g["required_keys"]:
            moving = str(parsed.get("moving", "")).lower()
            g["semantic"] = SEQUENCE_MOVING in moving and str(parsed.get("direction", "")).lower() == SEQUENCE_DIRECTION
    elif task == "drive":
        g["required_keys"] = "action" in parsed
        if g["required_keys"]:
            exp = DRIVE_EXPECTED[variant]
            action = str(parsed.get("action", "")).lower()
            ok = action == exp["action"]
            if "steer_min" in exp:
                try:
                    ok = ok and abs(float(parsed.get("steer", 0))) >= exp["steer_min"]
                except (TypeError, ValueError):
                    ok = False
            if "throttle_min" in exp:
                try:
                    ok = ok and float(parsed.get("throttle", 0)) >= exp["throttle_min"]
                except (TypeError, ValueError):
                    ok = False
            g["semantic"] = bool(ok)
    return g


def list_models(base: str, key: str) -> list[str]:
    req = urllib.request.Request(base.rstrip("/") + "/models",
                                 headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    return sorted(m["id"] for m in data.get("data", []))


_VISION_HINTS = re.compile(
    r"multimodal|vision|vlm|magistral|moondream|fara|winnow|gemma|ornith|mimo|ternary|qwen3\.8|qwen3\.5",
    re.I,
)


def auto_models(all_models: list[str]) -> list[str]:
    """Heuristic: models whose id suggests vision/multimodal capability."""
    return [m for m in all_models if _VISION_HINTS.search(m)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=DEFAULT_BASE, help="LocalAI base URL (default: $TANKIE_LOCALAI_BASE_URL)")
    ap.add_argument("--key", default=DEFAULT_KEY, help="API key (default: $TANKIE_LOCALAI_API_KEY)")
    ap.add_argument("--models", default=None, help="comma-separated model ids, or 'auto' (vision heuristics)")
    ap.add_argument("--tasks", default="detect,sequence,drive", help="comma list of detect,sequence,drive")
    ap.add_argument("--drive-variants", default="center,left,clear")
    ap.add_argument("--runs", type=int, default=1, help="runs per model/task/variant")
    ap.add_argument("--timeout", type=float, default=240.0, help="per-call timeout in seconds")
    ap.add_argument("--max-tokens", type=int, default=500)
    ap.add_argument("--out", default=None, help="results JSONL path (default: results/<timestamp>.jsonl)")
    ap.add_argument("--list", action="store_true", help="list models on the server and exit")
    ap.add_argument("--dry-run", action="store_true", help="generate scenes + payloads, print summary, no server call")
    args = ap.parse_args(argv)

    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    variants = [v.strip() for v in args.drive_variants.split(",") if v.strip()]

    if args.dry_run:
        for task in tasks:
            for variant in (variants if task == "drive" else [""]):
                payloads, n = _scene_payloads(task, variant)
                print(f"DRY-RUN {task}/{variant or '-'}: {n} image(s), "
                      f"payload ~{sum(len(p['image_url']['url']) for p in payloads) // 1024} KB")
        return 0

    if args.list:
        for m in list_models(args.base, args.key):
            print(m)
        return 0

    if not args.models:
        ap.error("--models is required (or use --list / --dry-run)")
    if args.models == "auto":
        models = auto_models(list_models(args.base, args.key))
        print(f"# auto-selected {len(models)} vision-ish models: {', '.join(models)}")
    else:
        models = [m.strip() for m in args.models.split(",") if m.strip()]

    out_path = args.out or os.path.join(RESULTS_DIR, f"bench-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    n_calls = sum(len(variants if t == "drive" else [""]) for t in tasks) * len(models) * args.runs
    print(f"# benchmark: {len(models)} model(s) x {len(tasks)} task(s) x {args.runs} run(s) = {n_calls} call(s)")
    print(f"# results -> {out_path}")

    ok = 0
    with open(out_path, "a", encoding="utf-8") as fh:
        for model in models:
            for task in tasks:
                for variant in (variants if task == "drive" else [""]):
                    for run in range(1, args.runs + 1):
                        rec = _call(args.base, args.key, model, task, variant,
                                   args.timeout, args.max_tokens)
                        rec["run"] = run
                        fh.write(json.dumps(rec) + "\n")
                        fh.flush()
                        g = rec["grade"]
                        status = (
                            "PASS" if g["semantic"]
                            else "json" if g["valid_json"]
                            else "err"
                        )
                        ok += status == "PASS"
                        print(f"[{status:4s}] {model:45s} {task:8s} {variant or '-':6s} "
                              f"t={rec['elapsed_s']}s http={rec['http']} "
                              f"{'| ' + rec['error'][:80] if rec['error'] else ''}", flush=True)
    print(f"# done: {ok}/{n_calls} fully-passing calls. Results: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
