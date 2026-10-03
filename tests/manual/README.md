# Manual / hardware-only tests

This directory is the home for **manual acceptance tests** that can only run on
dedicated hardware — the kind of checks that need the real tank (ESP8266 +
motor H-bridge + camera Pi) and/or a specific network state, so they are run by
a human on the box rather than in CI. They are **not** part of the automated
host suite (`tests/run_tests.sh`), which is what CI runs.

Add a new manual test here (a shell script, or a script + notes) and list it in
the table below. Keep each test **single-purpose**, **idempotent**, and safe to
re-run; prefer a `--dry-run` mode that validates config/dependencies without
touching the network or hardware.

| Test | What it needs | What it does |
|------|---------------|--------------|
| `flash_ota_test.sh` | The tank Pi + the ESP8266 (D1 Mini) on its `tankie-esp` fallback AP, serial console on `/dev/ttyUSB0` | One-command OTA acceptance test: confirms the AP is visible, joins the `tankie-esp` AP, captures the ESP console, verifies the web/OTA endpoints before **and** after, runs `raspberry_pi/flash_ota.sh` (firmware + data partition), then **always** restores the infrastructure WiFi. Run with `bash tests/manual/flash_ota_test.sh`. |
