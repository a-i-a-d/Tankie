#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Tankie — test an OTA firmware update from the tank Pi (issue #44 D)
#
# OTA flashing needs the ESP on a reachable network. On the tank the cleanest
# way is the ESP's own fallback AP ("tankie-esp", 192.168.4.1) — but the Pi's
# only uplink is wlan0, so joining the AP drops the infrastructure WiFi (and
# any SSH session) until we switch back. This script does the whole dance and
# ALWAYS restores the original connection, even on failure:
#
#   1. remember the currently active WiFi connection
#   2. connect wlan0 to the "tankie-esp" AP (SSID/password from flash.conf)
#   3. run flash_ota.sh against http://192.168.4.1/update and capture the log
#   4. (trap) restore the original WiFi connection
#
# Because step 2 kills the SSH link, run it in the background so it survives:
#
#   nohup bash raspberry_pi/flash_ota_test.sh > /tmp/ota_test.out 2>&1 &
#   # ... wait, then reconnect to the Pi over infrastructure WiFi and:
#   tail -f /tmp/ota_test.out
#   cat /tmp/tankie-ota-test.log
#
# The script is single-instance (lock file) and idempotent. `--dry-run`
# validates the config/dependencies without touching the network (safe to run
# anywhere, e.g. on a build box).
#
# Config (flash.conf): OTA_AP_SSID, OTA_AP_PASSWORD, OTA_URL.
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
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

FLASH_CONF="${SCRIPT_DIR}/flash.conf"
[ -f "${FLASH_CONF}" ] || die "flash.conf not found next to flash_ota_test.sh: ${FLASH_CONF}"
# shellcheck disable=SC1091
. "${FLASH_CONF}"
if [ -f /etc/tankie/flash.env ]; then
  # shellcheck disable=SC1091
  . /etc/tankie/flash.env
fi

# The OTA flasher: prefer the one next to this script, fall back to the copy
# setup.sh installs at /usr/local/bin/tankie-flash-ota.
FLASH_OTA="${SCRIPT_DIR}/flash_ota.sh"
[ -f "${FLASH_OTA}" ] || FLASH_OTA="/usr/local/bin/tankie-flash-ota"
[ -f "${FLASH_OTA}" ] || die "flash_ota.sh not found (looked in ${SCRIPT_DIR} and /usr/local/bin)"

LOG_FILE="${TANKIE_OTA_TEST_LOG:-/tmp/tankie-ota-test.log}"
LOCK_FILE="/tmp/tankie-ota-test.lock"
AP_WAIT_TIMEOUT="${AP_WAIT_TIMEOUT:-45}"

# --- dry-run: validate only, touch no network -------------------------------
if [ "${DRY_RUN}" = "1" ]; then
  log "dry-run: validating configuration and dependencies (no network changes)"
  command -v nmcli    >/dev/null 2>&1 || warn "nmcli not found (needed for the real run)"
  command -v curl     >/dev/null 2>&1 || warn "curl not found (needed by flash_ota.sh)"
  command -v md5sum   >/dev/null 2>&1 || warn "md5sum not found (needed by flash_ota.sh)"
  [ -f "${FLASH_OTA}" ] || die "flash_ota.sh not found: ${FLASH_OTA}"
  log "dry-run OK: OTA_URL=${OTA_URL}  AP_SSID=${OTA_AP_SSID}  OTA_UPDATE_FILESYSTEM=${OTA_UPDATE_FILESYSTEM}"
  exit 0
fi

# --- single instance ---------------------------------------------------------
if [ -e "${LOCK_FILE}" ]; then
  die "another OTA test appears to be running (lock ${LOCK_FILE} present); remove it if it is stale"
fi
echo "$$" > "${LOCK_FILE}"

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

# --- 1. join the tankie-esp AP ----------------------------------------------
log "connecting wlan0 to the '${OTA_AP_SSID}' AP …"
# Drop the infrastructure connection so we actually land on the AP.
if [ -n "${ACTIVE_CONN}" ]; then
  nmcli connection down id "${ACTIVE_CONN}" || true
fi
if [ -n "${OTA_AP_PASSWORD}" ]; then
  nmcli device wifi connect "${OTA_AP_SSID}" password "${OTA_AP_PASSWORD}" \
    || die "could not connect to the '${OTA_AP_SSID}' AP"
else
  nmcli device wifi connect "${OTA_AP_SSID}" \
    || die "could not connect to the '${OTA_AP_SSID}' AP"
fi

# Wait for the AP to hand us an address.
log "waiting for an address on the AP …"
deadline=$(( $(date +%s) + AP_WAIT_TIMEOUT ))
while [ "$(date +%s)" -lt "${deadline}" ]; do
  if ip -4 addr show wlan0 2>/dev/null | grep -q "inet 192\.168\.4\."; then
    log "on the AP with IP $(ip -4 -o addr show wlan0 2>/dev/null | awk '{print $4}')"
    break
  fi
  sleep 1
done
ip -4 addr show wlan0 2>/dev/null | grep -q "inet 192\.168\.4\." \
  || die "no 192.168.4.x address on wlan0 after ${AP_WAIT_TIMEOUT}s — check the AP/SSID"

# --- 2. run the OTA flash (this is what we are testing) ---------------------
log "running flash_ota.sh (log -> ${LOG_FILE}) …"
set +e
bash "${FLASH_OTA}" 2>&1 | tee "${LOG_FILE}"
FLASH_RC=${PIPESTATUS[0]}
set -e
if [ "${FLASH_RC}" -eq 0 ]; then
  log "OTA test PASSED — see ${LOG_FILE}"
else
  warn "OTA test FAILED (exit ${FLASH_RC}) — see ${LOG_FILE}"
fi
exit "${FLASH_RC}"
