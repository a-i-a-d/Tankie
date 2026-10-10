# Brain tests (issue #72 T1 · #73 T2 · #74 T3)

Offline unit tests for the Tankie AI brain. No tank, no Pi, no LocalAI, no
real network — pure data (Pydantic schemas), the config loader, the LocalAI
client (via `httpx.MockTransport` + a fake `openai` client), and the frame
grabber (via a fake source + `httpx.MockTransport`).

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

- `frame_grab` (T3, #74) — the non-blocking latest-frame contract:
  `read_latest()` returns the last frame from a fake source, `None` before the
  first frame and after `stale_after_s`; a source that stops producing keeps
  serving the last good frame until stale; `start()`/`stop()` are idempotent
  (no duplicate threads) and restart cleanly; `LLHLSSource` returns `None`
  (no exception) on an unreachable URL and when `cv2` is unavailable (proven
  in a subprocess); `HTTPHLSSource` (the Q2 option-(a) pure-HTTP fallback)
  decodes the latest LL-HLS segment via `httpx.MockTransport` and returns
  `None` on an unreachable/undecodable stream; `build_frame_grab()` /
  `create_source()` wire the config (`cam_url`, `grab_*`) to a source.

## CI

These tests are run by the `brain-tests` job (the T4 issue, #75). Phase 1
(#72) does not add a CI job — it only makes `pytest tests/brain/ -q` pass.
