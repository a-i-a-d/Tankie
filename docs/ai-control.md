← [Tankie](../README.md) · [Docs index](README.md) · *AI control*

# AI control

The [ai-control/](../ai-control/) folder contains the code that connects an AI
to the tank (see [ai-control/README.md](../ai-control/README.md) for what is
what). The AI drives the tank **through the Pi serial bridge** (see
[serial-protocol.md](serial-protocol.md)), not the ESP8266's WebSocket.

## Safety & state feedback

- **Safety watchdog (firmware):** if the tank is driving and no command is
  received for 5 seconds, the motors are stopped and the pan/tilt camera is
  recentered automatically. A watchdog event is broadcast to all websocket
  clients as `{"type":"watchdog"}` (issue #32).
- **State feedback:** the periodic websocket broadcast includes the current
  state in addition to the battery voltage, in the contract shape:
  `{"type":"state","seq":N,"battery":...,"speed":...,"steer":...,"pan":...,"tilt":...,"net_mode":"sta"|"ap","net_ip":"192.168.x.y","stream_url":"http://192.168.1.42:8889/cam/"}`
  (issue #32, #46, #51). The `net_mode` and `net_ip` fields let a client
  determine the ESP's IP address and WiFi mode (STA vs AP) without needing the
  boot console (issue #46). The `stream_url` field (issue #51) carries the
  Pi's own video-stream endpoint, pushed by the bridge via the `set_stream`
  command — the human web UI only shows the "Start video stream" button once a
  `stream_url` has been seen, so the iframe always points at the Pi's current
  (dynamic) IP.
- **JSON protocol (ai_control.py):** the LocalAI client speaks the JSON
  protocol contract — `{"cmd":"pan","angle":N}` / `{"cmd":"tilt","angle":N}`
  for the camera and one combined `{"cmd":"drive","speed":N,"steer":M}`
  object for the drive (the legacy `key=value` dialect was retired by #32,
  issue #38).
- **Continuous control (ai_control.py):** a background control loop re-issues
  the active drive command (the combined `drive` object) every second so the
  firmware watchdog stays armed while the AI is thinking between frames.
- **Autonomous drive profile:** AI-issued drive commands are clamped to
  `max_speed = 40` (see `AUTO_PROFILE` in
  `ai-control/LocalAI/ai_control.py`), slower than the manual joystick range,
  so a misbehaving model cannot drive the tank at full speed.
- **Pan/tilt:** the firmware clamps `pan`/`tilt` values to the 0–180 servo
  range; the AI tools already use relative moves (`up`/`down`/`left`/`right`/
  `center`) around the 90-degree center position.

## Configuration (issue #16)

No network endpoints are hard-coded in the AI code.

- **`ai-control/LocalAI/ai_control.py`** (LocalAI client) — one config source:
  env vars > optional `ai_control.json` (next to the script) > built-in
  defaults.

  | env var | json key | default | meaning |
  |---|---|---|---|
  | `LOCALAI_API_URL` | `api_base` | `http://localai.local:8080/v1` | LocalAI base URL (OpenAI-compatible) |
  | `LOCALAI_API_KEY` | `api_key` | `sk-0123456789` | LocalAI accepts any key or none |
  | `TANKIE_VIDEO_URL` | `video_url` | `rtsp://tankie_pi.local:8554/cam_low` | camera stream for OpenCV (fallbacks in the script) |

- **`raspberry_pi/serial_bridge/`** (Pi bridge) — `TANKIE_BRIDGE_SOCK` /
  `TANKIE_STATE_FILE` (see [serial-protocol.md](serial-protocol.md)); it
  pushes the Pi's own `stream_url` to the ESP (issue #51).
- **`tankie/data/`** (web UI) — stream URL is never hard-coded: the
  `stream_url` field in every `state` broadcast (see above), pushed by the
  bridge.
- **`ai-control/tankieControl`** — LocalAGI path, parked
  (mudler/LocalAGI#340); removed in a later cleanup step.

> **Dependencies:** the Python side is standard PyPI packages (incl. the
> `openai` SDK used only as the HTTP transport to LocalAI's OpenAI-compatible
> endpoint) — see the "Python dependencies" section in
> [build.md](build.md).

---

## See also
- [Serial control protocol](serial-protocol.md) — the transport the AI uses
- [Build](build.md) — Python dependencies (incl. `openai`)
- [AI control code (sub-README)](../ai-control/README.md)
- [Tankie (home)](../README.md)
