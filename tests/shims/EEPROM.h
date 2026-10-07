// Host-side shim for <EEPROM.h> (Tankie unit tests, issue #54).
//
// Mirrors the esp8266 core's EEPROM library API (libraries/EEPROM/EEPROM.h)
// so wificfg.cpp compiles UNCHANGED against either the real library
// (firmware build) or this shim (host tests).
//
// The shim is backed by a 4 KB buffer pre-filled with 0xFF - the same
// "blank flash sector" the real chip starts with - so the
// blank-sector / corrupt-sector fallback paths are exercised realistically.
// Tests can inspect/seed the sector via the host_eeprom_* hooks.
#pragma once

#include <Arduino.h>
#include <cstdint>
#include <cstring>

// The sector size the core's default EEPROM instance maps (0x3FB000, 4 KB).
#define HOST_EEPROM_SECTOR_SIZE 4096

// Test hooks (defined in test_main.cpp): the raw 4 KB sector image.
extern uint8_t host_eeprom_sector[HOST_EEPROM_SECTOR_SIZE];
inline void host_eeprom_reset() {
  memset(host_eeprom_sector, 0xFF, HOST_EEPROM_SECTOR_SIZE);
}

class EEPROMClass {
 public:
  EEPROMClass() {}
  EEPROMClass(uint32_t sector) { (void)sector; }

  void begin(size_t size) {
    if (size <= 0) return;
    if (size > HOST_EEPROM_SECTOR_SIZE) size = HOST_EEPROM_SECTOR_SIZE;
    size = (size + 3) & ~3u;
    _size = size;
    _dirty = false;
    // "flash read": the sector image is already the backing store.
  }

  bool end() {
    bool retval = commit();
    _size = 0;
    return retval;
  }

  uint8_t read(int address) {
    if (address < 0 || (size_t)address >= _size) return 0;
    return host_eeprom_sector[address];
  }

  void write(int address, uint8_t value) {
    if (address < 0 || (size_t)address >= _size) return;
    if (host_eeprom_sector[address] != value) {
      host_eeprom_sector[address] = value;
      _dirty = true;
    }
  }

  bool commit() {
    if (!_size) return false;
    if (!_dirty) return true;
    _dirty = false;
    return true;   // the backing store is the sector image itself
  }

  uint8_t* getDataPtr() {
    if (!_size) return nullptr;
    _dirty = true;
    return host_eeprom_sector;
  }

  uint8_t const* getConstDataPtr() const {
    if (!_size) return nullptr;
    return host_eeprom_sector;
  }

  size_t length() { return _size; }

  uint8_t& operator[](int address) { return host_eeprom_sector[address]; }
  uint8_t const& operator[](int address) const { return host_eeprom_sector[address]; }

 private:
  size_t _size = 0;
  bool _dirty = false;
};

extern EEPROMClass EEPROM;
