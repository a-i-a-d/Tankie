#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Static CI guard for the Pi install stack (issue #53) — pure bash, no hardware.
#
# Ensures the Pi-side setup (mediamtx + serial bridge) can never regress
# to depending on a repo checkout path:
#   1. no file in the repo references a clone path (/home/pi/Tankie) or
#      the old /opt/mediamtx location;
#   2. the tankie-serial.service template only references system paths;
#   3. the serial-bridge config uses installed system paths;
#   4. the installers target the installed system locations.
#
# Fails (exit 1) on any violation.
set -euo pipefail
cd "$(dirname "$0")/.."   # repo root

fail() { echo "[pi_install_check] FAIL: $*" >&2; exit 1; }
ok()   { printf '[pi_install_check] OK  : %s\n' "$*"; }

# self-exclude: this script mentions the banned strings in its own comments
self_excl='pi_install_check.sh'
self_git_excl='\.git/'

# --- 1. no clone-path references in the repo --------------------------------
if grep -rn '/home/pi/Tankie' --include='*' . | grep -v -e "${self_git_excl}" -e "${self_excl}" | grep -q .; then
  fail "clone-path reference found:"
  grep -rn '/home/pi/Tankie' --include='*' . | grep -v -e "${self_git_excl}" -e "${self_excl}"
fi
ok "no /home/pi/Tankie references in the repo"

# The old /opt/mediamtx install location must not come back.
if grep -rn '/opt/mediamtx' --include='*' . | grep -v -e "${self_git_excl}" -e "${self_excl}" | grep -q .; then
  fail "/opt/mediamtx references found (issue #53 moved mediamtx to /usr/local):"
  grep -rn '/opt/mediamtx' --include='*' . | grep -v -e "${self_git_excl}" -e "${self_excl}"
fi
ok "no /opt/mediamtx references in the repo"

# --- 2. serial-bridge service template only uses system paths -----------------
UNIT=raspberry_pi/serial_bridge/tankie-serial.service
grep -q 'ExecStart=/usr/bin/python3 /usr/local/lib/tankie/bridge.py' "${UNIT}" \
  || fail "tankie-serial.service ExecStart must reference /usr/local/lib/tankie/bridge.py"
grep -q 'ExecStart=.*--config /etc/tankie/serial_bridge.yaml' "${UNIT}" \
  || fail "tankie-serial.service must use --config /etc/tankie/serial_bridge.yaml"
grep -q 'WorkingDirectory=/var/lib/tankie' "${UNIT}" \
  || fail "tankie-serial.service WorkingDirectory must be /var/lib/tankie"
ok "tankie-serial.service references installed system paths only"

# --- 3. serial-bridge config is a template with system paths -------------------
CONF=raspberry_pi/conf/serial_bridge.yaml
grep -q 'state_file: /var/lib/tankie/state.json' "${CONF}" \
  || fail "serial_bridge.yaml state_file must be /var/lib/tankie/state.json"
grep -q 'socket_path: /run/tankie/bridge.sock' "${CONF}" \
  || fail "serial_bridge.yaml socket_path must be /run/tankie/bridge.sock"
ok "serial_bridge.yaml uses installed system paths"

# --- 4. installers target the installed system locations ----------------------
install_sh=raspberry_pi/serial_bridge/setup-serial.sh
grep -q '/usr/local/lib/tankie' "${install_sh}" \
  || fail "setup-serial.sh must install to /usr/local/lib/tankie"
grep -q '/usr/local/bin/tankie-serial' "${install_sh}" \
  || fail "setup-serial.sh must install CLI to /usr/local/bin/tankie-serial"
grep -q '/etc/tankie/serial_bridge.yaml' "${install_sh}" \
  || fail "setup-serial.sh must handle /etc/tankie/serial_bridge.yaml"
grep -q '/etc/systemd/system/tankie-serial.service' "${install_sh}" \
  || fail "setup-serial.sh must handle /etc/systemd/system/tankie-serial.service"
ok "setup-serial.sh targets the installed system locations"

setup_sh=raspberry_pi/setup.sh
grep -q 'SER_SETUP=' "${setup_sh}" \
  || fail "setup.sh must reference setup-serial.sh"
grep -q 'TANKIE_SER_DIR' "${setup_sh}" \
  || fail "setup.sh must define TANKIE_SER_DIR"
ok "setup.sh forwards to setup-serial.sh and defines TANKIE_SER_DIR"

echo "[pi_install_check] all checks passed (issue #53)"
