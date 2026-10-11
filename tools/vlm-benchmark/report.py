#!/usr/bin/env python3
"""Turn a VLM benchmark results file (vlm_bench.py JSONL) into a report.

Reads the JSONL records written by ``vlm_bench.py`` and aggregates them into
per-model / per-task pass-rate + latency tables, a ranked summary, and a
single "recommended" model (best pass rate, tie-broken by lowest median
latency). Emits human-readable tables to stdout and/or a Markdown report.

Pure stdlib -- no third-party dependencies, runs on any machine.

Usage:
  python3 tools/vlm-benchmark/report.py results/bench-20261010-235859.jsonl
  python3 tools/vlm-benchmark/report.py results/bench-*.jsonl --md report.md
  python3 tools/vlm-benchmark/report.py results/bench-*.jsonl --json
  python3 tools/vlm-benchmark/report.py results/bench-*.jsonl --models a,b --tasks detect,drive
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys
from datetime import datetime, timezone


def _load(paths: list[str]) -> list[dict]:
    """Load records from one or more JSONL paths (glob-expanded). Bad lines skipped."""
    files: list[str] = []
    for p in paths:
        expanded = sorted(glob.glob(p)) if any(c in p for c in "*?[") else [p]
        files.extend(expanded)
    seen: set[str] = set()
    recs: list[dict] = []
    for f in files:
        if not os.path.isfile(f):
            print(f"warning: no such file: {f}", file=sys.stderr)
            continue
        if os.path.abspath(f) in seen:
            continue
        seen.add(os.path.abspath(f))
        with open(f, encoding="utf-8") as fh:
            for ln, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    print(f"warning: {f}:{ln}: skipping malformed line", file=sys.stderr)
    return recs


def _stats(records: list[dict]) -> dict:
    """Aggregate a group of records into pass-rate + latency stats."""
    n = len(records)
    passed = sum(1 for r in records if _semantic(r))
    valid_json = sum(1 for r in records if _valid_json(r))
    errors = sum(1 for r in records if r.get("error"))
    lats = [r["elapsed_s"] for r in records if isinstance(r.get("elapsed_s"), (int, float))]
    return {
        "n": n,
        "pass": passed,
        "pass_rate": round(passed / n, 3) if n else 0.0,
        "valid_json": valid_json,
        "errors": errors,
        "lat_min": min(lats) if lats else None,
        "lat_med": round(statistics.median(lats), 2) if lats else None,
        "lat_max": max(lats) if lats else None,
    }


def _valid_json(r: dict) -> bool:
    g = r.get("grade") or {}
    return bool(g.get("valid_json"))


def _semantic(r: dict) -> bool:
    g = r.get("grade") or {}
    return bool(g.get("semantic"))


def _key(model: str, task: str, variant: str) -> str:
    return f"{task}/{variant}" if variant else task


def aggregate(records: list[dict], models: set[str] | None, tasks: set[str] | None) -> dict:
    """Build {model: {overall: stats, per_task: {key: stats}}}."""
    by_model: dict[str, list[dict]] = {}
    for r in records:
        m = r.get("model")
        t = r.get("task")
        if not m or not t:
            continue
        if models and m not in models:
            continue
        if tasks and t not in tasks:
            continue
        by_model.setdefault(m, []).append(r)

    out: dict[str, dict] = {}
    for m, recs in by_model.items():
        per_task: dict[str, dict] = {}
        for r in recs:
            key = _key(m, r.get("task", ""), r.get("variant", ""))
            per_task.setdefault(key, []).append(r)
        out[m] = {
            "overall": _stats(recs),
            "per_task": {k: _stats(v) for k, v in sorted(per_task.items())},
        }
    return out


def recommend(summary: dict) -> tuple[str | None, bool]:
    """Pick the best model: highest overall pass rate, tie-break lowest median latency.
    Returns (model, passed_all_tasks)."""
    if not summary:
        return None, False
    tasks_present = set()
    for m in summary.values():
        tasks_present.update(k.split("/")[0] for k in m["per_task"])

    def all_pass(m: dict) -> bool:
        for t in tasks_present:
            for k, st in m["per_task"].items():
                if k.split("/")[0] == t and st["n"] and st["pass"] != st["n"]:
                    return False
        return True

    def sort_key(item):
        m, d = item
        ov = d["overall"]
        return (-ov["pass_rate"], ov["lat_med"] if ov["lat_med"] is not None else 1e9)

    best = min(summary.items(), key=sort_key)
    return best[0], all_pass(best[1])


def _fmt_rate(st: dict) -> str:
    return f"{st['pass']}/{st['n']} ({st['pass_rate']:.0%})"


def _fmt_lat(st: dict) -> str:
    return f"{st['lat_med']}s" if st["lat_med"] is not None else "-"


def render_text(summary: dict, recommended: str | None, rec_all: bool) -> str:
    lines: list[str] = []
    models = sorted(summary)
    # per-task table
    all_tasks = sorted({k for d in summary.values() for k in d["per_task"]})
    header = f"{'model':46s}" + "".join(f"{t:>22s}" for t in all_tasks)
    lines.append("Per-task pass rate (pass/n) and median latency:")
    lines.append(header)
    lines.append("-" * len(header))
    for m in models:
        row = f"{m:46s}"
        for t in all_tasks:
            st = summary[m]["per_task"].get(t)
            cell = f"{_fmt_rate(st)} {_fmt_lat(st)}" if st else "-"
            row += f"{cell:>22s}"
        lines.append(row)
    lines.append("")
    # overall ranking
    lines.append("Overall ranking (pass rate, then median latency):")
    ranked = sorted(models, key=lambda m: (-summary[m]["overall"]["pass_rate"],
                                           summary[m]["overall"]["lat_med"] or 1e9))
    for m in ranked:
        ov = summary[m]["overall"]
        mark = "  <-- recommended" if m == recommended else ""
        lines.append(f"  {m:46s} {_fmt_rate(ov):16s} med {_fmt_lat(ov):>8s}{mark}")
    lines.append("")
    if recommended:
        verdict = "passes ALL tasks" if rec_all else "best available (does not pass every task)"
        lines.append(f"RECOMMENDED: {recommended}  ({verdict})")
    else:
        lines.append("RECOMMENDED: (no records)")
    return "\n".join(lines)


def render_md(summary: dict, recommended: str | None, rec_all: bool, src: list[str]) -> str:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    models = sorted(summary)
    all_tasks = sorted({k for d in summary.values() for k in d["per_task"]})
    md: list[str] = []
    md.append("# VLM benchmark report")
    md.append("")
    md.append(f"_Generated {now} from: {', '.join(src)}_")
    md.append("")
    md.append("## Per-task results")
    md.append("")
    md.append("| model | " + " | ".join(all_tasks) + " |")
    md.append("|---|" + "---|" * len(all_tasks))
    for m in models:
        cells = []
        for t in all_tasks:
            st = summary[m]["per_task"].get(t)
            cells.append(f"`{_fmt_rate(st)}` · {_fmt_lat(st)}" if st else "-")
        md.append(f"| {m} | " + " | ".join(cells) + " |")
    md.append("")
    md.append("## Overall ranking")
    md.append("")
    md.append("| rank | model | pass rate | median latency |")
    md.append("|---|---|---|---|")
    ranked = sorted(models, key=lambda m: (-summary[m]["overall"]["pass_rate"],
                                           summary[m]["overall"]["lat_med"] or 1e9))
    for i, m in enumerate(ranked, 1):
        ov = summary[m]["overall"]
        md.append(f"| {i} | {m} | {_fmt_rate(ov)} | {_fmt_lat(ov)} |")
    md.append("")
    if recommended:
        verdict = "passes all tasks" if rec_all else "best available"
        md.append(f"**Recommended:** `{recommended}` ({verdict})")
    md.append("")
    return "\n".join(md)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", nargs="+", help="results JSONL file(s), glob patterns allowed")
    ap.add_argument("--md", default=None, help="also write a Markdown report to this path")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON summary instead of tables")
    ap.add_argument("--models", default=None, help="comma-separated model ids to include")
    ap.add_argument("--tasks", default=None, help="comma list of tasks to include (detect,sequence,drive)")
    args = ap.parse_args(argv)

    models = {m.strip() for m in args.models.split(",")} if args.models else None
    tasks = {t.strip() for t in args.tasks.split(",")} if args.tasks else None

    records = _load(args.results)
    if not records:
        print("error: no records loaded", file=sys.stderr)
        return 1

    summary = aggregate(records, models, tasks)
    recommended, rec_all = recommend(summary)

    if args.json:
        print(json.dumps({
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "records": len(records),
            "models": len(summary),
            "recommended": recommended,
            "recommended_passes_all": rec_all,
            "summary": summary,
        }, indent=2))
    else:
        print(render_text(summary, recommended, rec_all))

    if args.md:
        with open(args.md, "w", encoding="utf-8") as fh:
            fh.write(render_md(summary, recommended, rec_all, args.results))
        print(f"\nmarkdown report -> {args.md}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
