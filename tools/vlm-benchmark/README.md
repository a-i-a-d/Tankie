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
| `report.py`     | JSONL → text/Markdown/JSON report + recommendation   |
| `results/`      | committed benchmark run (JSONL records)              |
| `scenes/`       | committed scene PNGs (for inspection / debugging)    |

## Usage

```bash
# list models on the server
python3 tools/vlm-benchmark/vlm_bench.py --list

# run all 3 tasks against specific models
python3 tools/vlm-benchmark/vlm_bench.py --models mistralai_magistral-small-2509-multimodal,moondream2

# run only detection, auto-select vision models
python3 tools/vlm-benchmark/vlm_bench.py --models auto --tasks detect

# offline dry-run (no server call)
python3 tools/vlm-benchmark/vlm_bench.py --dry-run

# generate a report from results
python3 tools/vlm-benchmark/report.py results/bench-20261010-235859.jsonl
python3 tools/vlm-benchmark/report.py results/*.jsonl --md report.md
python3 tools/vlm-benchmark/report.py results/*.jsonl --json
```

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
- **Grading** is data-driven and re-runnable: `valid_json` → `required_keys`
  → `semantic` (task-specific expected values).
- **Recommendation** in `report.py`: highest overall pass rate, tie-broken by
  lowest median latency.
