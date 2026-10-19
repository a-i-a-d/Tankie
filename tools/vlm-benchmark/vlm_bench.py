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
    '"steer":int(-100..100, positive=right, negative=left),"reason":str,"say":str}'
)

# expected semantics used for grading
DETECT_EXPECTED = ("circle", "square")  # red circle, green square, blue square (no triangle in scene)
SEQUENCE_MOVING = "square"
SEQUENCE_DIRECTION = "right"
DRIVE_EXPECTED = {
    "center": {"action": "stop"},
    # "left" = obstacle on the LEFT of the lane (drive_scene: x=90..210).
    # Correct avoidance is to steer RIGHT (wheels.h, issue #85: steer > 0 =
    # RIGHT, steer < 0 = LEFT), so the model must steer AWAY (positive). A
    # model steering INTO the obstacle (negative steer) is a FAIL.
    "left": {"action": ["forward", "right"], "steer_min": 20},
    "clear": {"action": ["forward"], "throttle_min": 40},
}

# The tasks the benchmark knows how to build a scene + prompt for.
TASKS = ("detect", "sequence", "drive")

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


def _load_tasks(path: str) -> tuple[list[dict], int]:
    """Load tasks from a JSONL task file (issue #126).

    Each line is a self-contained task definition (the shape the #125 web UI
    exports via ``taskToFile``): ``{name, model, task, variant, prompt,
    images:[dataURL], max_tokens, json_mode, expected, ts}``. Returns
    ``(tasks, malformed)`` where each task is normalized to
    ``{name, kind, variant, prompt, images, max_tokens, json_mode, expected}``
    and ``kind`` is the task's ``task`` field (``detect``/``sequence``/
    ``drive``/``custom``). Blank lines are skipped; malformed lines are
    counted, not fatal. A task with no usable ``prompt`` is dropped (and
    counted as malformed) so it can never produce an empty call.
    """
    tasks: list[dict] = []
    malformed = 0
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                malformed += 1
                continue
            if not isinstance(t, dict):
                malformed += 1
                continue
            prompt = t.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                malformed += 1
                continue
            kind = t.get("task") or "custom"
            if kind not in TASKS and kind != "custom":
                # Unknown task -> treat as custom (no built-in scene/expected).
                kind = "custom"
            images = t.get("images")
            if not isinstance(images, list):
                images = []
            images = [u for u in images if isinstance(u, str) and u]
            max_tokens = t.get("max_tokens")
            if not isinstance(max_tokens, int) or max_tokens <= 0:
                max_tokens = 500
            tasks.append({
                "name": t.get("name") or "",
                "kind": kind,
                "variant": t.get("variant") or "",
                "prompt": prompt,
                "images": images,
                "max_tokens": max_tokens,
                "json_mode": t.get("json_mode", True) is not False,
                "expected": t.get("expected") if isinstance(t.get("expected"), dict) else None,
            })
    return tasks, malformed


def _task_payloads(images: list[str]) -> list[dict]:
    """Build image payloads from a task's inline data-URLs (issue #126)."""
    return [{"type": "image_url", "image_url": {"url": u}} for u in images]


def _content_text(value) -> str:
    """Normalize an OpenAI ``content``/``reasoning`` field to plain text.

    Handles the three shapes a server may return: a plain ``str``, a list
    of content parts (``[{"type": "text", "text": ...}, ...]``), or
    ``None``. Anything else is ignored. Returns "" when there is no text.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = []
        for part in value:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return " ".join(s for s in (p.strip() for p in parts) if s)
    return ""


def _extract_answer(message: dict) -> str:
    """The model answer, with a reasoning-model fallback.

    Standard models put the answer in ``content``. Reasoning models (e.g.
    ``qwen3.8-heretic``) put it in ``reasoning`` and leave ``content``
    empty. Prefer a non-empty ``content``; otherwise fall back to
    ``reasoning`` so the benchmark grades the real answer, not a false
    negative. ``content``/``reasoning`` may be a string or a list of
    content parts (both are normalized via :func:`_content_text`).
    Returns "" when neither is present.
    """
    content = _content_text(message.get("content"))
    if content:
        return content
    return _content_text(message.get("reasoning"))


# HTTP status codes worth retrying: 503 = LocalAI "load-cooldown" (the model
# is not loaded yet and the server tells the client to back off). Other 5xx
# codes (e.g. 500 "could not load model") are deterministic -- retrying them
# only wastes time, so they fail fast.
_TRANSIENT_HTTP = (503,)
RETRY_DELAY_S = 1.0

# Default settle delay (seconds) between models. LocalAI keeps a model loaded
# after a call; if the previous model errored / failed to load, its backend
# worker may still be settling. A short pause before the next model's first
# call gives the server time to recover so one bad model does not poison the
# next one. 0 disables the pause.
DEFAULT_SETTLE_S = 30.0


def _call(base: str, key: str, model: str, task: str, variant: str,
          timeout_s: float, max_tokens: int, max_retries: int = 1,
          use_json_mode: bool = True, prompt: str | None = None,
          payloads: list[dict] | None = None, expected: dict | None = None,
          name: str | None = None) -> dict:
    """A benchmark call with a bounded retry on transient failures (L3).

    Retries ONLY transient HTTP 503 (LocalAI load-cooldown) -- a deterministic
    500 (model won't load), a 4xx, or a timeout is recorded as-is. ``retries``
    in the record shows how many retries were used (0 = first attempt was
    final). Never raises.

    ``prompt``/``payloads``/``expected``/``name`` (issue #126): when supplied
    (from a task file) they override the built-in scene + prompt + expected.
    """
    rec = _attempt(base, key, model, task, variant, timeout_s, max_tokens, use_json_mode,
                   prompt=prompt, payloads=payloads, expected=expected, name=name)
    retries = 0
    for _ in range(max(0, max_retries)):
        http = rec.get("http")
        if not (isinstance(http, int) and http in _TRANSIENT_HTTP):
            break
        time.sleep(RETRY_DELAY_S)
        rec = _attempt(base, key, model, task, variant, timeout_s, max_tokens, use_json_mode,
                       prompt=prompt, payloads=payloads, expected=expected, name=name)
        retries += 1
    rec["retries"] = retries
    return rec


def _attempt(base: str, key: str, model: str, task: str, variant: str,
             timeout_s: float, max_tokens: int, use_json_mode: bool = True,
             phase: str = "test", prompt: str | None = None,
             payloads: list[dict] | None = None,
             expected: dict | None = None, name: str | None = None) -> dict:
    """One chat/completions attempt. Returns a result record (never raises).

    Kept at module level (instead of inlined in ``_call``) so tests can inject
    a fake attempt -- the retry logic in ``_call`` is then testable offline.

    ``phase`` marks the record in the results file: ``"test"`` for the graded
    benchmark calls, ``"warmup"`` for the one-shot model-load call (issue #89).

    ``prompt``/``payloads``/``expected``/``name`` (issue #126): when supplied
    (from a task file) they override the built-in scene + prompt + expected;
    ``expected``/``name`` are stored on the record so the web UI can render +
    re-grade it.
    """
    if phase == "warmup":
        # Q1 (issue #89): reuse the real detect scene (image included so the
        # mmproj projector is loaded too) with a short prompt + capped output.
        prompt = WARMUP_PROMPT
        payloads, n_images = _scene_payloads("detect", "")
    elif prompt is not None and payloads is not None:
        # issue #126: task-file task -- use its prompt + images verbatim.
        n_images = len(payloads)
    else:
        prompt = _prompt_for(task)
        payloads, n_images = _scene_payloads(task, variant)
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": [{"type": "text", "text": prompt}] + payloads},
        ],
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    if use_json_mode:
        # L4: response_format is only sent when enabled -- models that reject
        # it (400) can be run with --no-json-mode and still be graded.
        body["response_format"] = {"type": "json_object"}
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
        "phase": phase,
        "name": name,
        "prompt": prompt,
        "prompt_sha": hashlib.sha256(prompt.encode()).hexdigest()[:12],
        "http": None, "elapsed_s": None, "content": None, "reasoning": None,
        "parsed": None,
        "error": None,
        "expected": expected,
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
    rec["grade"] = _grade(task, variant, rec, expected)
    return rec


def _expected_for(task: str, variant: str) -> dict | None:
    """The task-shaped expected answer for a built-in task (issue #126).

    Returns the dict the record should store under ``expected`` so the web UI
    can render + re-grade it without re-hardcoding the answer. ``None`` for
    ``custom`` (no built-in expected). The shape mirrors the UI's ``EXPECTED``
    and the ``expectedOverride`` contract from issue #125 (Q4=b).
    """
    if task == "detect":
        return {"detect": {"objects": list(DETECT_EXPECTED)}}
    if task == "sequence":
        return {"sequence": {"moving": SEQUENCE_MOVING, "direction": SEQUENCE_DIRECTION}}
    if task == "drive":
        return {"drive": {variant: DRIVE_EXPECTED[variant]}}
    return None


def _grade(task: str, variant: str, rec: dict, expected: dict | None = None) -> dict:
    """Grade a result record. All checks are data-driven and re-runnable.

    ``expected`` (issue #126) is an optional task-shaped expected answer --
    either a task file's ``expected`` (authoritative, human-adjustable per
    issue #125 Q4=b) or a record's stored ``expected``. When absent, the
    built-in defaults are used, so the original detect/sequence/drive
    behaviour is unchanged.
    """
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
            if expected is not None:
                keys = expected.get("detect", {}).get("objects")
                g["semantic"] = isinstance(keys, list) and bool(keys) and all(k in text for k in keys)
            else:
                g["semantic"] = all(k in text for k in DETECT_EXPECTED)
    elif task == "sequence":
        g["required_keys"] = "moving" in parsed and "direction" in parsed
        if g["required_keys"]:
            moving = str(parsed.get("moving", "")).lower()
            direction = str(parsed.get("direction", "")).lower()
            if expected is not None:
                seq = expected.get("sequence", {})
                exp_moving = seq.get("moving")
                exp_dir = seq.get("direction")
                g["semantic"] = bool(exp_moving) and bool(exp_dir) \
                    and str(exp_moving).lower() in moving and direction == str(exp_dir).lower()
            else:
                g["semantic"] = SEQUENCE_MOVING in moving and direction == SEQUENCE_DIRECTION
    elif task == "drive":
        g["required_keys"] = "action" in parsed
        if g["required_keys"]:
            if expected is not None:
                exp = expected.get("drive", {}).get(variant) or {}
            else:
                exp = DRIVE_EXPECTED[variant]
            action = str(parsed.get("action", "")).lower()
            allowed = exp["action"] if isinstance(exp.get("action"), list) else [exp.get("action")]
            ok = action in allowed
            if "steer_min" in exp:  # signed: steer must be >= this (e.g. +20 = steer right)
                try:
                    ok = ok and float(parsed.get("steer", 0)) >= exp["steer_min"]
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
    # Skip entries without a usable ``id`` (a malformed entry must not
    # crash --list).
    models = [m["id"] for m in data.get("data", [])
              if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]]
    return sorted(models)


# Models that can never be a vision-language model (ASR / TTS / VAD /
# embedding / image-gen / voice / speech classes). Excluded *before* any
# name-hint matching (M1: the old heuristic had false positives on text-only
# models and missed real vision models).
_NON_VISION = re.compile(
    r"asr|tts|vad|embedding|embed|supertonic|ace-step|voxcpm|flux|vibevoice|localvqe|whisper|silero|parakeet",
    re.I,
)

# Name hints that *suggest* vision capability. Deliberately conservative:
# a miss only costs a candidate (include it explicitly via --models).
# Fixes from the old regex: ``vllm`` now matches (the old ``vlm`` did not),
# and the blind ``mimo|ornith|ternary|qwen3.8|qwen3.5`` catch-alls are gone
# (they only produced text-only false positives).
_VISION_HINTS = re.compile(
    r"multimodal|vision|\bvlm\b|vllm|magistral|moondream|smolvlm|fara|winnow|gemma|arex|spark|ui-mate|glm",
    re.I,
)


def auto_models(all_models: list[str]) -> list[str]:
    """Tier 0+1 (offline): exclude non-vision classes, keep name-hint candidates.

    This is a heuristic -- the *reliable* selection is ``--probe``, which
    live-verifies each candidate actually accepts an image.
    """
    return [m for m in all_models
            if not _NON_VISION.search(m) and _VISION_HINTS.search(m)]


PROBE_PROMPT = "How many distinct colors do you see? Respond with JSON: \"{\\\"count\\\":int}\""

# The warmup call (issue #89): one cheap detect-scene call per model BEFORE the
# graded test calls, purely to load the model (weights + mmproj projector) into
# GPU memory. Its elapsed time is the cold/load time; it is recorded with
# phase="warmup" and excluded from all pass-rate / latency statistics.
WARMUP_PROMPT = (
    "Warm-up call. Look at the image and reply with JSON: {\"ok\":true}."
)
WARMUP_MAX_TOKENS = 64


def _probe_one(base: str, key: str, model: str, timeout_s: float = 45.0) -> tuple[bool, str]:
    """One tiny image call to test vision capability. Never raises.

    Returns ``(vision_capable, reason)``. A model is capable iff it accepts
    the image (HTTP 200) and returns a non-empty answer.
    """
    png = scene_gen.probe_image()
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Respond ONLY with a JSON object."},
            {"role": "user", "content": [
                {"type": "text", "text": PROBE_PROMPT},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{base64.b64encode(png).decode()}"}},
            ]},
        ],
        "max_tokens": 20,
        "temperature": 0,
    }
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
            message = data["choices"][0]["message"]
            answer = _extract_answer(message)
            if answer.strip():
                return True, "ok"
            return False, "empty answer"
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:120]
        except Exception:
            detail = ""
        return False, f"HTTP {e.code}: {detail}"
    except Exception as e:  # timeout, DNS, connection refused, ...
        return False, f"{type(e).__name__}: {e}"


def probe_filter(base: str, key: str, candidates: list[str],
                 timeout_s: float = 45.0) -> list[str]:
    """Live capability probe: keep only models that accept an image and answer."""
    kept: list[str] = []
    for m in candidates:
        ok, reason = _probe_one(base, key, m, timeout_s)
        print(f"# probe {m}: {'KEEP' if ok else 'DROP'} ({reason})")
        if ok:
            kept.append(m)
    return kept


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
    ap.add_argument("--probe", action="store_true",
                    help="with --models auto: live-probe each candidate with one tiny image; keep only vision-capable models")
    ap.add_argument("--save-scenes", default=None, metavar="DIR", help="write all scene PNGs to DIR and exit")
    ap.add_argument("--no-json-mode", action="store_true",
                    help="L4: do not send response_format=json_object (for models that reject it); the grader is unchanged")
    ap.add_argument("--settle", type=float, default=DEFAULT_SETTLE_S, metavar="SECONDS",
                    help="pause this long between models so the LocalAI server can settle "
                         "after a previous model errored / unloaded (default: 30; 0 disables)")
    ap.add_argument("--no-warmup", action="store_true",
                    help="skip the one-shot warmup call per model (issue #89); the first "
                         "test call then absorbs the cold model-load time")
    ap.add_argument("--tasks-file", default=None, metavar="PATH",
                    help="issue #126: run tasks from a JSONL task file (the #125 web-UI export) "
                         "instead of the built-in --tasks; each line's prompt/images/expected are used")
    args = ap.parse_args(argv)

    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    variants = [v.strip() for v in args.drive_variants.split(",") if v.strip()]

    # Fail fast on a typo'd task/variant: a bad value used to crash mid-run
    # (after partial results were already written). ap.error() exits 2 with
    # a clean usage message before any scene generation or server call.
    for t in tasks:
        if t not in TASKS:
            ap.error(f"unknown task {t!r} (choose from: {', '.join(TASKS)})")
    for v in variants:
        if v not in DRIVE_EXPECTED:
            ap.error(f"unknown drive variant {v!r} (choose from: {', '.join(DRIVE_EXPECTED)})")

    # issue #126: a task file takes precedence over --tasks / --drive-variants.
    # It is loaded + validated up front so a missing/empty file fails fast
    # (exit 2) instead of a silent no-op, and --dry-run can run offline.
    file_tasks: list[dict] = []
    if args.tasks_file:
        if not os.path.isfile(args.tasks_file):
            ap.error(f"--tasks-file not found: {args.tasks_file}")
        file_tasks, malformed = _load_tasks(args.tasks_file)
        if malformed:
            print(f"# warning: {malformed} malformed task line(s) skipped in {args.tasks_file}")
        if not file_tasks:
            ap.error(f"--tasks-file {args.tasks_file} has no usable tasks (each line needs a non-empty prompt)")

    if args.dry_run:
        if args.tasks_file:
            for t in file_tasks:
                payloads = _task_payloads(t["images"])
                n = len(payloads)
                print(f"DRY-RUN {t['name'] or t['kind']}/{t['variant'] or '-'}: {n} image(s), "
                      f"prompt {len(t['prompt'])} chars, "
                      f"payload ~{sum(len(p['image_url']['url']) for p in payloads) // 1024} KB")
        else:
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

    if args.save_scenes:
        paths = scene_gen.save_scenes(args.save_scenes)
        for p in paths:
            print(f"  {p} ({os.path.getsize(p)} bytes)")
        print(f"\n# saved {len(paths)} scene images to {args.save_scenes}")
        return 0

    if not args.models:
        ap.error("--models is required (or use --list / --dry-run)")
    if args.models == "auto":
        candidates = auto_models(list_models(args.base, args.key))
        print(f"# auto candidates (name hints): {', '.join(candidates) or '(none)'}")
        if args.probe:
            models = probe_filter(args.base, args.key, candidates)
            print(f"# probe kept {len(models)} vision-capable model(s): {', '.join(models) or '(none)'}")
            if not models:
                ap.error("--models auto --probe: no vision-capable models found "
                         "(use --models to name one explicitly)")
        else:
            models = candidates
            if not models:
                ap.error("--models auto: no candidates matched the name hints "
                         "(use --models to name models explicitly, or add --probe)")
    else:
        models = [m.strip() for m in args.models.split(",") if m.strip()]

    out_path = args.out or os.path.join(RESULTS_DIR, f"bench-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
    out_dir = os.path.dirname(out_path)
    if out_dir:  # a bare filename ("my-results.jsonl") has no directory part
        os.makedirs(out_dir, exist_ok=True)

    if args.tasks_file:
        n_tasks = len(file_tasks)
        n_calls = n_tasks * len(models) * args.runs
        n_warmup = 0 if args.no_warmup else len(models)
        print(f"# benchmark: {len(models)} model(s) x {n_tasks} task-file task(s) x {args.runs} run(s) = {n_calls} test call(s)")
        print(f"# warmup: {'disabled (--no-warmup)' if args.no_warmup else f'{n_warmup} model-load call(s), one per model (issue #89)'}")
        print(f"# results -> {out_path}")

        ok = 0
        with open(out_path, "a", encoding="utf-8") as fh:
            for i, model in enumerate(models):
                if i > 0 and args.settle > 0:
                    print(f"# settling {args.settle:g}s before {model} (server recovery window)", flush=True)
                    time.sleep(args.settle)
                # Warmup (issue #89): one cheap detect-scene call to load the
                # model (weights + mmproj) into GPU memory. Recorded with
                # phase="warmup", excluded from graded stats, never fatal.
                if not args.no_warmup:
                    rec = _attempt(args.base, args.key, model, "detect", "",
                                   args.timeout, WARMUP_MAX_TOKENS,
                                   use_json_mode=not args.no_json_mode,
                                   phase="warmup")
                    fh.write(json.dumps(rec) + "\n")
                    fh.flush()
                    w_status = "ok" if not rec["error"] else "err"
                    print(f"[warmup] {model:45s} t={rec['elapsed_s']}s http={rec['http']} "
                          f"{'| ' + rec['error'][:80] if rec['error'] else w_status}", flush=True)
                for t in file_tasks:
                    kind = t["kind"]
                    variant = t["variant"]
                    # expected: the task's own (authoritative) else the built-in
                    # default for built-in kinds (so records stay self-contained).
                    expected = t["expected"] if t["expected"] is not None else _expected_for(kind, variant)
                    for run in range(1, args.runs + 1):
                        rec = _call(args.base, args.key, model, kind, variant,
                                   args.timeout, t["max_tokens"],
                                   use_json_mode=t["json_mode"] and not args.no_json_mode,
                                   prompt=t["prompt"], payloads=_task_payloads(t["images"]),
                                   expected=expected, name=t["name"] or None)
                        rec["run"] = run
                        rec["phase"] = "test"
                        fh.write(json.dumps(rec) + "\n")
                        fh.flush()
                        g = rec["grade"]
                        status = (
                            "PASS" if g["semantic"]
                            else "json" if g["valid_json"]
                            else "err"
                        )
                        ok += status == "PASS"
                        label = t["name"] or kind
                        print(f"[{status:4s}] {model:45s} {label:20s} {variant or '-':6s} "
                              f"t={rec['elapsed_s']}s http={rec['http']} "
                              f"{'| ' + rec['error'][:80] if rec['error'] else ''}", flush=True)
        print(f"# done: {ok}/{n_calls} fully-passing calls. Results: {out_path}")
        return 0

    n_calls = sum(len(variants if t == "drive" else [""]) for t in tasks) * len(models) * args.runs
    n_warmup = 0 if args.no_warmup else len(models)
    print(f"# benchmark: {len(models)} model(s) x {len(tasks)} task(s) x {args.runs} run(s) = {n_calls} test call(s)")
    print(f"# warmup: {'disabled (--no-warmup)' if args.no_warmup else f'{n_warmup} model-load call(s), one per model (issue #89)'}")
    print(f"# results -> {out_path}")

    ok = 0
    with open(out_path, "a", encoding="utf-8") as fh:
        for i, model in enumerate(models):
            if i > 0 and args.settle > 0:
                print(f"# settling {args.settle:g}s before {model} (server recovery window)", flush=True)
                time.sleep(args.settle)
            # Warmup (issue #89): one cheap detect-scene call to load the model
            # (weights + mmproj) into GPU memory so every MEASURED call runs
            # warm. Recorded with phase="warmup" and excluded from the graded
            # stats; its elapsed_s is the cold/load time. Never fatal: if the
            # warmup errors, the test calls still run and record their own
            # errors.
            if not args.no_warmup:
                rec = _attempt(args.base, args.key, model, "detect", "",
                               args.timeout, WARMUP_MAX_TOKENS,
                               use_json_mode=not args.no_json_mode,
                               phase="warmup")
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                w_status = "ok" if not rec["error"] else "err"
                print(f"[warmup] {model:45s} t={rec['elapsed_s']}s http={rec['http']} "
                      f"{'| ' + rec['error'][:80] if rec['error'] else w_status}", flush=True)
            for task in tasks:
                for variant in (variants if task == "drive" else [""]):
                    for run in range(1, args.runs + 1):
                        rec = _call(args.base, args.key, model, task, variant,
                                   args.timeout, args.max_tokens,
                                   use_json_mode=not args.no_json_mode)
                        rec["run"] = run
                        rec["phase"] = "test"
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
