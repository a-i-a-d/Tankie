// Host-side shim for <ESP8266WiFi.h> (Tankie unit tests, issue #34).
//
// tankie.ino (the WS fallback handler under test) pulls in the full
// network stack; on the host we only need the symbols it references to
// COMPILE (the tests exercise the handler logic directly - the shim
// WiFi object is never used at runtime). IPAddress lives in the
// Arduino.h shim (SerialClass needs it for its print overloads).
#pragma once

#include <Arduino.h>

typedef enum {
  WL_IDLE_STATUS = 0,
  WL_NO_SSID_AVAIL,
  WL_SCAN_COMPLETED,
  WL_CONNECTED,
  WL_CONNECT_FAILED,
  WL_CONNECTION_LOST,
  WL_DISCONNECTED
} wl_status_t;

typedef enum { WIFI_OFF, WIFI_STA, WIFI_AP, WIFI_STA_AP } wifi_mode_t;

class WiFiClass {
 public:
  wifi_mode_t mode() { return WIFI_STA; }
  wifi_mode_t mode(wifi_mode_t) { return WIFI_STA; }
  bool begin(const char* ssid, const char* pass = NULL) { (void)ssid; (void)pass; return false; }
  bool config(IPAddress, IPAddress, IPAddress) { return true; }
  wl_status_t status() { return WL_IDLE_STATUS; }
  IPAddress localIP() { return IPAddress(); }
  void softAP(const char* ssid, const char* pass = NULL) { (void)ssid; (void)pass; }
  void softAPConfig(IPAddress, IPAddress, IPAddress) {}
  IPAddress softAPIP() { return IPAddress(); }
};

extern WiFiClass WiFi;

// The core's ESP object: wifimanager.cpp calls ESP.restart() after a
// config save. Compile-only on the host.
class ESPClass {
 public:
  void restart() {}
};
extern ESPClass ESP;
