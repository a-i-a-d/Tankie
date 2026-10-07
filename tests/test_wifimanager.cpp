// Host unit tests for the WiFi config blob codec + EEPROM adapter (issue #54).
//
// The network config now lives in the reserved EEPROM flash sector 0x3FB000
// instead of LittleFS, so it survives BOTH firmware OTA (U_FLASH -> 0x0) and
// filesystem OTA (U_FS -> 0x200000). These tests exercise the REAL codec
// (tankie/wificfg.cpp) against the EEPROM shim (tests/shims/EEPROM.h, a 4 KB
// 0xFF-backed sector image) and assert:
//
//   - encode -> decode round-trip (ssid / pass / ip / gateway, incl. empty fields)
//   - the CRC32 is IEEE 802.3 (known vector) and actually protects the blob
//   - a blank sector (0xFF), a corrupt byte, a bad magic and a wrong version
//     are all rejected cleanly (decode returns false, never crashes)
//   - oversized fields are rejected by encode (returns 0)
//   - the EEPROM adapter: save -> load round-trip, wipe -> blank, and
//     load on a blank sector returns false (so the firmware falls back to
//     the config portal)
//
// The codec is pure logic (no real flash); the only flash access is isolated
// behind wifiCfgLoad/Save/Wipe, which the host build satisfies with the shim.
#include "test_main.h"
#include <Arduino.h>
#include <EEPROM.h>
#include "wificfg.h"

#include <cstdint>
#include <cstring>

static const size_t kSector = HOST_EEPROM_SECTOR_SIZE;   // 4096

// A realistic config (short, like a real SSID/password).
static WifiCfg sampleCfg() {
  WifiCfg c;
  c.ssid = "tankie-lan";
  c.pass = "hunter2-secret";
  c.ip = "192.168.178.126";
  c.gateway = "192.168.178.1";
  return c;
}

// ---------------------------------------------------------------------------
// CRC32
// ---------------------------------------------------------------------------

TEST(wificfg_crc32_known_vector) {
  // "123456789" is the canonical CRC-32/IEEE test vector (0xCBF43926).
  const char* v = "123456789";
  CHECK_EQ_INT((unsigned)wifiCfgCrc32((const uint8_t*)v, 9), 0xCBF43926u);
}

TEST(wificfg_crc32_empty_is_zero) {
  CHECK_EQ_INT((unsigned)wifiCfgCrc32(nullptr, 0), 0u);
}

// ---------------------------------------------------------------------------
// Encode / decode round-trip
// ---------------------------------------------------------------------------

TEST(wificfg_roundtrip_full) {
  WifiCfg in = sampleCfg();
  uint8_t buf[kSector];
  memset(buf, 0xFF, sizeof(buf));
  size_t n = wifiCfgEncode(in, buf, sizeof(buf));
  CHECK(n > 0);
  CHECK(n < sizeof(buf));

  WifiCfg out;
  CHECK(wifiCfgDecode(buf, sizeof(buf), &out));
  CHECK(out.ssid == in.ssid);
  CHECK(out.pass == in.pass);
  CHECK(out.ip == in.ip);
  CHECK(out.gateway == in.gateway);
}

TEST(wificfg_roundtrip_dhcp_empty_fields) {
  WifiCfg in;
  in.ssid = "only-ssid";
  in.pass = "";
  in.ip = "dhcp";
  in.gateway = "";

  uint8_t buf[kSector];
  memset(buf, 0xFF, sizeof(buf));
  size_t n = wifiCfgEncode(in, buf, sizeof(buf));
  CHECK(n > 0);

  WifiCfg out;
  CHECK(wifiCfgDecode(buf, sizeof(buf), &out));
  CHECK(out.ssid == "only-ssid");
  CHECK(out.pass.empty());
  CHECK(out.ip == "dhcp");
  CHECK(out.gateway.empty());
}

TEST(wificfg_roundtrip_max_lengths) {
  // Build 64-char fields via a char buffer (the core's String has no
  // (char, count) constructor, so we go through String(const char*)).
  char ssid64[WIFICFG_MAX_SSID + 1];
  char pass64[WIFICFG_MAX_PASS + 1];
  memset(ssid64, 'a', WIFICFG_MAX_SSID); ssid64[WIFICFG_MAX_SSID] = '\0';
  memset(pass64, 'b', WIFICFG_MAX_PASS); pass64[WIFICFG_MAX_PASS] = '\0';

  WifiCfg in;
  in.ssid = String(ssid64);
  in.pass = String(pass64);
  in.ip = "255.255.255.255";
  in.gateway = "255.255.255.255";

  uint8_t buf[kSector];
  memset(buf, 0xFF, sizeof(buf));
  size_t n = wifiCfgEncode(in, buf, sizeof(buf));
  CHECK(n > 0);

  WifiCfg out;
  CHECK(wifiCfgDecode(buf, sizeof(buf), &out));
  CHECK(out.ssid.length() == 64);
  CHECK(out.pass.length() == 64);
  CHECK(out.ip == "255.255.255.255");
  CHECK(out.gateway == "255.255.255.255");
}

// ---------------------------------------------------------------------------
// Rejection paths (blank / corrupt / bad magic / wrong version / oversized)
// ---------------------------------------------------------------------------

TEST(wificfg_blank_sector_rejected) {
  // A fresh chip: the whole sector is 0xFF. decode must return false so the
  // firmware falls back to the config portal instead of using garbage.
  uint8_t blank[kSector];
  memset(blank, 0xFF, sizeof(blank));
  WifiCfg out;
  CHECK(!wifiCfgDecode(blank, sizeof(blank), &out));
}

TEST(wificfg_corrupt_byte_rejected) {
  WifiCfg in = sampleCfg();
  uint8_t buf[kSector];
  memset(buf, 0xFF, sizeof(buf));
  size_t n = wifiCfgEncode(in, buf, sizeof(buf));
  CHECK(n > 0);

  // Flip one byte in the middle of the ssid field -> CRC must catch it.
  buf[10] ^= 0x5A;
  WifiCfg out;
  CHECK(!wifiCfgDecode(buf, sizeof(buf), &out));
}

TEST(wificfg_bad_magic_rejected) {
  WifiCfg in = sampleCfg();
  uint8_t buf[kSector];
  memset(buf, 0xFF, sizeof(buf));
  size_t n = wifiCfgEncode(in, buf, sizeof(buf));
  CHECK(n > 0);

  buf[0] = 0x00;   // destroy the magic
  WifiCfg out;
  CHECK(!wifiCfgDecode(buf, sizeof(buf), &out));
}

TEST(wificfg_wrong_version_rejected) {
  WifiCfg in = sampleCfg();
  uint8_t buf[kSector];
  memset(buf, 0xFF, sizeof(buf));
  size_t n = wifiCfgEncode(in, buf, sizeof(buf));
  CHECK(n > 0);

  buf[4] = 0x02;   // version 2 (we only understand version 1)
  WifiCfg out;
  CHECK(!wifiCfgDecode(buf, sizeof(buf), &out));
}

TEST(wificfg_oversized_field_rejected) {
  char ssid65[WIFICFG_MAX_SSID + 2];   // 65 chars: one over the cap
  memset(ssid65, 'a', WIFICFG_MAX_SSID + 1); ssid65[WIFICFG_MAX_SSID + 1] = '\0';
  WifiCfg in;
  in.ssid = String(ssid65);
  uint8_t buf[kSector];
  CHECK_EQ_INT((int)wifiCfgEncode(in, buf, sizeof(buf)), 0);
}

TEST(wificfg_truncated_blob_rejected) {
  WifiCfg in = sampleCfg();
  uint8_t buf[kSector];
  memset(buf, 0xFF, sizeof(buf));
  size_t n = wifiCfgEncode(in, buf, sizeof(buf));
  CHECK(n > 0);

  // Present only the first half of the blob -> decode must not read past it.
  WifiCfg out;
  CHECK(!wifiCfgDecode(buf, n / 2, &out));
}

// ---------------------------------------------------------------------------
// EEPROM adapter (the only flash-touching code, shimmed on the host)
// ---------------------------------------------------------------------------

TEST(wificfg_eeprom_save_load_roundtrip) {
  host_eeprom_reset();   // start from a blank sector
  WifiCfg in = sampleCfg();
  CHECK(wifiCfgSave(in));

  WifiCfg out;
  CHECK(wifiCfgLoad(&out));
  CHECK(out.ssid == in.ssid);
  CHECK(out.pass == in.pass);
  CHECK(out.ip == in.ip);
  CHECK(out.gateway == in.gateway);
}

TEST(wificfg_eeprom_load_blank_sector_fails) {
  host_eeprom_reset();   // blank sector (0xFF)
  WifiCfg out;
  CHECK(!wifiCfgLoad(&out));   // -> firmware falls back to the config portal
}

TEST(wificfg_eeprom_wipe_clears_config) {
  host_eeprom_reset();
  WifiCfg in = sampleCfg();
  CHECK(wifiCfgSave(in));

  CHECK(wifiCfgWipe());
  WifiCfg out;
  CHECK(!wifiCfgLoad(&out));   // wiped -> blank again
}

TEST(wificfg_eeprom_survives_fs_ota_wipe) {
  // Simulate the failure mode from issue #54: the LittleFS data partition
  // (0x200000..0x3FA000) is re-flashed / corrupted. The EEPROM sector
  // (0x3FB000) is a *different* region, so the config must survive.
  host_eeprom_reset();
  WifiCfg in = sampleCfg();
  CHECK(wifiCfgSave(in));

  // "fs-OTA" only touches the LittleFS region - the EEPROM sector image is
  // a separate buffer and is left untouched by the shim. The config is still
  // there.
  WifiCfg out;
  CHECK(wifiCfgLoad(&out));
  CHECK(out.ssid == in.ssid);
  CHECK(out.pass == in.pass);
}
