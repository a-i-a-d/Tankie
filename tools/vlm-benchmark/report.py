#!/usr/bin/env python3
"""Turn a VLM benchmark results file (vlm_bench.py JSONL) into a report.

Reads the JSONL records written by ``vlm_bench.py`` and aggregates them into
per-model / per-task pass-rate + latency tables, a ranked summary, and a
single "recommended" model (best pass rate, then fewest errors, then lowest
WARM median latency of successful test-phase calls -- issue #89). The
one-time cold model-load time (warmup call) is reported separately in the
"Load (cold)" column. Emits human-readable tables to stdout and/or a
Markdown report.

Pure stdlib -- no third-party dependencies, runs on any machine.

Usage:
  python3 tools/vlm-benchmark/report.py results/old/bench-20261010-235859.jsonl
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


def _is_warmup(r: dict) -> bool:
    """True for the one-shot model-load (cold) call.

    Records written by vlm_bench.py carry an explicit ``phase``
    (``"warmup"`` / ``"test"``). Older results files (pre issue #89) have no
    ``phase`` field at all; those are all treated as graded/test calls so the
    committed historical baselines keep rendering exactly as before.
    """
    return r.get("phase") == "warmup"


def _stats(records: list[dict]) -> dict:
    """Aggregate a group of records into pass-rate + latency stats.

    issue #89: the graded statistics (n / pass / pass_rate / errors /
    ``lat_med``) are computed over the **test-phase** calls only -- the
    warmup (cold load) call is excluded so ``lat_med`` is the warm,
    steady-state median. The warmup's own latency is reported separately as
    ``lat_cold_med`` (informational; the one-time cost of loading the model
    into GPU memory). ``lat_err_med`` keeps the error-call latencies (L1).
    """
    tests = [r for r in records if not _is_warmup(r)]
    warmups = [r for r in records if _is_warmup(r)]
    n = len(tests)
    passed = sum(1 for r in tests if _semantic(r))
    valid_json = sum(1 for r in tests if _valid_json(r))
    errors = sum(1 for r in tests if r.get("error"))
    warmup_errors = sum(1 for r in warmups if r.get("error"))
    lats = [r["elapsed_s"] for r in tests if isinstance(r.get("elapsed_s"), (int, float))]
    # L1: latency stats are computed over SUCCESSFUL calls only (no error).
    # A fast HTTP 500 must not lower the median -- that would let the
    # ranking tie-break favor a model that errors quickly over one that
    # works slowly. Error-call latencies are kept separately (lat_err_med).
    ok_lats = [r["elapsed_s"] for r in tests
               if not r.get("error") and isinstance(r.get("elapsed_s"), (int, float))]
    err_lats = [r["elapsed_s"] for r in tests
                if r.get("error") and isinstance(r.get("elapsed_s"), (int, float))]
    cold_lats = [r["elapsed_s"] for r in warmups
                 if not r.get("error") and isinstance(r.get("elapsed_s"), (int, float))]
    return {
        "n": n,
        "pass": passed,
        "pass_rate": round(passed / n, 3) if n else 0.0,
        "valid_json": valid_json,
        "errors": errors,
        "warmup_errors": warmup_errors,
        "lat_min": min(lats) if lats else None,
        "lat_med": round(statistics.median(ok_lats), 2) if ok_lats else None,
        "lat_max": max(lats) if lats else None,
        "lat_err_med": round(statistics.median(err_lats), 2) if err_lats else None,
        "lat_cold_med": round(statistics.median(cold_lats), 2) if cold_lats else None,
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
        # issue #89: warmup (cold-load) records are excluded from the graded
        # per-task stats; the model's cold load time is surfaced separately.
        tests = [r for r in recs if not _is_warmup(r)]
        warmups = [r for r in recs if _is_warmup(r)]
        per_task: dict[str, dict] = {}
        for r in tests:
            key = _key(m, r.get("task", ""), r.get("variant", ""))
            per_task.setdefault(key, []).append(r)
        cold_lats = [r["elapsed_s"] for r in warmups
                     if not r.get("error") and isinstance(r.get("elapsed_s"), (int, float))]
        out[m] = {
            "overall": _stats(tests),
            "per_task": {k: _stats(v) for k, v in sorted(per_task.items())},
            "load_cold_s": round(statistics.median(cold_lats), 2) if cold_lats else None,
            "warmup_errors": sum(1 for r in warmups if r.get("error")),
        }
    return out


def _rank_key(ov: dict):
    """L2: ranking order = pass rate (desc) -> error count (asc) -> median
    latency (asc). A model that errors more is ranked below one that doesn't,
    even if it errors faster; latency is only the last tie-break. Models with
    no successful call (lat_med None) sort last within their tier.

    issue #89 (Q3): the latency tie-break is the **warm** median (test-phase
    calls only) -- the steady-state number the brain actually experiences.
    The one-time cold load cost is informational only (in tank use the model
    stays resident in GPU memory)."""
    return (-ov["pass_rate"], ov["errors"], ov["lat_med"] if ov["lat_med"] is not None else 1e9)


def recommend(summary: dict) -> tuple[str | None, bool]:
    """Pick the best model: highest overall pass rate, then fewest errors,
    then lowest **warm** median (successful test-phase call) latency.
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

    best = min(summary.items(), key=lambda item: _rank_key(item[1]["overall"]))
    return best[0], all_pass(best[1])


def _fmt_rate(st: dict) -> str:
    return f"{st['pass']}/{st['n']} ({st['pass_rate']:.0%})"


def _fmt_lat(st: dict) -> str:
    return f"{st['lat_med']}s" if st["lat_med"] is not None else "-"


def render_text(summary: dict, recommended: str | None, rec_all: bool) -> str:
    lines: list[str] = []
    models = sorted(summary)
    # per-task table (issue #89: "Load (cold)" column BEFORE the task columns)
    all_tasks = sorted({k for d in summary.values() for k in d["per_task"]})
    header = f"{'model':46s}{'Load (cold)':>14s}" + "".join(f"{t:>22s}" for t in all_tasks)
    lines.append("Per-task pass rate (pass/n) and WARM median latency (cold load shown separately):")
    lines.append(header)
    lines.append("-" * len(header))
    for m in models:
        row = f"{m:46s}"
        cold = summary[m].get("load_cold_s")
        row += f"{(str(cold) + 's') if cold is not None else '-':>14s}"
        for t in all_tasks:
            st = summary[m]["per_task"].get(t)
            cell = f"{_fmt_rate(st)} {_fmt_lat(st)}" if st else "-"
            row += f"{cell:>22s}"
        lines.append(row)
    lines.append("")
    # overall ranking
    lines.append("Overall ranking (pass rate, then warm median latency):")
    ranked = sorted(models, key=lambda m: _rank_key(summary[m]["overall"]))
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
    md.append("| model | Load (cold) | " + " | ".join(all_tasks) + " |")
    md.append("|---|---|" + "---|" * len(all_tasks))
    for m in models:
        cold = summary[m].get("load_cold_s")
        cold_cell = f"{cold}s" if cold is not None else "-"
        cells = []
        for t in all_tasks:
            st = summary[m]["per_task"].get(t)
            cells.append(f"`{_fmt_rate(st)}` · {_fmt_lat(st)}" if st else "-")
        md.append(f"| {m} | {cold_cell} | " + " | ".join(cells) + " |")
    md.append("")
    md.append("## Overall ranking")
    md.append("")
    md.append("| rank | model | pass rate | warm median latency |")
    md.append("|---|---|---|---|")
    ranked = sorted(models, key=lambda m: _rank_key(summary[m]["overall"]))
    for i, m in enumerate(ranked, 1):
        ov = summary[m]["overall"]
        md.append(f"| {i} | {m} | {_fmt_rate(ov)} | {_fmt_lat(ov)} |")
    md.append("")
    if recommended:
        verdict = "passes all tasks" if rec_all else "best available"
        md.append(f"**Recommended:** `{recommended}` ({verdict})")
    md.append("")
    return "\n".join(md)


_HTML_CSS = """
:root{--bg:#0f1117;--card:#171a23;--fg:#e6e8ee;--muted:#8b90a0;--line:#262b38;
--pass:#2ea44f;--fail:#e5484d;--partial:#d29922;--err:#6e7681;--accent:#4493f8;}
*{box-sizing:border-box;}
body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--fg);padding:32px;max-width:1200px;margin:0 auto;}
h1{font-size:22px;margin:0 0 4px;}
h2{font-size:16px;margin:32px 0 12px;color:var(--fg);}
.sub{color:var(--muted);font-size:13px;margin-bottom:24px;}
.cards{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:20px;}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 18px;min-width:110px;}
.card .k{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em;}
.card .v{font-size:22px;font-weight:600;margin-top:4px;}
.card.rec .v{color:var(--accent);font-size:14px;word-break:break-word;}
table{border-collapse:collapse;width:100%;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;}
th,td{padding:10px 12px;text-align:center;border-bottom:1px solid var(--line);}
th{background:#12151d;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em;}
td.model,th.model{text-align:left;font-weight:500;}
tbody tr:last-child td{border-bottom:none;}
.cell{display:flex;flex-direction:column;align-items:center;gap:2px;}
.mark{font-size:17px;line-height:1;}
.lat{font-size:11px;color:var(--muted);}
td.pass{background:rgba(46,164,79,.10);}
td.fail{background:rgba(229,72,77,.10);}
td.partial{background:rgba(210,153,34,.12);}
td.error{background:rgba(110,118,129,.14);}
td.empty{background:rgba(110,118,129,.06);}
.badge{display:inline-block;padding:2px 8px;border-radius:20px;font-size:10px;font-weight:700;margin-left:6px;vertical-align:middle;}
.badge.rec{background:rgba(68,147,248,.20);color:var(--accent);}
tr.rec-row td{box-shadow:inset 0 0 0 1px var(--accent);}
td .rec-dot{color:var(--accent);font-size:11px;}
td.coldload{background:rgba(94,158,255,.06);}
td.coldload .lat{color:#7fb0ff;font-size:12px;}
details{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:0 16px;margin-bottom:10px;}
summary{cursor:pointer;padding:12px 0;font-weight:600;list-style:none;}
summary::-webkit-details-marker{display:none;}
summary::before{content:"\\25B6";display:inline-block;margin-right:8px;font-size:10px;color:var(--muted);transition:transform .15s;}
details[open] summary::before{transform:rotate(90deg);}
pre{background:#0c0e14;border:1px solid var(--line);border-radius:8px;padding:12px;overflow:auto;font-size:12px;white-space:pre-wrap;word-break:break-word;margin:10px 0 14px;}
.prompt{background:#12151d;border-left:3px solid var(--accent);border-radius:6px;padding:8px 12px;font-size:12px;white-space:pre-wrap;word-break:break-word;margin:10px 0 4px;color:#c8cdd9;}
.prompt .lbl{display:block;font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin-bottom:4px;}
.err{color:#ff8b8e;font-weight:600;}
.ok{color:var(--pass);font-weight:600;}
.kv{color:var(--muted);font-size:12px;}
.temp{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;padding:1px 6px;border-radius:10px;margin-left:6px;vertical-align:middle;}
.temp.cold{background:rgba(94,158,255,.18);color:#7fb0ff;}
.temp.warm{background:rgba(255,171,64,.18);color:#ffb066;}
.legend{display:flex;gap:16px;flex-wrap:wrap;margin:8px 0 20px;font-size:12px;color:var(--muted);}
.legend span{display:inline-flex;align-items:center;gap:6px;}
.dot{width:11px;height:11px;border-radius:3px;display:inline-block;}
footer{margin-top:28px;color:var(--muted);font-size:12px;}
"""


def render_html(summary: dict, recommended: str | None, rec_all: bool,
                src: list[str], records: list[dict]) -> str:
    """Render a self-contained, dark-themed HTML report (inline CSS, no assets).

    Includes summary cards, a color-coded per-task matrix (hover a cell for the
    model's raw answer / error), an overall ranking, and per-model raw answers.
    """
    import html as _html

    def esc(v):
        return _html.escape(str(v)) if v is not None else ""

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    models = sorted(summary)
    all_tasks = sorted({k for d in summary.values() for k in d["per_task"]})
    total_calls = sum(d["overall"]["n"] for d in summary.values())
    total_pass = sum(d["overall"]["pass"] for d in summary.values())
    total_err = sum(d["overall"]["errors"] for d in summary.values())
    pass_rate = (total_pass / total_calls) if total_calls else 0.0

    # raw records grouped by (model, task) for tooltips + detail
    by_model_task: dict[tuple, list] = {}
    for r in records:
        m, t = r.get("model"), r.get("task")
        if m and t:
            by_model_task.setdefault((m, t), []).append(r)

    def cell_status(st):
        if not st or st.get("n", 0) == 0:
            return "empty", ""
        if st["pass"] == st["n"]:
            return "pass", "&#10003;"
        if st["pass"] > 0:
            return "partial", "&#9888;"
        if st.get("errors", 0) >= st["n"]:
            return "error", "&#10006;"
        return "fail", "&#10007;"

    def cell_tip(model, task, st):
        recs = by_model_task.get((model, task), [])
        for r in recs:
            if r.get("error"):
                return esc(r["error"])[:400]
        for r in recs:
            if r.get("content"):
                return esc(r["content"])[:400]
        return ""

    # ---- matrix rows (issue #89: "Load (cold)" column before the task columns)
    matrix_rows = []
    for m in models:
        badge = '<span class="badge rec">recommended</span>' if m == recommended else ""
        tds = [f'<td class="model">{esc(m)}{badge}</td>']
        cold = summary[m].get("load_cold_s")
        cold_txt = f"{cold}s" if cold is not None else "-"
        tds.append(
            '<td class="coldload" title="one-time model load (warmup call, issue #89)">'
            f'<div class="cell"><span class="lat">{esc(cold_txt)}</span></div></td>'
        )
        for t in all_tasks:
            st = summary[m]["per_task"].get(t)
            status, mark = cell_status(st)
            tip = cell_tip(m, t, st) if status in ("fail", "error", "partial") else ""
            lat = f'{st["lat_med"]}s' if st and st.get("lat_med") is not None else "-"
            title = f' title="{tip}"' if tip else ""
            tds.append(
                f'<td class="{status}"{title}>'
                f'<div class="cell"><span class="mark">{mark}</span>'
                f'<span class="lat">{esc(lat)}</span></div></td>'
            )
        matrix_rows.append("<tr>" + "".join(tds) + "</tr>")

    # ---- ranking rows
    ranked = sorted(models, key=lambda m: _rank_key(summary[m]["overall"]))
    rank_rows = []
    for i, m in enumerate(ranked, 1):
        ov = summary[m]["overall"]
        row_cls = ' class="rec-row"' if m == recommended else ""
        rec_dot = ' <span class="rec-dot">&#9733;</span>' if m == recommended else ""
        pr = f'{ov["pass"]}/{ov["n"]} ({ov["pass_rate"]:.0%})'
        lat = f'{ov["lat_med"]}s' if ov.get("lat_med") is not None else "-"
        rank_rows.append(
            f'<tr{row_cls}><td>{i}</td><td class="model">{esc(m)}{rec_dot}</td>'
            f'<td>{pr}</td><td>{esc(lat)}</td><td>{ov["errors"]}</td></tr>'
        )

    # ---- per-model detail (raw answers)
    detail = []
    for m in models:
        model_recs = [r for r in records if r.get("model") == m]
        model_recs.sort(key=lambda r: (r.get("task", ""), r.get("variant", "")))
        items = []
        for r in model_recs:
            t, v = r.get("task"), r.get("variant") or "-"
            g = r.get("grade") or {}
            mark = '<span class="ok">PASS</span>' if g.get("semantic") else '<span class="err">FAIL</span>'
            body = r.get("content") or r.get("reasoning") or (r.get("error") or "(no response)")
            # prompt: full text from the record; older records fall back to the hash
            prompt = r.get("prompt") or f"(prompt not stored - sha256[:12]={r.get('prompt_sha') or '?'})"
            # issue #89: the cold/warm badge is driven by the record's ``phase``
            # (warmup = the one-shot cold model-load call; test = warm, steady
            # state). Older results files have no ``phase`` field; for those we
            # fall back to the run number (run 1 = cold, run 2+ = warm) so the
            # committed historical baselines still label sensibly.
            phase = r.get("phase")
            if phase == "warmup":
                temp = "cold"
            elif phase == "test":
                temp = "warm"
            else:
                run_no = r.get("run")
                temp = "warm" if isinstance(run_no, int) and run_no >= 2 else "cold"
            temp_badge = f'<span class="temp {temp}">{temp}</span>'
            items.append(
                f'<div><b>{esc(t)} / {esc(v)}</b> &mdash; {mark} {temp_badge} '
                f'<span class="kv">http {esc(r.get("http"))} &middot; {esc(r.get("elapsed_s"))}s</span></div>'
                f'<div class="prompt"><span class="lbl">prompt</span>{esc(prompt)}</div>'
                f'<pre>{esc(body)}</pre>'
            )
        detail.append(f'<details><summary>{esc(m)}</summary>' + "".join(items) + "</details>")

    cards = f'''
    <div class="cards">
      <div class="card"><div class="k">Models</div><div class="v">{len(models)}</div></div>
      <div class="card"><div class="k">Calls</div><div class="v">{total_calls}</div></div>
      <div class="card"><div class="k">Pass rate</div><div class="v">{pass_rate:.0%}</div></div>
      <div class="card"><div class="k">Errors</div><div class="v">{total_err}</div></div>
      <div class="card rec"><div class="k">Recommended</div><div class="v">{esc(recommended or "&mdash;")}</div></div>
    </div>'''

    legend = '''
    <div class="legend">
      <span><span class="dot" style="background:var(--pass)"></span> pass</span>
      <span><span class="dot" style="background:var(--partial)"></span> partial</span>
      <span><span class="dot" style="background:var(--fail)"></span> fail (semantic)</span>
      <span><span class="dot" style="background:var(--err)"></span> error (no valid response)</span>
      <span>&middot; hover any cell for the model&rsquo;s raw answer / error</span>
    </div>'''

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VLM Benchmark Report</title>
<style>{_HTML_CSS}</style></head>
<body>
<h1>VLM Benchmark Report</h1>
<div class="sub">Generated {esc(now)} &middot; {esc(", ".join(src))} &middot; {total_calls} calls across {len(models)} models</div>
{cards}
{legend}
<h2>Per-task results</h2>
<table>
<thead><tr><th class="model">model</th><th title="one-time model load (warmup call, issue #89)">Load (cold)</th>{"".join(f"<th>{esc(t)}</th>" for t in all_tasks)}</tr></thead>
<tbody>{"".join(matrix_rows)}</tbody>
</table>
<h2>Overall ranking</h2>
<table>
<thead><tr><th>#</th><th class="model">model</th><th>pass rate</th><th>warm median</th><th>errors</th></tr></thead>
<tbody>{"".join(rank_rows)}</tbody>
</table>
<h2>Per-model detail (raw answers)</h2>
{"".join(detail)}
<footer>Tankie VLM benchmark &middot; generated by <code>report.py --html</code></footer>
</body></html>
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", nargs="+", help="results JSONL file(s), glob patterns allowed")
    ap.add_argument("--md", default=None, help="also write a Markdown report to this path")
    ap.add_argument("--html", default=None, help="also write a self-contained HTML report to this path (no external assets)")
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
    if args.html:
        with open(args.html, "w", encoding="utf-8") as fh:
            fh.write(render_html(summary, recommended, rec_all, args.results, records))
        print(f"\nhtml report -> {args.html}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
