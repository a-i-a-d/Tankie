← [Tankie](../../README.md) · [Docs index](../../docs/README.md)

# Manual / hardware-only tests

This directory is the home for **manual acceptance tests** that can only run on
dedicated hardware — the kind of checks that need the real tank (ESP8266 +
motor H-bridge + camera Pi) and/or a specific network state, so they are run by
a human on the box rather than in CI. They are **not** part of the automated
host suite (`tests/run_tests.sh`), which is what CI runs.

| Test | What it needs | What it does |
|------|---------------|--------------|
| `flash_ota_test.sh` | The tank Pi + the ESP8266 (D1 Mini) on its `tankie-esp` fallback AP, serial console on `/dev/ttyUSB0` | One-command OTA acceptance test: confirms the AP is visible, joins the `tankie-esp` AP, captures the ESP console, verifies the web/OTA endpoints before **and** after, runs `raspberry_pi/flash_ota.sh` (firmware + data partition), then **always** restores the infrastructure WiFi. Run with `bash tests/manual/flash_ota_test.sh`. |
| `run_manual.py` + `specs/` | The tank Pi, tank powered + on a clear surface, bridge daemon running | **Systematic low→high integration test harness** (issue #87): walks the test plan, shows the expected hardware behaviour, sends the action through the right layer, auto-checks machine-verifiable evidence, and asks PASS/FAIL/SKIP + a comment. See below. |

---

## The manual integration test harness (issue #87)

A **human-in-the-loop** harness that walks a low→high integration test plan on
the real tank — from raw serial firmware commands up to the AI instructing the
tank. For each test it prints the **expected behaviour**, sends the action
through the right **layer adapter**, **auto-checks** the machine-verifiable
evidence, and prompts the operator for **PASS / FAIL / SKIP + a comment**. It
writes a JSONL + self-contained dark-theme HTML report.

### Layout

```
tests/manual/
  run_manual.py        # the runner (stdlib + PyYAML; the stable entry point)
  brain_manual_harness.py  # offline Phase 2 brain harness (S6-07…S6-16, no hardware)
  requirements.txt     # operator deps (PyYAML + pyserial; per-layer extras noted)
  specs/               # declarative test specs — the STABLE CONTRACT
    power.yaml         #   Stage 0 — power & boot
    serial.yaml        #   Stage 1 — raw serial (firmware protocol)
    bridge.yaml        #   Stage 2 — bridge daemon (socket + state store)
    camera.yaml        #   Stage 4 — mediamtx streaming
    ai.yaml            #   Stage 5 — LocalAI + legacy ai_control.py
    brain.yaml         #   Stage 6 — AI brain: reflex (T4) + deliberation (T5) + orchestrator (T6)
    soak.yaml          #   Stage 7 — cross-cutting / soak / safety
  adapters/            # thin layer adapters (one per signal stage)
    base.py            #   shared core: evidence shape + assertion engine (pure)
    bridge.py  raw_serial.py  camera.py  localai.py  ai_control.py  brain.py
  results/             # generated reports (git-ignored)
```

### Quick start (on the tank Pi)

```bash
pip3 install -r tests/manual/requirements.txt          # PyYAML + pyserial
# per-layer extras (only for the layers you will use):
#   camera RTSP probe : apt install ffmpeg
#   brain (Stage 6)   : pip3 install -r raspberry_pi/brain/requirements.txt
#   ai_control (5)    : pip3 install -r ai-control/LocalAI/requirements.txt

python3 tests/manual/run_manual.py --dry-run           # validate, no hardware
python3 tests/manual/brain_manual_harness.py           # Phase 2 brain wiring, offline (no tank)
python3 tests/manual/run_manual.py --suite all \
    --i-confirm-tank-is-clear                          # the full low→high run
python3 tests/manual/run_manual.py --suite bridge --filter S2-03   # one test
```

The report lands in `tests/manual/results/manual-<timestamp>.{jsonl,html}`.

### The spec format (the stable contract)

Each test case is a small YAML mapping. This is the **contract a future web UI
will consume** — the runner is transport-agnostic and stateless per-test, so a
web front-end is a thin client over the same spec (the issue's "design for
that"):

```yaml
- id: S2-03
  layer: bridge                # which signal stage (selects the adapter)
  title: "auto_profile clamp (AI speed limit, issue #31)"
  safety: med                  # low | med | high (gates --max-safety)
  setup:    [{cmd: stop}]      # optional best-effort pre-steps
  action:   {cmd: drive, speed: 200, steer: 0, settle_s: 2}
  observe:
    expected: "A speed of 200 is clamped to the auto_profile max_speed (40)…"
    auto:                            # machine-checkable assertions (optional)
      - path: reply.clamped.speed
        eq: 40
  teardown: [{cmd: stop}]      # optional best-effort post-steps
```

Assertion operators: `eq`, `ne`, `gt`, `lt`, `gte`, `lte`, `contains`, and
`exists: true|false`. Paths are dotted (e.g. `state.last_state.speed`). A
missing path is a **failure** — a test that cannot find the evidence it asserts
on is not passing.

### Safety

* `--i-confirm-tank-is-clear` is **required** before any motion test (safety
  `med`/`high`) runs.
* `--max-safety low|med|high` caps the safety levels that will run.
* `Ctrl-C` sends `stop` to the tank immediately.
* Motion tests carry a `teardown: [{cmd: stop}]` so the tank always ends stopped.

### Design-for-web (issue #87 "a web based interface … for a later step")

The runner is transport-agnostic and stateless per-test, and **the YAML spec is
the stable contract**. A later web UI is a thin client over the same spec + a
tiny `run_manual.py --serve` HTTP form — no rework of the tests. Nothing here
requires a web stack to run today.

### No CI job (issue #87 Q7)

Per the issue's decision, this is a **hardware acceptance tool**, not a CI job.
The code is kept `py_compile`-clean and `--dry-run` validates the specs +
dependencies anywhere (safe on a build box) without touching the tank/network.
