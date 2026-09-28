#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Tankie — Raspberry Pi Zero 2 W camera streaming setup (mediamtx)
#
# Sets up the video streaming stack from scratch on a fresh Raspberry Pi OS
# (64-bit) so that the IMX708 / Camera Module 3 (CSI port) is exposed as:
#
#   cam       : 1080p30 H.264  -> human (WebRTC :8889, HLS :8888, RTSP :8554, RTMP :1935)
#   cam_low   : 480p15  H.264  -> AI    (same protocols, path "cam_low")
#
# The camera + encoders only run while at least one client is connected
# (mediamtx `sourceOnDemand`), so there is zero bandwidth/CPU when idle.
#
# The mediamtx release binary is self-contained: it embeds the `mtxrpicam`
# camera helper, `libcamera`, and the Raspberry Pi IPA configs, so this
# script does NOT need to install GStreamer, ffmpeg, or libcamera from apt.
#
# It also installs the WiFi watchdog (wlan0-watchdog.sh + .timer + .service) that
# detects a missing wlan0 (brcmfmac firmware crash, see
# https://github.com/a-i-a-d/Tankie/issues/1) and reloads the driver /
# restarts NetworkManager to bring it back — no reboot needed.
#
# And it installs the ESP8266 (D1 Mini) toolchain (arduino-cli + the esp8266
# core, which ships the xtensa compiler, esptool, and mklittlefs) plus the
# firmware helpers `tankie-build` and `tankie-flash` in /usr/local/bin, so a
# fresh system can build (firmware + LittleFS data partition) and flash the
# ESP from the Pi out of the box (see
# https://github.com/a-i-a-d/Tankie/issues/21).
#
# Usage:
#   sudo bash setup.sh
#
# The script is idempotent — safe to re-run to re-apply the configuration.
# ---------------------------------------------------------------------------
set -euo pipefail

# --- pinned, known-good versions -------------------------------------------
MEDIAMTX_VERSION="v1.21.1"
MEDIAMTX_ARCH="linux_arm64"
MEDIAMTX_URL="https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_${MEDIAMTX_ARCH}.tar.gz"
# SHA256 of the official release tarball (pin for integrity)
MEDIAMTX_SHA256="6a3aa635fb60ea9b8d566ec306f0a42ff1b6b52a3942bc2baffbe55880d4c3dd"

INSTALL_DIR="/opt/mediamtx"
BIN="${INSTALL_DIR}/mediamtx"
CONF="${INSTALL_DIR}/mediamtx.yml"
SERVICE="mediamtx.service"
WATCHDOG_SERVICE="wlan0-watchdog.service"
WATCHDOG_SCRIPT="/usr/local/bin/wlan0-watchdog.sh"
WATCHDOG_TIMER="wlan0-watchdog.timer"
ESP_BUILD_SCRIPT="/usr/local/bin/tankie-build"
ESP_FLASH_SCRIPT="/usr/local/bin/tankie-flash"
ESP_ENV="/etc/tankie/flash.env"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_CONF="${SCRIPT_DIR}/mediamtx.yml"
SRC_SERVICE="${SCRIPT_DIR}/mediamtx.service"
SRC_WATCHDOG="${SCRIPT_DIR}/wlan0-watchdog.service"
SRC_WATCHDOG_SCRIPT="${SCRIPT_DIR}/wlan0-watchdog.sh"
SRC_TIMER="${SCRIPT_DIR}/wlan0-watchdog.timer"
SRC_ESP_BUILD="${SCRIPT_DIR}/build.sh"
SRC_ESP_FLASH="${SCRIPT_DIR}/flash.sh"

log()  { printf '\033[1;32m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# --- 0. sanity checks -------------------------------------------------------
[ "$(id -u)" -eq 0 ] || die "Please run as root (sudo bash setup.sh)"

ARCH="$(uname -m)"
if [ "${ARCH}" != "aarch64" ] && [ "${ARCH}" != "arm64" ]; then
  warn "Expected an arm64 (aarch64) Raspberry Pi, detected '${ARCH}'. Continuing, but the pinned binary is arm64."
fi

command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1 \
  || die "curl or wget is required to download mediamtx"

# --- 1. enable the CSI camera interface (best-effort, idempotent) ----------
if command -v raspi-config >/dev/null 2>&1; then
  log "Enabling the camera interface (raspi-config)..."
  raspi-config nonint do_camera >/dev/null 2>&1 || warn "raspi-config do_camera failed (camera may already be enabled)"
fi

# Ensure the camera is auto-detected on the next boot (newer OS uses /boot/firmware)
for CFG in /boot/firmware/config.txt /boot/config.txt; do
  if [ -f "${CFG}" ]; then
    if ! grep -qE '^\s*camera_auto_detect=1' "${CFG}"; then
      log "Setting camera_auto_detect=1 in ${CFG}"
      echo "camera_auto_detect=1" | tee -a "${CFG}" >/dev/null
      REBOOT_NEEDED=1
    fi
    break
  fi
done

# --- 2. (service runs as root, matching the current as-built state) --------

# --- 3. install the mediamtx binary (skip if already the right version) ----
mkdir -p "${INSTALL_DIR}"

need_download=1
if [ -x "${BIN}" ]; then
  if "${BIN}" --version 2>/dev/null | grep -q "${MEDIAMTX_VERSION#v}"; then
    log "mediamtx ${MEDIAMTX_VERSION} already installed at ${BIN} — skipping download"
    need_download=0
  fi
fi

if [ "${need_download}" -eq 1 ]; then
  log "Downloading mediamtx ${MEDIAMTX_VERSION} (${MEDIAMTX_ARCH})..."
  TMP="$(mktemp -d)"
  trap 'rm -rf "${TMP}"' EXIT
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL -o "${TMP}/mediamtx.tar.gz" "${MEDIAMTX_URL}"
  else
    wget -q -O "${TMP}/mediamtx.tar.gz" "${MEDIAMTX_URL}"
  fi

  if [ -n "${MEDIAMTX_SHA256}" ]; then
    log "Verifying SHA256 checksum..."
    ACTUAL="$(sha256sum "${TMP}/mediamtx.tar.gz" | awk '{print $1}')"
    [ "${ACTUAL}" = "${MEDIAMTX_SHA256}" ] || die "Checksum mismatch! Got ${ACTUAL}, expected ${MEDIAMTX_SHA256}"
    log "Checksum OK"
  fi

  tar -xzf "${TMP}/mediamtx.tar.gz" -C "${TMP}"
  install -m 0755 "${TMP}/mediamtx" "${BIN}"
  log "Installed mediamtx to ${BIN}"
fi

# --- 4. install configuration ----------------------------------------------
log "Installing configuration to ${CONF}"
install -m 0644 "${SRC_CONF}" "${CONF}"

# --- 5. install + enable the systemd service -------------------------------
log "Installing systemd service ${SERVICE}"
install -m 0644 "${SRC_SERVICE}" "/etc/systemd/system/${SERVICE}"
systemctl daemon-reload
systemctl enable "${SERVICE}" >/dev/null 2>&1
systemctl restart "${SERVICE}"
sleep 2

# --- 6. install + enable the WiFi watchdog (timer triggers the service) ----
log "Installing WiFi watchdog (${WATCHDOG_SERVICE} + ${WATCHDOG_TIMER})"
install -m 0755 "${SRC_WATCHDOG_SCRIPT}" "${WATCHDOG_SCRIPT}"
install -m 0644 "${SRC_WATCHDOG}" "/etc/systemd/system/${WATCHDOG_SERVICE}"
install -m 0644 "${SRC_TIMER}" "/etc/systemd/system/${WATCHDOG_TIMER}"
systemctl daemon-reload
systemctl enable "${WATCHDOG_TIMER}" >/dev/null 2>&1
systemctl restart "${WATCHDOG_TIMER}"
sleep 1

# --- 7. install the ESP8266 toolchain (arduino-cli + esp8266 core) ---------
# build.sh needs arduino-cli (to compile) and the esp8266 core (which ships the
# xtensa toolchain, esptool for flash.sh, and mklittlefs for the LittleFS data
# partition). Install them on a fresh system so the ESP build/flash path is out
# of the box. Best-effort: a network hiccup warns but does not abort the
# mediamtx setup.
ARDUINO_CLI="$(command -v arduino-cli || true)"
if [ -z "${ARDUINO_CLI}" ]; then
  log "arduino-cli not found — installing to /usr/local/bin …"
  if command -v curl >/dev/null 2>&1; then
    if curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh \
        | BINDIR=/usr/local/bin sh; then
      ARDUINO_CLI=/usr/local/bin/arduino-cli
    else
      warn "arduino-cli install failed — the ESP build/flash helpers will not work until you install it"
    fi
  else
    warn "curl not available — cannot auto-install arduino-cli; install it manually for the ESP build/flash path"
  fi
fi
[ -n "${ARDUINO_CLI}" ] || warn "arduino-cli still missing — skipping the esp8266 core install"

if [ -n "${ARDUINO_CLI}" ]; then
  # Register the esp8266 board index (idempotent; it is the arduino-cli default).
  ESP8266_INDEX="http://arduino.esp8266.com/stable/package_esp8266com_index.json"
  if ! "${ARDUINO_CLI}" config get board_manager.additional_urls 2>/dev/null | grep -qF "${ESP8266_INDEX}"; then
    log "Adding the esp8266 board index to arduino-cli …"
    "${ARDUINO_CLI}" config add board_manager.additional_urls "${ESP8266_INDEX}" \
      || warn "could not add the esp8266 board index"
  fi
  # Install the esp8266 core (a no-op if already installed). This pulls in the
  # xtensa toolchain, esptool, and mklittlefs that build.sh / flash.sh need.
  log "Ensuring the esp8266 core is installed (arduino-cli core install esp8266:esp8266) …"
  "${ARDUINO_CLI}" core install esp8266:esp8266 \
    || warn "esp8266 core install failed — the ESP build/flash helpers will not work until you install it"
fi

# --- 8. install the ESP8266 build/flash helpers ----------------------------
log "Installing ESP8266 firmware helpers (tankie-build, tankie-flash)"
[ -f "${SRC_ESP_BUILD}" ] || die "build.sh not found next to setup.sh: ${SRC_ESP_BUILD}"
[ -f "${SRC_ESP_FLASH}" ] || die "flash.sh not found next to setup.sh: ${SRC_ESP_FLASH}"
install -m 0755 "${SRC_ESP_BUILD}" "${ESP_BUILD_SCRIPT}"
install -m 0755 "${SRC_ESP_FLASH}" "${ESP_FLASH_SCRIPT}"

# The installed scripts resolve the sketch relative to their own location,
# which is /usr/local/bin — so point them at the canonical checkout.
# Existing env files are preserved (idempotent re-runs).
mkdir -p /var/lib/tankie/flash "$(dirname "${ESP_ENV}")"
if [ ! -f "${ESP_ENV}" ]; then
  # Prefer the checkout this setup.sh was run from, then common locations.
  if [ -f "${SCRIPT_DIR}/../tankie/tankie.ino" ]; then
    CHECKOUT="$(cd "${SCRIPT_DIR}/.." && pwd)"
  elif [ -d /opt/tankie ]; then
    CHECKOUT=/opt/tankie
  elif [ -d /root/Tankie ]; then
    CHECKOUT=/root/Tankie
  else
    CHECKOUT=/opt/tankie
    warn "No Tankie checkout found — adjust TANKIE_SKETCH_DIR in ${ESP_ENV} once you clone the repo"
  fi
  cat > "${ESP_ENV}" <<ENVEOF
# Tankie ESP8266 build/flash overrides (sourced by tankie-build / tankie-flash)
TANKIE_SKETCH_DIR=${CHECKOUT}/tankie
TANKIE_FLASH_DIR=/var/lib/tankie/flash
ENVEOF
  chmod 0644 "${ESP_ENV}"
  log "ESP8266 env written: ${ESP_ENV} (TANKIE_SKETCH_DIR=${CHECKOUT}/tankie)"
else
  log "ESP8266 env already present: ${ESP_ENV} (left untouched)"
fi

# --- 9. verify -------------------------------------------------------------
log "Verifying installation..."
"${BIN}" --version | head -1
log "Validating configuration..."
"${BIN}" --validate-conf="${CONF}" && log "Configuration is valid" || warn "Config validation reported a warning"

if systemctl is-active --quiet "${SERVICE}"; then
  log "Service '${SERVICE}' is active."
else
  warn "Service is not active — check: journalctl -u ${SERVICE} -n 50"
fi

if systemctl is-active --quiet "${WATCHDOG_TIMER}"; then
  log "WiFi watchdog timer is active (checks wlan0 every 60s, first check 90s after boot)."
else
  warn "WiFi watchdog timer is not active — check: systemctl status ${WATCHDOG_TIMER}"
fi

if [ -x "${ESP_BUILD_SCRIPT}" ] && [ -x "${ESP_FLASH_SCRIPT}" ]; then
  log "ESP8266 helpers installed: ${ESP_BUILD_SCRIPT}, ${ESP_FLASH_SCRIPT}"
else
  warn "ESP8266 helpers missing — check /usr/local/bin/tankie-{build,flash}"
fi

if ls -d "${HOME}"/.arduino15/packages/esp8266/tools/mklittlefs/*/mklittlefs >/dev/null 2>&1; then
  log "esp8266 core + mklittlefs present (LittleFS data partition build ready)"
else
  warn "esp8266 core / mklittlefs not found — the LittleFS data partition build will be skipped until you run: arduino-cli core install esp8266:esp8266"
fi

# Show the live path status (camera will be 'ready' only once a client connects)
if command -v curl >/dev/null 2>&1; then
  log "Current path status (via local API):"
  curl -fsS http://127.0.0.1:9997/v3/paths/list 2>/dev/null | head -c 400 || true
  echo
fi

log "Done."
cat <<'EOF'

Next steps / how to use:
  * Human (low latency) :  http://<pi>:8889/cam/          (WebRTC player page)
                           http://<pi>:8888/cam/index.m3u8 (LL-HLS)
                           rtsp://<pi>:8554/cam
  * AI capture          :  rtsp://<pi>:8554/cam_low        (H.264, OpenCV/ffmpeg)
                           http://<pi>:8888/cam_low/index.m3u8
  * Is a client online? :  curl -s http://127.0.0.1:9997/v3/paths/list
  * Service status      :  systemctl status mediamtx
  * Logs                :  journalctl -u mediamtx -f
  * WiFi watchdog status:  systemctl status wlan0-watchdog.timer
  * WiFi watchdog logs  :  journalctl -u wlan0-watchdog -f
  * Disable the watchdog:  systemctl disable --now wlan0-watchdog.timer
  * Build ESP8266 fw    :  tankie-build     (firmware + LittleFS data partition)
  * Flash ESP8266 fw    :  tankie-flash     (D1 Mini on /dev/ttyUSB0)

Note: if you just enabled the camera for the first time, reboot once so the
      camera driver loads and /dev/video0 appears.
EOF
