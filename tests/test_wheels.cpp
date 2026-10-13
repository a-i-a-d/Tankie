// Unit tests for the pure drive-math (tankie/wheels.cpp) - issue #14.
//
// computeWheels(speed, steer) is the single source of truth for the
// wheel outputs. It is pure C++ (no Arduino), so this file tests the
// math directly, without any motor/pin shims:
//
//   - clampWheel() saturates to [-255, 255]
//   - straight / turn-left / turn-right, forward and backward
//   - the full (speed, steer) matrix stays within [-255, 255]
//     (the legacy r_steer was unclamped: at +/-255, +/-255 the inner
//     wheel computed +/-257, only saved by analogWrite() saturation)
//   - spin-in-place at speed == 0 (NEW): steer > 0 = right, steer < 0 = left
//   - out-of-range inputs are clamped, never crash
#include "test_main.h"
#include "wheels.h"

TEST(clampWheel_bounds) {
  CHECK_EQ_INT(clampWheel(0), 0);
  CHECK_EQ_INT(clampWheel(1), 1);
  CHECK_EQ_INT(clampWheel(-1), -1);
  CHECK_EQ_INT(clampWheel(255), 255);
  CHECK_EQ_INT(clampWheel(-255), -255);
  CHECK_EQ_INT(clampWheel(256), 255);
  CHECK_EQ_INT(clampWheel(-256), -255);
  CHECK_EQ_INT(clampWheel(1000), 255);
  CHECK_EQ_INT(clampWheel(-1000), -255);
}

TEST(wheels_straight_forward) {
  WheelOutputs o = computeWheels(127, 0);
  CHECK_EQ_INT(o.left, 127);
  CHECK_EQ_INT(o.right, 127);
}

TEST(wheels_straight_backward) {
  WheelOutputs o = computeWheels(-127, 0);
  CHECK_EQ_INT(o.left, -127);
  CHECK_EQ_INT(o.right, -127);
}

TEST(wheels_forward_turn_right) {
  // steer > 0 = right: inner (right) wheel slows down, outer (left) at speed
  WheelOutputs o = computeWheels(127, 127);
  CHECK_EQ_INT(o.left, 127);
  CHECK_EQ_INT(o.right, 0);
}

TEST(wheels_forward_turn_left) {
  WheelOutputs o = computeWheels(127, -127);
  CHECK_EQ_INT(o.left, 0);
  CHECK_EQ_INT(o.right, 127);
}

TEST(wheels_forward_partial_steer) {
  // steer > 0 = right: inner (right) = 127 - 64*127/127 = 63, outer (left) = 127
  WheelOutputs o = computeWheels(127, 64);
  CHECK_EQ_INT(o.left, 127);
  CHECK_EQ_INT(o.right, 63);
}

TEST(wheels_backward_turn_right) {
  // steer > 0 = right: inner (right) wheel slows down, outer (left) at speed
  WheelOutputs o = computeWheels(-127, 127);
  CHECK_EQ_INT(o.left, -127);
  CHECK_EQ_INT(o.right, 0);
}

TEST(wheels_backward_turn_left) {
  WheelOutputs o = computeWheels(-127, -127);
  CHECK_EQ_INT(o.left, 0);
  CHECK_EQ_INT(o.right, -127);
}

TEST(wheels_backward_partial_steer) {
  // steer < 0 = left: inner (left) = -127 + 64 = -63, outer (right) = -127
  WheelOutputs o = computeWheels(-127, -64);
  CHECK_EQ_INT(o.left, -63);
  CHECK_EQ_INT(o.right, -127);
}

TEST(wheels_spin_in_place_right) {
  // NEW (issue #14): at speed == 0 steer > 0 turns right - right wheel
  // reverses, left wheel drives forward.
  WheelOutputs o = computeWheels(0, 127);
  CHECK_EQ_INT(o.left, 127);
  CHECK_EQ_INT(o.right, -127);
}

TEST(wheels_spin_in_place_left) {
  WheelOutputs o = computeWheels(0, -127);
  CHECK_EQ_INT(o.left, -127);
  CHECK_EQ_INT(o.right, 127);
}

TEST(wheels_spin_in_place_full) {
  WheelOutputs o = computeWheels(0, 255);
  CHECK_EQ_INT(o.left, 255);
  CHECK_EQ_INT(o.right, -255);
  WheelOutputs p = computeWheels(0, -255);
  CHECK_EQ_INT(p.left, -255);
  CHECK_EQ_INT(p.right, 255);
}

TEST(wheels_zero_speed_zero_steer) {
  WheelOutputs o = computeWheels(0, 0);
  CHECK_EQ_INT(o.left, 0);
  CHECK_EQ_INT(o.right, 0);
}

TEST(wheels_extreme_forward_right_clamped) {
  // The legacy bug (issue #14): r_steer = 255*255/127 = 511, so the inner
  // wheel computed 255 - 511 = -256 (even -257 with the float rounding) and
  // flipped 2 PWM into reverse, only saved by analogWrite() saturation.
  // Now the inner wheel clamps at -255, no direction flip.
  WheelOutputs o = computeWheels(255, 255);
  CHECK_EQ_INT(o.left, 255);
  CHECK_EQ_INT(o.right, -255);
}

TEST(wheels_extreme_forward_left_clamped) {
  WheelOutputs o = computeWheels(255, -255);
  CHECK_EQ_INT(o.left, -255);
  CHECK_EQ_INT(o.right, 255);
}

TEST(wheels_extreme_backward_right_clamped) {
  WheelOutputs o = computeWheels(-255, 255);
  CHECK_EQ_INT(o.left, -255);
  CHECK_EQ_INT(o.right, 255);
}

TEST(wheels_extreme_backward_left_clamped) {
  WheelOutputs o = computeWheels(-255, -255);
  CHECK_EQ_INT(o.left, 255);
  CHECK_EQ_INT(o.right, -255);
}

TEST(wheels_out_of_range_inputs_clamped) {
  // Inputs beyond the contract range must not overflow: clamp first.
  WheelOutputs o = computeWheels(1000, 1000);
  CHECK(o.left >= -255 && o.left <= 255);
  CHECK(o.right >= -255 && o.right <= 255);
  WheelOutputs p = computeWheels(-1000, -1000);
  CHECK(p.left >= -255 && p.left <= 255);
  CHECK(p.right >= -255 && p.right <= 255);
  WheelOutputs q = computeWheels(0, 1000);   // spin-in-place also clamps
  CHECK_EQ_INT(q.left, 255);
  CHECK_EQ_INT(q.right, -255);
}

TEST(wheels_full_matrix_within_range) {
  // Exhaustive: every (speed, steer) in the contract range must produce
  // both outputs within [-255, 255].
  for (int s = -255; s <= 255; s++) {
    for (int st = -255; st <= 255; st++) {
      WheelOutputs o = computeWheels(s, st);
      CHECK(o.left >= -255 && o.left <= 255);
      CHECK(o.right >= -255 && o.right <= 255);
    }
  }
}
