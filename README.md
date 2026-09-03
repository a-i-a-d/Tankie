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
- **Safety watchdog (firmware):** if the tank is driving and no command is received for 5 seconds, the motors are stopped and the pan/tilt camera is recentered automatically. A watchdog event is broadcast to all websocket clients as `{"watchdog":true,...}`.
- **State feedback:** the periodic websocket broadcast now includes the current state in addition to the battery voltage: `{"battery":...,"speed":...,"steer":...,"pan":...,"tilt":...}`.
- **Continuous control (ai_control.py):** the drive commands (`speed`/`steer`) that were previously never sent are now transmitted, and a background control loop re-issues the active drive command every second so the tank keeps moving while the AI is thinking between frames.
- **Autonomous drive profile:** AI-issued drive commands are clamped to `max_speed = 40` (see `AUTO_PROFILE` in `ai_control/LocalAI/ai_control.py`), slower than the manual joystick range, so a misbehaving model cannot drive the tank at full speed.
- **Pan/tilt:** the firmware clamps `pan`/`tilt` values to the 0-180 servo range; the AI tools already use relative moves (`up`/`down`/`left`/`right`/`center`) around the 90-degree center position.

## Projects used
- [joy.js](https://github.com/bobboteck/JoyStick)
- [SparkFun TB6612 library](https://github.com/sparkfun/SparkFun_TB6612FNG_Arduino_Library)
