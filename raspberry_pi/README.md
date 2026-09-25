# Raspberry Pi (Tankie) — video streaming

This directory contains everything for the **Raspberry Pi Zero 2 W** part of
Tankie: the camera video-streaming stack (mediamtx) that feeds both the human
remote-control UI and the AI perception loop.

The ESP8266 in the rest of the repo handles motor / servo control. The Pi is a
separate box bolted on top whose only job here is to turn the CSI camera
(Sony IMX708 / Camera Module 3) into low-latency, on-demand video streams.

## What is in this directory

| File | Purpose |
|------|---------|
| `setup.sh` | Idempotent installer. Downloads the pinned, self-contained `mediamtx` binary, enables the CSI camera, installs the config + systemd service, and starts streaming. Run with `sudo bash setup.sh`. |
| `mediamtx.yml` | The mediamtx configuration: two on-demand streams from the one camera (`cam` 1080p30 for humans, `cam_low` 480p15 for the AI). |
| `mediamtx.service` | systemd unit that runs `mediamtx` as a non-root user and keeps it alive. |

## The two streams

Both come from the **same** IMX708 sensor via the hardware H.264 encoder.
`rpiCameraSecondary` lets mediamtx expose a second, independently-encoded
stream from the same sensor — no re-encode, no second camera, no extra process.

| Path | Resolution | FPS | Codec | For |
|------|-----------|-----|-------|-----|
| `cam` | 1920×1080 | 30 | H.264 (hardware) | human remote control |
| `cam_low` | 640×480 | 15 | H.264 (hardware) | AI / LLM perception |

Both are **on-demand** (`sourceOnDemand: yes`): the camera and the two
encoders only run while at least one client is connected, and stop ~10 s after
the last client leaves. Idle bandwidth/CPU is zero.

## Endpoints (once running)

| Use | URL |
|-----|-----|
| Human — WebRTC (lowest latency, ~<300 ms) | `http://<pi>:8889/cam/` |
| Human — LL-HLS (browser fallback) | `http://<pi>:8888/cam/index.m3u8` |
| Human — RTSP | `rtsp://<pi>:8554/cam` |
| AI — RTSP (H.264, OpenCV/ffmpeg) | `rtsp://<pi>:8554/cam_low` |
| AI — LL-HLS | `http://<pi>:8888/cam_low/index.m3u8` |
| Ops — is a client connected? | `curl -s http://127.0.0.1:9997/v3/paths/list` |

> The management API is bound to loopback only, so it is not reachable from
> the network.

## Quick start (fresh Pi)

```bash
# 1. Flash Raspberry Pi OS (64-bit) and connect the Camera Module 3 to the CSI port.
# 2. Copy this directory to the Pi (or check out the repo) and run:
sudo bash setup.sh
```

That's it. `setup.sh` handles the camera enable, the binary, the config, and
the service. If the camera was enabled for the first time, reboot once so the
driver loads and `/dev/video0` appears.

## Why mediamtx (and why this version)

- **Self-contained binary.** The official `mediamtx` release binary embeds the
  `mtxrpicam` camera helper, `libcamera`, and the Raspberry Pi IPA configs.
  So there is **no** need to install GStreamer, ffmpeg, or libcamera from apt —
  which is exactly what makes a clean, reproducible setup on a Zero 2 W easy.
- **`rpiCameraSecondary`** (the dual-stream feature) requires mediamtx
  **≥ v1.19.2**. We pin **v1.21.1** in `setup.sh` (SHA256-verified).
- **On-demand + hardware encode** keeps the single A35-core-class Pi cool and
  the 2.4 GHz WiFi link free when nobody is watching.

## Notes / related issues

- The Pi's IP is the single source of truth for both streams. The ESP8266 web
  UI still hard-codes `http://10.42.0.1:8889/cam/` — that is tracked in
  [#16](https://github.com/a-i-a-d/Tankie/issues/16) (one config source).
- Audio (mic/speaker) is a separate concern and is being folded into the Pi
  web app; see [#20](https://github.com/a-i-a-d/Tankie/issues/20) and
  [#21](https://github.com/a-i-a-d/Tankie/issues/21).
- The full as-built Pi documentation (hardware, networking, audio, serial)
  lives in [#21](https://github.com/a-i-a-d/Tankie/issues/21).
