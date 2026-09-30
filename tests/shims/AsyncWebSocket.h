// Host-side shim for <AsyncWebSocket.h> (Tankie unit tests, issue #34).
//
// Mirrors the type surface of the real ESPAsyncWebServer library's
// AsyncWebSocket.h (AwsFrameInfo layout, AwsEventHandler signature,
// event types) so tankie.ino compiles UNCHANGED against either the real
// library (firmware build) or this shim (host tests).
//
// One addition over the real library: a capture hook for tests.
//   host_ws_text_all - every AsyncWebSocket::textAll() payload appended
//                      here, so tests can assert on the state/watchdog
//                      broadcasts the handler triggers.
#pragma once

#include <Arduino.h>
#include <cstdint>
#include <functional>

// Layout-compatible with the real library's AwsFrameInfo (AsyncWebSocket.h).
typedef struct {
  uint8_t  message_opcode;
  uint32_t num;
  uint8_t  final;
  uint8_t  masked;
  uint8_t  opcode;
  uint64_t len;
  uint8_t  mask[4];
  uint64_t index;
} AwsFrameInfo;

typedef enum { WS_CONTINUATION, WS_TEXT, WS_BINARY, WS_DISCONNECT = 0x08,
               WS_PING, WS_PONG } AwsFrameType;

typedef enum { WS_EVT_CONNECT, WS_EVT_DISCONNECT, WS_EVT_PONG,
               WS_EVT_ERROR, WS_EVT_DATA } AwsEventType;

class AsyncWebSocket;

// Minimal client surface (the real library's AsyncWebSocketClient has
// id() + remoteIP()); tankie.ino's eventHandler() logs both.
class AsyncWebSocketClient {
 public:
  uint32_t id() { return 0; }
  IPAddress remoteIP() { return IPAddress(); }
};

// Capture hook (host tests only): every textAll() payload is appended
// here so tests can assert on the state/watchdog broadcasts. Defined
// in tests/test_main.cpp.
extern std::string host_ws_text_all;

// Same signature as the real library's AwsEventHandler (std::function).
typedef std::function<void(AsyncWebSocket * server, AsyncWebSocketClient * client,
                           AwsEventType type, void * arg, uint8_t *data, size_t len)>
    AwsEventHandler;

class AsyncWebSocket {
 public:
  explicit AsyncWebSocket(const String& path) : path_(path) {}
  void onEvent(AwsEventHandler handler) { (void)handler; }
  void cleanupClients() {}

  // Capture hook (host tests only): the real library queues these for the
  // socket; the host build records them so tests can assert on them.
  void textAll(const char* message, size_t len = 0) {
    (void)len;
    if (message) host_ws_text_all += message;
  }
  void textAll(char* message) { textAll(message ? message : ""); }
  void textAll(const String& message) { host_ws_text_all += message.c_str(); }

 private:
  String path_;
};

inline void host_clear_ws_text_all() { host_ws_text_all.clear(); }
