// WiFi config blob — issue #54.
//
// The network configuration (SSID / password / static IP / gateway) is
// persisted in the reserved EEPROM flash sector 0x3FB000 (4 KB) instead of
// LittleFS, so it survives BOTH firmware OTA (U_FLASH -> 0x0) and
// filesystem OTA (U_FS -> 0x200000). A failed/corrupt fs-OTA no longer
// strands the ESP on the config-portal AP.
//
// This header is the single source of truth for the blob layout and the
// encode/decode API. It is pure C++ (no flash access) so it is fully
// host-testable; the only file that touches <EEPROM.h> is wificfg.cpp.
//
// Blob layout (little-endian), at the start of the 4 KB sector:
//   offset 0   : uint32 magic   (WIFICFG_MAGIC, "TANE")
//   offset 4   : uint16 version (WIFICFG_VERSION)
//   offset 6   : uint16 ssid_len  + ssid bytes
//   ...        : uint16 pass_len  + pass bytes
//   ...        : uint16 ip_len    + ip bytes   (0 = DHCP)
//   ...        : uint16 gateway_len + gateway bytes
//   ...        : uint32 crc32     (IEEE 802.3, over everything above)
//
// SSIDs/passwords are short, so the whole blob is well under 256 bytes;
// the remaining ~3.7 KB of the sector is spare room for future config.
#ifndef _TANKIE_WIFICFG_H_
#define _TANKIE_WIFICFG_H_

#include <Arduino.h>
#include <stdint.h>
#include <stddef.h>

// Blob magic + version (compared as little-endian values on the wire).
#define WIFICFG_MAGIC   0x544B4E45u  // "TANE"
#define WIFICFG_VERSION 1u

// Field length caps (keep the blob well under the 4 KB sector).
#define WIFICFG_MAX_SSID      64
#define WIFICFG_MAX_PASS      64
#define WIFICFG_MAX_IP        16    // "255.255.255.255"
#define WIFICFG_MAX_GATEWAY   16

// The in-memory representation of the stored network configuration.
struct WifiCfg {
  String ssid;
  String pass;
  String ip;        // empty or "dhcp" = DHCP
  String gateway;
};

// ---------------------------------------------------------------------------
// Pure codec (fully host-testable, no flash access)
// ---------------------------------------------------------------------------

// Encode a WifiCfg into a binary blob (magic/version/lengths/fields/CRC32).
// Returns the number of bytes written to `out`, or 0 when a field exceeds
// its cap or `out` is too small.
size_t wifiCfgEncode(const WifiCfg& cfg, uint8_t* out, size_t outSize);

// Decode a binary blob into a WifiCfg. Returns true only when the magic,
// version and trailing CRC32 are all valid; false for a blank, corrupt or
// wrong-version sector (so callers can fall back cleanly, never crash).
bool wifiCfgDecode(const uint8_t* in, size_t inSize, WifiCfg* outCfg);

// CRC32 (IEEE 802.3, reflected, poly 0xEDB88320, init/xorout 0xFFFFFFFF)
// over a byte range. Exposed for the host tests.
uint32_t wifiCfgCrc32(const uint8_t* data, size_t len);

// ---------------------------------------------------------------------------
// EEPROM adapter (the only place <EEPROM.h> is used - see wificfg.cpp)
// ---------------------------------------------------------------------------

// Load the config from the EEPROM sector. Returns true when a valid config
// was found, false when the sector is blank/corrupt.
bool wifiCfgLoad(WifiCfg* out);

// Save the config to the EEPROM sector (erase + write). Returns true on
// success. This is the single source of truth for the network config.
bool wifiCfgSave(const WifiCfg& cfg);

// Wipe the EEPROM sector (erase, leave blank). Returns true on success.
bool wifiCfgWipe();

#endif  // _TANKIE_WIFICFG_H_
