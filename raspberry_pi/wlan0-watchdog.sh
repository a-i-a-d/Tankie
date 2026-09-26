#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Tankie — wlan0 WiFi watchdog (see https://github.com/a-i-a-d/Tankie/issues/1)
#
# The Pi Zero 2 W's brcmfmac firmware can crash, tear down wlan0 and never
# re-register it, leaving the Pi unreachable. This script is invoked by
# wlan0-watchdog.service (wlan0-watchdog.timer: 90 s after boot, then every
# 60 s) and self-recovers without a reboot:
#
#   1. healthy (wlan0 present)          -> exit 0 (no-op)
#   2. missing, module not loaded       -> exit 0 (a reload would not help)
#   3. missing, module loaded           -> rmmod/modprobe brcmfmac (primary)
#   4. still missing after the reload   -> systemctl restart NetworkManager
#   5. still missing after that         -> log ERROR, exit 1 (next tick retries)
#
# Installed to /usr/local/bin/wlan0-watchdog.sh by setup.sh.
# TimeoutStartSec=120 in the unit caps a hung recovery.
# ---------------------------------------------------------------------------
set -u

LOG_PREFIX="[wlan0-watchdog]"

log() { echo "${LOG_PREFIX} $*"; }

# True if the wlan0 interface exists (ip exits non-zero for unknown links).
wlan0_present() { ip link show wlan0 >/dev/null 2>&1; }

# True if the brcmfmac module is currently loaded.
brcmfmac_loaded() { lsmod | grep -q '^brcmfmac[[:space:]]'; }

# --- 1. healthy -------------------------------------------------------------
if wlan0_present; then
  log "wlan0 present - healthy"
  exit 0
fi

log "wlan0 MISSING - attempting recovery"

# --- 2. nothing to reload ----------------------------------------------------
if ! brcmfmac_loaded; then
  log "brcmfmac not loaded and wlan0 missing - skipping (reload would not help)"
  exit 0
fi

# --- 3. primary: reload the WiFi driver --------------------------------------
log "primary: reloading brcmfmac (rmmod + modprobe)"
rmmod brcmfmac 2>&1 || log "rmmod brcmfmac failed (module busy?)"
modprobe brcmfmac 2>&1 || log "modprobe brcmfmac failed"
sleep 10

if wlan0_present; then
  log "recovered: wlan0 is back after driver reload"
  exit 0
fi

# --- 4. fallback: restart NetworkManager -------------------------------------
log "fallback: wlan0 still missing - restarting NetworkManager"
systemctl restart NetworkManager 2>&1 || log "NetworkManager restart failed"
sleep 5

if wlan0_present; then
  log "recovered: wlan0 is back after NetworkManager restart"
  exit 0
fi

# --- 5. give up (the timer will retry on the next tick) ----------------------
log "ERROR: wlan0 still missing after all recovery attempts - will retry on next tick"
exit 1
