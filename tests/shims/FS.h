// Host-side shim for <FS.h> (Tankie unit tests, issue #34).
//
// tankie.ino / wifimanager.cpp reference the LittleFS global object and
// the File/Dir types. The host tests never mount a filesystem - this is
// a compile-only stub (LittleFS is defined in LittleFS.h).
#pragma once

#include <Arduino.h>
#include <LittleFS.h>
