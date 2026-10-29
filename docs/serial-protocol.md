← [Tankie](../README.md) · [Docs index](README.md) · *Serial control protocol*

# Serial control protocol (ESP8266 over the Pi native UART0)

The ESP8266 is controlled over the Pi's native UART0 (`/dev/ttyS0` @ 921600 8N1,
wired TX→RX / RX→TX / GND→GND — the permanent control link; `/dev/ttyUSB0` is
only the USB flashing adapter) using a line-oriented JSON protocol (NDJSON) — see
[issue #29](https://github.com/a-i-a-d/Tankie/issues/29). The WiFi/WebUI path stays
available as a fallback.

## Pi → ESP (commands)

```json
{"cmd":"drive","speed":50,"steer":0}
{"cmd":"pan","angle":90}
{"cmd":"tilt","angle":30}
{"cmd":"pan-rel","delta":+20}
{"cmd":"tilt-rel","delta":-10}
{"cmd":"center"}
{"cmd":"sweep","axis":"pan","from":0,"to":180,"steps":20}
{"cmd":"stop"}
{"cmd":"set_stream","ip":"192.168.1.42","port":8889,"path":"/cam/"}
{"cmd":"set_wifi","ssid":"tankie-lan","pass":"hunter2","ip":"192.168.178.126","gateway":"192.168.178.1"}
{"cmd":"set_wifi","reset":true}
{"cmd":"get_wifi"}
{"cmd":"reboot"}
```

## ESP → Pi (responses)

```json
{"type":"hello","proto":1,"fw":"v0.1-serial"}
{"type":"ack","seq":1}
{"type":"error","seq":2,"code":"range","field":"speed"}
{"type":"watchdog"}
{"type":"sweep","axis":"pan","done":true}
{"type":"state","seq":1,"battery":7.42,"speed":50,"steer":0,"pan":90,"tilt":90,"net_mode":"sta","net_ip":"192.168.1.42","stream_url":"http://192.168.1.42:8889/cam/"}
{"type":"wifi","ssid":"tankie-lan","mode":"sta","ip":"192.168.178.126"}
```

## Command notes

`set_stream` lets the Pi — the only component that knows its own (dynamic) IP —
push its video-stream endpoint to the ESP. The ESP stores it and appends it as
`stream_url` to every `state` broadcast; the human web UI then points its video
iframe at that URL instead of a hard-coded address. The field is omitted until
the Pi has sent one, so the line shape is unchanged for pairs that do not use
it.

`set_wifi` / `get_wifi` / `reboot` let the Pi — or a Pi-only agent — re-network
and restart the tank over the serial link without a browser. `set_wifi` stores
the WiFi config (ssid / pass / optional static ip + gateway, or `reset:true` to
wipe it) in the **same reserved EEPROM sector `0x3FB000`** the config portal
uses (see [wifi-config.md](wifi-config.md)) and reboots to apply — the exact
same path, so the web and serial config paths behave identically. `get_wifi`
echoes the current `ssid` / `mode` / `ip` (no password). `reboot` restarts the
ESP on demand (so an agent with only serial access can reboot the tank without
power-cycling). All three are additive — protocol version stays 1.

The ESP keeps the `stream_url` only in RAM, so an ESP reboot loses it. The
bridge therefore self-heals: it tracks whether the ESP currently holds a
`stream_url` (from the ESP's own `state` broadcasts) and, on its 30 s stream
tick, force-pushes `set_stream` once whenever the Pi has a LAN IP but the ESP
reports no `stream_url`; the push stops as soon as the ESP confirms the URL, so
it never spams. This restores the "Start video stream" button within ~30 s of an
ESP reboot even if the `hello` handshake is missed (e.g. when boot noise and the
`hello` land on the same serial line — the reader now extracts the first
parseable JSON object instead of requiring the line to start with `{`).

## Rules

Strict validation + clamping on the ESP (speed/steer ±255, pan/tilt 0–180,
relative deltas ±180); a command watchdog stops the motors if the link goes
quiet (`SERIAL_WATCHDOG_MS`, compile-time in `tankie/config_serial.h`);
seq-based acks so a dead link is detectable; a hello/proto handshake catches
firmware/Pi mismatches. Non-JSON lines (debug output) are ignored.

## Steer sign convention

**Steer sign convention** ([issue #85](https://github.com/a-i-a-d/Tankie/issues/85)):
`steer > 0` = turn **RIGHT**, `steer < 0` = turn **LEFT** (the inner wheel is
the one on the turn's center side). This is the single convention used across
the whole stack — the firmware (`tankie/wheels.cpp`), the reflex brain
(`raspberry_pi/brain/reflex.py`), the Go proxy, `ai_control.py` and the web-UI
joystick — and matches the intuitive / VLM-output convention.

## Pan/tilt improvements (additive — protocol version stays 1)

- `pan-rel` / `tilt-rel` move the servo by a delta from the current angle
  (clamped to 0–180).
- `center` sets pan=90 **and** tilt=90 in one ack.
- `sweep` steps the camera across a range non-blockingly (~2 s default,
  `steps` 1–50) and emits `{"type":"sweep","axis":...,"done":true}` on
  completion; `stop` (or an explicit pan/tilt) cancels it.

The **autonomous drive profile** is enforced in the bridge
(`raspberry_pi/conf/serial_bridge.yaml` → `auto_profile`): AI-issued `drive`
commands are clamped to `max_speed`/`max_steer` before they reach the ESP, and
the 250 ms keep-alive re-sends the clamped values. Manual (CLI / Web UI)
traffic is not clamped.

## Pi-side bridge (`raspberry_pi/serial_bridge/`)

- `bridge.py` — daemon holding the control link (`/dev/ttyS0` by default):
  reader thread (NDJSON in, seq/ack tracking, link health), writer thread
  (250 ms keep-alive re-send of the active drive command), state store
  (`/var/lib/tankie/state.json`), Unix socket (`/run/tankie/bridge.sock`).
  Installed by `setup.sh` to `/usr/local/lib/tankie/bridge.py`.
- `tankie-serial.py` — CLI: `drive --speed 50 --steer 0`, `pan 90`, `tilt 30`,
  `pan-rel 20`, `tilt-rel -10`, `center`, `sweep --axis pan --from 0 --to 180
  [--steps 20]`, `stop`, `state`, `watchdog-test`, `wifi --ssid tankie-lan
  --pass hunter2 [--ip 192.168.178.126 --gateway 192.168.178.1]` / `wifi
  --reset` / `wifi` (query), `reboot` (or `--raw` to talk to the port
  directly). Installed by `setup.sh` as `tankie-serial` in `/usr/local/bin`.
- `conf/serial_bridge.yaml` — single config source (serial_port, baud,
  keepalive_ms, ack_timeout_ms, state_file, socket_path, stream_port,
  stream_path). Installed by `setup.sh` to `/etc/tankie/serial_bridge.yaml`;
  `--config` wins, then the installed system config, then the checkout-relative
  `conf/` (dev execution).
- `tankie-serial.service` — systemd unit (`Restart=always`, dialout group),
  installed by `setup.sh` to `/etc/systemd/system`. References only installed
  system paths.
- `setup-serial.sh` — idempotent installer (deps, udev rule, systemd). Invoked
  by `setup.sh`; also runnable standalone for just the serial bridge.
- `test_serial_proto.py` — raw-port protocol test (T1–T10, incl. the pan-rel /
  tilt-rel / center / sweep commands).

```sh
sudo bash raspberry_pi/setup.sh                          # install everything (incl. serial bridge)
tankie-serial drive --speed 50 --steer 0
tankie-serial state
```

---

## See also
- [WiFi configuration](wifi-config.md) — `set_wifi` / `get_wifi` share the same EEPROM sector
- [AI control](ai-control.md) — the AI drives the tank through this bridge
- [Raspberry Pi (sub-README)](../raspberry_pi/README.md) — the Pi-side streaming + flashing stack
- [Tankie (home)](../README.md)
