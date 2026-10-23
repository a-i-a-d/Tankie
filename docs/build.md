← [Tankie](../README.md) · [Docs index](README.md) · *Build*

# Build — firmware toolchains & all dependencies

The ESP8266 firmware (in [`tankie/`](../tankie/)) can be built with **any** of
the three toolchains below. They all target the same board — **Wemos D1 Mini**
(`esp8266:esp8266:d1_mini`, ESP8266 core 3.1.2) — and produce a compatible
firmware + LittleFS data partition. The only thing that differs between them is
how the `ELEGANTOTA_USE_ASYNC_WEBSERVER=1` build flag and the dependencies are
declared (see each method's notes).

## 1. PlatformIO (recommended for local dev)

Install [PlatformIO](https://platformio.org/): `pip install platformio`

The config lives at the repo root in [`platformio.ini`](../platformio.ini).
Build, upload, and monitor from the CLI:
```
pio run                 # firmware
pio run -t buildfs      # LittleFS data partition (tankie/data/)
pio run -t upload       # flash firmware (USB, once / reflashable boards)
pio run -t uploadfs     # flash the LittleFS data partition
```
`platformio.ini` pins the board, the LittleFS layout
(`board_build.ldscript = eagle.flash.4m2m.ld`, matching the 4M/FS:2MB
`2072576`-byte image), the `ELEGANTOTA_USE_ASYNC_WEBSERVER=1` flag, and the
library dependencies. No file moves are needed — `src_dir`/`data_dir` point at
the existing `tankie/` and `tankie/data/`.

## 2. arduino-cli (used on the tank Pi)

The Pi build script (`raspberry_pi/build.sh`) uses `arduino-cli` and produces
the exact image the firmware + LittleFS partition expect:
```
bash raspberry_pi/build.sh
```
For a one-off manual compile:
```
arduino-cli compile --fqbn esp8266:esp8266:d1_mini \
  --build-property "compiler.cpp.extra_flags=-DELEGANTOTA_USE_ASYNC_WEBSERVER=1" \
  tankie
```
`sketch.yaml` (in [`tankie/`](../tankie/)) pins the FQBN, the esp8266 core
(3.1.2), and every external library version, so the build is reproducible
without relying on a personal IDE state.

## 3. Arduino IDE

- Install [Arduino IDE 2.2.1 or newer](https://www.arduino.cc/en/software/)
  and the [Arduino core for ESP8266](https://github.com/esp8266/Arduino).
- Open `tankie/` as a project. Install the libraries from
  [`tankie/libraries.json`](../tankie/libraries.json) (or add them via
  *Sketch → Include Library → Manage Libraries*):
  - ElegantOTA 4.0.0
  - ESPAsyncWebServer 3.1.0
  - ESPAsyncTCP 1.2.4
  - AsyncTCP 1.1.4
  - Servo 1.3.0
- Select **Wemos D1 Mini** as the board.
- **Build note:** the sketch uses the async web stack, so ElegantOTA must be
  compiled in async mode — add `ELEGANTOTA_USE_ASYNC_WEBSERVER=1` to
  *File → Preferences → Additional compiler flags* (or the board's custom
  flags). The same flag is applied automatically by the PlatformIO and
  arduino-cli methods above.

## Firmware libraries (ESP8266)

| Library | Version |
|---|---|
| [ElegantOTA](https://github.com/ayushsharma82/ElegantOTA) | 4.0.0 |
| [ESPAsyncWebServer](https://github.com/me-no-dev/ESPAsyncWebServer) | 3.1.0 |
| [ESPAsyncTCP](https://github.com/me-no-dev/ESPAsyncTCP) | 1.2.4 |
| [AsyncTCP](https://github.com/me-no-dev/AsyncTCP) | 1.1.4 |
| [Servo](https://github.com/arduino/arduino-libraries) | 1.3.0 |

The ESP8266 core also bundles its own interrupt-driven Servo, so the `Servo.h`
used by the sketch is the core's (the generic arduino-libraries Servo is for
the arduino-cli path).

> **Build flag (all paths):** `ELEGANTOTA_USE_ASYNC_WEBSERVER=1` is **required**
> — ElegantOTA and the core's built-in `WebServer` both define an `HTTP_GET`
> enum, and this flag switches ElegantOTA to the async web stack to resolve the
> clash. It is set automatically by the PlatformIO and arduino-cli methods; add
> it manually for the Arduino IDE.

## Python dependencies (AI / Pi side)

The AI control client, the brain, and the manual-test harness each pin their
own dependencies (lower-bound ranges; raise the floor only when a release
diverges and breaks the client). All are standard PyPI packages — **no vendored
or modified libraries**. `openai` is used purely as the HTTP transport to
LocalAI's OpenAI-compatible endpoint (`base_url`); it is not a fork.

| Component | `requirements.txt` | Dependencies |
|---|---|---|
| LocalAI control client (`ai-control/LocalAI/ai_control.py`) | [`ai-control/LocalAI/requirements.txt`](../ai-control/LocalAI/requirements.txt) | `opencv-python-headless>=4.8,<5`, `openai>=1.0,<2`, `termcolor>=2.2,<3` |
| AI brain (`raspberry_pi/brain/`) | [`raspberry_pi/brain/requirements.txt`](../raspberry_pi/brain/requirements.txt) | `openai>=1.0,<2`, `httpx>=0.27,<1`, `pydantic>=2.5,<3`, `PyYAML>=6.0,<7`, `pytest>=8.0,<9`, `numpy>=1.24`, `opencv-python-headless>=4.8` |
| Manual test harness (`tests/manual/`) | [`tests/manual/requirements.txt`](../tests/manual/requirements.txt) | see file |

> The Pi has no display, so every `cv2` dependency uses the **headless** wheel
> (`opencv-python-headless`); `cv2.imshow`/`waitKey` in the client is only a
> local debug aid, not a runtime requirement.

---

## See also
- [Firmware & data upload](ota.md) — flashing the built firmware + LittleFS
- [Serial control protocol](serial-protocol.md) — the serial link the firmware speaks
- [AI control](ai-control.md) — what the Python components do
- [Tankie (home)](../README.md)
