#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Tankie — test an OTA firmware update from the tank Pi (issue #44 D)
#
# OTA flashing needs the ESP on a reachable network. On the tank the cleanest
# way is the ESP's own fallback AP ("tankie-esp", 192.168.4.1) — but the
# Pi's only uplink is wlan0, so joining the AP drops the infrastructure WiFi
# (and any SSH session) until we switch back. This script does the whole dance
# and ALWAYS restores the original connection, even on failure:
#
#   0. preflight: a ROOT `iwlist wlan0 scan` to confirm the tankie-esp AP is
#      actually visible (a non-root scan only shows the connected AP), so a
#      missing AP is caught BEFORE we lose our SSH uplink
#   1. remember the currently active WiFi connection
#   2. connect wlan0 to the "tankie-esp" AP (SSID/password from flash.conf)
#   3. start capturing the ESP's serial console (/dev/ttyUSB0 @ 921600) so the
#      boot + OTA activity is logged for evidence
#   4. verify the firmware's web endpoints (tank page + assets, the ElegantOTA
#      portal + the 4.x /ota/metadata API, and the :8080 config portal) BEFORE
#      the OTA — a 404 on /ota/metadata here means the firmware is still the
#      old 3.x library and the OTA would fail, so we catch it early
#   5. run flash_ota.sh against http://192.168.4.1/update and capture the log
#   6. verify the same endpoints AGAIN after the OTA reboot (proves the new
#      firmware came back and is serving)
#   7. stop the serial capture; (trap) restore the original WiFi connection
#
# Because step 2 kills the SSH link, run it in the background so it survives:
#
#   nohup bash tests/manual/flash_ota_test.sh > /tmp/ota_test.out 2>&1 &
#   # ... wait, then reconnect to the Pi over infrastructure WiFi and:
#   tail -f /tmp/ota_test.out
#   cat /tmp/tankie-ota-test.log
#   cat /tmp/tankie-ota-serial.log
#
# The script is single-instance (lock file) and idempotent. `--dry-run`
# validates the config/dependencies without touching the network (safe to run
# anywhere, e.g. on a build box).
#
# Config (flash.conf): OTA_AP_SSID, OTA_AP_PASSWORD, OTA_URL,
#   FLASH_SERIAL_PORT (console capture port).
# ---------------------------------------------------------------------------
set -euo pipefail

log()  { printf '\033[1;32m[ota-test]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[ota-test]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[ota-test]\033[0m %s\n' "$*" >&2; exit 1; }

DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then
  DRY_RUN=1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

FLASH_DIR="${REPO_ROOT}/raspberry_pi"
FLASH_CONF="${FLASH_CONF:-${FLASH_DIR}/flash.conf}"
[ -f "${FLASH_CONF}" ] || die "flash.conf not found (looked in ${FLASH_CONF} and ${FLASH_DIR}): ${FLASH_CONF}"
# shellcheck disable=SC1091
. "${FLASH_CONF}"
if [ -f /etc/tankie/flash.env ]; then
  # shellcheck disable=SC1091
  . /etc/tankie/flash.env
fi

# The OTA flasher: prefer the canonical copy in raspberry_pi/, fall back to the
# copy setup.sh installs at /usr/local/bin/tankie-flash-ota.
FLASH_OTA="${FLASH_OTA:-${FLASH_DIR}/flash_ota.sh}"
[ -f "${FLASH_OTA}" ] || FLASH_OTA="/usr/local/bin/tankie-flash-ota"
[ -f "${FLASH_OTA}" ] || die "flash_ota.sh not found (looked in ${FLASH_DIR} and /usr/local/bin)"

# Derive the OTA API base (scheme://host[:port]) from the portal URL, the same
# way flash_ota.sh does, so the endpoint checks hit the right host.
case "${OTA_URL}" in
  *://*/*) OTA_BASE="${OTA_URL%/*}" ;;
  *)       OTA_BASE="${OTA_URL}" ;;
esac

LOG_FILE="${TANKIE_OTA_TEST_LOG:-/tmp/tankie-ota-test.log}"
SERIAL_LOG="${TANKIE_OTA_SERIAL_LOG:-/tmp/tankie-ota-serial.log}"
LOCK_FILE="/tmp/tankie-ota-test.lock"
AP_WAIT_TIMEOUT="${AP_WAIT_TIMEOUT:-45}"
# The ESP streams its console on UART0 at the serial-proto baud (config_serial.h).
SERIAL_PORT="${FLASH_SERIAL_PORT:-/dev/ttyUSB0}"
SERIAL_BAUD="${OTA_SERIAL_BAUD:-921600}"
SERIAL_CAPTURE_PID=""

# --- dry-run: validate only, touch no network -------------------------------
if [ "${DRY_RUN}" = "1" ]; then
  log "dry-run: validating configuration and dependencies (no network changes)"
  command -v nmcli    >/dev/null 2>&1 || warn "nmcli not found (needed for the real run)"
  command -v curl     >/dev/null 2>&1 || warn "curl not found (needed for the endpoint checks + flash_ota.sh)"
  command -v md5sum   >/dev/null 2>&1 || warn "md5sum not found (needed by flash_ota.sh)"
  command -v stty     >/dev/null 2>&1 || warn "stty not found (needed for the serial console capture)"
  command -v cat      >/dev/null 2>&1 || warn "cat not found (needed for the serial console capture)"
  command -v iwlist   >/dev/null 2>&1 || warn "iwlist not found (needed for the root AP scan)"
  command -v sudo     >/dev/null 2>&1 || warn "sudo not found (needed for the root AP scan)"
  [ -e "${SERIAL_PORT}" ] || warn "serial port ${SERIAL_PORT} not present (console capture will be skipped)"
  [ -f "${FLASH_OTA}" ] || die "flash_ota.sh not found: ${FLASH_OTA}"
  log "dry-run OK: OTA_URL=${OTA_URL}  AP_SSID=${OTA_AP_SSID}  OTA_UPDATE_FILESYSTEM=${OTA_UPDATE_FILESYSTEM}"
  log "dry-run OK: serial capture ${SERIAL_PORT}@${SERIAL_BAUD} -> ${SERIAL_LOG}"
  exit 0
fi

# --- single instance ---------------------------------------------------------
if [ -e "${LOCK_FILE}" ]; then
  die "another OTA test appears to be running (lock ${LOCK_FILE} present); remove it if it is stale"
fi
echo "$$" > "${LOCK_FILE}"

# --- helpers -----------------------------------------------------------------

# Confirm the tankie-esp AP is visible with a ROOT scan. a-i-a-d reports a
# non-root `iwlist wlan0 scan` only shows the currently-connected AP; a root
# scan sees all of them. Run this BEFORE dropping the infrastructure WiFi so a
# missing AP is caught while we still have our SSH uplink. Non-fatal: the real
# test is the join step below.
root_ap_scan() {
  log "scanning for WiFi APs as root (sudo iwlist wlan0 scan) …"
  local out ssids
  if ! out="$(sudo -n iwlist wlan0 scan 2>/dev/null)"; then
    warn "root iwlist scan failed — cannot confirm the AP is visible; continuing"
    return 0
  fi
  ssids="$(printf '%s\n' "${out}" | grep -oE 'ESSID:"[^"]*"' | sed 's/ESSID:"//; s/"$//' | sort -u)"
  if [ -z "${ssids}" ]; then
    warn "root scan returned no SSIDs — continuing"
    return 0
  fi
  log "APs visible (root scan): $(printf '%s' "${ssids}" | tr '\n' ' ')"
  if printf '%s\n' "${ssids}" | grep -qx "${OTA_AP_SSID}"; then
    log "confirmed '${OTA_AP_SSID}' is visible — good"
  else
    warn "'${OTA_AP_SSID}' NOT in the root scan — the join step may fail (is the ESP on the AP?)"
  fi
  return 0
}

# Verify the firmware's web stack is serving. Covers: the tank control page and
# its assets (port 80), the ElegantOTA portal + the 4.x /ota/metadata API
# (port 80), and the WiFi config portal (port 8080). A 404 on /ota/metadata
# means the firmware is still the old 3.x library and the OTA would fail.
verify_endpoints() {
  local label="$1"
  local ep code ok=0 fail=0
  log "${label}: verifying endpoints at ${OTA_BASE} …"
  for ep in "/" "/style.css" "/script.js" "/joy.js" "/tankie.png" "/ota/metadata" "/update"; do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "${OTA_BASE}${ep}" 2>/dev/null)" || code="000"
    if [ "${code}" = "200" ]; then
      ok=$((ok+1)); log "  [OK]   ${ep} -> ${code}"
    else
      fail=$((fail+1)); warn "  [FAIL] ${ep} -> ${code} (expected 200)"
    fi
  done
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "${OTA_BASE}:8080/" 2>/dev/null)" || code="000"
  if [ "${code}" = "200" ]; then
    ok=$((ok+1)); log "  [OK]   :8080/ (config portal) -> ${code}"
  else
    fail=$((fail+1)); warn "  [FAIL] :8080/ (config portal) -> ${code} (expected 200)"
  fi
  log "${label}: ${ok} OK, ${fail} failed"
  [ "${fail}" -eq 0 ]
}

# Capture the ESP's serial console (UART0 @ 921600) to a log file for evidence
# of the boot + OTA activity. Non-fatal: a missing port just skips the capture.
start_serial_capture() {
  if [ ! -e "${SERIAL_PORT}" ]; then
    warn "serial port ${SERIAL_PORT} not present — skipping the console capture"
    return 0
  fi
  log "capturing the ESP console on ${SERIAL_PORT} @ ${SERIAL_BAUD} -> ${SERIAL_LOG}"
  # raw, no echo; -hupcl avoids dropping DTR (and resetting the ESP) on close.
  stty -F "${SERIAL_PORT}" "${SERIAL_BAUD}" raw -echo -hupcl 2>/dev/null || true
  : > "${SERIAL_LOG}"
  cat "${SERIAL_PORT}" >> "${SERIAL_LOG}" 2>/dev/null &
  SERIAL_CAPTURE_PID=$!
  log "serial capture started (pid ${SERIAL_CAPTURE_PID})"
}

stop_serial_capture() {
  if [ -n "${SERIAL_CAPTURE_PID}" ] && kill -0 "${SERIAL_CAPTURE_PID}" 2>/dev/null; then
    kill "${SERIAL_CAPTURE_PID}" 2>/dev/null || true
    wait "${SERIAL_CAPTURE_PID}" 2>/dev/null || true
    log "serial capture stopped (pid ${SERIAL_CAPTURE_PID})"
  fi
  SERIAL_CAPTURE_PID=""
}

# --- remember the current connection (for the restore trap) ------------------
RESTORE_CONN=""
ACTIVE_CONN="$(nmcli -t -f NAME,ACTIVE connection show 2>/dev/null | awk -F: '$2=="yes"{print $1}' | head -1 || true)"
ACTIVE_IFACE="$(nmcli -t -f DEVICE,CONNECTION device status 2>/dev/null | awk -F: '$1=="wifi"{print $2}' | head -1 || true)"
if [ -n "${ACTIVE_CONN}" ]; then
  RESTORE_CONN="${ACTIVE_CONN}"
fi
log "current active WiFi connection: '${RESTORE_CONN:-<none>}' (device: '${ACTIVE_IFACE:-<none>}')"

restore_wifi() {
  local rc=$?
  stop_serial_capture
  rm -f "${LOCK_FILE}"
  log "restoring the original WiFi connection …"
  if [ -n "${RESTORE_CONN}" ]; then
    nmcli connection up id "${RESTORE_CONN}" || warn "could not restore '${RESTORE_CONN}' — reconnect manually"
  else
    # Nothing was active before: just leave the AP up (or up any default).
    nmcli connection up 2>/dev/null || true
  fi
  if [ "${rc}" -ne 0 ]; then
    warn "flash_ota.sh exited non-zero (${rc}) — the WiFi has been restored, see ${LOG_FILE}"
  fi
}
trap restore_wifi EXIT

# --- 0. preflight: confirm the AP is visible (root scan) ---------------------
root_ap_scan

# --- 1. join the tankie-esp AP ----------------------------------------------
# nmcli needs NetworkManager D-Bus permissions to change state. On the tank Pi
# the user lacks them directly but has passwordless sudo (sudo -n), so fall back
# to sudo when the direct call is denied. (The root AP scan above already proves
# sudo -n works; on a box without sudo the direct call is the only option.)
nmcli_wifi() {
  nmcli "$@" || {
    command -v sudo >/dev/null 2>&1 && sudo -n nmcli "$@"
  }
}

log "connecting wlan0 to the '${OTA_AP_SSID}' AP …"
# Join the AP directly: nmcli switches the device to it and deactivates the
# infrastructure connection automatically. (Deactivating the infrastructure
# connection *first* can race the radio and make the first DHCP lease time out
# with 'IP configuration could not be reserved' — observed on the tank Pi.)
join_ap() {
  if [ -n "${OTA_AP_PASSWORD}" ]; then
    nmcli_wifi device wifi connect "${OTA_AP_SSID}" password "${OTA_AP_PASSWORD}"
  else
    nmcli_wifi device wifi connect "${OTA_AP_SSID}"
  fi
}

# Wait for the AP to hand us an address; retry the join a couple of times if
# the first DHCP lease times out while the radio settles.
joined=0
for attempt in 1 2 3; do
  log "AP join attempt ${attempt} …"
  join_ap || warn "AP join attempt ${attempt} reported an error — retrying"
  deadline=$(( $(date +%s) + AP_WAIT_TIMEOUT ))
  while [ "$(date +%s)" -lt "${deadline}" ]; do
    if ip -4 addr show wlan0 2>/dev/null | grep -q "inet 192\.168\.4\."; then
      joined=1
      break
    fi
    sleep 1
  done
  [ "${joined}" = "1" ] && break
done
[ "${joined}" = "1" ] \
  || die "no 192.168.4.x address on wlan0 after ${AP_WAIT_TIMEOUT}s — check the AP/SSID"
log "on the AP with IP $(ip -4 -o addr show wlan0 2>/dev/null | awk '{print $4}')"

# --- 2. start the serial console capture -------------------------------------
start_serial_capture

# --- 3. verify the endpoints BEFORE the OTA (catch a stale 3.x fw early) -----
if verify_endpoints "before OTA"; then
  log "pre-OTA endpoint check PASSED (firmware is serving, /ota/metadata present)"
else
  warn "pre-OTA endpoint check FAILED — the OTA will likely fail; continuing to see the error"
fi

# --- 4. run the OTA flash (this is what we are testing) ----------------------
log "running flash_ota.sh (log -> ${LOG_FILE}) …"
set +e
bash "${FLASH_OTA}" 2>&1 | tee "${LOG_FILE}"
FLASH_RC=${PIPESTATUS[0]}
set -e
if [ "${FLASH_RC}" -eq 0 ]; then
  log "OTA flash PASSED — see ${LOG_FILE}"
else
  warn "OTA flash FAILED (exit ${FLASH_RC}) — see ${LOG_FILE}"
fi

# --- 5. verify the endpoints AFTER the OTA reboot ----------------------------
# Give the rebooted firmware a moment to come back before re-checking.
sleep 3
if verify_endpoints "after OTA"; then
  log "post-OTA endpoint check PASSED (new firmware is up and serving)"
else
  warn "post-OTA endpoint check FAILED — the new firmware may not be serving"
fi

# --- 6. stop the serial capture ----------------------------------------------
stop_serial_capture

if [ "${FLASH_RC}" -eq 0 ]; then
  log "OTA test PASSED — see ${LOG_FILE} (flash) + ${SERIAL_LOG} (console)"
else
  warn "OTA test FAILED (exit ${FLASH_RC}) — see ${LOG_FILE} + ${SERIAL_LOG}"
fi
exit "${FLASH_RC}"
