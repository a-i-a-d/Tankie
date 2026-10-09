#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Tankie — wlan0 WiFi watchdog (see https://github.com/a-i-a-d/Tankie/issues/1)
#
# The Pi Zero 2 W's brcmfmac firmware can crash, tear down wlan0 and never
# re-register it, leaving the Pi unreachable. This script is invoked by
# wlan0-watchdog.service (wlan0-watchdog.timer: 90 s after boot, then every
# 60 s) and self-recovers:
#
#   1. healthy (wlan0 present)          -> exit 0 (no-op, reset failure count)
#   2. missing, module not loaded       -> fresh modprobe brcmfmac (#68: a
#                                          crash can leave it fully unloaded;
#                                          a fresh load recovers it)
#   3. missing, module loaded           -> rmmod brcmfmac_wcc + brcmfmac,
#                                          then modprobe (#68: the
#                                          firmware-variant module
#                                          brcmfmac_wcc is auto-loaded on
#                                          top of brcmfmac and HOLDS it, so
#                                          rmmod brcmfmac alone always fails
#                                          with "module busy: brcmfmac_wcc")
#   4. still missing after the reload   -> systemctl restart NetworkManager
#   5. still missing after that         -> log ERROR, bump failure counter
#   6. 3 consecutive failed ticks       -> systemctl reboot (bounded last
#                                          resort; counter lives in /run
#                                          (tmpfs) so it resets on reboot)
#
# Installed to /usr/local/bin/wlan0-watchdog.sh by setup.sh.
# TimeoutStartSec=120 in the unit caps a hung recovery.
# ---------------------------------------------------------------------------
set -u

LOG_PREFIX="[wlan0-watchdog]"
# Overridable for the test harness (tests/wlan0_watchdog_harness.sh); the
# default is the runtime dir on the Pi (tmpfs, resets on reboot).
STATE_DIR="${TANKIE_WD_STATE_DIR:-/run/tankie}"
STATE_FILE="${STATE_DIR}/wlan0-watchdog-failures"
MAX_FAILURES=3

log() { echo "${LOG_PREFIX} $*"; }

# True if the wlan0 interface exists (ip exits non-zero for unknown links).
wlan0_present() { ip link show wlan0 >/dev/null 2>&1; }

# True if the brcmfmac module is currently loaded.
brcmfmac_loaded() { lsmod | grep -q '^brcmfmac[[:space:]]'; }

# Runtime state dir (tmpfs — resets on reboot, which is what we want).
mkdir -p "${STATE_DIR}" 2>/dev/null || true

# Reset the failure counter (wlan0 recovered or healthy).
reset_failures() {
  rm -f "${STATE_FILE}" 2>/dev/null || true
}

# Read the current failure count (0 if no file / unreadable).
get_failures() {
  local n=0
  if [ -f "${STATE_FILE}" ]; then
    n=$(cat "${STATE_FILE}" 2>/dev/null || echo 0)
  fi
  case "${n}" in ''|*[!0-9]*) n=0;; esac
  echo "${n}"
}

# Bump the failure counter and print the new value.
bump_failures() {
  local n
  n=$(get_failures)
  n=$((n + 1))
  echo "$n" > "${STATE_FILE}"
  echo "$n"
}

# --- 1. healthy -------------------------------------------------------------
if wlan0_present; then
  reset_failures
  log "wlan0 present - healthy"
  exit 0
fi

log "wlan0 MISSING - attempting recovery"

# --- 2. module not loaded: attempt a fresh load (issue #68) -----------------
# Previously this branch just gave up ("reload would not help"), but if the
# crash left brcmfmac fully unloaded, a fresh modprobe is exactly what
# recovers it.
if ! brcmfmac_loaded; then
  log "brcmfmac not loaded - attempting a fresh load (modprobe brcmfmac)"
  modprobe brcmfmac 2>&1 || log "modprobe brcmfmac failed"
  sleep 10
  if wlan0_present; then
    reset_failures
    log "recovered: wlan0 is back after fresh load"
    exit 0
  fi
fi

# --- 3. primary: reload the WiFi driver (issue #68: correct rmmod order) ----
# The BCM43430B0 firmware-variant module (brcmfmac_wcc) is auto-loaded on top
# of brcmfmac and HOLDS it (brcmfmac refcnt=1, holders=brcmfmac_wcc), so
# `rmmod brcmfmac` alone always fails with "module busy: brcmfmac_wcc".
# Unload the dependent FIRST, then the driver, then reload — the kernel
# re-autoloads the variant on the next brcmfmac probe.
log "primary: reloading brcmfmac (rmmod brcmfmac_wcc + brcmfmac, then modprobe)"
rmmod brcmfmac_wcc 2>/dev/null || log "rmmod brcmfmac_wcc failed (not loaded?)"
rmmod brcmfmac 2>&1 || log "rmmod brcmfmac failed (module busy?)"
modprobe brcmfmac 2>&1 || log "modprobe brcmfmac failed"
sleep 10

if wlan0_present; then
  reset_failures
  log "recovered: wlan0 is back after driver reload"
  exit 0
fi

# --- 4. fallback: restart NetworkManager -------------------------------------
log "fallback: wlan0 still missing - restarting NetworkManager"
systemctl restart NetworkManager 2>&1 || log "NetworkManager restart failed"
sleep 5

if wlan0_present; then
  reset_failures
  log "recovered: wlan0 is back after NetworkManager restart"
  exit 0
fi

# --- 5. all recovery failed: bump counter, maybe reboot ----------------------
n=$(bump_failures)
log "ERROR: wlan0 still missing after all recovery attempts (failure ${n}/${MAX_FAILURES})"

if [ "$n" -ge "$MAX_FAILURES" ]; then
  log "ERROR: ${MAX_FAILURES} consecutive failed ticks - rebooting (last resort)"
  # Give the log a moment to flush before the reboot.
  sleep 2
  systemctl reboot 2>/dev/null || reboot
fi

exit 1
