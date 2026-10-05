// Stream-info snapshot — issue #51.
//
// The Raspberry Pi (which owns the camera + mediamtx) knows its own LAN IP.
// It pushes that to the ESP8266 over the existing serial NDJSON link via the
// `set_stream` command, so the human web UI can point its video iframe at the
// correct `http://<pi>:<port><path>` URL instead of a hard-coded, stale IP
// (the old Heltec-era `http://10.42.0.1:8889/cam/`).
//
// Fields:
//   url   - the full stream URL, e.g. "http://192.168.1.42:8889/cam/"
//           (empty until the Pi has sent a set_stream command)
//
// serialproto.cpp and tankie.ino read it in their state broadcasts and append
// it as the `stream_url` field (issue #51). The web UI (tankie/data/script.js)
// only shows the "Start video stream" button once a non-empty stream_url has
// been seen in a state broadcast (A1: button appears only when the Pi provides
// an IP; no error/hint otherwise).
//
// The Pi bridge (raspberry_pi/serial_bridge/bridge.py) sets this by sending
// set_stream on startup and whenever its own IP changes.

#ifndef _TANKIE_STREAMINFO_H_
#define _TANKIE_STREAMINFO_H_

#include <Arduino.h>

struct StreamInfo {
  String url;    // e.g. "http://192.168.1.42:8889/cam/" (empty until set)
};

// Global singleton — set by the Pi bridge (set_stream command), read by
// serialproto.cpp + tankie.ino in their state broadcasts.
extern StreamInfo streamInfo;

#endif  // _TANKIE_STREAMINFO_H_
