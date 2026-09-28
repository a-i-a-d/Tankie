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
#   bash flash.sh                 # port defaults to /dev/ttyUSB0
#   PORT=/dev/ttyUSB1 bash flash.sh
#
# Requirements (one-time, see raspberry_pi/README.md "Flashing the ESP8266"):
#   * arduino-cli with the esp8266 core:  arduino-cli core install esp8266:esp8266
#   * pyserial (boot check only):         pip3 install --user pyserial
#
# Tracked: https://github.com/a-i-a-d/Tankie/issues/21
# ---------------------------------------------------------------------------
set -euo pipefail

# --- configuration ----------------------------------------------------------
PORT="${PORT:-/dev/ttyUSB0}"
BAUD=115200

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# System-wide overrides (written by setup.sh on the tank)
if [ -f /etc/tankie/flash.env ]; then
  # shellcheck disable=SC1091
  . /etc/tankie/flash.env
fi

SKETCH_DIR="${TANKIE_SKETCH_DIR:-${REPO_ROOT}/tankie}"
OUT_DIR="${TANKIE_FLASH_DIR:-/tmp/tankie-flash}"   # stable dir -> shared with build.sh
BIN="${OUT_DIR}/tankie.ino.bin"
DATA_BIN="${OUT_DIR}/tankie.ino.data.bin"
BUILD_SH="${SCRIPT_DIR}/build.sh"

log()  { printf '\033[1;32m[flash]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[flash]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[flash]\033[0m %s\n' "$*" >&2; exit 1; }

# --- preflight ---------------------------------------------------------------
[ -f "${BUILD_SH}" ] || die "build.sh not found next to flash.sh: ${BUILD_SH}"
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
s = serial.Serial(port, 115200, timeout=1)
s.reset_input_buffer()
end = time.time() + 12
buf = b""
while time.time() < end:
    d = s.read(1024)
    if d:
        buf += d
        if b"Battery Voltage" in buf:
            break
s.close()
sys.exit(0 if b"Battery Voltage" in buf else 1)
PY
then
  log "OK: flash verified — new firmware is up (console active)"
elif python3 -c "import serial" 2>/dev/null; then
  die "flash completed, but no firmware console output was seen — check the ESP power/UART"
else
  warn "flash completed (boot check skipped — pyserial not installed: pip3 install --user pyserial)"
fi
