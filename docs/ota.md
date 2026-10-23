← [Tankie](../README.md) · [Docs index](README.md) · *Firmware & data upload*

# Firmware & data upload

The firmware makes use of [ElegantOTA](https://github.com/ayushsharma82/ElegantOTA),
which allows updating firmware and LittleFS data via the browser over WiFi.
This can be used after installing the firmware once via USB.

## Firmware

### Upload via USB
The first upload has to happen via USB and can be done as usual with the
Arduino IDE:
- select **Wemos D1 Mini** as board
- select the USB port it is connected to
- click **Upload**

### Upload via ElegantOTA
In the Arduino IDE, select **Sketch → Export Compiled Binary**. The exported
`.bin` file will end up in the `tankie/build` folder. Upload it
through the ElegantOTA web UI available at `http://<ip_of_tankie>/update`.

## Data (LittleFS)

Additional to the firmware, files from the [data folder](../tankie/data/) have
to be uploaded as a LittleFS filesystem.

### Upload via USB
- Install the [LittleFS uploader plugin for Arduino 2.2.1 and higher](https://github.com/earlephilhower/arduino-littlefs-upload)
- In the Arduino IDE press **[Shift]+[Control]+[p]**. A menu will appear, enter
  **littlefs** and click **Upload LittleFS to Pico/ESP...**
- It will create the littlefs `.bin` file and upload it

### Upload via ElegantOTA
Follow the instructions for the USB data upload. It will fail if not connected
via USB, but create the data `.bin` file in the **/tmp** directory. To find the
exact name, look at the console output of the tool in the Arduino IDE. You can
simply copy that file from `/tmp` and upload it via the ElegantOTA UI.

> **Why this survives:** the WiFi config is stored in the reserved EEPROM
> flash sector `0x3FB000`, outside the LittleFS data partition, so an
> fs-OTA (even a failed one) does not wipe the network config — see
> [wifi-config.md](wifi-config.md).

---

## See also
- [WiFi configuration](wifi-config.md) — the config that survives an fs-OTA
- [Build](build.md) — how the firmware + data images are produced
- [Tankie (home)](../README.md)
