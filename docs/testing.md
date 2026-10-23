← [Tankie](../README.md) · [Docs index](README.md) · *Testing*

# Testing

The ESP8266 firmware logic is covered by a host-side unit-test suite (no ESP
toolchain needed) in [tests/](../tests/), plus a CI compile-check for the full
sketch in [.github/workflows/tests.yml](../.github/workflows/tests.yml).

## Host-side unit tests (firmware)

- `tests/test_wheels.cpp` — the pure drive-math `computeWheels()`
  ([tankie/wheels.cpp](../tankie/wheels.cpp), no Arduino deps): straight /
  turn-left / turn-right in fwd + rev, spin-in-place at `speed == 0`, the
  clamped ±255 extreme corners (issue #14), and an exhaustive
  (speed, steer) matrix check that both outputs stay within [-255, 255].
- `tests/test_tankdrive.cpp` — `TankDrive` end-to-end through the real
  `Motor` H-bridge: the full branch matrix (stop / spin-in-place / fw / bw
  x straight / left / right, partial steer, boundary values), asserting the
  real `config.h` H-bridge pins (PWM + In1/In2 direction).
- `tests/test_motor.cpp` — `Motor::drive/brake/standby` and the
  `forward/back/left/right/brake` free functions.
- `tests/test_bat_voltage.cpp` — the battery voltage-divider math
  (`getBatVoltage()`, extracted from `tankie.ino` into
  [tankie/batt.cpp](../tankie/batt.cpp)).
- `tests/test_wifimanager.cpp` — the WiFi config blob codec + EEPROM adapter
  ([tankie/wificfg.cpp](../tankie/wificfg.cpp), issue #54): encode/decode
  round-trip, the CRC32 (known IEEE vector), and the blank-sector /
  corrupt-byte / bad-magic / wrong-version / oversized-field / truncated-blob
  rejection paths, plus the EEPROM save→load, wipe, and "survives an fs-OTA
  wipe" round-trips.
- `tests/test_set_wifi.cpp` — the `set_wifi` / `get_wifi` / `reboot` serial
  commands (issue #56) against the REAL codec + EEPROM shim: a save lands the
  config in the EEPROM sector (read back via `wifiCfgLoad`) and sets the
  reboot-pending flag, `reset:true` wipes the sector, malformed input
  (missing/empty ssid, oversized ssid/pass, bad ip/gateway) is rejected with
  no save, a full-cap (64-char ssid+pass) line is not truncated (`lineBuf_`
  128→256), and `get_wifi` returns ssid/mode/ip (no password).
- `tests/test_ws_fallback.cpp` — the WebSocket fallback handler
  (`handleWebSocketMessage()` in [tankie/tankie.ino](../tankie/tankie.ino))
  compiled against the network shims in [tests/shims/](../tests/shims/):
  valid drive/pan/tilt/stop move the motors/servos, out-of-range /
  missing-field / non-JSON / unknown-cmd messages are rejected (no motor or
  servo change), binary + fragmented frames are ignored, and the
  out-of-bounds `data[len] = 0` write is gone (a canary byte at `data[len]`
  is left untouched) (issue #34).

## Protocol-contract harnesses (web UI / AI / bridge)

- `tests/web_ui_harness.js` — the web UI (`tankie/data/script.js`) in a Node
  VM sandbox: every outgoing websocket message is valid JSON and exactly the
  protocol-contract shapes (drive / pan / tilt / stop), and the incoming
  state/ack/error/watchdog/hello broadcasts are handled (issue #32).
- `tests/ai_control_json_harness.py` — the LocalAI client
  (`ai-control/LocalAI/ai_control.py`) with stubbed deps: every outgoing
  message is a contract JSON object (drive / pan / tilt / pan-rel / tilt-rel /
  center / sweep), the AUTO_PROFILE speed + steer clamp is applied, the
  control loop re-issues the combined drive object, and the
  state/watchdog broadcasts parse (issue #38, #31).
- `tests/bridge_auto_profile_harness.py` — the Pi bridge daemon
  (`raspberry_pi/serial_bridge/bridge.py`) with stubbed serial/yaml: the
  autonomous drive profile clamps speed/steer (and reports `clamped`), and
  the pan-rel / tilt-rel / center / sweep commands dispatch the correct NDJSON
  line with validation (issue #31).
- `tests/bridge_stream_rearm_harness.py` — the Pi bridge daemon's stream
  re-arm (issue #57) with stubbed serial/yaml: `_extract_json` recovers a
  JSON object from a clean line, a garbage-prefixed line (the ESP boot-noise +
  `hello` that share one serial line), a no-brace line, and a stray-brace
  line; `_on_state` tracks the ESP's `stream_url` presence; and the 30 s
  re-arm tick force-pushes `set_stream` exactly once when the Pi has a LAN IP
  but the ESP reports no stream, and stops once the ESP confirms it.
- `tests/bridge_wifi_harness.py` — the Pi bridge daemon's `set_wifi` /
  `get_wifi` / `reboot` dispatch (issue #56) with stubbed serial/yaml: each
  command hands the exact NDJSON line to the serial port, `set_wifi` without
  a ssid (and without `reset`) is rejected, and unknown commands still error.
- `tests/wlan0_watchdog_harness.sh` — the Pi WiFi watchdog
  (`raspberry_pi/wlan0-watchdog.sh`) with PATH-shimmed ip/lsmod/rmmod/
  modprobe/systemctl/sleep/reboot: healthy no-op, fresh-load recovery
  (module not loaded), correct rmmod order (brcmfmac_wcc before brcmfmac),
  bounded reboot after 3 failed ticks, counter reset on recovery, corrupt
  counter sanitization, and static rmmod-order check (issue #68).
- `ai-control/tankieControl/main_test.go` — the LocalAGI wrapper's
  drive/steer/camera handlers against a stub websocket tank: contract JSON
  shapes only, steer combined with the active speed, center = pan 90 + tilt 90
  (issue #39).

## Run locally (any machine with g++, node, go)

```sh
bash tests/run_tests.sh
node tests/web_ui_harness.js                    # web UI JSON protocol (node)
python3 tests/ai_control_json_harness.py        # LocalAI client JSON protocol
python3 tests/bridge_auto_profile_harness.py    # bridge auto-profile + new commands
python3 tests/bridge_stream_rearm_harness.py    # bridge stream re-arm (issue #57)
python3 tests/bridge_wifi_harness.py            # bridge set_wifi/get_wifi/reboot (issue #56)
bash tests/wlan0_watchdog_harness.sh            # Pi WiFi watchdog recovery ladder (issue #68)
(cd ai-control/tankieControl && go test ./...)  # LocalAGI wrapper JSON protocol
```

The tests compile the real firmware `.cpp` files against a minimal Arduino
shim ([tests/shims/Arduino.h](../tests/shims/Arduino.h)) and exit non-zero on
any failure.

## CI jobs ([.github/workflows/tests.yml](../.github/workflows/tests.yml))

- `unit-tests` — the host-side firmware suite above.
- `web-ui` — `tests/web_ui_harness.js`.
- `go-checks` — `gofmt -l` + `go vet ./...` + `go build ./...` +
  `go test ./...` inside `ai-control/tankieControl` (the only real Go module —
  `ai-control/functions/` has no `go.mod`) (issue #19).
- `python-checks` — `py_compile ai-control/LocalAI/ai_control.py` and a
  `pip install -r requirements.txt` smoke (issue #19).
- `brain-tests` — the AI brain offline suite (see
  [tests/brain/README.md](../tests/brain/README.md)).
- `bridge-safety` — bridge safety invariants.
- `vlm-bench` — the VLM benchmark harness (see
  [tools/vlm-benchmark/README.md](../tools/vlm-benchmark/README.md)).
- `firmware-build` — compiles the complete sketch with arduino-cli
  (esp8266 core 3.1.2, `esp8266:esp8266:d1_mini`,
  `-DELEGANTOTA_USE_ASYNC_WEBSERVER=1`) so the sketch can never break the
  build/flash path used on the tank Pi.
- `pi-install-check` / `platformio-build` — the Pi installer and a
  PlatformIO firmware build.

## Not covered (intentionally)

The network stack itself (the shims in [tests/shims/](../tests/shims/) are
compile-only stubs — no real sockets) and hardware-in-the-loop / OTA
behavior. The WebSocket *command handler* in `tankie.ino` IS covered by
`tests/test_ws_fallback.cpp` (issue #34).

---

## See also
- [Serial control protocol](serial-protocol.md) — the contract the harnesses assert
- [AI control](ai-control.md) — the AI client under test
- [Brain tests (sub-README)](../tests/brain/README.md) · [Manual tests (sub-README)](../tests/manual/README.md)
- [Tankie (home)](../README.md)
