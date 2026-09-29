// Host-side Servo shim for the Tankie unit tests (issue #29).
//
// On the ESP8266, <Servo.h> is the core's interrupt-driven Servo library.
// On the host build, the Servo class is defined in the Arduino.h shim
// (attach/write/read + a reset() test hook). This header simply pulls it
// in so `#include <Servo.h>` resolves identically on both targets.
//
// NOTE: must stay named `Servo.h` - serialproto.cpp includes it as <Servo.h>.
#pragma once
#include "Arduino.h"
