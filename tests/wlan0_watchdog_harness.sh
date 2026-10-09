#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Test harness for raspberry_pi/wlan0-watchdog.sh (issue #68) — pure bash,
# no hardware.
#
# Drives the REAL watchdog script with PATH-shimmed ip/lsmod/rmmod/modprobe/
# systemctl/sleep/reboot and asserts the recovery ladder:
#   T1  healthy wlan0                      -> no-op, exit 0, counter reset
#   T2  wlan0 gone, module NOT loaded      -> fresh modprobe recovers (T2 fix)
#   T3  wlan0 gone, module loaded+held     -> rmmod wcc THEN brcmfmac,
#                                             modprobe recovers (T1 fix)
#   T4  all recovery fails, 3 ticks        -> reboot on 3rd tick only
#   T5  recovery succeeds after failures   -> counter reset, no reboot
#   T6  corrupt failure counter            -> sanitized, no crash
#   T7  static: rmmod brcmfmac_wcc precedes rmmod brcmfmac in the script
#
# Simulation model: wlan0 "appears" once the driver has been (re)loaded,
# i.e. the `ip` shim reports wlan0 present if (a) the scenario pre-set it
# present, or (b) `appear_after_modprobe` is enabled and modprobe brcmfmac
# has been called. Fails (exit 1) on any violation.
set -u
cd "$(dirname "$0")/.."   # repo root

SCRIPT="raspberry_pi/wlan0-watchdog.sh"
SANDBOX="$(mktemp -d)"
SHIM="${SANDBOX}/bin"
STATE="${SANDBOX}/state"
mkdir -p "${SHIM}" "${STATE}"

PASS=0
FAIL=0
ok()   { PASS=$((PASS+1)); printf '[wlan0_watchdog] OK  : %s\n' "$*"; }
fail() { FAIL=$((FAIL+1)); printf '[wlan0_watchdog] FAIL: %s\n' "$*" >&2; }

# --- shims ------------------------------------------------------------------
# `ip link show wlan0` -> exit 0 iff wlan0 is "present" in the simulation.
cat > "${SHIM}/ip" << 'SH'
#!/usr/bin/env bash
if [ -f "$WD_STATE/wlan0_present" ] && [ "$(cat "$WD_STATE/wlan0_present" 2>/dev/null)" = "1" ]; then
  exit 0
fi
# wlan0 appears once the driver has been (re)loaded (simulated recovery).
if [ -f "$WD_STATE/appear_after_modprobe" ] \
   && [ -f "$WD_STATE/calls.txt" ] \
   && grep -q "modprobe brcmfmac" "$WD_STATE/calls.txt" 2>/dev/null; then
  exit 0
fi
exit 1
SH
# `lsmod` -> prints the module table from $STATE/lsmod.txt (if present)
cat > "${SHIM}/lsmod" << 'SH'
#!/usr/bin/env bash
[ -f "$WD_STATE/lsmod.txt" ] && cat "$WD_STATE/lsmod.txt"
exit 0
SH
# `rmmod <mod>` -> records the call, removes the module from the table
cat > "${SHIM}/rmmod" << 'SH'
#!/usr/bin/env bash
echo "rmmod $1" >> "$WD_STATE/calls.txt"
if [ -f "$WD_STATE/lsmod.txt" ]; then
  grep -v "^$1[[:space:]]" "$WD_STATE/lsmod.txt" > "$WD_STATE/lsmod.txt.new" || true
  mv "$WD_STATE/lsmod.txt.new" "$WD_STATE/lsmod.txt"
fi
exit 0
SH
# `modprobe <mod>` -> records the call, (re)adds the module to the table
cat > "${SHIM}/modprobe" << 'SH'
#!/usr/bin/env bash
echo "modprobe $1" >> "$WD_STATE/calls.txt"
if [ -f "$WD_STATE/lsmod.txt" ] && ! grep -q "^$1[[:space:]]" "$WD_STATE/lsmod.txt"; then
  echo "$1 16384 0" >> "$WD_STATE/lsmod.txt"
fi
exit 0
SH
# `systemctl <args>` -> records the call
cat > "${SHIM}/systemctl" << 'SH'
#!/usr/bin/env bash
echo "systemctl $*" >> "$WD_STATE/calls.txt"
exit 0
SH
# `reboot` -> records the call (do NOT actually reboot!)
cat > "${SHIM}/reboot" << 'SH'
#!/usr/bin/env bash
echo "reboot" >> "$WD_STATE/calls.txt"
exit 0
SH
# `sleep` -> no-op (keeps the harness fast)
cat > "${SHIM}/sleep" << 'SH'
#!/usr/bin/env bash
exit 0
SH
chmod +x "${SHIM}"/*

# --- scenario helper ---------------------------------------------------------
reset_scenario() {
  rm -rf "${STATE}"/*
  mkdir -p "${STATE}"
  : > "${STATE}/calls.txt"
  echo 0 > "${STATE}/wlan0_present"
  : > "${STATE}/lsmod.txt"
}

run_watchdog() {
  # Run the real script with the shims first in PATH and an isolated state dir.
  PATH="${SHIM}:${PATH}" TANKIE_WD_STATE_DIR="${STATE}" WD_STATE="${STATE}" \
    bash "${SCRIPT}" > "${STATE}/stdout.txt" 2>&1
  return $?
}

state_has() { grep -q "$1" "${STATE}/calls.txt"; }
state_seq() { grep -o "rmmod [a-z0-9_]*\|modprobe [a-z0-9_]*" "${STATE}/calls.txt" | tr '\n' ' '; }
reboot_called() { grep -qE '^(systemctl reboot|reboot)$' "${STATE}/calls.txt"; }

# --- T1: healthy wlan0 -> no-op, exit 0, counter reset -----------------------
reset_scenario
echo 1 > "${STATE}/wlan0_present"
echo "brcmfmac 16384 1 brcmfmac_wcc" > "${STATE}/lsmod.txt"
echo 3 > "${STATE}/wlan0-watchdog-failures"
run_watchdog; rc=$?
[ "$rc" -eq 0 ] && ok "T1 healthy: exit 0" || fail "T1 healthy: exit $rc"
[ ! -f "${STATE}/wlan0-watchdog-failures" ] && ok "T1 healthy: failure counter reset" || fail "T1 healthy: counter not reset"
state_has "rmmod\|modprobe" && fail "T1 healthy: recovery commands ran: $(state_seq)" || ok "T1 healthy: no recovery commands"

# --- T2: wlan0 gone, module NOT loaded -> fresh modprobe recovers ------------
reset_scenario
: > "${STATE}/lsmod.txt"                       # brcmfmac not loaded
touch "${STATE}/appear_after_modprobe"         # wlan0 comes back after the load
run_watchdog; rc=$?
[ "$rc" -eq 0 ] && ok "T2 fresh-load: exit 0" || fail "T2 fresh-load: exit $rc"
state_has "^modprobe brcmfmac$" && ok "T2 fresh-load: modprobe brcmfmac attempted" || fail "T2 fresh-load: no modprobe attempt: $(state_seq)"
grep -q "recovered: wlan0 is back after fresh load" "${STATE}/stdout.txt" && ok "T2 fresh-load: recovery logged" || fail "T2 fresh-load: no recovery log"
[ ! -f "${STATE}/wlan0-watchdog-failures" ] && ok "T2 fresh-load: no failure recorded" || fail "T2 fresh-load: failure recorded"

# --- T3: wlan0 gone, module loaded+held -> correct rmmod order ---------------
reset_scenario
echo "brcmfmac_wcc 16384 0" > "${STATE}/lsmod.txt"
echo "brcmfmac 16384 1 brcmfmac_wcc" >> "${STATE}/lsmod.txt"
touch "${STATE}/appear_after_modprobe"         # wlan0 comes back after reload
run_watchdog; rc=$?
[ "$rc" -eq 0 ] && ok "T3 driver-reload: exit 0" || fail "T3 driver-reload: exit $rc"
seq="$(state_seq)"
first_wcc=$(grep -n "^rmmod brcmfmac_wcc" "${STATE}/calls.txt" | head -1 | cut -d: -f1)
first_core=$(grep -n "^rmmod brcmfmac$" "${STATE}/calls.txt" | head -1 | cut -d: -f1)
if [ -n "$first_wcc" ] && [ -n "$first_core" ] && [ "$first_wcc" -lt "$first_core" ]; then
  ok "T3 driver-reload: rmmod brcmfmac_wcc before rmmod brcmfmac (${seq})"
else
  fail "T3 driver-reload: wrong rmmod order (${seq})"
fi
state_has "^modprobe brcmfmac$" && ok "T3 driver-reload: modprobe brcmfmac after unload" || fail "T3 driver-reload: no modprobe: ${seq}"
grep -q "recovered: wlan0 is back after driver reload" "${STATE}/stdout.txt" && ok "T3 driver-reload: recovery logged" || fail "T3 driver-reload: no recovery log"

# --- T4: all recovery fails -> reboot only on the 3rd failed tick ------------
reset_scenario
echo "brcmfmac 16384 1 brcmfmac_wcc" > "${STATE}/lsmod.txt"
# wlan0 stays gone through all attempts (no appear_after_modprobe)
run_watchdog; rc=$?
[ "$rc" -eq 1 ] && ok "T4 tick1: exit 1" || fail "T4 tick1: exit $rc"
[ "$(cat "${STATE}/wlan0-watchdog-failures")" = "1" ] && ok "T4 tick1: counter=1" || fail "T4 tick1: counter=$(cat "${STATE}/wlan0-watchdog-failures")"
reboot_called && fail "T4 tick1: rebooted too early" || ok "T4 tick1: no reboot yet"

run_watchdog; rc=$?
[ "$rc" -eq 1 ] && ok "T4 tick2: exit 1" || fail "T4 tick2: exit $rc"
[ "$(cat "${STATE}/wlan0-watchdog-failures")" = "2" ] && ok "T4 tick2: counter=2" || fail "T4 tick2: counter=$(cat "${STATE}/wlan0-watchdog-failures")"
reboot_called && fail "T4 tick2: rebooted too early" || ok "T4 tick2: no reboot yet"

run_watchdog; rc=$?
[ "$rc" -eq 1 ] && ok "T4 tick3: exit 1" || fail "T4 tick3: exit $rc"
[ "$(cat "${STATE}/wlan0-watchdog-failures")" = "3" ] && ok "T4 tick3: counter=3" || fail "T4 tick3: counter=$(cat "${STATE}/wlan0-watchdog-failures")"
reboot_called && ok "T4 tick3: reboot triggered (last resort)" || fail "T4 tick3: no reboot: $(state_seq)"
state_has "^systemctl restart NetworkManager$" && ok "T4 tick3: NetworkManager fallback attempted" || fail "T4 tick3: no NetworkManager fallback"

# --- T5: recovery succeeds after failures -> counter reset, no reboot --------
reset_scenario
echo "brcmfmac 16384 1 brcmfmac_wcc" > "${STATE}/lsmod.txt"
echo 2 > "${STATE}/wlan0-watchdog-failures"
touch "${STATE}/appear_after_modprobe"         # reload succeeds this time
run_watchdog; rc=$?
[ "$rc" -eq 0 ] && ok "T5 recover-after-failures: exit 0" || fail "T5: exit $rc"
[ ! -f "${STATE}/wlan0-watchdog-failures" ] && ok "T5: counter reset after recovery" || fail "T5: counter not reset"
reboot_called && fail "T5: rebooted despite recovery" || ok "T5: no reboot"

# --- T6: corrupt failure counter -> sanitized, no crash ----------------------
reset_scenario
echo "brcmfmac 16384 1 brcmfmac_wcc" > "${STATE}/lsmod.txt"
echo "garbage" > "${STATE}/wlan0-watchdog-failures"
run_watchdog; rc=$?
[ "$rc" -eq 1 ] && ok "T6 corrupt-counter: handled (exit 1)" || fail "T6 corrupt-counter: exit $rc"
[ "$(cat "${STATE}/wlan0-watchdog-failures")" = "1" ] && ok "T6 corrupt-counter: sanitized to 1" || fail "T6 corrupt-counter: value=$(cat "${STATE}/wlan0-watchdog-failures")"
reboot_called && fail "T6 corrupt-counter: rebooted despite sanitize" || ok "T6 corrupt-counter: no reboot"

# --- T7: static — rmmod order in the script itself ---------------------------
line_wcc=$(grep -n "^rmmod brcmfmac_wcc" "${SCRIPT}" | head -1 | cut -d: -f1)
line_core=$(grep -n "^rmmod brcmfmac 2>&1" "${SCRIPT}" | head -1 | cut -d: -f1)
if [ -n "$line_wcc" ] && [ -n "$line_core" ] && [ "$line_wcc" -lt "$line_core" ]; then
  ok "T7 static: rmmod brcmfmac_wcc (L${line_wcc}) precedes rmmod brcmfmac (L${line_core})"
else
  fail "T7 static: rmmod order wrong (wcc=L${line_wcc:-?} core=L${line_core:-?})"
fi
grep -q "systemctl reboot" "${SCRIPT}" && ok "T7 static: reboot last-resort present" || fail "T7 static: reboot last-resort missing"
grep -q "MAX_FAILURES=3" "${SCRIPT}" && ok "T7 static: bounded at 3 failed ticks" || fail "T7 static: MAX_FAILURES not 3"

# --- summary ------------------------------------------------------------------
echo
echo "[wlan0_watchdog] ${PASS} passed, ${FAIL} failed"
rm -rf "${SANDBOX}"
[ "$FAIL" -eq 0 ] || exit 1
