#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Tankie — build the ESP8266 (NodeMCU D1 Mini) firmware
#
# Standalone build helper: compiles the `tankie` sketch with arduino-cli and
# exports the merged image (bootloader + app) that `flash.sh` writes to the
# chip. `flash.sh` calls this script for its build step, so the two always
# agree on FQBN, flags, and output location.
#
# Usage:
#   bash build.sh                    # sketch defaults to <repo>/tankie
#   TANKIE_SKETCH_DIR=/path/to/sketch bash build.sh
#   TANKIE_FLASH_DIR=/custom/out bash build.sh
#
# Output:
#   ${TANKIE_FLASH_DIR:-/tmp/tankie-flash}/tankie.ino.bin
#
# Requirements (one-time, see raspberry_pi/README.md "Flashing the ESP8266"):
#   * arduino-cli with the esp8266 core:  arduino-cli core install esp8266:esp8266
#
# Installed by setup.sh as: tankie-build
# ---------------------------------------------------------------------------
set -euo pipefail

# --- configuration ----------------------------------------------------------
# ElegantOTA + the core's WebServer both define an HTTP_GET enum -> clash.
# This single global flag is the library's documented toggle and fixes it.
FQBN="esp8266:esp8266:d1_mini"
EXTRA_FLAGS="-DELEGANTOTA_USE_ASYNC_WEBSERVER=1"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# System-wide overrides (written by setup.sh on the tank)
if [ -f /etc/tankie/flash.env ]; then
  # shellcheck disable=SC1091
  . /etc/tankie/flash.env
fi

SKETCH_DIR="${TANKIE_SKETCH_DIR:-${REPO_ROOT}/tankie}"
OUT_DIR="${TANKIE_FLASH_DIR:-/tmp/tankie-flash}"   # stable dir -> flash.sh finds the artifact
BIN="${OUT_DIR}/tankie.ino.bin"

log()  { printf '\033[1;32m[build]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[build]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[build]\033[0m %s\n' "$*" >&2; exit 1; }

# --- preflight ---------------------------------------------------------------
[ -f "${SKETCH_DIR}/tankie.ino" ] || die "sketch not found: ${SKETCH_DIR}/tankie.ino (set TANKIE_SKETCH_DIR or /etc/tankie/flash.env)"
command -v arduino-cli >/dev/null 2>&1 || die "arduino-cli not found in PATH"

# The esp8266 core provides the toolchain (and esptool for flash.sh)
if ! arduino-cli core list 2>/dev/null | awk '{print $1}' | grep -qx "esp8266:esp8266"; then
  die "esp8266 core not installed — run: arduino-cli core install esp8266:esp8266"
fi

# --- build -------------------------------------------------------------------
log "building ${FQBN} (extra flags: ${EXTRA_FLAGS}) …"
log "sketch: ${SKETCH_DIR}"
mkdir -p "${OUT_DIR}"
arduino-cli compile --fqbn "${FQBN}" \
  --build-property "build.extra_flags=${EXTRA_FLAGS}" \
  --export-binaries --output-dir "${OUT_DIR}" "${SKETCH_DIR}"
[ -f "${BIN}" ] || die "build did not produce ${BIN}"
log "built $(stat -c%s "${BIN}") bytes -> ${BIN}"
