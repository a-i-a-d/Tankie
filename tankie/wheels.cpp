#include "wheels.h"

int clampWheel(int v) {
  if (v > 255) return 255;
  if (v < -255) return -255;
  return v;
}

WheelOutputs computeWheels(int speed, int steer) {
  const int s = clampWheel(speed);
  const int st = clampWheel(steer);

  if (s == 0) {
    // Spin in place (NEW, issue #14): steer > 0 = turn left (left wheel
    // reverses, right wheel drives forward), steer < 0 = turn right.
    return WheelOutputs{-st, st};
  }

  // Moving: the legacy relative-steer scaling, now in pure integer math.
  // The inner wheel (left when steering left, right when steering right)
  // is offset from the outer wheel by |steer| * |speed| / 127, and BOTH
  // outputs are clamped to the PWM range (issue #14: the legacy r_steer
  // was unclamped, so at speed=+/-255, steer=+/-255 the inner wheel
  // computed +/-257 and only analogWrite() saturation saved it).
  const int inner = s - (st < 0 ? -st : st) * s / 127;
  if (st > 0) return WheelOutputs{clampWheel(inner), s};   // turn left
  if (st < 0) return WheelOutputs{s, clampWheel(inner)};   // turn right
  return WheelOutputs{s, s};                                // straight
}
