# Brain tests (issue #72, Phase 1 T1)

Offline unit tests for the Tankie AI brain scaffolding. No tank, no Pi, no
LocalAI, no network — pure data (Pydantic schemas) + config loader.

## Run

From the repo root:

```sh
# one-off (venv with the Phase 1 deps):
python3 -m venv .venv && . .venv/bin/activate
pip install -r raspberry_pi/brain/requirements.txt
pytest tests/brain/ -q
```

`conftest.py` adds `raspberry_pi/` to `sys.path` so `import brain` resolves
regardless of where pytest is invoked from.

## What is covered

- `Action` — round-trip JSON, defaults, rejection of bad `intent`/`cmd` and
  out-of-range `speed`/`steer`/`pan`/`tilt`, rejection of extra keys, and
  `clamp()` bounding to the AUTO_PROFILE limits.
- `Detection` / `VLMResult` / `ReflexResult` / `BridgeResult` / `TankState`
  — shape + validation.
- `DataLogRecord` — a full cycle serialises to one valid JSON line (the
  data-log artifact for Phase 6), minimal record, extra-key rejection.
- `config.load_config()` — the single-config-source contract:
  env var > `brain.yaml` > built-in defaults; missing/corrupt/non-dict YAML
  falls back to defaults without raising; nested `auto_profile`/`timeouts`;
  the committed `conf/brain.yaml` loads and is sane.
- `localai_client.LocalAIClient` (issue #73, T2) — every endpoint
  (`health` / `chat` / `detect` / `depth` / `transcribe` / `speak`) through an
  injectable `httpx.MockTransport` + a fake `openai` client: exact request
  shape (path / body / headers), the `width/height → w/h` detection mapping +
  `min_confidence` filter, the documented `depth()` stub, and the **never-
  raises** error matrix (timeout / 5xx / malformed JSON / SDK exception all
  become `{"ok": False, "error": ...}`).

## CI

These tests are run by the `brain-tests` job (the T4 issue, #75). Phase 1
(#72) does not add a CI job — it only makes `pytest tests/brain/ -q` pass.
