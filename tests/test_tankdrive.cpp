// Unit tests for TankDrive (tankie/tankdrive.cpp) - issue #5.
//
// TankDrive turns a (speed, steer) pair into per-motor values:
//   r_steer = steer * (|speed| / 127)      (updateRelativeSteer)
//   then per branch: L/R = speed +/- r_steer (updateMotors)
//
// The tests drive the REAL class on the REAL config.h pins and assert on
// the resulting H-bridge output: the PWM magnitude on PWMA/PWMB and the
// In1/In2 direction pins, for every branch of updateMotors():
//
//   stop            speed == 0
//   fw straight/L   speed > 0, r_steer >= 0   -> L = speed - r_steer, R = speed
//   fw right        speed > 0, r_steer < 0    -> L = speed, R = speed + r_steer
//   bw straight/L   speed < 0, r_steer >= 0   -> L = speed + r_steer, R = speed
//   bw right        speed < 0, r_steer < 0    -> L = speed, R = speed - r_steer
//
// Motor wiring per tankie.ino (config.h pins):
//   M1 (left)  = Motor(AIN1, AIN2, PWMA, offset 1, STBY)
//   M2 (right) = Motor(BIN1, BIN2, PWMB, offset 1, STBY)
//   fwd: In1=HIGH, In2=LOW   rev: In1=LOW, In2=HIGH   (SparkFun_TB6612)
#include "test_main.h"
#include "config.h"
#include "SparkFun_TB6612.h"
#include "tankdrive.h"

// The two motors, exactly as tankie.ino wires them (offset 1, shared STBY).
static Motor mLeft(AIN1, AIN2, PWMA, 1, STBY);
static Motor mRight(BIN1, BIN2, PWMB, 1, STBY);
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

TEST(stop_ignores_steer) {
  resetTank();
  tank.setSteer(200);   // steer with zero speed -> still stopped
  tank.setSpeed(0);
  expectMotor(AIN1, AIN2, PWMA, 0);
  expectMotor(BIN1, BIN2, PWMB, 0);
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
  tank.setSteer(127);   // r_steer = 127 -> L = 127-127 = 0, R = 127
  expectMotor(AIN1, AIN2, PWMA, 0);
  expectMotor(BIN1, BIN2, PWMB, 127);
}

TEST(fw_right) {
  resetTank();
  tank.setSpeed(127);
  tank.setSteer(-127);  // r_steer = -127 -> L = 127, R = 127-127 = 0
  expectMotor(AIN1, AIN2, PWMA, 127);
  expectMotor(BIN1, BIN2, PWMB, 0);
}

TEST(fw_partial_steer) {
  resetTank();
  tank.setSpeed(127);
  tank.setSteer(64);    // r_steer = int(64 * 1.0) = 64 -> L = 63, R = 127
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
  tank.setSteer(127);   // r_steer = 127 -> L = -127+127 = 0, R = -127
  expectMotor(AIN1, AIN2, PWMA, 0);
  expectMotor(BIN1, BIN2, PWMB, -127);
}

TEST(bw_right) {
  resetTank();
  tank.setSpeed(-127);
  tank.setSteer(-127);  // r_steer = -127 -> L = -127, R = -127-(-127) = 0
  expectMotor(AIN1, AIN2, PWMA, -127);
  expectMotor(BIN1, BIN2, PWMB, 0);
}

TEST(bw_partial_steer) {
  resetTank();
  tank.setSpeed(-127);
  tank.setSteer(-64);   // r_steer = -64 -> L = -127, R = -127+64 = -63
  expectMotor(AIN1, AIN2, PWMA, -127);
  expectMotor(BIN1, BIN2, PWMB, -63);
}

TEST(fw_extreme_values_wrap_into_reverse) {
  // Documented behavior: r_steer scales with |speed|, so at full speed a
  // full steer makes the inner motor exceed zero and flip direction
  // (r_steer = int(255 * 255/127) = 512 -> L = 255 - 512 = -257, i.e.
  // 257 PWM in REVERSE), while the outer motor saturates at 255.
  resetTank();
  tank.setSpeed(255);
  tank.setSteer(255);
  expectMotor(AIN1, AIN2, PWMA, -257);
  expectMotor(BIN1, BIN2, PWMB, 255);
}

TEST(bw_extreme_values_wrap_into_reverse) {
  // Same wrap-around in reverse gear: L = -255 + 512 = +257 (FORWARD).
  resetTank();
  tank.setSpeed(-255);
  tank.setSteer(255);
  expectMotor(AIN1, AIN2, PWMA, 257);
  expectMotor(BIN1, BIN2, PWMB, -255);
}
