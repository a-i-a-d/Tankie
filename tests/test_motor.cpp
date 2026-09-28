// Unit tests for the SparkFun TB6612FNG Motor class (issue #5).
//
// Covers the H-bridge pin logic of Motor::drive()/fwd()/rev()/brake()/
// standby(), the offset multiplication, and the free functions
// forward()/back()/left()/right()/brake().
//
// Pin convention (SparkFun_TB6612.cpp):
//   fwd: In1=HIGH, In2=LOW,  PWM=speed
//   rev: In1=LOW,  In2=HIGH, PWM=speed
//   brake: In1=HIGH, In2=HIGH, PWM=0
//   standby: STBY=LOW
#include "test_main.h"
#include "config.h"
#include "SparkFun_TB6612.h"

// A motor on the REAL config.h pins (motor A), offset 1, shared STBY.
static Motor mA(AIN1, AIN2, PWMA, 1, STBY);

// A motor with offset 2 to verify the speed * offset multiplication.
static Motor mOffset(AIN1, AIN2, PWMA, 2, STBY);

static void resetPins() {
  for (int i = 0; i < 64; i++) host_pin_level[i] = HIGH;
  for (int i = 0; i < 64; i++) host_pwm[i] = 0;
}

TEST(motor_forward) {
  resetPins();
  mA.drive(120);
  CHECK_EQ_INT(digitalRead(STBY), HIGH);   // drive() releases standby
  CHECK_EQ_INT(digitalRead(AIN1), HIGH);
  CHECK_EQ_INT(digitalRead(AIN2), LOW);
  CHECK_EQ_INT(host_pwm[PWMA], 120);
}

TEST(motor_reverse) {
  resetPins();
  mA.drive(-120);
  CHECK_EQ_INT(digitalRead(STBY), HIGH);
  CHECK_EQ_INT(digitalRead(AIN1), LOW);
  CHECK_EQ_INT(digitalRead(AIN2), HIGH);
  CHECK_EQ_INT(host_pwm[PWMA], 120);
}

TEST(motor_zero_is_forward_zero_pwm) {
  resetPins();
  mA.drive(0);
  CHECK_EQ_INT(digitalRead(AIN1), HIGH);
  CHECK_EQ_INT(digitalRead(AIN2), LOW);
  CHECK_EQ_INT(host_pwm[PWMA], 0);
}

TEST(motor_offset_multiplies_speed) {
  resetPins();
  mOffset.drive(100);   // 100 * offset 2 -> 200 PWM
  CHECK_EQ_INT(host_pwm[PWMA], 200);
  CHECK_EQ_INT(digitalRead(AIN1), HIGH);
  CHECK_EQ_INT(digitalRead(AIN2), LOW);
}

TEST(motor_offset_reverse) {
  resetPins();
  mOffset.drive(-50);   // -50 * 2 = -100 -> rev(100)
  CHECK_EQ_INT(host_pwm[PWMA], 100);
  CHECK_EQ_INT(digitalRead(AIN1), LOW);
  CHECK_EQ_INT(digitalRead(AIN2), HIGH);
}

TEST(motor_brake) {
  resetPins();
  mA.drive(100);
  mA.brake();
  CHECK_EQ_INT(digitalRead(AIN1), HIGH);
  CHECK_EQ_INT(digitalRead(AIN2), HIGH);
  CHECK_EQ_INT(host_pwm[PWMA], 0);
}

TEST(motor_standby) {
  resetPins();
  mA.drive(100);
  mA.standby();
  CHECK_EQ_INT(digitalRead(STBY), LOW);
  // standby() only touches the STBY pin, the H-bridge state is untouched
  CHECK_EQ_INT(digitalRead(AIN1), HIGH);
  CHECK_EQ_INT(digitalRead(AIN2), LOW);
  CHECK_EQ_INT(host_pwm[PWMA], 100);
}

TEST(free_forward) {
  resetPins();
  Motor mB(BIN1, BIN2, PWMB, 1, STBY);
  forward(mA, mB, 80);
  CHECK_EQ_INT(host_pwm[PWMA], 80);
  CHECK_EQ_INT(host_pwm[PWMB], 80);
  CHECK_EQ_INT(digitalRead(AIN1), HIGH);
  CHECK_EQ_INT(digitalRead(BIN1), HIGH);
}

TEST(free_forward_default_speed) {
  resetPins();
  Motor mB(BIN1, BIN2, PWMB, 1, STBY);
  forward(mA, mB);
  CHECK_EQ_INT(host_pwm[PWMA], DEFAULTSPEED);
  CHECK_EQ_INT(host_pwm[PWMB], DEFAULTSPEED);
}

TEST(free_back_ignores_sign) {
  resetPins();
  Motor mB(BIN1, BIN2, PWMB, 1, STBY);
  back(mA, mB, -80);   // abs(-80) -> both reverse at 80
  CHECK_EQ_INT(host_pwm[PWMA], 80);
  CHECK_EQ_INT(host_pwm[PWMB], 80);
  CHECK_EQ_INT(digitalRead(AIN1), LOW);
  CHECK_EQ_INT(digitalRead(BIN1), LOW);
  CHECK_EQ_INT(digitalRead(AIN2), HIGH);
  CHECK_EQ_INT(digitalRead(BIN2), HIGH);
}

TEST(free_left_pivot) {
  resetPins();
  Motor mB(BIN1, BIN2, PWMB, 1, STBY);
  left(mA, mB, 100);   // temp = 100/2 = 50 -> left rev, right fwd
  CHECK_EQ_INT(host_pwm[PWMA], 50);
  CHECK_EQ_INT(host_pwm[PWMB], 50);
  CHECK_EQ_INT(digitalRead(AIN1), LOW);
  CHECK_EQ_INT(digitalRead(BIN1), HIGH);
}

TEST(free_right_pivot) {
  resetPins();
  Motor mB(BIN1, BIN2, PWMB, 1, STBY);
  right(mA, mB, 100);  // temp = 50 -> left fwd, right rev
  CHECK_EQ_INT(host_pwm[PWMA], 50);
  CHECK_EQ_INT(host_pwm[PWMB], 50);
  CHECK_EQ_INT(digitalRead(AIN1), HIGH);
  CHECK_EQ_INT(digitalRead(BIN1), LOW);
}

TEST(free_brake_both) {
  resetPins();
  Motor mB(BIN1, BIN2, PWMB, 1, STBY);
  forward(mA, mB, 100);
  brake(mA, mB);
  CHECK_EQ_INT(digitalRead(AIN1), HIGH);
  CHECK_EQ_INT(digitalRead(AIN2), HIGH);
  CHECK_EQ_INT(digitalRead(BIN1), HIGH);
  CHECK_EQ_INT(digitalRead(BIN2), HIGH);
  CHECK_EQ_INT(host_pwm[PWMA], 0);
  CHECK_EQ_INT(host_pwm[PWMB], 0);
}
