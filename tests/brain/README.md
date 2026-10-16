# Brain tests (issue #72 T1 · #73 T2 · #74 T3 · #80 T4)

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

- `reflex` (T4, #80) — the System 1 safety net: the pure
  `ReflexController.decide()` decision matrix (A1 configurable block classes,
  A2 in-lane/close geometry + the default-OFF `reflex_obstacle_enabled` switch,
  A3 arc-around (issue #105), AUTO_PROFILE clamp, pan/tilt passthrough, invalid-pending
  handling); `ReflexController.to_bridge_cmd()` (a stop stays a stop — it never
  re-introduces a pending value the reflex overrode); `ReflexLoop` (the 10 Hz
  thread) with fake grabber/LocalAI/bridge proving the invariants — no frame
  or `detect()` failure ⇒ `drive 0 0` (invariant 4), a center-lane obstacle
  pre-empts a pending drive (invariant 2), the A2 switch both ways, the A4
  quiet-idle option, and `stop()` always sends one final `drive 0 0`;
  `BridgeClient` against a **real Unix socket** served by a fake in-process
  bridge (happy path, timeout, missing socket, malformed response — all
  `{"ok": ...}`, never raises); `frame_to_b64()` JPEG round-trip + `None`.
  The new `config` reflex keys (`reflex_obstacle_enabled` /
  `reflex_block_classes` / `reflex_lane_half_width` / `reflex_min_bottom_ratio`
  / `reflex_steer_around`) load from YAML and env (`TANKIE_REFLEX_*`) and
  coerce correctly.
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
  Issue #107: a *real* MPEG-TS video segment (generated with
  `cv2.VideoWriter`) decodes to a frame via the `cv2.VideoCapture("file://…")`
  path, while `cv2.imdecode` alone returns `None` for it; `rtsp://` URLs
  route to `LLHLSSource` (cv2 handles RTSP natively), including the
  `prefer="http"` redirect, which is logged.

## CI

These tests are run by the `brain-tests` job (the T4 issue, #75). Phase 1
(#72) does not add a CI job — it only makes `pytest tests/brain/ -q` pass.
