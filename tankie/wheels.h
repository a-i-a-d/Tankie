// Pure drive-math for the Tankie tank drive (issue #14).
//
// This module has NO Arduino dependencies and no I/O: it is plain C++ so
// the wheel computation can be unit-tested on the host (tests/test_wheels.cpp)
// and is the single source of truth for how a (speed, steer) pair maps to
// per-motor outputs. TankDrive (tankie/tankdrive.cpp) delegates to it.
//
// Sign convention (unchanged from the legacy 5-branch updateMotors()):
//   speed > 0 = forward, speed < 0 = backward
//   steer > 0 = turn LEFT  (left wheel is the inner wheel)
//   steer < 0 = turn RIGHT (right wheel is the inner wheel)
//
// Both outputs are always clamped to [-255, 255] (the PWM range of the
// TB6612FNG), for every speed/steer combination.
#pragma once

// One pair of motor outputs (left wheel, right wheel), each in [-255, 255].
struct WheelOutputs {
  int left;
  int right;
};

// Compute the left/right wheel outputs for a (speed, steer) command.
//
//   s  = clamp(speed)          ([-255, 255])
//   st = clamp(steer)          ([-255, 255])
//
//   speed == 0 : left = -st, right = st          (spin-in-place, NEW)
//
//   speed != 0 : the outer wheel runs at s; the inner wheel is offset from
//                it by the legacy relative-steer amount |st| * |s| / 127
//                  inner = s - |st| * s / 127
//                (integer division, no floats). Both outputs are clamped:
//                  steer > 0 (left)  : left  = clamp(inner), right = s
//                  steer < 0 (right) : left  = s, right = clamp(inner)
//                  steer == 0       : left  = s, right = s
//
// The clamp is what fixes the legacy bug (issue #14): r_steer was
// unclamped, so at speed = +/-255, steer = +/-255 the inner wheel computed
// +/-257 and only analogWrite() saturation saved it. All arithmetic is
// integer and deterministic (no floats).
WheelOutputs computeWheels(int speed, int steer);

// Clamp a wheel value to the TB6612FNG PWM range [-255, 255].
int clampWheel(int v);
