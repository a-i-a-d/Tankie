#ifndef _TANKIE_WIFIMANAGER_H_
#define _TANKIE_WIFIMANAGER_H_

#include <Arduino.h>
#include <ESP8266WiFi.h>
#include <ESPAsyncWebServer.h>
#include <FS.h>

// ---------------------------------------------------------------------------
// WiFiManager - WiFi configuration for the Tankie firmware
//
// Replaces the WiFiManager *library* (which clashes with
// ESPAsyncWebServer / ElegantOTA) with a small class that implements the
// same idea on top of the already-running ESPAsyncWebServer:
//
//   1. Credentials + static IP are read from files on LittleFS
//      (ssid.txt / pass.txt / ip.txt / gateway.txt).
//   2. If the stored network connects in time -> STA mode, done.
//   3. Otherwise the ESP opens its own access point (default "tankie-esp")
//      and the web form (data/wifimanager.html) served at
//      http://192.168.4.1:8080 (dedicated config server, port 8080) can be
//      used to enter SSID / password / IP / gateway.
//   4. The form POSTs back to the ESP; the values are written to LittleFS
//      and the ESP reboots, retrying step 2 with the new credentials.
//
// Approach based on:
//   https://randomnerdtutorials.com/esp8266-nodemcu-wi-fi-manager-asyncwebserver/
// Requested in: https://github.com/a-i-a-d/Tankie/issues/21
//
// Usage (see tankie.ino):
//   WiFiManager wifiManager;
//   // in setup(), before the web server is started:
//   wifiManager.begin("tankie-esp", "tankie1234");
//   // in loop():
//   wifiManager.loop();   // performs the deferred reboot after a config save
//   // the config server on port 8080 serves wifimanager.html while
//   // inConfigMode() is true (port 80 always serves the tank page)
// ---------------------------------------------------------------------------

class WiFiManager {
 public:
  WiFiManager();

  // Begin WiFi: try STA with the stored credentials, fall back to the
  // config-portal AP on failure. `apSsid` / `apPassword` name the fallback
  // AP (open AP when apPassword is NULL/empty).
  // Returns true when connected in STA mode, false when the portal AP is up.
  bool begin(const char* apSsid, const char* apPassword = NULL);

  // Call from loop(): performs the deferred reboot after a config was saved.
  void loop();

  // True while the ESP is serving the config portal (stored network failed).
  bool inConfigMode() const { return _inConfigMode; }

  // Handle the POST of the config form: stores the submitted fields in
  // LittleFS and schedules the reboot. Call from the config server's
  // (port 8080) "/" POST handler.
  void handleConfigPost(AsyncWebServerRequest* request);

  // Read-only access to the active configuration (for logging).
  const String& ssid() const { return _ssid; }
  const String& password() const { return _pass; }
  const String& ip() const { return _ip; }
  const String& gateway() const { return _gateway; }

 private:
  String _ssid;
  String _pass;
  String _ip;
  String _gateway;
  String _apSsid;
  String _apPassword;
  bool _inConfigMode = false;
  bool _rebootPending = false;

  bool connectSTA(unsigned long timeoutMs);
  void startPortal();
  String readFile(const char* path);
  bool writeFile(const char* path, const String& content);
};

#endif  // _TANKIE_WIFIMANAGER_H_
