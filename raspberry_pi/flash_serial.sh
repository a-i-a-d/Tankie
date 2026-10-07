#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Tankie — flash the ESP8266 (NodeMCU D1 Mini) firmware from the Pi Zero 2 W
#
# Chain:  D1 Mini --(CH340 1a86:7523)--> passive USB hub --> Pi OTG port
# The CH340 drives EN (RST) + IO0 (BOOT) from DTR/RTS, so esptool can force
# the chip into download mode automatically — no button dance needed.
#
# Steps:
#   1. Build the sketch via build.sh (arduino-cli, esp8266 core) unless a
#      fresh binaries already exist (rebuild only when the sources are newer).
#   2. Flash the merged image AND the LittleFS data partition with esptool
#      (both ship from the esp8266 core package).
#   3. Verify the chip rebooted and is running the new firmware by reading
#      its UART0 console (the firmware streams "Battery Voltage: …" lines).
#
# Usage:
#   bash flash_serial.sh                 # port defaults to /dev/ttyUSB0 (flash.conf)
#   FLASH_SERIAL_PORT=/dev/ttyUSB1 bash flash_serial.sh
#
# Requirements (one-time, see raspberry_pi/README.md "Flashing the ESP8266"):
#   * arduino-cli with the esp8266 core:  arduino-cli core install esp8266:esp8266
#   * pyserial (boot check only):         pip3 install --user pyserial
#
# Tracked: https://github.com/a-i-a-d/Tankie/issues/21
# ---------------------------------------------------------------------------
set -euo pipefail

log()  { printf '\033[1;32m[flash]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[flash]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[flash]\033[0m %s\n' "$*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# --- configuration ----------------------------------------------------------
# Defaults live in flash.conf (issue #44 D, shared with flash_ota.sh). A value
# already set in the environment wins over the flash.conf default, and the
# system-wide /etc/tankie/flash.env (written by setup.sh) wins over both.
FLASH_CONF="${SCRIPT_DIR}/conf/flash.conf"
[ -f "${FLASH_CONF}" ] || die "flash.conf not found in conf/ next to flash_serial.sh: ${FLASH_CONF}"
# shellcheck disable=SC1091
. "${FLASH_CONF}"
if [ -f /etc/tankie/flash.env ]; then
  # shellcheck disable=SC1091
  . /etc/tankie/flash.env
fi

PORT="${FLASH_SERIAL_PORT}"
BAUD="${FLASH_SERIAL_BAUD}"

SKETCH_DIR="${TANKIE_SKETCH_DIR:-${REPO_ROOT}/tankie}"
OUT_DIR="${TANKIE_FLASH_DIR:-/tmp/tankie-flash}"   # stable dir -> shared with build.sh
BIN="${OUT_DIR}/tankie.ino.bin"
DATA_BIN="${OUT_DIR}/tankie.ino.data.bin"
BUILD_SH="${SCRIPT_DIR}/build.sh"

# --- preflight ---------------------------------------------------------------
[ -f "${BUILD_SH}" ] || die "build.sh not found next to flash_serial.sh: ${BUILD_SH}"
[ -f "${SKETCH_DIR}/tankie.ino" ] || die "sketch not found: ${SKETCH_DIR}/tankie.ino"
command -v arduino-cli >/dev/null 2>&1 || die "arduino-cli not found in PATH"
[ -e "${PORT}" ] || die "serial port ${PORT} not present (is the CH340/D1 Mini connected?)"

# esptool ships inside the esp8266 core package
ESPTOOL="$(ls -d "${HOME}"/.arduino15/packages/esp8266/hardware/esp8266/*/tools/esptool/esptool.py 2>/dev/null | sort -V | tail -1 || true)"
[ -n "${ESPTOOL}" ] && [ -f "${ESPTOOL}" ] || die "esptool.py not found — run: arduino-cli core install esp8266:esp8266"

# --- 1. build (skip if the binary is newer than every sketch source) --------
LATEST_SOURCE="$(ls -t "${SKETCH_DIR}"/*.ino "${SKETCH_DIR}"/*.h "${SKETCH_DIR}"/*.cpp "${SKETCH_DIR}"/data/* 2>/dev/null | head -1)"
if [ -f "${BIN}" ] && [ -n "${LATEST_SOURCE}" ] && [ ! "${LATEST_SOURCE}" -nt "${BIN}" ]; then
  log "reusing existing build ${BIN} (newer than sources)"
else
  bash "${BUILD_SH}"
  [ -f "${BIN}" ] || die "build did not produce ${BIN}"
fi
[ -f "${DATA_BIN}" ] || die "data partition ${DATA_BIN} missing — run build.sh (needs the esp8266 core's mklittlefs)"

# --- 2. flash ----------------------------------------------------------------
# Default esptool reset (DTR/RTS) puts the ESP8266 into download mode and
# hard-resets it afterwards — this is exactly what the CH340 wiring provides.
#
# NOTE (issue #54): only 0x0 (firmware) and 0x200000 (LittleFS data partition)
# are written. The WiFi config now lives in the reserved EEPROM flash sector
# 0x3FB000 (see tankie/wificfg.h), which is intentionally NOT erased here —
# so a serial re-flash keeps the saved network (and a failed/corrupt fs-OTA no
# longer strands the ESP on the config-portal AP).
log "flashing ${PORT} @ ${BAUD} baud …"
python3 "${ESPTOOL}" --chip esp8266 --port "${PORT}" --baud "${BAUD}" \
  write_flash 0x0 "${BIN}" 0x200000 "${DATA_BIN}"

# --- 3. boot verification ----------------------------------------------------
log "waiting for the ESP8266 to reboot and reading its console …"
if python3 - "${PORT}" <<'PY'
import sys, time
try:
    import serial
except ImportError:
    sys.exit(3)   # pyserial missing -> skip check, don't fail the flash
port = sys.argv[1]
# Issue #29: the firmware now runs at 921600 baud and emits a
# {"type":"hello",...} JSON line on boot. The old firmware
# (115200, "Battery Voltage" text) is still accepted as a fallback.
s = serial.Serial(port, 921600, timeout=1)
s.reset_input_buffer()
end = time.time() + 12
buf = b""
while time.time() < end:
    d = s.read(1024)
    if d:
        buf += d
        if b'"type":"hello"' in buf or b"Battery Voltage" in buf:
            break
s.close()
ok = (b'"type":"hello"' in buf) or (b"Battery Voltage" in buf)
sys.exit(0 if ok else 1)
PY
then
  log "OK: flash verified — new firmware is up (hello / console active)"
elif python3 -c "import serial" 2>/dev/null; then
  die "flash completed, but no firmware console output was seen — check the ESP power/UART"
else
  warn "flash completed (boot check skipped — pyserial not installed: pip3 install --user pyserial)"
fi
