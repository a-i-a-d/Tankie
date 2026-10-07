#include "wificfg.h"

// ---------------------------------------------------------------------------
// Little-endian helpers (the blob layout is little-endian, matching the
// ESP8266's native byte order - no swaps needed on the target).
// ---------------------------------------------------------------------------
static void putU16(uint8_t* p, uint16_t v) { p[0] = (uint8_t)(v & 0xFF); p[1] = (uint8_t)(v >> 8); }
static void putU32(uint8_t* p, uint32_t v) {
  p[0] = (uint8_t)(v & 0xFF); p[1] = (uint8_t)((v >> 8) & 0xFF);
  p[2] = (uint8_t)((v >> 16) & 0xFF); p[3] = (uint8_t)((v >> 24) & 0xFF);
}
static uint16_t getU16(const uint8_t* p) {
  return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}
static uint32_t getU32(const uint8_t* p) {
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
         ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

// ---------------------------------------------------------------------------
// CRC32 (IEEE 802.3: reflected, poly 0xEDB88320, init/xorout 0xFFFFFFFF)
// Bit-by-bit (no table): the blob is tiny (<256 B), so this is fast enough
// and keeps the code path identical on the ESP8266 and the host.
// ---------------------------------------------------------------------------
uint32_t wifiCfgCrc32(const uint8_t* data, size_t len) {
  uint32_t crc = 0xFFFFFFFFu;
  for (size_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (int b = 0; b < 8; b++) {
      crc = (crc >> 1) ^ (0xEDB88320u & (uint32_t)(-(int32_t)(crc & 1u)));
    }
  }
  return crc ^ 0xFFFFFFFFu;
}

// ---------------------------------------------------------------------------
// Encode
// ---------------------------------------------------------------------------
size_t wifiCfgEncode(const WifiCfg& cfg, uint8_t* out, size_t outSize) {
  if (!out || outSize < 16) return 0;
  if (cfg.ssid.length() > WIFICFG_MAX_SSID) return 0;
  if (cfg.pass.length() > WIFICFG_MAX_PASS) return 0;
  if (cfg.ip.length() > WIFICFG_MAX_IP) return 0;
  if (cfg.gateway.length() > WIFICFG_MAX_GATEWAY) return 0;

  size_t total = 8 +
                 2 + cfg.ssid.length() +
                 2 + cfg.pass.length() +
                 2 + cfg.ip.length() +
                 2 + cfg.gateway.length() +
                 4;   // trailing CRC32
  if (total > outSize) return 0;

  size_t p = 0;
  putU32(out + p, WIFICFG_MAGIC); p += 4;
  putU16(out + p, WIFICFG_VERSION); p += 2;

  const struct { const String& s; uint16_t cap; } fields[] = {
    { cfg.ssid,    (uint16_t)WIFICFG_MAX_SSID },
    { cfg.pass,    (uint16_t)WIFICFG_MAX_PASS },
    { cfg.ip,      (uint16_t)WIFICFG_MAX_IP },
    { cfg.gateway, (uint16_t)WIFICFG_MAX_GATEWAY },
  };
  for (const auto& f : fields) {
    uint16_t len = (uint16_t)f.s.length();
    putU16(out + p, len); p += 2;
    if (len) { memcpy(out + p, f.s.c_str(), len); p += len; }
  }

  uint32_t crc = wifiCfgCrc32(out, p);
  putU32(out + p, crc); p += 4;
  return p;
}

// ---------------------------------------------------------------------------
// Decode
// ---------------------------------------------------------------------------
bool wifiCfgDecode(const uint8_t* in, size_t inSize, WifiCfg* outCfg) {
  if (!in || !outCfg || inSize < 12) return false;   // magic+version+>=1 field len+CRC

  if (getU32(in) != WIFICFG_MAGIC) return false;
  if (getU16(in + 4) != WIFICFG_VERSION) return false;

  size_t p = 6;
  const uint16_t caps[] = {
    (uint16_t)WIFICFG_MAX_SSID, (uint16_t)WIFICFG_MAX_PASS,
    (uint16_t)WIFICFG_MAX_IP,   (uint16_t)WIFICFG_MAX_GATEWAY,
  };
  String* fields[] = { &outCfg->ssid, &outCfg->pass, &outCfg->ip, &outCfg->gateway };

  for (int i = 0; i < 4; i++) {
    if (p + 2 > inSize) return false;
    uint16_t len = getU16(in + p); p += 2;
    if (len > caps[i]) return false;                 // corrupt/oversized field
    if (p + len > inSize) return false;              // runs past the blob
    fields[i]->clear();
    if (len) {
      // The blob is not null-terminated, so copy the field into a temp
      // buffer and construct the String from that (the core's String has
      // no (const char*, len) constructor - String(const char*) reads until
      // a null byte, which would bleed into the next field).
      char tmp[WIFICFG_MAX_SSID + 1];
      memcpy(tmp, in + p, len);
      tmp[len] = '\0';
      *fields[i] = String(tmp);
    }
    p += len;
  }

  if (p + 4 > inSize) return false;
  uint32_t stored = getU32(in + p);
  if (wifiCfgCrc32(in, p) != stored) return false;   // CRC mismatch -> corrupt

  return true;
}

// ---------------------------------------------------------------------------
// EEPROM adapter - the ONLY place the flash sector is touched.
//
// The core's EEPROM library maps its default instance to the reserved
// sector 0x3FB000 (sector 1019 of the 4M2M layout) - see
// libraries/EEPROM/EEPROM.cpp. begin() flash-reads the sector, commit()
// erases + flash-writes it. The sector is outside both the firmware
// region (0x0..0x1FA000) and the LittleFS region (0x200000..0x3FA000),
// so both OTA paths leave our config untouched.
// ---------------------------------------------------------------------------
#include <EEPROM.h>

static const size_t WIFICFG_SECTOR_SIZE = 4096;

bool wifiCfgLoad(WifiCfg* out) {
  EEPROM.begin(WIFICFG_SECTOR_SIZE);
  const uint8_t* data = EEPROM.getConstDataPtr();
  if (!data) return false;
  bool ok = wifiCfgDecode(data, WIFICFG_SECTOR_SIZE, out);
  EEPROM.end();
  return ok;
}

bool wifiCfgSave(const WifiCfg& cfg) {
  // Encode directly into the EEPROM library's own heap buffer (begin() has
  // already allocated + flash-read the 4 KB sector). This avoids a second
  // 4 KB stack buffer, which matters on the ESP8266's tight RAM budget.
  EEPROM.begin(WIFICFG_SECTOR_SIZE);
  uint8_t* data = EEPROM.getDataPtr();
  if (!data) {
    Serial.println("[wificfg] EEPROM.begin failed");
    return false;
  }
  memset(data, 0xFF, WIFICFG_SECTOR_SIZE);   // blank the spare tail (commit erases+writes the full sector)
  size_t n = wifiCfgEncode(cfg, data, WIFICFG_SECTOR_SIZE);
  if (n == 0) {
    Serial.println("[wificfg] encode failed (field too long?)");
    EEPROM.end();
    return false;
  }
  bool ok = EEPROM.commit();
  EEPROM.end();
  if (!ok) Serial.println("[wificfg] EEPROM.commit failed");
  return ok;
}

bool wifiCfgWipe() {
  // Blank the whole sector (all 0xFF) so the config is gone and the next
  // boot falls back to the config portal.
  EEPROM.begin(WIFICFG_SECTOR_SIZE);
  uint8_t* data = EEPROM.getDataPtr();
  if (!data) {
    Serial.println("[wificfg] EEPROM.begin failed (wipe)");
    return false;
  }
  memset(data, 0xFF, WIFICFG_SECTOR_SIZE);
  bool ok = EEPROM.commit();
  EEPROM.end();
  if (!ok) Serial.println("[wificfg] EEPROM.commit failed (wipe)");
  return ok;
}
