// Unit tests for the SparkFun TB6612FNG Motor class (issue #5).
//
// Covers the H-bridge pin logic of Motor::drive()/fwd()/rev()/brake()/
// standby().
//
// Pin convention (SparkFun_TB6612.cpp):
//   fwd: In1=HIGH, In2=LOW,  PWM=speed
//   rev: In1=LOW,  In2=HIGH, PWM=speed
//   brake: In1=HIGH, In2=HIGH, PWM=0
//   standby: STBY=LOW
#include "test_main.h"
#include "config.h"
#include "SparkFun_TB6612.h"

// A motor on the REAL config.h pins (motor A), shared STBY.
static Motor mA(AIN1, AIN2, PWMA, STBY);

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
