// Host-side shim for <ESPAsyncWebServer.h> (Tankie unit tests, issue #34).
//
// Provides just enough of the async web server API for tankie.ino and
// wifimanager.cpp to COMPILE on the host. Nothing here is functional:
// the tests drive the command handlers (handleWebSocketMessage,
// otaPowersaveHandler) directly, not through the server.
//
// issue #133 additions (test hooks, compile-only on the device):
//   * host_wsQueryParam — the "on" query-param value the handler's
//     getParam("on") call returns (injected by the /ota/powersave tests;
//     empty = no query param, like a real request without one).
//   * host_lastSendCode / host_lastSendBody — recorded by the plain
//     send(code, type, body) overload so the tests can assert on the HTTP
//     status + body the handler produced.
//
// The hooks are C++17 inline variables (the shim is a header with no
// separate .cpp), so every translation unit shares the same storage.
#pragma once

#include <Arduino.h>
#include <ESPAsyncTCP.h>
// The real ESPAsyncWebServer.h includes AsyncWebSocket.h (line 468)
// and FS.h (line 27); the WS types must be complete before
// AsyncWebServer::addHandler/AsyncWebSocket reference them.
#include <AsyncWebSocket.h>
#include <FS.h>

enum { HTTP_GET, HTTP_POST, HTTP_PUT, HTTP_DELETE, HTTP_PATCH };

// issue #133: test hooks (shared across all TUs via C++17 inline variables).
inline std::string host_wsQueryParam = "";
inline int host_lastSendCode = 0;
inline std::string host_lastSendBody = "";

class AsyncWebParameter {
 public:
  String name() const { return String(); }
  // issue #133: returns the injected query-param value ("" = no param).
  String value() const { return String(host_wsQueryParam.c_str()); }
  bool isPost() const { return true; }
};

class AsyncWebServerRequest {
 public:
  size_t params() const { return 0; }
  // Index form (wifimanager.cpp's param loop) - no params on the host.
  const AsyncWebParameter* getParam(size_t) const { return nullptr; }
  // Name form (tankie.ino's /ota/powersave handler, issue #133).
  const AsyncWebParameter* getParam(const String& name) const {
    if (name != "on" || host_wsQueryParam.empty()) return nullptr;
    static const AsyncWebParameter p;
    return &p;
  }
  bool hasParam(const String& name, bool post = false) const {
    (void)post;
    return name == "on" && !host_wsQueryParam.empty();
  }
  void send(int code, const String& type, const String& content) {
    (void)type;
    // issue #133: record for the host tests.
    host_lastSendCode = code;
    host_lastSendBody = content.c_str();
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
