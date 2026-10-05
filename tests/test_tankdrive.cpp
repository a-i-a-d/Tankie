// Unit tests for TankDrive (tankie/tankdrive.cpp) - issue #5, issue #14.
//
// TankDrive turns a (speed, steer) pair into per-motor values by
// delegating to the pure, host-testable computeWheels() (issue #14):
//   inner wheel = speed - |steer| * speed / 127   (clamped to +/-255)
//   outer wheel = speed
//   speed == 0  : spin-in-place (left = -steer, right = steer)
//
// The tests drive the REAL class on the REAL config.h pins and assert on
// the resulting H-bridge output: the PWM magnitude on PWMA/PWMB and the
// In1/In2 direction pins, for every branch:
//
//   stop            speed == 0, steer == 0
//   spin-in-place   speed == 0, steer != 0        (NEW, issue #14)
//   fw straight     speed > 0, steer == 0         -> L = R = speed
//   fw left         speed > 0, steer > 0          -> L = speed - r, R = speed
//   fw right        speed > 0, steer < 0          -> L = speed, R = speed + r
//   bw straight     speed < 0, steer == 0         -> L = R = speed
//   bw left         speed < 0, steer > 0          -> L = speed + r, R = speed
//   bw right        speed < 0, steer < 0          -> L = speed, R = speed - r
//
// (r = |steer| * |speed| / 127, the legacy relative-steer amount)
//
// Motor wiring per tankie.ino (config.h pins):
//   M1 (left)  = Motor(AIN1, AIN2, PWMA, STBY)
//   M2 (right) = Motor(BIN1, BIN2, PWMB, STBY)
//   fwd: In1=HIGH, In2=LOW   rev: In1=LOW, In2=HIGH   (SparkFun_TB6612)
#include "test_main.h"
#include "config.h"
#include "SparkFun_TB6612.h"
#include "tankdrive.h"

// The two motors, exactly as tankie.ino wires them (shared STBY).
static Motor mLeft(AIN1, AIN2, PWMA, STBY);
static Motor mRight(BIN1, BIN2, PWMB, STBY);
static TankDrive tank(&mLeft, &mRight);

// Reset the pin state to the "fresh hardware" defaults (all HIGH / released)
// and make sure the tank is stopped before each scenario.
static void resetTank() {
  for (int i = 0; i < 64; i++) host_pin_level[i] = HIGH;
  for (int i = 0; i < 64; i++) host_pwm[i] = 0;
  tank.setSpeed(0);
  tank.setSteer(0);
}

// Assert one motor's full H-bridge output: PWM magnitude + In1/In2 state.
// expected >= 0 -> forward (In1 HIGH / In2 LOW), expected < 0 -> reverse.
static void expectMotor(int in1, int in2, int pwm, int expected) {
  CHECK_EQ_INT(digitalRead(in1), expected >= 0 ? HIGH : LOW);
  CHECK_EQ_INT(digitalRead(in2), expected >= 0 ? LOW : HIGH);
  CHECK_EQ_INT(host_pwm[pwm], expected < 0 ? -expected : expected);
}

TEST(stop_both_motors) {
  resetTank();
  tank.setSpeed(0);
  tank.setSteer(0);
  expectMotor(AIN1, AIN2, PWMA, 0);
  expectMotor(BIN1, BIN2, PWMB, 0);
  CHECK_EQ_INT(digitalRead(STBY), HIGH);  // drive() always releases standby
}

TEST(spin_in_place_left) {
  // NEW (issue #14): steer with zero speed spins the tank in place.
  // steer > 0 = left: left wheel reverses, right wheel drives forward.
  resetTank();
  tank.setSpeed(0);
  tank.setSteer(127);
  expectMotor(AIN1, AIN2, PWMA, -127);
  expectMotor(BIN1, BIN2, PWMB, 127);
}

TEST(spin_in_place_right) {
  resetTank();
  tank.setSpeed(0);
  tank.setSteer(-127);
  expectMotor(AIN1, AIN2, PWMA, 127);
  expectMotor(BIN1, BIN2, PWMB, -127);
}

TEST(spin_in_place_full) {
  resetTank();
  tank.setSpeed(0);
  tank.setSteer(255);
  expectMotor(AIN1, AIN2, PWMA, -255);
  expectMotor(BIN1, BIN2, PWMB, 255);
}

TEST(fw_straight) {
  resetTank();
  tank.setSpeed(127);
  tank.setSteer(0);
  expectMotor(AIN1, AIN2, PWMA, 127);
  expectMotor(BIN1, BIN2, PWMB, 127);
}

TEST(fw_left) {
  resetTank();
  tank.setSpeed(127);
  tank.setSteer(127);   // r = 127 -> L = 127-127 = 0, R = 127
  expectMotor(AIN1, AIN2, PWMA, 0);
  expectMotor(BIN1, BIN2, PWMB, 127);
}

TEST(fw_right) {
  resetTank();
  tank.setSpeed(127);
  tank.setSteer(-127);  // r = 127 -> L = 127, R = 127-127 = 0
  expectMotor(AIN1, AIN2, PWMA, 127);
  expectMotor(BIN1, BIN2, PWMB, 0);
}

TEST(fw_partial_steer) {
  resetTank();
  tank.setSpeed(127);
  tank.setSteer(64);    // r = int(64 * 1.0) = 64 -> L = 63, R = 127
  expectMotor(AIN1, AIN2, PWMA, 63);
  expectMotor(BIN1, BIN2, PWMB, 127);
}

TEST(bw_straight) {
  resetTank();
  tank.setSpeed(-127);
  tank.setSteer(0);
  expectMotor(AIN1, AIN2, PWMA, -127);
  expectMotor(BIN1, BIN2, PWMB, -127);
}

TEST(bw_left) {
  resetTank();
  tank.setSpeed(-127);
  tank.setSteer(127);   // r = 127 -> L = -127+127 = 0, R = -127
  expectMotor(AIN1, AIN2, PWMA, 0);
  expectMotor(BIN1, BIN2, PWMB, -127);
}

TEST(bw_right) {
  resetTank();
  tank.setSpeed(-127);
  tank.setSteer(-127);  // r = 127 -> L = -127, R = -127-(-127) = 0
  expectMotor(AIN1, AIN2, PWMA, -127);
  expectMotor(BIN1, BIN2, PWMB, 0);
}

TEST(bw_partial_steer) {
  resetTank();
  tank.setSpeed(-127);
  tank.setSteer(-64);   // r = 64 -> L = -127, R = -127+64 = -63
  expectMotor(AIN1, AIN2, PWMA, -127);
  expectMotor(BIN1, BIN2, PWMB, -63);
}

TEST(fw_extreme_values_clamped) {
  // FIXED (issue #14): the legacy unclamped r_steer made the inner wheel
  // compute 255 - 511 = -256 (i.e. 256 PWM in REVERSE, only saved by
  // analogWrite() saturation). The inner wheel now clamps at -255, with no
  // direction flip; the outer wheel saturates at 255.
  resetTank();
  tank.setSpeed(255);
  tank.setSteer(255);
  expectMotor(AIN1, AIN2, PWMA, -255);
  expectMotor(BIN1, BIN2, PWMB, 255);
}

TEST(bw_extreme_values_clamped) {
  // Same clamping in reverse gear: the inner wheel was +257 (FORWARD),
  // now it clamps at +255, no direction flip.
  resetTank();
  tank.setSpeed(-255);
  tank.setSteer(255);
  expectMotor(AIN1, AIN2, PWMA, 255);
  expectMotor(BIN1, BIN2, PWMB, -255);
}
