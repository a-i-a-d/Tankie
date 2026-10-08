# AI Tank Control

This folder contains code to connect an AI to the tank and drive it. This is experimental. The idea is to send images from the camera to an multimodal LLM that then uses tools to control the tank.

Originally planned to work with LocalAGI, it currently only works with LocalAI, since LocalAGI has a bug that prevents us from sending json messages to the Response API (https://github.com/mudler/LocalAGI/issues/340). The LocalAI integration is a rather basic python script that was mainly created to get some AI control working until the LocalAGI issue is fixed. 

# What is what?

## LocalAGI

This part is still incomplete and lacks sending video images to the model.

For LocalAGI Functions can be used by agents to send control commands to the tank. Since functions are limited to LocalAGI integrated go modules, all commands go through the `tankieControl` wrapper, that handles the websocket connection to the tank. 


## LocalAI

For using the tank with LocalAI, use `ai_control.py`. It is a proof of
concept at the moment.

**Configuration (issue #16):** the script reads one config source —
env vars > optional `ai_control.json` next to the script > built-in
defaults:

| env var        | `ai_control.json` key | default                        | meaning                          |
|----------------|-----------------------|--------------------------------|----------------------------------|
| `LOCALAI_API_URL`   | `api_base`  | `http://localai.local:8080/v1` | LocalAI base URL (OpenAI-compatible) |
| `LOCALAI_API_KEY`   | `api_key`   | `sk-0123456789`                | LocalAI accepts any key or none   |
| `TANKIE_VIDEO_URL`  | `video_url` | `rtsp://tankie_pi.local:8554/cam_low` | camera stream for OpenCV |

Dependencies are in `requirements.txt` (headless OpenCV; the Pi has no
display). `ai_control.json` may contain any subset of the keys above.

## Transport (issue #33)

`ai_control.py` drives the tank **via the Pi serial bridge** (`raspberry_pi/serial_bridge/`), not the ESP8266's WebSocket: commands go to the bridge daemon over the Unix socket (`/run/tankie/bridge.sock`, one JSON line in → one JSON line out) and tank state is read from the bridge state store (`/var/lib/tankie/state.json`). The bridge owns the 250 ms drive keep-alive that re-arms the ESP watchdog. Both paths are overridable via `TANKIE_BRIDGE_SOCK` / `TANKIE_STATE_FILE` for testing.
