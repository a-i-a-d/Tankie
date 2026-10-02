// Host-side shim for <ESPAsyncWebServer.h> (Tankie unit tests, issue #34).
//
// Provides just enough of the async web server API for tankie.ino and
// wifimanager.cpp to COMPILE on the host. Nothing here is functional:
// the tests drive the WebSocket command handler (handleWebSocketMessage)
// directly, not through the server.
#pragma once

#include <Arduino.h>
#include <ESPAsyncTCP.h>
// The real ESPAsyncWebServer.h includes AsyncWebSocket.h (line 468)
// and FS.h (line 27); the WS types must be complete before
// AsyncWebServer::addHandler/AsyncWebSocket reference them.
#include <AsyncWebSocket.h>
#include <FS.h>

enum { HTTP_GET, HTTP_POST, HTTP_PUT, HTTP_DELETE, HTTP_PATCH };

class AsyncWebParameter {
 public:
  String name() const { return String(); }
  String value() const { return String(); }
  bool isPost() const { return true; }
};

class AsyncWebServerRequest {
 public:
  size_t params() const { return 0; }
  const AsyncWebParameter* getParam(size_t) const { return nullptr; }
  bool hasParam(const String&, bool) const { return false; }
  void send(int code, const String& type, const String& content) {
    (void)code; (void)type; (void)content;
  }
  void send(int code, const String& type, const String& content,
            const String& headers) { (void)code; (void)type; (void)content; (void)headers; }
  // tankie.ino serves the web UI from LittleFS this way (the real
  // signature takes FS&; the const ref also binds the global LittleFS).
  void send(const FSClass& fs, const String& path, const String& contentType = String(),
            bool download = false, String (*callback)(const String&) = nullptr) {
    (void)fs; (void)path; (void)contentType; (void)download; (void)callback;
  }
};


class AsyncWebServer {
 public:
  AsyncWebServer(uint16_t port) : port_(port) {}
  void on(const String& uri, int method, void (*fn)(AsyncWebServerRequest*)) {
    (void)uri; (void)method; (void)fn;
  }
  // The ElegantOTA library (v4) registers /ota/upload with a body handler
  // (the real library's 5-arg on()); the shim accepts and ignores it so
  // the host build compiles unchanged.
  void on(const String& uri, int method, void (*fn)(AsyncWebServerRequest*),
          void (*upload)(AsyncWebServerRequest*),
          void (*body)(AsyncWebServerRequest*, uint8_t*, size_t, size_t, size_t)) {
    (void)uri; (void)method; (void)fn; (void)upload; (void)body;
  }
  void onNotFound(void (*fn)(AsyncWebServerRequest*)) { (void)fn; }
  void onRequestBody(void (*fn)(AsyncWebServerRequest*, uint8_t*, size_t)) { (void)fn; }
  void addHandler(AsyncWebSocket* ws) { (void)ws; }
  void begin() {}
 private:
  uint16_t port_;
};

