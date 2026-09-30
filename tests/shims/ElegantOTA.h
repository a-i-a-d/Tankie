// Host-side shim for <ElegantOTA.h> (Tankie unit tests, issue #34).
//
// tankie.ino starts ElegantOTA in setup() (ElegantOTA.begin(&server)) and
// polls it in loop() (ElegantOTA.loop()). The host tests never call
// setup()/loop(), so this is a compile-only stub matching the real
// library's instance-based API (extern ElegantOTAClass ElegantOTA).
#pragma once

#include <Arduino.h>
#include <ESPAsyncWebServer.h>

class ElegantOTAClass {
 public:
  void begin(AsyncWebServer* server, const char* username = "",
             const char* password = "") { (void)server; (void)username; (void)password; }
  void loop() {}
};

extern ElegantOTAClass ElegantOTA;
