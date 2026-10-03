// Network state snapshot — issue #46.
//
// The ESP8266 publishes its current WiFi mode and IP address in every
// state broadcast (serial NDJSON link + WebSocket fallback) so a client
// can determine the IP and mode without needing to see the boot console
// message.
//
// Fields:
//   mode  - "sta" (connected to a network) or "ap" (config portal AP)
//   ip    - the IP string ("192.168.1.42" in STA, "192.168.4.1" in AP)
//   ssid  - the connected SSID (empty in AP mode)
//
// The WiFiManager class sets these on connect / portal start.
// serialproto.cpp and tankie.ino read them in their state broadcasts.

#ifndef _TANKIE_NETSTATE_H_
#define _TANKIE_NETSTATE_H_

#include <Arduino.h>

struct NetState {
  String mode;    // "sta" or "ap"
  String ip;      // e.g. "192.168.1.42" or "192.168.4.1"
  String ssid;    // connected SSID (empty in AP mode)
};

// Global singleton — set by WiFiManager, read by serialproto + tankie.ino.
extern NetState netState;

#endif  // _TANKIE_NETSTATE_H_
