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
#   GET  /ota/powersave?on=true|false     -> toggle WiFi modem-sleep (issue #133)
#   POST /ota/upload  (multipart "file") -> stream the image in
#
# After each successful upload the ESP auto-reboots (~2 s later) into the new
# image, so this script waits for it to come back online before the next step.
#
# Configuration (flash.conf, shared with flash_serial.sh; env overrides win):
#   OTA_URL                ElegantOTA portal URL (default http://192.168.4.1/update)
#   OTA_UPDATE_FILESYSTEM  also OTA the LittleFS data partition (default true)
#   OTA_UPLOAD_RATE      rate-limit the data-partition upload (default 40k; issue #133)
#   OTA_RETRY_COUNT      data-partition upload attempts before failing (default 3; issue #133)
#   BRIDGE_SOCK          serial-bridge socket (fallback reboot path; issue #133)
#   TANKIE_SERIAL_PORT   ESP UART for the reboot command (default /dev/ttyS0; issue #133)
#   OTA_REBOOT_TIMEOUT     seconds to wait for the device to reboot back
#   OTA_RETRY_COUNT      data-partition upload attempts before failing (default 3, issue #133)
#   BRIDGE_SOCK          serial-bridge socket used to preserve the saved WiFi
#                        across a retry-reboot (default /run/tankie/bridge.sock, issue #133)
#
# Usage:
#   bash flash_ota.sh                    # firmware + (default) data partition
#   OTA_UPDATE_FILESYSTEM=false bash flash_ota.sh   # firmware only
#   OTA_UPLOAD_RATE      rate-limit the data-partition upload (default 40k; issue #133)
#   OTA_RETRY_COUNT      data-partition upload attempts before failing (default 3; issue #133)
#   BRIDGE_SOCK          serial-bridge socket (fallback reboot path; issue #133)
#   TANKIE_SERIAL_PORT   ESP UART for the reboot command (default /dev/ttyS0; issue #133)
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
# $3 (optional) = curl --limit-rate for the upload (e.g. "40k"); empty =
# full speed. Returns non-zero (instead of exiting) so the caller can retry
# - the data-partition upload is retried up to OTA_RETRY_COUNT times
# (issue #133).
ota_flash() {
  local mode="$1" file="$2" rate="${3:-}"
  [ -f "${file}" ] || die "image not found: ${file}"
  local size md5 resp code body
  size="$(stat -c%s "${file}")"
  md5="$(md5sum "${file}" | awk '{print $1}')"
  log "OTA ${mode}: uploading $(basename "${file}") (${size} bytes, md5 ${md5}${rate:+, rate-limited to ${rate}})"

  # Open the flash region (the device validates the MD5 before accepting data).
  resp="$(curl -sS -w $'\n%{http_code}' --max-time 15 \
    "${OTA_BASE}/ota/start?mode=${mode}&hash=${md5}")"
  code="${resp##*$'\n'}"
  body="${resp%$'\n'*}"
  if [ "${code}" != "200" ]; then
    warn "ota/start failed (HTTP ${code}): ${body}"
    return 1
  fi
  log "ota/start OK"

  # Stream the image in. The device verifies the MD5 on completion.
  # issue #133: for the data partition the upload is rate-limited
  # (OTA_UPLOAD_RATE, default 40k) - a full-speed 2 MB burst overloads the
  # ESP8266's async-web receive pbufs (they fill while flash writes are
  # blocking the loop) and the connection drops mid-transfer. Throttling
  # below the flash-write rate keeps the pbufs drained and the upload
  # completes reliably (verified live).
  local rate_args=()
  [ -n "${rate}" ] && rate_args=(--limit-rate "${rate}")
  resp="$(curl -sS -w $'\n%{http_code}' --max-time 600 \
    "${rate_args[@]}" \
    -F "file=@${file};type=application/octet-stream" \
    "${OTA_BASE}/ota/upload")"
  code="${resp##*$'\n'}"
  body="${resp%$'\n'*}"
  if [ "${code}" != "200" ]; then
    warn "ota/upload failed (HTTP ${code}): ${body}"
    return 1
  fi
  log "ota/upload OK — the ESP is rebooting into the new image"

  wait_for_reboot
}

# --- issue #133: data-partition OTA hardening -------------------------------
#
# The sustained ~2 MB flash erase+write of the LittleFS data partition
# DROPS the ESP8266's upload connection mid-transfer in STA mode (at a
# random percent), leaving the core Updater stuck - the next ota/start then
# fails with 400 "ERROR[0]: No Error" until a reboot clears it. A full-speed
# 2 MB burst overloads the ESP8266's async-web receive pbufs (they fill
# while the flash writes block the loop); throttling below the flash-write
# rate keeps the pbufs drained and the upload completes reliably (verified
# live). In AP mode the upload also completes, which is why it "never
# failed there."
#
# Mitigations here:
#   1. /ota/powersave?on=false  - disable WiFi modem-sleep for the upload
#      (firmware route, issue #133); restored on the next reboot.
#   2. rate-limit the fs upload (OTA_UPLOAD_RATE, default 40k) so the
#      receive pbufs never fill.
#   3. retry the fs upload up to OTA_RETRY_COUNT times (default 3),
#      rebooting the device between attempts (direct serial {"cmd":"reboot"},
#      preserving the saved WiFi) to clear the stuck Updater.
# The serial-bridge socket (issue #29) - fallback reboot path (the daemon
# accepts plain commands). Override with BRIDGE_SOCK.
BRIDGE_SOCK="${BRIDGE_SOCK:-/run/tankie/bridge.sock}"
# How many times to attempt the data-partition upload before failing.
OTA_RETRY_COUNT="${OTA_RETRY_COUNT:-3}"
# Upload rate limit for the data-partition upload (issue #133). A full-speed
# 2 MB burst overloads the ESP8266 async-web receive pbufs; throttling
# below the flash-write rate keeps them drained. Default 40k (40 KB/s).
# Set to "0" or empty for full speed.
OTA_UPLOAD_RATE="${OTA_UPLOAD_RATE:-40k}"

# Disable (or re-enable) WiFi modem-sleep on the device. A no-op (warning)
# if the running firmware predates the /ota/powersave route.
ota_powersave() {
  local on="$1"
  local resp code
  resp="$(curl -sS -w $'\n%{http_code}' --max-time 5 \
    "${OTA_BASE}/ota/powersave?on=${on}" 2>/dev/null)" || true
  code="${resp##*$'\n'}"
  if [ "${code}" = "200" ]; then
    log "modem-sleep ${on} (WiFi power save ${on})"
  else
    warn "could not set modem-sleep (HTTP ${code:-n/a}) - the firmware may predate the /ota/powersave route (issue #133); continuing"
  fi
}


# Reboot the device to clear the stuck core Updater (issue #133) while
# PRESERVING the saved WiFi. Primary path: a direct serial reboot
# ({"cmd":"reboot"} to the ESP's UART) - the ESP reboots and reconnects to
# its saved network, so nothing is lost. This works even while the serial
# bridge daemon holds the port (the ESP's RX line is shared). Fallback: the
# serial-bridge socket ({"cmd":"reboot"}), then a plain :8080 config POST.
# The serial port is the Pi native UART0 wired to the ESP (issue #29);
# override with TANKIE_SERIAL_PORT.
TANKIE_SERIAL_PORT="${TANKIE_SERIAL_PORT:-/dev/ttyS0}"

reboot_device_preserve_wifi() {
  log "rebooting the device to clear the stuck Updater (saved WiFi preserved) …"
  local rebooted=false

  # 1) Direct serial reboot (preferred - works while the bridge holds the port).
  if [ -e "${TANKIE_SERIAL_PORT}" ]; then
    if printf '{"cmd":"reboot"}\n' > "${TANKIE_SERIAL_PORT}" 2>/dev/null; then
      log "reboot command sent to ${TANKIE_SERIAL_PORT}"
      rebooted=true
    fi
  fi

  # 2) Fallback: the serial-bridge socket.
  if [ "${rebooted}" = "false" ] && [ -S "${BRIDGE_SOCK}" ]; then
    if python3 - "${BRIDGE_SOCK}" <<'PY' 2>/dev/null
import json, socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(5); s.connect(sys.argv[1])
s.sendall(b'{"cmd":"reboot"}\n')
try: s.recv(4096)
except Exception: pass
PY
    then
      log "reboot command sent via the bridge socket"
      rebooted=true
    fi
  fi

  if [ "${rebooted}" = "false" ]; then
    warn "could not send a reboot (no ${TANKIE_SERIAL_PORT}, no bridge socket) - waiting for the device to recover on its own"
  fi

  wait_for_reboot
  log "device rebooted - the stuck Updater is cleared"
}

# --- 2. OTA ------------------------------------------------------------------
preflight_device

# issue #133: warn early if neither the ESP UART nor the bridge socket
# is available - a failed data-partition upload then cannot be retried
# (the retry-reboot has no way to clear the stuck Updater).
if [ ! -e "${TANKIE_SERIAL_PORT}" ] && [ ! -S "${BRIDGE_SOCK}" ]; then
  warn "no ${TANKIE_SERIAL_PORT} and no bridge socket ${BRIDGE_SOCK} - if a data-partition upload fails, the retry-reboot cannot clear the stuck Updater"
fi

# Firmware region: already flashes reliably (issue #133 - the failure is
# specific to the sustained data-partition write).
ota_flash "fr" "${BIN}"

if [ "${OTA_UPDATE_FILESYSTEM}" = "true" ]; then
  if [ -f "${DATA_BIN}" ]; then
    # issue #133: the sustained ~2 MB flash erase+write RESETS the ESP8266
    # mid-upload in STA mode (modem-sleep beacon-poll vs. the flash write),
    # leaving the core Updater stuck. Disable modem-sleep for the upload
    # and retry the fs upload up to OTA_RETRY_COUNT times, rebooting the
    # device (preserving the saved WiFi) between attempts to clear the
    # stuck Updater.
    ota_powersave false
    attempt=1
    fs_done=false
    while [ "${attempt}" -le "${OTA_RETRY_COUNT}" ]; do
      log "data-partition upload, attempt ${attempt}/${OTA_RETRY_COUNT}"
      if ota_flash "fs" "${DATA_BIN}" "${OTA_UPLOAD_RATE}"; then
        fs_done=true
        break
      fi
      warn "data-partition upload failed (attempt ${attempt}/${OTA_RETRY_COUNT})"
      if [ "${attempt}" -lt "${OTA_RETRY_COUNT}" ]; then
        reboot_device_preserve_wifi
      fi
      attempt=$((attempt + 1))
    done
    if [ "${fs_done}" = "true" ]; then
      log "data partition flashed OK"
    else
      ota_powersave true
      die "data-partition OTA failed after ${OTA_RETRY_COUNT} attempts (issue #133) - the ESP may be on the config AP (tankie-esp, 192.168.4.1) or need a power cycle; the firmware region is up to date"
    fi
  else
    warn "skipping the filesystem OTA (no ${DATA_BIN})"
  fi
else
  log "filesystem OTA disabled (OTA_UPDATE_FILESYSTEM=${OTA_UPDATE_FILESYSTEM})"
fi

log "OK: OTA flash complete — the ESP8266 is running the new firmware"
