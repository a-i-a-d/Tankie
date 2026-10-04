# Tankie
Remote Controlled Tank for AI integration
<br>
<br>
<br>
![](media/tankie.png)

## Description
Tankie is a DFRobot Devastator Tank platform controlled by a ESP8266 micro controller and a SparkFun Dual TB6612FNG Motor Driver. It can be connected to an existing WiFi network or provide an access point to connect to. Remote control works via websocket. Additionally a pan/tilt bracket kit with two servos is installed on top, which can be used to attach a camera and move that around. 

This repository contains the motor and pan/tilt control (ESP8266) as well as the video input. For video, an additional Raspberry Pi Zero 2 W with a camera is installed on top of the tank; its setup (mediamtx streaming) lives in the [raspberry_pi/](raspberry_pi/) directory. Audio input is still being worked out (see the issues).

## Parts
1x [DFRobot Devastator](https://www.berrybase.de/dfrobot-devastator-tank-mobile-roboterplattform)<br>
1x [D1 Mini - ESP8266 Micro Controller](https://www.berrybase.de/en/detail/019234a3e5a1705e9e602f2dd7ea7f72)<br>
1x [SparkFun Motor Driver - Dual TB6612FNG](https://www.sparkfun.com/sparkfun-motor-driver-dual-tb6612fng-1a.html)<br>
1x [USB DC Buck Step Down Converter 6-24V 12V/24V To 5V 3A](https://www.diymore.cc/products/usb-dc-buck-step-down-converter-6-24v-12v-24v-to-5v-3a-car-charger-module)<br>
1x [Pan/Tilt Bracket Kit](https://www.robotshop.com/products/pan-tilt-bracket-kit-single-attachment)<br>
2x [G90 Micro Servo 5V Plastic Motor](https://eckstein-shop.de/WaveShare-SG90-Micro-Servo-5V-Plastic-Motor-180Grad-EN)<br>
1x [Breadboard](https://www.berrybase.de/en/detail/019234a3c572735085405d3bf4e22c71) or 1x [breadboard, double-sided, 70 x 50 mm](https://www.reichelt.com/de/en/shop/product/breadboard_double-sided_70_x_50_mm-319111?&LANGUAGE=en)<br>

## Hardware Setup
![](media/Tankie_fritzing.png)
**Please note:** The DC-DC converter and 9V power source in the image are wrong, a 9V battery won't be sufficient to power Tankie, use instead the 6x 1.5V battery bos that comes with the Devastator Kit. The DC-DC converter in the image can be used, however, it requires to have the output value adjusted manually, I'd suggest to use the converter in the parts list instead.

## Software Requirments
This project is currently built with ArduinoIDE, but eventually will be switched to PlatformIO. To build the firmware, you will need to install aditional arduino libraries listed below.

### Tools
- [ArduinoIDE 2.2.1 or newer](https://www.arduino.cc/en/software/)
- [Arduino core for ESP8266 WiFi chip](https://github.com/esp8266/Arduino)
- [LittleFS uploader plugin for Arduno 2.2.1 and higher](https://github.com/earlephilhower/arduino-littlefs-upload)

> **Build note (ElegantOTA + AsyncWebServer):** this sketch uses the async web stack, so ElegantOTA has to be compiled in async mode. With ArduinoIDE add `ELEGANTOTA_USE_ASYNC_WEBSERVER=1` to *File → Preferences → Additional compiler flags* (or the board's custom flags). With arduino-cli:
> ```
> arduino-cli compile --fqbn esp8266:esp8266:d1_mini \
>   --build-property "compiler.cpp.extra_flags=-DELEGANTOTA_USE_ASYNC_WEBSERVER=1" tankie
> ```

### Libraries
To be installed within the ArduinoIDE
- AsyncTCP
- ESPAsyncTCP
- ESPAsyncWebServer
- ElegantOTA

## Firmware
- Pinout and other config setting can be set in the [config.h](tankie/config.h) file.

### WiFi configuration (WiFiManager)
The firmware no longer hard-codes WiFi credentials or an `AP_MODE` switch.
Instead it uses a small [WiFiManager](tankie/wifimanager.h) class (implemented
on top of the already-running `ESPAsyncWebServer`, so it does not clash with
`ElegantOTA` / `ESPAsyncWebServer` the way the `WiFiManager` library does).

On boot the ESP:
1. reads the saved network (`ssid.txt` / `pass.txt` / `ip.txt` / `gateway.txt`)
   from LittleFS and tries to connect in **STA mode**;
2. if that fails (or nothing is saved yet) it opens its own access point
   `tankie-esp` (password `tankie1234`) and serves the **setup web page** at
   `http://192.168.4.1:8080` where you can enter the SSID / password / static
   IP / gateway. Saving reboots the ESP and retries step 1.

   The setup form runs on a dedicated config server (port **8080**); port 80
   always serves the tank control page, even in config mode (it shows a hint
   pointing at the portal URL).

The AP name + password are set in [config.h](tankie/config.h):
```
#define APSSID "tankie-esp"
#define APPSK  "tankie1234"
```

The saved network lives in LittleFS, so it can also be (re)configured at any
time by deleting `ssid.txt` / `pass.txt` (or by using the setup page), without
re-flashing. See issue [#21](https://github.com/a-i-a-d/Tankie/issues/21) for
the background on why the `WiFiManager` library was replaced.

### Upload via USB
The first upload has to happen via usb and can be done as usual with the Arduino IDE
- select Wemos D1 Mini as board
- select the usb board it is connected to
- click on upload

### Upload via ElegantOTG
The firmware makes use of [ElegantOTG](https://github.com/ayushsharma82/ElegantOTA), which allows to update firmware and littlefs data via the browser over Wifi. This can be used after installing the firmwar once vi usb.

In the ArduinoIDE, select __Sketch->Export Compiled Binary__. The exported .bin file will end up in the [build](tankie/build) folder. Upload it through the ElegantOTA web ui available at __http://<ip_of_tankie>/update__.

## Data
Additional to the firmware, files from the [data folder](tankie/data/) have to be uploaded as littlefs filesystem.


### Upload via USB
- Install the [LittleFS uploader plugin for Arduno 2.2.1 and higher](https://github.com/earlephilhower/arduino-littlefs-upload)
- In the ArduinoIDE press __[Shift]__+__[Control]__+__[p]__. A menu will appear, enter __littlefs__ and click __Upload LittleFS to Pico/ESP...__
- It will create the littlefs .bin file and upload it

### Upload via ElegantOTG 
Follow the instructions for the usb data upload. It will fail if not connected via usb, but create the data .bin file in the **/tmp** directory. To find the exact name, look at the consle output of the tool in the arduinoIDE. You can simply copy that file from /tmp and upload it via the ElegantOTG UI. 

## RC Usage
Connect to __http://<ip_of_tankie>__ address with a browser. You should see a control interface with two joysticks and fields that display steer, speed, pan and tilt values as well as the current voltage of the power supply.
<br>
<br>
<br>
![](media/tankie_web_ui.png)

## AI Usage
The [ai-control](ai-control/) folder contains the code that connects an AI to the tank (see [ai-control/README.md](ai-control/README.md)).

### Safety & state feedback
- **Safety watchdog (firmware):** if the tank is driving and no command is received for 5 seconds, the motors are stopped and the pan/tilt camera is recentered automatically. A watchdog event is broadcast to all websocket clients as `{"type":"watchdog"}` (issue #32).
- **State feedback:** the periodic websocket broadcast includes the current state in addition to the battery voltage, in the contract shape: `{"type":"state","seq":N,"battery":...,"speed":...,"steer":...,"pan":...,"tilt":...,"net_mode":"sta"|"ap","net_ip":"192.168.x.y"}` (issue #32, #46). The `net_mode` and `net_ip` fields let a client determine the ESP's IP address and WiFi mode (STA vs AP) without needing the boot console (issue #46).
- **JSON protocol (ai_control.py):** the LocalAI client speaks the JSON protocol contract — `{"cmd":"pan","angle":N}` / `{"cmd":"tilt","angle":N}` for the camera and one combined `{"cmd":"drive","speed":N,"steer":M}` object for the drive (the legacy `key=value` dialect was retired by #32, issue #38).
- **Continuous control (ai_control.py):** a background control loop re-issues the active drive command (the combined `drive` object) every second so the firmware watchdog stays armed while the AI is thinking between frames.
- **Autonomous drive profile:** AI-issued drive commands are clamped to `max_speed = 40` (see `AUTO_PROFILE` in `ai_control/LocalAI/ai_control.py`), slower than the manual joystick range, so a misbehaving model cannot drive the tank at full speed.
- **Pan/tilt:** the firmware clamps `pan`/`tilt` values to the 0-180 servo range; the AI tools already use relative moves (`up`/`down`/`left`/`right`/`center`) around the 90-degree center position.


## Serial control protocol (ESP8266 over /dev/ttyUSB0)
The ESP8266 is controlled over the USB-serial link (`/dev/ttyUSB0` @ 921600 8N1)
using a line-oriented JSON protocol (NDJSON) — see
[issue #29](https://github.com/a-i-a-d/Tankie/issues/29). The WiFi/WebUI
path above stays available as a fallback.

Pi → ESP (commands):

```json
{"cmd":"drive","speed":50,"steer":0}
{"cmd":"pan","angle":90}
{"cmd":"tilt","angle":30}
{"cmd":"stop"}
```

ESP → Pi (responses):

```json
{"type":"hello","proto":1,"fw":"v0.1-serial"}
{"type":"ack","seq":1}
{"type":"error","seq":2,"code":"range","field":"speed"}
{"type":"watchdog"}
{"type":"state","seq":1,"battery":7.42,"speed":50,"steer":0,"pan":90,"tilt":90,"net_mode":"sta","net_ip":"192.168.1.42"}
```

Rules: strict validation + clamping on the ESP (speed/steer ±255, pan/tilt
0–180); a command watchdog stops the motors if the link goes quiet
(`SERIAL_WATCHDOG_MS`, compile-time in `tankie/config_serial.h`);
seq-based acks so a dead link is detectable; a hello/proto handshake
catches firmware/Pi mismatches. Non-JSON lines (debug output) are ignored.

### Pi-side bridge (`raspberry_pi/serial_bridge/`)

- `bridge.py` — daemon holding `/dev/ttyUSB0`: reader thread (NDJSON in,
  seq/ack tracking, link health), writer thread (250 ms keep-alive
  re-send of the active drive command), state store
  (`/var/lib/tankie/state.json`), Unix socket (`/run/tankie/bridge.sock`).
- `tankie-serial.py` — CLI: `drive --speed 50 --steer 0`, `pan 90`, `tilt 30`,
  `stop`, `state`, `watchdog-test` (or `--raw` to talk to the port directly).
- `conf/serial_bridge.yaml` — single config source (serial_port, baud, keepalive_ms,
  ack_timeout_ms, state_file, socket_path).
- `tankie-serial.service` — systemd unit (`Restart=always`, dialout group).
- `setup-serial.sh` — idempotent installer (deps, udev rule, systemd).
- `test_serial_proto.py` — raw-port protocol test (T1–T7 of the issue).

```sh
sudo bash raspberry_pi/serial_bridge/setup-serial.sh   # install + start
raspberry_pi/serial_bridge/tankie-serial.py drive --speed 50 --steer 0
raspberry_pi/serial_bridge/tankie-serial.py state
python3 raspberry_pi/serial_bridge/test_serial_proto.py   # raw-port test
```


## Testing
The ESP8266 firmware logic is covered by a host-side unit-test suite (no ESP
toolchain needed) in [tests/](tests/), plus a CI compile-check for the full
sketch in [.github/workflows/tests.yml](.github/workflows/tests.yml):

- `tests/test_wheels.cpp` - the pure drive-math `computeWheels()`
  ([tankie/wheels.cpp](tankie/wheels.cpp), no Arduino deps): straight /
  turn-left / turn-right in fwd + rev, spin-in-place at `speed == 0`,
  the clamped +/-255 extreme corners (issue #14), and an exhaustive
  (speed, steer) matrix check that both outputs stay within [-255, 255]
- `tests/test_tankdrive.cpp` - `TankDrive` end-to-end through the real
  `Motor` H-bridge: the full branch matrix (stop / spin-in-place / fw / bw
  x straight / left / right, partial steer, boundary values), asserting
  the real `config.h` H-bridge pins (PWM + In1/In2 direction)
- `tests/test_motor.cpp` - `Motor::drive/brake/standby` and the
  `forward/back/left/right/brake` free functions
- `tests/test_bat_voltage.cpp` - the battery voltage-divider math
  (`getBatVoltage()`, extracted from `tankie.ino` into [tankie/batt.cpp](tankie/batt.cpp))
- `tests/test_ws_fallback.cpp` - the WebSocket fallback handler
  (`handleWebSocketMessage()` in [tankie/tankie.ino](tankie/tankie.ino))
  compiled against the network shims in [tests/shims/](tests/shims/):
  valid drive/pan/tilt/stop move the motors/servos, out-of-range /
  missing-field / non-JSON / unknown-cmd messages are rejected (no motor
  or servo change), binary + fragmented frames are ignored, and the
  out-of-bounds `data[len] = 0` write is gone (a canary byte at
  `data[len]` is left untouched) (issue #34)
- `tests/web_ui_harness.js` - the web UI (tankie/data/script.js) in a Node
  VM sandbox: every outgoing websocket message is valid JSON and exactly
  the protocol-contract shapes (drive / pan / tilt / stop), and the
  incoming state/ack/error/watchdog/hello broadcasts are handled
  (issue #32)
- `tests/ai_control_json_harness.py` - the LocalAI client
  (ai-control/LocalAI/ai_control.py) with stubbed deps: every outgoing
  message is a contract JSON object (drive / pan / tilt), the control
  loop re-issues the combined drive object, and the state/watchdog
  broadcasts parse (issue #38)
- `ai-control/tankieControl/main_test.go` - the LocalAGI wrapper's
  drive/steer/camera handlers against a stub websocket tank: contract
  JSON shapes only, steer combined with the active speed, center =
  pan 90 + tilt 90 (issue #39)

Run it locally (any machine with g++, node, go):

```sh
bash tests/run_tests.sh
node tests/web_ui_harness.js   # web UI JSON protocol (node)
python3 tests/ai_control_json_harness.py   # LocalAI client JSON protocol
(cd ai-control/tankieControl && go test ./...)   # LocalAGI wrapper JSON protocol
```

The tests compile the real firmware `.cpp` files against a minimal Arduino
shim ([tests/shims/Arduino.h](tests/shims/Arduino.h)) and exit non-zero on
any failure. The `firmware-build` CI job additionally compiles the complete
sketch with arduino-cli (esp8266 core 3.1.2, `esp8266:esp8266:d1_mini`,
`-DELEGANTOTA_USE_ASYNC_WEBSERVER=1`) so the sketch can never break the
build/flash path used on the tank Pi.

Not covered (intentionally): the network stack itself (the shims in
[tests/shims/](tests/shims/) are compile-only stubs - no real sockets)
and hardware-in-the-loop / OTA behavior. The WebSocket *command handler*
in `tankie.ino` IS covered by `tests/test_ws_fallback.cpp` (issue #34).

## Projects used
- [joy.js](https://github.com/bobboteck/JoyStick)
- [SparkFun TB6612 library](https://github.com/sparkfun/SparkFun_TB6612FNG_Arduino_Library)
