#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Tankie — build the ESP8266 (NodeMCU D1 Mini) firmware
#
# Standalone build helper: compiles the `tankie` sketch with arduino-cli and
# exports the merged image (bootloader + app) that `flash_serial.sh` writes to
# the chip, AND builds the LittleFS data partition image (the web UI in
# `tankie/data/`) that the firmware serves over HTTP. `flash_serial.sh` and
# `flash_ota.sh` call this
# script for its build step, so the two always agree on FQBN, flags, and
# output location.
#
# Usage:
#   bash build.sh                    # sketch defaults to <repo>/tankie
#   TANKIE_SKETCH_DIR=/path/to/sketch bash build.sh
#   TANKIE_FLASH_DIR=/custom/out bash build.sh
#   ARDUINO_JOBS=<n> bash build.sh     # override parallel compile jobs (0 = auto)
#
# Output (both land in the same dir):
#   ${TANKIE_FLASH_DIR:-/tmp/tankie-flash}/tankie.ino.bin        (firmware)
#   ${TANKIE_FLASH_DIR:-/tmp/tankie-flash}/tankie.ino.data.bin   (LittleFS)
#
# Requirements (one-time, see raspberry_pi/README.md "Flashing the ESP8266"):
#   * arduino-cli with the esp8266 core:  arduino-cli core install esp8266:esp8266
#     (the core also ships the `mklittlefs` tool used for the data partition)
#
# Installed by setup.sh as: tankie-build
# ---------------------------------------------------------------------------
set -euo pipefail

# --- configuration ----------------------------------------------------------
# ElegantOTA + the core's WebServer both define an HTTP_GET enum -> clash.
# This single global flag is the library's documented toggle and fixes it.
FQBN="esp8266:esp8266:d1_mini"
EXTRA_FLAGS="-DELEGANTOTA_USE_ASYNC_WEBSERVER=1"

# Parallel jobs for arduino-cli compile (0 = auto: one job per CPU core).
#
# Low-RAM boxes (e.g. a Pi Zero 2 W with 416 MB) can crash under the default
# parallel build (observed on PR #26) — cap the job count to what the
# *effective* memory allows. Override with ARDUINO_JOBS=<n> if needed.
#
# IMPORTANT (issue #54 / OOM): the effective limit must come from the
# cgroup, not /proc/meminfo. Inside a container /proc/meminfo reports the
# HOST's RAM (e.g. 64 GB), so a naive "auto = all cores" build launches one
# xtensa compiler per core and OOMs a 4 GB container. Read the cgroup memory
# limit first (v2 memory.max, then v1 memory.limit_in_bytes), falling back to
# /proc/meminfo only when no cgroup limit is set.
#
# Per-job budget: a single xtensa C++ compile (cc1plus) can use ~100-300 MB,
# and arduino-cli also runs the assembler/linker concurrently. Reserve 512 MB
# per job so the total stays well inside the container limit.
PER_JOB_KB=524288
MIN_JOBS=1
MAX_JOBS=32

# Effective memory in KB: cgroup v2 -> cgroup v1 -> /proc/meminfo.
effective_mem_kb() {
  local kb=""
  # cgroup v2
  if [ -r /sys/fs/cgroup/memory.max ]; then
    kb="$(cat /sys/fs/cgroup/memory.max 2>/dev/null || true)"
  fi
  # cgroup v1 (unlimited is reported as a huge value ~ 9.2e18)
  if { [ -z "${kb}" ] || [ "${kb}" = "max" ]; } && [ -r /sys/fs/cgroup/memory/memory.limit_in_bytes ]; then
    local v1
    v1="$(cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null || true)"
    if [ -n "${v1}" ] && [ "${v1}" -lt 1000000000000 ] 2>/dev/null; then
      kb="${v1}"
    fi
  fi
  # bytes -> KB
  if [ -n "${kb}" ] && [ "${kb}" != "max" ] && [ "${kb}" -gt 0 ] 2>/dev/null; then
    echo $(( kb / 1024 ))
    return
  fi
  # fall back to /proc/meminfo (bare metal / no cgroup limit)
  awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null
}

JOBS="${ARDUINO_JOBS:-0}"
if [ "${JOBS}" -eq 0 ]; then
  MEM_KB="$(effective_mem_kb)"
  MEM_KB="${MEM_KB:-0}"
  if [ "${MEM_KB}" -gt 0 ]; then
    JOBS=$(( MEM_KB / PER_JOB_KB ))
    [ "${JOBS}" -lt "${MIN_JOBS}" ] && JOBS="${MIN_JOBS}"
    [ "${JOBS}" -gt "${MAX_JOBS}" ] && JOBS="${MAX_JOBS}"
  fi
  # Also never exceed the number of online CPU cores.
  NCPU="$(nproc 2>/dev/null || echo 1)"
  [ "${JOBS}" -gt "${NCPU}" ] && JOBS="${NCPU}"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# System-wide overrides (written by setup.sh on the tank)
if [ -f /etc/tankie/flash.env ]; then
  # shellcheck disable=SC1091
  . /etc/tankie/flash.env
fi

SKETCH_DIR="${TANKIE_SKETCH_DIR:-${REPO_ROOT}/tankie}"
OUT_DIR="${TANKIE_FLASH_DIR:-/tmp/tankie-flash}"   # stable dir -> flash_serial.sh / flash_ota.sh find the artifact
BIN="${OUT_DIR}/tankie.ino.bin"
DATA_BIN="${OUT_DIR}/tankie.ino.data.bin"
DATA_DIR="${SKETCH_DIR}/data"

log()  { printf '\033[1;32m[build]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[build]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[build]\033[0m %s\n' "$*" >&2; exit 1; }

# --- preflight ---------------------------------------------------------------
[ -f "${SKETCH_DIR}/tankie.ino" ] || die "sketch not found: ${SKETCH_DIR}/tankie.ino (set TANKIE_SKETCH_DIR or /etc/tankie/flash.env)"
command -v arduino-cli >/dev/null 2>&1 || die "arduino-cli not found in PATH"

# The esp8266 core provides the toolchain (and esptool for flash_serial.sh)
if ! arduino-cli core list 2>/dev/null | awk '{print $1}' | grep -qx "esp8266:esp8266"; then
  die "esp8266 core not installed — run: arduino-cli core install esp8266:esp8266"
fi

# The esp8266 core also ships mklittlefs (used to build the data partition).
MKLITTLEFS="$(ls -d "${HOME}"/.arduino15/packages/esp8266/tools/mklittlefs/*/mklittlefs 2>/dev/null | sort -V | tail -1 || true)"
HAVE_MKLITTLEFS=0
[ -n "${MKLITTLEFS}" ] && [ -x "${MKLITTLEFS}" ] && HAVE_MKLITTLEFS=1

# --- build (firmware) --------------------------------------------------------
log "building ${FQBN} (extra flags: ${EXTRA_FLAGS}, jobs: ${JOBS}) …"
log "sketch: ${SKETCH_DIR}"
mkdir -p "${OUT_DIR}"
arduino-cli compile --fqbn "${FQBN}" \
  --jobs "${JOBS}" \
  --build-property "build.extra_flags=${EXTRA_FLAGS}" \
  --export-binaries --output-dir "${OUT_DIR}" "${SKETCH_DIR}"
[ -f "${BIN}" ] || die "build did not produce ${BIN}"
log "built $(stat -c%s "${BIN}") bytes -> ${BIN}"

# --- build (LittleFS data partition) -----------------------------------------
# The firmware serves its web UI (tankie/data/) from a LittleFS partition.
# The D1 Mini's default flash layout (4M, FS:2MB) puts the FS partition at
# 0x200000, 2072576 bytes, page 256 / block 8192 — the values the core's
# linker script bakes into the firmware (see local.eagle.flash.ld.h). We bake
# the same values into the image so the on-chip FS and the image agree.
if [ ! -d "${DATA_DIR}" ]; then
  warn "no data dir ${DATA_DIR} — skipping the LittleFS data partition"
elif [ "${HAVE_MKLITTLEFS}" -ne 1 ]; then
  warn "mklittlefs not found in the esp8266 core — skipping the data partition"
  warn "       (re-run: arduino-cli core install esp8266:esp8266)"
else
  FS_PAGE=256
  FS_BLOCK=8192
  FS_SIZE=2072576   # 0x3FA000 - 0x200000  (D1 Mini 4M / FS:2MB default layout)
  log "building LittleFS data partition (page ${FS_PAGE}, block ${FS_BLOCK}, size ${FS_SIZE}) …"
  "${MKLITTLEFS}" -c "${DATA_DIR}" \
    -p "${FS_PAGE}" -b "${FS_BLOCK}" -s "${FS_SIZE}" \
    "${DATA_BIN}"
  [ -f "${DATA_BIN}" ] || die "data partition build did not produce ${DATA_BIN}"
  log "built data partition $(stat -c%s "${DATA_BIN}") bytes -> ${DATA_BIN}"
fi
