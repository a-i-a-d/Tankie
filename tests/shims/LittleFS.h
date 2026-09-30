// Host-side shim for "LittleFS.h" (Tankie unit tests, issue #34).
//
// Compile-only stubs for the File / Dir API surface tankie.ino and
// wifimanager.cpp use (open/readString/exists/openDir/...). The host
// tests never mount a filesystem.
#pragma once

#include <Arduino.h>
#include <ctime>

class File {
 public:
  File() {}
  explicit operator bool() const { return false; }
  bool isDirectory() const { return false; }
  size_t size() const { return 0; }
  String readString() { return String(); }
  String readString(size_t) { return String(); }
  size_t print(const String&) { return 0; }
  size_t println(const String&) { return 0; }
  void close() {}
  time_t getCreationTime() { return 0; }
  time_t getLastWrite() { return 0; }
};

class Dir {
 public:
  bool next() { return false; }
  String fileName() { return String(); }
  File openFile(const char*) { return File(); }
};

class FSClass {
 public:
  bool begin() { return false; }
  bool exists(const char*) const { return false; }
  File open(const char*, const char*) { return File(); }
  Dir openDir(const char*) { return Dir(); }
};

extern FSClass LittleFS;
