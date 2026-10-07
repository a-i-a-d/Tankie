#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Tankie serial bridge — idempotent installer (issue #29, #53)
#
# Installs the Pi-side serial control link system-wide, so the runtime never
# depends on where the repo checkout lives:
#   * apt dependencies (python3-serial, python3-yaml)
#   * udev rule for a stable port name (/dev/tankie-serial -> ttyUSB0)
#   * state + socket directories (/var/lib/tankie, /run/tankie)
#   * daemon -> /usr/local/lib/tankie/bridge.py
#   * CLI  -> /usr/local/bin/tankie-serial
#   * config -> /etc/tankie/serial_bridge.yaml (install only if absent;
#     pass --reset-config to force-reinstall, with a timestamped backup)
#   * systemd unit (enable + restart)
#
# Safe to re-run: every step is idempotent.
#
# Usage (run as root, e.g. from the repo checkout):
#   bash setup-serial.sh
#   bash setup-serial.sh --reset-config   # replace config from the template
#
# The bridge runs as the `pi` user (dialout group) per tankie-serial.service.
set -euo pipefail

# Only one copy of this script may run; resolve the checkout root it lives in.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SER_DIR="${SCRIPT_DIR}"

RUN_USER="${TANKIE_USER:-pi}"
SER_BIN_DIR="/usr/local/lib/tankie"
SER_CLI="/usr/local/bin/tankie-serial"
SER_CONF="/etc/tankie/serial_bridge.yaml"
SER_CONF_SRC="${SCRIPT_DIR}/../conf/serial_bridge.yaml"
SER_UNIT_SRC="${SER_DIR}/tankie-serial.service"
SER_UNIT="/etc/systemd/system/tankie-serial.service"
UDEV_RULE="/etc/udev/rules.d/99-tankie-serial.rules"
STATE_DIR="/var/lib/tankie"
SOCK_DIR="/run/tankie"

# Accept --reset-config (forwarded by setup.sh)
RESET_CONF=0
for arg in "$@"; do
  case "${arg}" in
    --reset-config) RESET_CONF=1 ;;
    *) warn "unknown option: ${arg}" ;;
  esac
done

log()  { printf '\033[1;32m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[setup]\033[0m %s\n' "$*" >&2; }

if [ "$(id -u)" -ne 0 ]; then
  warn "this script should be run as root (setup.sh forwards it as root)"
fi

# --- 1. dependencies --------------------------------------------------------
log "installing Python dependencies (pyserial, pyyaml) — apt first, pip fallback …"
if command -v apt-get >/dev/null 2>&1; then
  (apt-get update && \
   apt-get install -y --no-install-recommends python3-serial python3-yaml) \
    || warn "apt-get install failed — will try pip"
else
  warn "apt-get not found — falling back to pip"
fi
if ! python3 -c "import serial, yaml" >/dev/null 2>&1; then
  if command -v pip3 >/dev/null 2>&1; then
    pip3 install --quiet pyserial pyyaml 2>/dev/null || \
      pip3 install --quiet --user pyserial pyyaml 2>/dev/null || \
      warn "pip3 install failed — ensure pyserial + pyyaml are available"
  else
    warn "pip3 not found — ensure pyserial + pyyaml are installed"
  fi
fi

# --- 2. directories ---------------------------------------------------------
log "creating state + socket directories …"
mkdir -p "${STATE_DIR}" "${SOCK_DIR}"
chown "${RUN_USER}:${RUN_USER}" "${STATE_DIR}" 2>/dev/null || true
# /run is tmpfs; recreate it on boot via tmpfiles so it exists before the service starts.
tee /etc/tmpfiles.d/tankie-serial.conf >/dev/null <<TMP
d ${SOCK_DIR} 0755 ${RUN_USER} ${RUN_USER} -
d ${STATE_DIR} 0755 ${RUN_USER} ${RUN_USER} -
TMP
systemctl enable systemd-tmpfiles-setup.service >/dev/null 2>&1 || true

# --- 3. daemon + CLI --------------------------------------------------------
log "installing daemon + CLI to system locations …"
mkdir -p "${SER_BIN_DIR}"
install -m 0755 "${SER_DIR}/bridge.py" "${SER_BIN_DIR}/bridge.py"
install -m 0755 "${SER_DIR}/tankie-serial.py" "${SER_CLI}"
chmod +x "${SER_CLI}"
log "  daemon -> ${SER_BIN_DIR}/bridge.py"
log "  CLI    -> ${SER_CLI}"

# --- 4. config --------------------------------------------------------------
log "installing serial-bridge config …"
if [ "${RESET_CONF}" -eq 1 ]; then
  if [ -f "${SER_CONF}" ]; then
    BACKUP="${SER_CONF}.$(date +%m%d%H%M)"
    cp "${SER_CONF}" "${BACKUP}"
    chmod 0644 "${BACKUP}"
    log "  existing config backed up to ${BACKUP}"
  fi
  install -m 0644 "${SER_CONF_SRC}" "${SER_CONF}"
  log "  replaced config with template"
else
  if [ ! -f "${SER_CONF}" ]; then
    install -m 0644 "${SER_CONF_SRC}" "${SER_CONF}"
    log "  new config installed to ${SER_CONF}"
  else
    log "  config already present at ${SER_CONF} (left untouched; pass --reset-config to replace)"
  fi
fi

# --- 5. systemd unit --------------------------------------------------------
# The unit file in the repo is already written with the installed system paths
# (issue #53), so it can be copied verbatim.
log "installing systemd unit …"
install -m 0644 "${SER_UNIT_SRC}" "${SER_UNIT}"
systemctl daemon-reload
systemctl enable tankie-serial.service
log "starting tankie-serial …"
systemctl restart tankie-serial.service || warn "service failed to start (check: journalctl -u tankie-serial -n 50)"

# --- 6. udev rule (stable port name) ----------------------------------------
log "installing udev rule for a stable serial port name …"
tee "${UDEV_RULE}" >/dev/null <<UDEV
# Tankie ESP8266 CH340 USB-serial bridge (issue #29)
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", SYMLINK+="tankie-serial", MODE="0660", GROUP="dialout"
UDEV
udevadm control --reload-rules >/dev/null 2>&1 || true
udevadm trigger >/dev/null 2>&1 || true

log "done. Useful commands:"
log "  systemctl status tankie-serial"
log "  journalctl -u tankie-serial -f"
log "  tankie-serial state"
log "  tankie-serial drive --speed 50 --steer 0"
