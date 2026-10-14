#!/usr/bin/env python3
"""Tankie manual integration test runner (issue #87).

A **human-in-the-loop** harness that walks a low->high integration test plan on
the real tank. For each test it:

  1. prints the **expected hardware behaviour** (large + clear),
  2. sends the action through the right **layer adapter** (bridge / raw serial /
     camera / LocalAI / ai_control),
  3. captures **evidence** (reply, state store, raw log),
  4. **auto-checks** the machine-verifiable assertions (state fields, HTTP 200,
     ack present, clamp value, ...),
  5. prompts the operator for **PASS / FAIL / SKIP + an optional comment**,
  6. records everything and writes a **JSONL + self-contained HTML report**.

Design-for-web (issue #87 "a web based interface ... for a later step"): the
runner is transport-agnostic and stateless per-test, and the **YAML spec is the
stable contract**. A later web UI is a thin client over the same spec + a tiny
``--serve`` form — no rework of the tests.

Usage (on the tank Pi, with the tank powered + on a clear surface):
    python3 tests/manual/run_manual.py --suite all
    python3 tests/manual/run_manual.py --suite bridge --filter S2-02
    python3 tests/manual/run_manual.py --dry-run          # validate, no hardware

Safety:
    --max-safety low|med|high   cap the safety levels that will run (default: all)
    --i-confirm-tank-is-clear  required before any motion test runs
    Ctrl-C / the stop key       sends `stop` immediately

No CI job is added for this (issue #87 Q7) — it is a hardware acceptance tool.
The code is kept ``py_compile``-clean and ``--dry-run`` works anywhere.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import html
import json
import os
import signal
import sys
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple

# ---------------------------------------------------------------------------
# Import the adapter package (same directory).
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from adapters import get_adapter
    from adapters.base import evaluate_assertions
except ImportError:  # pragma: no cover - adapters are in-tree
    get_adapter = None
    evaluate_assertions = None

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is a declared dep
    yaml = None

SPECS_DIR = os.path.join(_HERE, "specs")
RESULTS_DIR = os.path.join(_HERE, "results")
DEFAULT_MAX_SAFETY = "high"
SAFETY_ORDER = {"low": 0, "med": 1, "high": 2}
VERDICTS = ("pass", "fail", "skip")


# ===========================================================================
# Spec loading + validation
# ===========================================================================
REQUIRED_CASE_FIELDS = ("id", "layer", "title", "safety")


def _load_yaml(path: str) -> Any:
    with open(path) as f:
        return yaml.safe_load(f)


def validate_case(case: Any, idx: int) -> List[str]:
    """Return a list of schema errors for one test case (empty = valid)."""
    errors: List[str] = []
    if not isinstance(case, dict):
        return [f"case #{idx}: not a mapping (got {type(case).__name__})"]
    for field in REQUIRED_CASE_FIELDS:
        if field not in case or case[field] in (None, ""):
            errors.append(f"case #{idx} ({case.get('id', '?')}): missing required field '{field}'")
    if case.get("safety") not in SAFETY_ORDER:
        errors.append(f"case #{idx} ({case.get('id', '?')}): safety must be one of {sorted(SAFETY_ORDER)}")
    if "action" not in case or not isinstance(case.get("action"), dict):
        errors.append(f"case #{idx} ({case.get('id', '?')}): 'action' must be a mapping")
    observe = case.get("observe")
    if observe is not None and not isinstance(observe, dict):
        errors.append(f"case #{idx} ({case.get('id', '?')}): 'observe' must be a mapping")
    # Validate auto assertions are well-formed (path + one known op).
    auto = (observe or {}).get("auto")
    if auto is not None:
        if not isinstance(auto, list):
            errors.append(f"case #{idx} ({case.get('id', '?')}): 'observe.auto' must be a list")
        else:
            from adapters.base import SUPPORTED_OPS
            for a in auto:
                if not isinstance(a, dict) or "path" not in a:
                    errors.append(f"case #{idx} ({case.get('id', '?')}): auto assertion missing 'path'")
                    continue
                ops = [k for k in a if k != "path"]
                if "exists" not in a and not any(k in SUPPORTED_OPS for k in ops):
                    errors.append(
                        f"case #{idx} ({case.get('id', '?')}): auto assertion for "
                        f"{a['path']!r} has no operator (eq/ne/gt/lt/gte/lte/contains/exists)"
                    )
    return errors


def load_specs(specs_dir: str, suite: str) -> List[Tuple[str, Any]]:
    """Load test cases. Returns a list of ``(suite_name, case)`` tuples.

    ``suite`` is ``all`` or a specific suite name (a spec file basename
    without ``.yaml``).
    """
    if not os.path.isdir(specs_dir):
        return []
    out: List[Tuple[str, Any]] = []
    for fn in sorted(os.listdir(specs_dir)):
        if not fn.endswith((".yaml", ".yml")):
            continue
        name = os.path.splitext(fn)[0]
        if suite != "all" and name != suite:
            continue
        data = _load_yaml(os.path.join(specs_dir, fn))
        cases = data if isinstance(data, list) else (data.get("cases") if isinstance(data, dict) else None)
        if cases is None:
            continue
        for case in cases:
            out.append((name, case))
    return out


def validate_all(pairs: List[Tuple[str, Any]]) -> List[str]:
    """Validate every case + enforce unique ids across all suites."""
    errors: List[str] = []
    seen: Dict[str, str] = {}
    for i, (suite, case) in enumerate(pairs):
        cid = case.get("id", f"<{suite}[{i}]>") if isinstance(case, dict) else f"<{suite}[{i}]>"
        for e in validate_case(case, i):
            errors.append(f"[{suite}] {e}")
        if isinstance(case, dict):
            cid = case.get("id")
            if cid in seen:
                errors.append(f"duplicate test id {cid!r} in {suite} and {seen[cid]}")
            else:
                seen[cid] = suite
    return errors


# ===========================================================================
# Test execution
# ===========================================================================
def _adapter_for(layer: str, cfg: Mapping[str, Any]):
    return get_adapter(layer, cfg)


def run_one(case: Mapping[str, Any], cfg: Mapping[str, Any], dry_run: bool) -> Dict[str, Any]:
    """Run a single test case and return a result record."""
    layer = case["layer"]
    action = dict(case.get("action") or {})
    settle_s = float(action.pop("settle_s", 0.0) or 0.0)
    observe = case.get("observe") or {}
    expected = observe.get("expected", "")
    auto = observe.get("auto")

    result: Dict[str, Any] = {
        "id": case["id"],
        "layer": layer,
        "title": case["title"],
        "safety": case["safety"],
        "expected": expected,
        "action": action,
        "evidence": {},
        "auto": [],
        "auto_ok": None,
        "verdict": None,
        "comment": "",
        "error": None,
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
    }

    if dry_run:
        result["dry_run"] = True
        return result

    adapter = _adapter_for(layer, cfg)

    # setup (best-effort, e.g. stop before a motion test)
    for step in (case.get("setup") or []):
        try:
            adapter.run(step)
        except Exception:  # noqa: BLE001 - setup is best-effort
            pass

    # action (``noop`` = purely observational: the operator drives/observes by
    # hand, e.g. a cold boot or an operator-typed serial line; no adapter call)
    if action.get("noop"):
        evidence = {"reply": {"ok": True, "noop": True}}
    else:
        try:
            evidence = adapter.run(action)
        except Exception as exc:  # noqa: BLE001 - adapter should not raise, but be safe
            evidence = {"reply": {"ok": False, "error": f"adapter raised: {exc}"}}
    result["evidence"] = evidence

    # settle (give the tank time to move so the operator can observe)
    if settle_s > 0:
        time.sleep(settle_s)

    # auto-checks
    if auto:
        results = evaluate_assertions(evidence, auto)
        result["auto"] = [r.to_dict() for r in results]
        result["auto_ok"] = all(r.passed for r in results)
    else:
        result["auto_ok"] = None  # no machine-checkable evidence -> human only

    # teardown (best-effort, e.g. stop after a motion test)
    for step in (case.get("teardown") or []):
        try:
            adapter.run(step)
        except Exception:  # noqa: BLE001
            pass

    return result


# ===========================================================================
# Operator prompt
# ===========================================================================
def prompt_operator(case: Mapping[str, Any], result: Dict[str, Any]) -> Tuple[str, str]:
    """Prompt the operator for a verdict + comment. Returns (verdict, comment)."""
    print()
    print("=" * 78)
    print(f"TEST {result['id']}  [{result['safety'].upper()}]  {result['title']}")
    print(f"  layer : {result['layer']}")
    if result["expected"]:
        print(f"  EXPECT: {result['expected']}")
    if result["auto_ok"] is True:
        print("  AUTO  : all machine-checkable assertions PASSED")
    elif result["auto_ok"] is False:
        print("  AUTO  : one or more machine-checkable assertions FAILED:")
        for a in result["auto"]:
            if not a["passed"]:
                print(f"           - {a['path']} {a['op']} {a['expected']!r} "
                      f"(actual={a['actual']!r}) {a['detail']}")
    elif result["auto_ok"] is None:
        print("  AUTO  : (no machine-checkable assertions — rely on your observation)")
    if result["evidence"].get("reply", {}).get("error"):
        print(f"  NOTE  : {result['evidence']['reply']['error']}")
    print("=" * 78)

    while True:
        raw = input("  Verdict [P]ass / [F]ail / [S]kip (default P): ").strip().lower()
        if raw in ("", "p", "pass"):
            verdict = "pass"
        elif raw in ("f", "fail"):
            verdict = "fail"
        elif raw in ("s", "skip"):
            verdict = "skip"
        else:
            print("  (enter p / f / s)")
            continue
        break
    comment = input("  Comment (optional, Enter to skip): ").strip()
    return verdict, comment


# ===========================================================================
# Report
# ===========================================================================
def write_report(results: List[Dict[str, Any]], results_dir: str, meta: Dict[str, Any]) -> Tuple[str, str]:
    os.makedirs(results_dir, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    jsonl_path = os.path.join(results_dir, f"manual-{stamp}.jsonl")
    html_path = os.path.join(results_dir, f"manual-{stamp}.html")

    # JSONL (one record per test) + a header record.
    with open(jsonl_path, "w") as f:
        f.write(json.dumps({"type": "meta", **meta, "tests": len(results)}) + "\n")
        for r in results:
            f.write(json.dumps({"type": "test", **r}) + "\n")

    # Self-contained dark-theme HTML.
    with open(html_path, "w") as f:
        f.write(_render_html(results, meta))

    return jsonl_path, html_path


def _render_html(results: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    def esc(x):
        return html.escape(str(x))

    rows = []
    counts = {"pass": 0, "fail": 0, "skip": 0}
    for r in results:
        v = r.get("verdict") or ("skip" if r.get("dry_run") else "pending")
        if v in counts:
            counts[v] += 1
        auto_badge = ""
        if r.get("auto_ok") is True:
            auto_badge = '<span class="ok">auto ✓</span>'
        elif r.get("auto_ok") is False:
            auto_badge = '<span class="bad">auto ✗</span>'
        elif r.get("auto_ok") is None:
            auto_badge = '<span class="dim">auto —</span>'
        verdict_cls = {"pass": "ok", "fail": "bad", "skip": "dim"}.get(v, "dim")
        comment = f'<div class="comment">{esc(r.get("comment", ""))}</div>' if r.get("comment") else ""
        expected = f'<div class="expected">{esc(r.get("expected", ""))}</div>' if r.get("expected") else ""
        rows.append(
            f'<tr><td class="id">{esc(r["id"])}</td>'
            f'<td>{esc(r["layer"])}</td>'
            f'<td class="title">{esc(r["title"])}{expected}{comment}</td>'
            f'<td>{auto_badge}</td>'
            f'<td><span class="verdict {verdict_cls}">{esc(v.upper())}</span></td></tr>'
        )

    suites = sorted({r.get("layer", "?") for r in results})
    gen = esc(_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tankie manual integration test report</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:#0d1117; color:#e6edf3;
         font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif; }}
  .wrap {{ max-width:1080px; margin:0 auto; padding:28px 20px 60px; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  .sub {{ color:#8b949e; margin:0 0 20px; font-size:13px; }}
  .cards {{ display:flex; gap:12px; flex-wrap:wrap; margin:18px 0 26px; }}
  .card {{ background:#161b22; border:1px solid #30363d; border-radius:10px; padding:12px 18px; min-width:110px; }}
  .card .n {{ font-size:26px; font-weight:700; }}
  .card .l {{ font-size:12px; color:#8b949e; text-transform:uppercase; letter-spacing:.04em; }}
  .ok {{ color:#3fb950; }} .bad {{ color:#f85149; }} .dim {{ color:#8b949e; }}
  table {{ width:100%; border-collapse:collapse; background:#161b22; border:1px solid #30363d;
          border-radius:10px; overflow:hidden; }}
  th,td {{ text-align:left; padding:10px 12px; border-bottom:1px solid #21262d; vertical-align:top; }}
  th {{ background:#1c2128; font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:#8b949e; }}
  tr:last-child td {{ border-bottom:none; }}
  td.id {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; white-space:nowrap; }}
  td.title {{ min-width:280px; }}
  .expected {{ color:#8b949e; font-size:13px; margin-top:4px; }}
  .comment {{ color:#d29922; font-size:13px; margin-top:4px; font-style:italic; }}
  .verdict {{ font-weight:700; font-size:13px; }}
  span.ok, span.bad, span.dim {{ font-size:12px; }}
  .foot {{ color:#8b949e; font-size:12px; margin-top:22px; }}
</style></head>
<body><div class="wrap">
  <h1>Tankie — manual integration test report</h1>
  <p class="sub">Generated {gen} &middot; {esc(meta.get('suite','all'))} suite &middot;
     {esc(meta.get('operator','operator'))} &middot; {len(results)} tests</p>
  <div class="cards">
    <div class="card"><div class="n ok">{counts['pass']}</div><div class="l">pass</div></div>
    <div class="card"><div class="n bad">{counts['fail']}</div><div class="l">fail</div></div>
    <div class="card"><div class="n dim">{counts['skip']}</div><div class="l">skip</div></div>
    <div class="card"><div class="n">{len(results)}</div><div class="l">total</div></div>
  </div>
  <table>
    <thead><tr><th>ID</th><th>Layer</th><th>Test / expected</th><th>Auto</th><th>Verdict</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  <p class="foot">Layers: {esc(', '.join(suites))}. Evidence (replies, state, raw logs) is in the
     sibling <code>.jsonl</code> file. Generated by <code>tests/manual/run_manual.py</code> (issue #87).</p>
</div></body></html>
"""


# ===========================================================================
# main
# ===========================================================================
def _resolve_cfg(args) -> Dict[str, Any]:
    """Build the adapter config from CLI args + env (no IP literals in code)."""
    cfg: Dict[str, Any] = {}
    # bridge
    if args.bridge_sock:
        cfg["socket_path"] = args.bridge_sock
    elif os.environ.get("TANKIE_BRIDGE_SOCK"):
        cfg["socket_path"] = os.environ["TANKIE_BRIDGE_SOCK"]
    if args.state_file:
        cfg["state_file"] = args.state_file
    elif os.environ.get("TANKIE_STATE_FILE"):
        cfg["state_file"] = os.environ["TANKIE_STATE_FILE"]
    # raw serial
    if args.serial_port:
        cfg["serial_port"] = args.serial_port
    elif os.environ.get("TANKIE_SERIAL_PORT"):
        cfg["serial_port"] = os.environ["TANKIE_SERIAL_PORT"]
    # camera
    if args.camera_host:
        cfg["host"] = args.camera_host
    elif os.environ.get("TANKIE_PI_HOST"):
        cfg["host"] = os.environ["TANKIE_PI_HOST"]
    # localai
    if args.localai_url:
        cfg["base_url"] = args.localai_url
    elif os.environ.get("TANKIE_LOCALAI_BASE_URL"):
        cfg["base_url"] = os.environ["TANKIE_LOCALAI_BASE_URL"]
    if args.vlm_model:
        cfg["vlm_model"] = args.vlm_model
    elif os.environ.get("TANKIE_VLM_MODEL"):
        cfg["vlm_model"] = os.environ["TANKIE_VLM_MODEL"]
    return cfg


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Tankie manual integration test runner (issue #87)")
    ap.add_argument("--suite", default="all", help="suite name (a spec basename) or 'all' (default)")
    ap.add_argument("--filter", default=None, help="only run test ids containing this substring")
    ap.add_argument("--max-safety", default=DEFAULT_MAX_SAFETY, choices=sorted(SAFETY_ORDER),
                    help="cap the safety levels that run (default: high = all)")
    ap.add_argument("--i-confirm-tank-is-clear", action="store_true",
                    help="confirm the tank is on a clear, safe surface (required for motion tests)")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate specs + dependencies; do NOT touch the tank or network")
    ap.add_argument("--results-dir", default=RESULTS_DIR, help="where to write the report")
    ap.add_argument("--operator", default=os.environ.get("USER", "operator"),
                    help="operator name recorded in the report")
    # adapter config overrides (all optional)
    ap.add_argument("--bridge-sock", default=None, help="bridge Unix socket path")
    ap.add_argument("--state-file", default=None, help="bridge state store path")
    ap.add_argument("--serial-port", default=None, help="raw serial port (e.g. /dev/ttyS0)")
    ap.add_argument("--camera-host", default=None, help="Pi host for the camera probes")
    ap.add_argument("--localai-url", default=None, help="LocalAI base URL")
    ap.add_argument("--vlm-model", default=None, help="a vision model name for the LocalAI chat probe")
    args = ap.parse_args(argv)

    if yaml is None:
        print("error: PyYAML is required (pip3 install -r tests/manual/requirements.txt)", file=sys.stderr)
        return 2
    if get_adapter is None:
        print("error: the adapters package could not be imported", file=sys.stderr)
        return 2

    # -- load + validate ----------------------------------------------------
    pairs = load_specs(SPECS_DIR, args.suite)
    if not pairs:
        print(f"error: no test cases found for suite {args.suite!r} in {SPECS_DIR}", file=sys.stderr)
        return 2

    errors = validate_all(pairs)
    if errors:
        print("spec validation FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    # -- dry-run: validate deps, no hardware --------------------------------
    if args.dry_run:
        print(f"[dry-run] {len(pairs)} valid test case(s) in suite {args.suite!r}")
        layers = sorted({c["layer"] for _, c in pairs})
        print(f"[dry-run] layers used: {', '.join(layers)}")
        # Check each layer has a known adapter (no connection).
        known = {"bridge", "raw_serial", "camera", "localai", "ai_control", "brain"}
        unknown = [l for l in layers if l not in known]
        if unknown:
            print(f"[dry-run] WARNING: unknown layer(s): {', '.join(unknown)}", file=sys.stderr)
        # Local dependency checks only (no network in dry-run).
        try:
            import serial  # noqa: F401
            print("[dry-run] pyserial: available")
        except ImportError:
            print("[dry-run] pyserial: NOT installed (needed for the raw_serial layer)")
        print("[dry-run] OK — specs are valid and the runner is importable. "
              "No tank/network was touched.")
        return 0

    # -- safety gate --------------------------------------------------------
    max_level = SAFETY_ORDER[args.max_safety]
    any_motion = any(SAFETY_ORDER[c["safety"]] >= SAFETY_ORDER["med"] for _, c in pairs)
    if any_motion and not args.i_confirm_tank_is_clear:
        print("error: this suite contains motion tests (safety >= med).", file=sys.stderr)
        print("       Put the tank on a clear, flat surface, keep a hand near the power,", file=sys.stderr)
        print("       then re-run with --i-confirm-tank-is-clear.", file=sys.stderr)
        return 1

    cfg = _resolve_cfg(args)

    # -- preflight (warn, don't block) --------------------------------------
    print("preflight (warnings only — the operator confirms each test):")
    seen_layers = set()
    for _, c in pairs:
        if c["layer"] in seen_layers:
            continue
        seen_layers.add(c["layer"])
        try:
            adapter = _adapter_for(c["layer"], cfg)
            for name, ok, detail in adapter.preflight():
                mark = "ok " if ok else "WARN"
                print(f"  [{mark}] {c['layer']}: {name} — {detail}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [WARN] {c['layer']}: preflight error — {exc}")
    print()

    # -- run ----------------------------------------------------------------
    results: List[Dict[str, Any]] = []
    for suite, case in pairs:
        if args.filter and args.filter not in case["id"]:
            continue
        if SAFETY_ORDER[case["safety"]] > max_level:
            print(f"  (skipping {case['id']}: safety {case['safety']} > max {args.max_safety})")
            continue
        result = run_one(case, cfg, dry_run=False)
        verdict, comment = prompt_operator(case, result)
        result["verdict"] = verdict
        result["comment"] = comment
        results.append(result)
        tag = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}[verdict]
        print(f"  -> {case['id']}: {tag}" + (f"  ({comment})" if comment else ""))

    if not results:
        print("no tests matched the filter — nothing to run.")
        return 0

    # -- report -------------------------------------------------------------
    meta = {
        "suite": args.suite,
        "operator": args.operator,
        "max_safety": args.max_safety,
        "filter": args.filter,
    }
    jsonl_path, html_path = write_report(results, args.results_dir, meta)
    n_pass = sum(1 for r in results if r["verdict"] == "pass")
    n_fail = sum(1 for r in results if r["verdict"] == "fail")
    n_skip = sum(1 for r in results if r["verdict"] == "skip")
    print()
    print(f"done: {n_pass} pass, {n_fail} fail, {n_skip} skip (of {len(results)})")
    print(f"  JSONL : {jsonl_path}")
    print(f"  HTML  : {html_path}")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted — sending stop to the tank…")
        try:
            _stop_cfg = {"socket_path": os.environ.get("TANKIE_BRIDGE_SOCK", "/run/tankie/bridge.sock")}
            _adapter_for("bridge", _stop_cfg).run({"cmd": "stop"})
        except Exception:  # noqa: BLE001
            pass
        raise
