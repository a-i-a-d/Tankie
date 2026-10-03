#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Tankie — flash the ESP8266 (NodeMCU D1 Mini) over the air (OTA), issue #44 D
#
# Instead of putting the chip into USB download mode (which the TB6612FNG's
# boot-strapping pins can fight — see issue #44), this pushes the firmware
# (and, by default, the LittleFS data partition) over WiFi using the
# ElegantOTA HTTP API already built into the firmware:
#
#   GET  /ota/metadata                 -> device info (hw, flash sizes)
#   GET  /ota/start?mode=fr|fs&hash=.. -> open the flash region (MD5-checked)
#   POST /ota/upload  (multipart "file") -> stream the image in
#
# After each successful upload the ESP auto-reboots (~2 s later) into the new
# image, so this script waits for it to come back online before the next step.
#
# Configuration (flash.conf, shared with flash_serial.sh; env overrides win):
#   OTA_URL                ElegantOTA portal URL (default http://192.168.4.1/update)
#   OTA_UPDATE_FILESYSTEM  also OTA the LittleFS data partition (default true)
#   OTA_REBOOT_TIMEOUT     seconds to wait for the device to reboot back
#
# Usage:
#   bash flash_ota.sh                    # firmware + (default) data partition
#   OTA_UPDATE_FILESYSTEM=false bash flash_ota.sh   # firmware only
#   OTA_URL=http://192.168.4.1/update bash flash_ota.sh
#
# The ESP must be reachable at OTA_URL — either on the saved WiFi (STA mode,
# use its DHCP IP) or on the "tankie-esp" config AP (192.168.4.1). On the
# tank Pi use tests/manual/flash_ota_test.sh to hop onto the AP, run this, and restore the
# infrastructure WiFi automatically.
#
# Requirements: curl + md5sum (present on the Pi), arduino-cli + esp8266 core
# for the build step (see build.sh).
# ---------------------------------------------------------------------------
set -euo pipefail

log()  { printf '\033[1;32m[ota]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[ota]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[ota]\033[0m %s\n' "$*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# --- configuration ----------------------------------------------------------
# Defaults live in flash.conf (issue #44 D, shared with flash_serial.sh). A
# value already set in the environment wins over the flash.conf default, and
# the system-wide /etc/tankie/flash.env (written by setup.sh) wins over both.
FLASH_CONF="${SCRIPT_DIR}/conf/flash.conf"
[ -f "${FLASH_CONF}" ] || die "flash.conf not found in conf/ next to flash_ota.sh: ${FLASH_CONF}"
# shellcheck disable=SC1091
. "${FLASH_CONF}"
if [ -f /etc/tankie/flash.env ]; then
  # shellcheck disable=SC1091
  . /etc/tankie/flash.env
fi

# Derive the OTA API base (scheme://host[:port]) from the portal URL.
# Strip a trailing path (e.g. "/update"); a bare host (no path) is used as-is.
case "${OTA_URL}" in
  *://*/*) OTA_BASE="${OTA_URL%/*}" ;;
  *)       OTA_BASE="${OTA_URL}" ;;
esac

SKETCH_DIR="${TANKIE_SKETCH_DIR:-${REPO_ROOT}/tankie}"
OUT_DIR="${TANKIE_FLASH_DIR:-/tmp/tankie-flash}"   # stable dir -> shared with build.sh
BIN="${OUT_DIR}/tankie.ino.bin"
DATA_BIN="${OUT_DIR}/tankie.ino.data.bin"
BUILD_SH="${SCRIPT_DIR}/build.sh"

# --- preflight ---------------------------------------------------------------
command -v curl    >/dev/null 2>&1 || die "curl not found in PATH"
command -v md5sum  >/dev/null 2>&1 || die "md5sum not found in PATH"
command -v arduino-cli >/dev/null 2>&1 || die "arduino-cli not found in PATH"
[ -f "${BUILD_SH}" ] || die "build.sh not found next to flash_ota.sh: ${BUILD_SH}"
[ -f "${SKETCH_DIR}/tankie.ino" ] || die "sketch not found: ${SKETCH_DIR}/tankie.ino"

# --- 1. build (skip if the binaries are newer than every sketch source) -----
LATEST_SOURCE="$(ls -t "${SKETCH_DIR}"/*.ino "${SKETCH_DIR}"/*.h "${SKETCH_DIR}"/*.cpp "${SKETCH_DIR}"/data/* 2>/dev/null | head -1)"
if [ -f "${BIN}" ] && [ -n "${LATEST_SOURCE}" ] && [ ! "${LATEST_SOURCE}" -nt "${BIN}" ]; then
  log "reusing existing build ${BIN} (newer than sources)"
else
  bash "${BUILD_SH}"
  [ -f "${BIN}" ] || die "build did not produce ${BIN}"
fi
[ -f "${DATA_BIN}" ] || warn "data partition ${DATA_BIN} missing — the filesystem OTA step will be skipped"

# --- helpers -----------------------------------------------------------------

# Confirm the device is reachable and report what it says about itself.
preflight_device() {
  log "checking the device at ${OTA_BASE} …"
  local meta
  meta="$(curl -fsS --max-time 8 "${OTA_BASE}/ota/metadata" 2>/dev/null)" \
    || die "could not reach ${OTA_BASE}/ota/metadata — is the ESP on this network (AP or STA)?"
  log "device metadata: ${meta}"
}

# Wait for the ESP to reboot (go offline) and then come back online. The
# device auto-reboots ~2 s after a successful upload, so we first wait for it
# to drop, then poll until /ota/metadata answers again.
wait_for_reboot() {
  local deadline
  log "waiting for the ESP to reboot …"
  # 1) wait for it to go offline (grace period for the reboot to start).
  deadline=$(( $(date +%s) + 20 ))
  while [ "$(date +%s)" -lt "${deadline}" ]; do
    if ! curl -fsS --max-time 3 "${OTA_BASE}/ota/metadata" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  # 2) wait for it to come back online.
  deadline=$(( $(date +%s) + OTA_REBOOT_TIMEOUT ))
  while [ "$(date +%s)" -lt "${deadline}" ]; do
    if curl -fsS --max-time 5 "${OTA_BASE}/ota/metadata" >/dev/null 2>&1; then
      log "device is back online after the OTA reboot"
      return 0
    fi
    sleep 2
  done
  die "device did not come back online within ${OTA_REBOOT_TIMEOUT}s after the OTA reboot"
}

# Upload one image: mode is "fr" (firmware) or "fs" (filesystem).
ota_flash() {
  local mode="$1" file="$2"
  [ -f "${file}" ] || die "image not found: ${file}"
  local size md5 resp code body
  size="$(stat -c%s "${file}")"
  md5="$(md5sum "${file}" | awk '{print $1}')"
  log "OTA ${mode}: uploading $(basename "${file}") (${size} bytes, md5 ${md5})"

  # Open the flash region (the device validates the MD5 before accepting data).
  resp="$(curl -sS -w $'\n%{http_code}' --max-time 15 \
    "${OTA_BASE}/ota/start?mode=${mode}&hash=${md5}")"
  code="${resp##*$'\n'}"
  body="${resp%$'\n'*}"
  [ "${code}" = "200" ] || die "ota/start failed (HTTP ${code}): ${body}"
  log "ota/start OK"

  # Stream the image in. The device verifies the MD5 on completion.
  resp="$(curl -sS -w $'\n%{http_code}' --max-time 600 \
    -F "file=@${file};type=application/octet-stream" \
    "${OTA_BASE}/ota/upload")"
  code="${resp##*$'\n'}"
  body="${resp%$'\n'*}"
  [ "${code}" = "200" ] || die "ota/upload failed (HTTP ${code}): ${body}"
  log "ota/upload OK — the ESP is rebooting into the new image"

  wait_for_reboot
}

# --- 2. OTA ------------------------------------------------------------------
preflight_device

ota_flash "fr" "${BIN}"

if [ "${OTA_UPDATE_FILESYSTEM}" = "true" ]; then
  if [ -f "${DATA_BIN}" ]; then
    ota_flash "fs" "${DATA_BIN}"
  else
    warn "skipping the filesystem OTA (no ${DATA_BIN})"
  fi
else
  log "filesystem OTA disabled (OTA_UPDATE_FILESYSTEM=${OTA_UPDATE_FILESYSTEM})"
fi

log "OK: OTA flash complete — the ESP8266 is running the new firmware"
