#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Tankie serial bridge — idempotent installer (issue #29)
#
# Installs the Pi-side serial control link:
#   * apt/pip dependencies (pyserial, pyyaml)
#   * a udev rule for a stable port name (/dev/tankie-serial -> ttyUSB0)
#   * the state + socket directories
#   * the systemd unit (enable + start)
#
# Safe to re-run: every step is idempotent.
#
# Usage:
#   sudo bash setup-serial.sh
#
# The bridge runs as the `pi` user (dialout group) per tankie-serial.service.
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_SRC="${SCRIPT_DIR}/tankie-serial.service"
SERVICE_DEST="/etc/systemd/system/tankie-serial.service"
UDEV_RULE="/etc/udev/rules.d/99-tankie-serial.rules"
STATE_DIR="/var/lib/tankie"
SOCK_DIR="/run/tankie"
RUN_USER="${TANKIE_USER:-pi}"

log()  { printf '\033[1;32m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[setup]\033[0m %s\n' "$*"; }

# --- 1. dependencies --------------------------------------------------------
log "installing Python dependencies (pyserial, pyyaml) …"
if command -v pip3 >/dev/null 2>&1; then
  pip3 install --quiet pyserial pyyaml 2>/dev/null || \
    pip3 install --quiet --user pyserial pyyaml 2>/dev/null || \
    warn "pip3 install failed — ensure pyserial + pyyaml are available"
else
  warn "pip3 not found — ensure pyserial + pyyaml are installed"
fi

# --- 2. directories ---------------------------------------------------------
log "creating state + socket directories …"
sudo mkdir -p "${STATE_DIR}" "${SOCK_DIR}"
sudo chown "${RUN_USER}:${RUN_USER}" "${STATE_DIR}" 2>/dev/null || true
# /run is tmpfs; recreate on boot via the service (WorkingDirectory). We
# also add a tmpfiles entry so it exists before the service starts.
sudo tee /etc/tmpfiles.d/tankie-serial.conf >/dev/null <<TMP
d ${SOCK_DIR} 0755 ${RUN_USER} ${RUN_USER} -
d ${STATE_DIR} 0755 ${RUN_USER} ${RUN_USER} -
TMP
sudo systemctl enable systemd-tmpfiles-setup.service 2>/dev/null || true

# --- 3. udev rule (stable port name) ----------------------------------------
# The CH340 (QinHeng 1a86:7523) on this tank is /dev/ttyUSB0. A udev symlink
# /dev/tankie-serial keeps the name stable if a second USB-serial device
# appears. The control link uses the Pi's native UART0 (/dev/ttyS0, the
# permanent TX/RX wiring); the udev symlink is a convenience for the USB
# flashing adapter if you prefer a stable name for that.
log "installing udev rule for a stable serial port name …"
sudo tee "${UDEV_RULE}" >/dev/null <<UDEV
# Tankie ESP8266 CH340 USB-serial bridge (issue #29)
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", SYMLINK+="tankie-serial", MODE="0660", GROUP="dialout"
UDEV
sudo udevadm control --reload-rules 2>/dev/null || true
sudo udevadm trigger 2>/dev/null || true

# --- 4. systemd unit --------------------------------------------------------
log "installing systemd unit …"
sudo cp "${SERVICE_SRC}" "${SERVICE_DEST}"
sudo systemctl daemon-reload
sudo systemctl enable tankie-serial.service
log "starting tankie-serial …"
sudo systemctl restart tankie-serial.service || warn "service failed to start (check: journalctl -u tankie-serial -n 50)"

log "done. Useful commands:"
log "  systemctl status tankie-serial"
log "  journalctl -u tankie-serial -f"
log "  ${SCRIPT_DIR}/tankie-serial.py state"
log "  ${SCRIPT_DIR}/tankie-serial.py drive --speed 50 --steer 0"
