← [Tankie](../README.md) · [Docs index](README.md) · *WiFi configuration*

# WiFi configuration

The firmware does not hard-code WiFi credentials or an `AP_MODE` switch.
Instead it uses a small [WiFiManager](../tankie/wifimanager.h) class
(implemented on top of the already-running `ESPAsyncWebServer`, so it does not
clash with `ElegantOTA` / `ESPAsyncWebServer` the way the `WiFiManager`
library does) — see [issue #21](https://github.com/a-i-a-d/Tankie/issues/21).


On boot the ESP:
1. reads the saved network from the **reserved EEPROM flash sector
   `0x3FB000`** (a dedicated 4 KB sector, see below) and tries to connect in
   **STA mode**;
2. if that fails (or nothing is saved yet) it opens its own access point
   `tankie-esp` (password `tankie1234`) and serves the **setup web page** at
   `http://192.168.4.1:8080` where you can enter the SSID / password / static
   IP / gateway. Saving writes the EEPROM sector and reboots the ESP, retrying
   step 1.

   The setup form runs on a dedicated config server (port **8080**); port 80
   always serves the tank control page, even in config mode (it shows a hint
   pointing at the portal URL).

The AP name + password are set in [config.h](../tankie/config.h):
```
#define APSSID "tankie-esp"
#define APPSK  "tankie1234"
```

## Where the network config lives

The saved network is stored in the reserved **EEPROM flash sector `0x3FB000`**
(4 KB), *not* on LittleFS. That sector is outside both the firmware region
(`0x0`) and the LittleFS data partition (`0x200000`), so the config
**survives both firmware OTA and filesystem/data-partition OTA** — a failed or
corrupt fs-OTA no longer wipes the WiFi credentials and strands the ESP on the
`tankie-esp` config portal.

The blob is a small length-prefixed structure (magic + version +
ssid/pass/ip/gateway + a trailing CRC32) managed by
[`wificfg.h`](../tankie/wificfg.h) / [`wificfg.cpp`](../tankie/wificfg.cpp); a
blank or corrupt sector is detected via the magic + CRC and cleanly falls back
to the config portal.

> The `set_wifi` / `get_wifi` serial commands write to this **same** sector —
> see [serial-protocol.md](serial-protocol.md) — so the web and serial config
> paths behave identically.

---

## See also
- [Serial control protocol](serial-protocol.md) — `set_wifi` / `get_wifi` over the serial link
- [Firmware & data upload](ota.md) — why the config survives an fs-OTA
- [Tankie (home)](../README.md)
