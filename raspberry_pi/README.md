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
| `setup.sh` | Idempotent installer. Downloads the pinned, self-contained `mediamtx` binary (now in `/usr/local/mediamtx`, config in `/etc/tankie/`), enables the CSI camera, and installs the streaming stack **plus the serial-bridge stack** system-wide (daemon, CLI, config, unit — no repo checkout needed at runtime). Run with `sudo bash setup.sh` (add `--reset-config` to also replace `/etc/tankie/serial_bridge.yaml`, backing up the current file, MMDDhhmm). |
| `build.sh` | Build the ESP8266 (D1 Mini) firmware **and the LittleFS data partition** with arduino-cli (esp8266 core) → `tankie.ino.bin` + `tankie.ino.data.bin`. Run with `bash build.sh`. |
| `flash_serial.sh` | Build (via `build.sh`) + flash the firmware **and the LittleFS data partition** + boot-verify the ESP8266 (D1 Mini) over USB (CH340 → `/dev/ttyUSB0`). Run with `bash flash_serial.sh`. |
| `flash_ota.sh` | Flash the ESP8266 **over the air** (ElegantOTA over WiFi) — firmware + (by default) the LittleFS data partition. Run with `bash flash_ota.sh`. |
| `tests/manual/flash_ota_test.sh` | **Manual, hardware-only** OTA acceptance test from the tank Pi: joins the `tankie-esp` AP, runs `flash_ota.sh`, then restores the infrastructure WiFi. Lives in `tests/manual/` (the home for manual/hardware-only tests). Run with `bash tests/manual/flash_ota_test.sh`. |
| `conf/` | All Pi config files in one place (issue #48). |
| `conf/flash.conf` | Shared flash configuration (serial port/baud, OTA URL, AP name/password, filesystem toggle). Sourced by both flash scripts; env overrides win. |
| `conf/mediamtx.yml` | The mediamtx configuration: two on-demand streams from the one camera (`cam` 1080p30 for humans, `cam_low` 480p15 for the AI). |
| `conf/serial_bridge.yaml` | Serial-bridge configuration (serial_port, baud, keepalive_ms, ack_timeout_ms, state_file, socket_path, stream_port, stream_path). Auto-discovered by `bridge.py` / `tankie-serial` as: `--config` wins, then the installed `/etc/tankie/serial_bridge.yaml`, then the checkout-relative `conf/` (dev execution). |
| `mediamtx.service` | systemd unit that runs `mediamtx` (as root) and keeps it alive (WorkingDirectory `/usr/local/mediamtx`, config `/etc/tankie/mediamtx.yml`).|
| `wlan0-watchdog.service` | One-shot recovery unit: if `wlan0` is missing, reloads the `brcmfmac` driver (fallback: restarts NetworkManager). Never reboots. |
| `wlan0-watchdog.sh` | The actual check/recovery logic (health check, driver reload, NetworkManager fallback), installed to `/usr/local/bin/wlan0-watchdog.sh` and called by the service. |
| `wlan0-watchdog.timer` | systemd timer that triggers the watchdog 90 s after boot, then every 60 s. |

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

## WiFi watchdog

The Pi Zero 2 W's on-board WiFi (`wlan0`, BCM43430B0 / `brcmfmac`) has a
known firmware bug: the firmware can crash, tear down `wlan0`, and the
interface is never re-registered — leaving the Pi unreachable (diagnosis
in [\#1](https://github.com/a-i-a-d/Tankie/issues/1)). Since `wlan0` is
now the Pi's **only** uplink, `setup.sh` installs a watchdog that
self-recovers without a reboot:

- `wlan0-watchdog.timer` fires 90 s after boot, then every 60 s.
- Each tick runs `wlan0-watchdog.service`, which calls `wlan0-watchdog.sh`
  (installed to `/usr/local/bin` by `setup.sh`); the script checks
  `ip link show wlan0`.
  - **healthy** → exits immediately (no-op).
  - **missing** → `rmmod brcmfmac && modprobe brcmfmac` (primary fix from #1);
    if `wlan0` is still gone ~10 s later → `systemctl restart NetworkManager`
    (fallback from #1). If both fail it logs an error and retries on the
    next tick. `TimeoutStartSec=120` caps a hung recovery.

```bash
systemctl status wlan0-watchdog.timer   # timer state
journalctl -u wlan0-watchdog -f         # live watchdog log
systemctl disable --now wlan0-watchdog.timer   # disable the watchdog
```

## Flashing the ESP8266 (D1 Mini) from the Pi

The ESP8266 firmware (`../tankie`) is built and flashed **on the Pi** over the
USB-serial chain — no desktop machine needed:

```
D1 Mini ── CH340 (1a86:7523) ── passive USB hub ── Pi OTG port ── /dev/ttyUSB0
```

Because the CH340 drives **EN (RST)** and **IO0 (BOOT)** from the DTR/RTS
lines, `esptool` can force the chip into download mode and hard-reset it
automatically — there is **no button dance**.

```bash
# one-time prerequisites
arduino-cli core install esp8266:esp8266   # provides the toolchain + esptool
pip3 install --user pyserial               # only for the boot check

# build only
bash raspberry_pi/build.sh

# build (if needed) + flash over USB + verify boot
bash raspberry_pi/flash_serial.sh

# ...or flash over the air (the ESP must be reachable, e.g. on the tankie-esp AP)
bash raspberry_pi/flash_ota.sh
```

What `flash_serial.sh` does:

1. **Build** — via `build.sh` (standalone: `tankie-build`):
   - **Firmware:** `arduino-cli compile --fqbn esp8266:esp8266:d1_mini` with
     the global flag `--build-property
     "build.extra_flags=-DELEGANTOTA_USE_ASYNC_WEBSERVER=1"`. That flag is
     **required**: ElegantOTA and the core's `WebServer` both define an
     `HTTP_GET` enum, and this is the library's documented toggle to drop one.
     Produces the merged image `tankie.ino.bin` (≈ 382 kB, bootloader magic
     `0xE9`).
   - **LittleFS data partition:** the firmware serves its web UI (`tankie/data/`)
     from a LittleFS partition, so `build.sh` also packs that directory into
     `tankie.ino.data.bin` with the core's `mklittlefs` (page 256 / block 8192 /
     size 2072576 — the D1 Mini's default 4 MB, FS:2 MB layout, matching what the
     core's linker script bakes into the firmware).
   - Re-runs reuse the binaries while they are newer than the sources (including
     `tankie/data/`).
2. **Flash** — `esptool.py --chip esp8266 write_flash 0x0 tankie.ino.bin
   0x200000 tankie.ino.data.bin` (esptool ships inside the esp8266 core
   package): the firmware at `0x0` and the LittleFS data partition at
   `0x200000`. A full flash takes ~45 s at 115200 baud; `Hash of data
   verified` is the success marker.
3. **Verify** — reads the ESP's UART0 console and expects the firmware's
   `Battery Voltage: …` stream, proving the new firmware actually booted.

`setup.sh` installs the toolchain (arduino-cli + the esp8266 core, which ships
the xtensa compiler, esptool, and `mklittlefs`) and the helpers on a fresh
system as **`tankie-build`**, **`tankie-flash`**, **`tankie-flash-ota`** and
**`tankie-flash-ota-test`** in `/usr/local/bin`, so the ESP can be built
(firmware + LittleFS data partition) and flashed — over USB or over the air —
from the Pi without the repo checkout in the way.

Notes:

- **Cable matters.** A bad/cheap USB cable (or a loose connection) makes the
  CH340's DTR/RTS control transfers fail at the USB level
  (`failed to send control message: -71`, `ioctl(TIOCMBIS) → EFAULT`) and
  esptool dies with `Timed out waiting for packet header` — even though the
  data path looks fine. With a good cable the exact same setup works; if you
  hit that error, swap the cable first.
- **Power matters.** The ESP8266's WiFi TX bursts hit ~500 mA; if the D1 Mini
  is powered only through the passive hub + OTG, prefer powering it from the
  tank's 5 V rail and using USB for data only.
- **OTA is the recommended path** when the TB6612FNG is connected: its
  boot-strapping pins (AIN1=GPIO0, STBY=GPIO2, BIN1=GPIO15) can fight the
  CH340's attempt to force download mode, so a USB flash can hang on
  `Connecting…` (and the motors can spin at max speed) — see
  [#44](https://github.com/a-i-a-d/Tankie/issues/44). `flash_ota.sh` pushes
  the same images over WiFi via ElegantOTA (`/ota/start` → `/ota/upload`)
  with no download mode at all.
- **Fallbacks** if USB ever regresses: hold **BOOT** + tap **RST** on the D1
  Mini to force download mode manually, or use `flash_ota.sh` once the ESP is
  on a reachable network.

Full history and diagnosis: [#21](https://github.com/a-i-a-d/Tankie/issues/21).

## Notes / related issues

- The Pi's IP is the single source of truth for both streams. The ESP8266 web
  UI no longer hard-codes a stream IP: the bridge detects the Pi's own LAN IP
  and pushes it to the ESP over the serial link via the `set_stream` command
  (issue [#51](https://github.com/a-i-a-d/Tankie/issues/51)), and the web UI
  only shows the "Start video stream" button once that `stream_url` has been
  seen. The `stream_port` / `stream_path` are configured in
  `conf/serial_bridge.yaml` (one config source, per
  [#16](https://github.com/a-i-a-d/Tankie/issues/16)).
- The ESP keeps the stream endpoint only in RAM, so an ESP reboot loses it. The
  bridge self-heals (issue [#57](https://github.com/a-i-a-d/Tankie/issues/57)):
  it tracks whether the ESP currently holds a `stream_url` (from the ESP's own
  `state` broadcasts) and, on its 30 s stream tick, force-pushes `set_stream`
  once whenever the Pi has a LAN IP but the ESP reports no `stream_url`; the
  push stops as soon as the ESP confirms the URL, so it never spams. The reader
  also extracts the first parseable JSON object from a serial line instead of
  requiring it to start with `{`, so a `hello` that shares a line with ESP boot
  noise is no longer dropped.
- Audio (mic/speaker) is a separate concern and is being folded into the Pi
  web app; see [#20](https://github.com/a-i-a-d/Tankie/issues/20) and
  [#21](https://github.com/a-i-a-d/Tankie/issues/21).
- The full as-built Pi documentation (hardware, networking, audio, serial)
  lives in [#21](https://github.com/a-i-a-d/Tankie/issues/21).
