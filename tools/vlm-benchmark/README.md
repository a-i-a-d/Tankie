# VLM Benchmark — Tankie brain model comparison

Repeatable benchmark for comparing vision-language models on the LocalAI
server across the three tasks the deliberation/reflex loops actually need:

| task     | input          | expected output                          |
|----------|----------------|------------------------------------------|
| `detect` | 1 frame        | `{"objects":[{label,confidence,bbox}]}`  |
| `sequence` | N frames    | `{"moving":str,"direction":str}`         |
| `drive`  | 1 frame        | `{"action","throttle","steer","reason","say"}` |

Scenes are **generated on the fly** (pure-stdlib PNG encoder in `scene_gen.py`)
— no binary assets in the repo, no Pillow dependency.

## Files

| file            | purpose                                              |
|-----------------|------------------------------------------------------|
| `vlm_bench.py`  | the benchmark runner (stdlib-only, `urllib`)         |
| `scene_gen.py`  | synthetic scene generator (pure-stdlib PNG encoder)  |
| `report.py`     | JSONL → text/Markdown/HTML/JSON report + recommendation |
| `benchmark-ui.html` | dynamic browser frontend: load runs, compare, custom tests (issue #90) |
| `results/`      | new benchmark runs (JSONL records; git-ignored)      |
| `results/baseline-*.jsonl` | tracked baselines (committed)                  |
| `results/old/`  | superseded / historical runs (kept for reference)    |
| `scenes/`       | committed scene PNGs (for inspection / debugging)    |

## Usage

```bash
# list models on the server
python3 tools/vlm-benchmark/vlm_bench.py --list

# run all 3 tasks against specific models
python3 tools/vlm-benchmark/vlm_bench.py --models mistralai_magistral-small-2509-multimodal,moondream2

# run only detection, auto-select vision models (name-hint heuristic)
python3 tools/vlm-benchmark/vlm_bench.py --models auto --tasks detect

# auto-select + LIVE-verify: probe each candidate with one tiny image and
# keep only models that actually accept images (reliable selection; needs the server)
python3 tools/vlm-benchmark/vlm_bench.py --models auto --probe --tasks detect

# offline dry-run (no server call)
python3 tools/vlm-benchmark/vlm_bench.py --dry-run

# skip the one-shot warmup call per model (old behaviour)
python3 tools/vlm-benchmark/vlm_bench.py --models a,b --no-warmup

# run tasks from a JSONL task file (the #125 web-UI export) instead of the
# built-in tasks -- each line's prompt / images / expected are used (issue #126)
python3 tools/vlm-benchmark/vlm_bench.py --models a,b --tasks-file tasks-my-test.jsonl

# offline dry-run of a task file (no server, no --models needed)
python3 tools/vlm-benchmark/vlm_bench.py --dry-run --tasks-file tasks-my-test.jsonl

# generate a report from results (text to stdout, or write to file)
python3 tools/vlm-benchmark/report.py results/old/bench-20261010-235859.jsonl
python3 tools/vlm-benchmark/report.py results/*.jsonl --md report.md
python3 tools/vlm-benchmark/report.py results/*.jsonl --html report.html
python3 tools/vlm-benchmark/report.py results/*.jsonl --json
```# generate a report from results (text to stdout, or write to file)
python3 tools/vlm-benchmark/report.py results/old/bench-20261010-235859.jsonl
python3 tools/vlm-benchmark/report.py results/*.jsonl --md report.md
python3 tools/vlm-benchmark/report.py results/*.jsonl --html report.html
python3 tools/vlm-benchmark/report.py results/*.jsonl --json
```

## Dynamic UI (issue #90)

`benchmark-ui.html` is a **self-contained** browser frontend (inline CSS + JS,
no CDN / build step / external assets) for exploring benchmark results
interactively. Open it directly from the filesystem (`file://`) in any modern
browser — no server required.

- **Runs** — load one or more JSONL result files (each becomes a named run);
  per-run cards show models / calls / pass rate and the recommended model.
- **Results matrix** — a sortable model × task matrix (pass/n + warm median
  latency, color-coded), with a click-through detail drawer showing the full
  prompt, response, grade, HTTP status, latency and cold/warm label.
- **Compare two runs** — pick run A (baseline) and run B (candidate) to see
  per-model deltas (pass rate, warm median, errors) with an improved/regressed
  summary and an **only-show-changed** filter (e.g. to diff two runs that used
  slightly different prompts).
- **Custom tests** — edit the prompt, attach image(s), set the model / task /
  variant / max-tokens, and run live against the LocalAI server (CORS must be
  enabled on the server). Results are graded in the browser (same rules as
  `vlm_bench._grade`) and can be **exported as JSONL** in the exact
  `vlm_bench.py` schema (so `report.py` can report on them too) or imported
  back. Saved tests persist in the browser's `localStorage`.

### Task editor + task file (issue #125)

The custom-test form is a **task editor**: it adds a **Name** field and an
**Expected answer** (JSON) field, and it **persists the image(s)** — so a
"task" is a self-contained `name` + `model` + `task`/`variant` + `prompt` +
`images` + `expected` bundle.

- **Goal 1 — see the image.** Result records now store the image bytes
  (`images: [dataURL, …]`) and the detail drawer renders them. For the
  built-in `detect`/`sequence`/`drive` tasks the 8 deterministic scene PNGs
  are embedded in the HTML, so *existing* runner baselines (which store no
  image bytes) also show their scene in the drawer.
- **Goal 2 — task editor.** Name + expected + images are all captured; the
  saved-task list shows a thumbnail, the expected answer, and **load /
  export / delete** per task.
- **Goal 3 — save tasks.** Tasks live in two places:
  - **localStorage** (scratch, this browser) — capped at **10 tasks** (the
    ~5 MB origin budget, since each task can carry a ~200 KB image).
  - **task file** (the shareable artifact) — export all (or one) task(s) as
    **JSONL** (`tasks-<name>.jsonl`), and import a task file back. This is
    pure text (no binary assets) and the file the runner will `--tasks-file`
    load in **#126**.
- **Image cap.** Uploads are capped at **200 KB per image** (~2× a worst-case
  640×480 camera JPEG); larger files are rejected with a clear message.
- **Expected answer (Q4=b).** Grading uses the task's `expected` when set
  (falls back to the built-in `EXPECTED`), so a human can tune it to real
  model behaviour.

> Follow-ups: committing web-UI-created tests to the repo (+ showing the
> `git add/commit` command in the UI) → **#127**.

### Runner task files (issue #126)

The runner now **consumes** those task files:

```bash
python3 tools/vlm-benchmark/vlm_bench.py --models a,b --tasks-file tasks-my-test.jsonl
```

- `--tasks-file <path>` (JSONL) takes precedence over `--tasks` /
  `--drive-variants`. Each line is a self-contained task (`name`, `task`/
  `variant`, `prompt`, `images`, `max_tokens`, `json_mode`, `expected`).
- The task's **`expected` is authoritative** (issue #125 Q4=b); when a task
  omits it, the built-in default for that task kind is used — so the original
  `detect`/`sequence`/`drive` behaviour is unchanged.
- Records store the task's `name` + `expected` (and the images), so the web UI
  can render + re-grade them. `--dry-run --tasks-file` works **offline** (no
  `--models`, no server). Malformed lines are skipped with a warning; a missing
  or empty file fails fast (exit 2).

> Follow-up: committing web-UI-created tests to the repo (+ showing the
> `git add/commit` command in the UI) → **#127**.

The UI's pure logic (parse / stats / aggregate / rank / compare / grade /
chat-request shape) is exercised offline in CI by harness section 18, which
runs the real inline `<script>` in a Node VM sandbox — no browser, no network.


## Environment

| variable                  | default                         |
|---------------------------|---------------------------------|
| `TANKIE_LOCALAI_BASE_URL` | `http://localai.local:8080/v1`  |
| `TANKIE_LOCALAI_API_KEY`  | `sk-0123456789`                 |

## Design notes

- **Pure stdlib** — no third-party dependencies. Runs on any machine that can
  reach the LocalAI server.
- **Scenes are committed** to `scenes/` so the exact input images can be inspected
  (the detect scene contains a red circle, green square, and blue square — **no
  triangle**; the `DETECT_EXPECTED` tuple was corrected to match).
- **Never-raises** — every call is recorded (request metadata, HTTP status,
  timing, full response, parsed JSON, grade) to the JSONL results file.
  Network errors, timeouts, and malformed responses are captured, not fatal.
- **Audit trail** — each record stores the full prompt (plus its `sha256[:12]`),
  and the HTML report shows the prompt above every model response. Older records
  without a `prompt` field fall back to showing the hash.
- **Warmup pass (cold load)** — before the graded calls of each model, one
  cheap `detect`-scene call (short prompt, `max_tokens` 64) loads the model
  (weights + mmproj projector) into GPU memory, so **every measured call runs
  warm**. The warmup is recorded with `phase: "warmup"` (test calls:
  `phase: "test"`) and is **excluded** from all pass-rate / latency stats; its
  elapsed time is the one-time cold/load cost, shown in the **"Load (cold)"**
  column (before the task columns) of the text/Markdown/HTML reports.
  `--no-warmup` restores the old behaviour.
- **Cold/warm labels** — in the per-model detail section every record is
  labeled `cold` (warmup call) or `warm` (test call) based on its `phase`
  field. Older results files without `phase` fall back to the `run` number
  (run 1 = cold, run 2+ = warm).
- **Grading** is data-driven and re-runnable: `valid_json` → `required_keys`
  → `semantic` (task-specific expected values).
- **Recommendation** in `report.py`: highest overall pass rate, tie-broken by
  lowest **warm** median latency (steady-state; the one-time cold load is
  informational only).
- **Dynamic UI** (`benchmark-ui.html`, issue #90): the interactive frontend
  reuses the *same* aggregation / ranking / grading semantics as `report.py`
  and `vlm_bench.py` (the JS core mirrors them 1:1 and is cross-checked in
  harness section 18), so the dynamic view and the static report always agree.
  It is fully self-contained and works from `file://`; custom-test results are
  emitted in the exact `vlm_bench.py` JSONL schema for round-tripping.
