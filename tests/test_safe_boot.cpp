// Safe motor state at boot (issue #44 C).
//
// When the ESP8266 boots with the TB6612FNG connected, the boot-strapping
// pins (AIN1=GPIO0, STBY=GPIO2, BIN1=GPIO15) can sit in indeterminate
// states and the H-bridge can drive the motors at max speed before the
// firmware gets going. The fix is setMotorsSafe(), called as the FIRST
// statement in setup() (before Serial/LittleFS/servos), which forces every
// motor pin to a known non-driving state: STBY=LOW (standby) + all
// direction/PWM pins LOW.
//
// These tests exercise the REAL config.h pins (the shim maps D1..D8 to the
// true D1 Mini GPIOs) and assert:
//   1. setMotorsSafe() drives all seven motor pins LOW from a "bad boot"
//      state (direction pins left HIGH, STBY left HIGH).
//   2. setup() (which calls setMotorsSafe() first) leaves the pins safe,
//      even though the rest of setup() would otherwise re-configure them.
#include "test_main.h"
#include <Arduino.h>   // D1..D8 + host_pin_level/host_pwm tables
#include <LittleFS.h>  // host_littlefs_begin_ok hook
#include "config.h"

// The sketch's safe-state helper (defined in tankie.ino, forward-declared
// there so the host build compiles it as plain C++).
void setMotorsSafe();
void setup();

static void resetPins() {
  for (int i = 0; i < 64; i++) host_pin_level[i] = HIGH;
  for (int i = 0; i < 64; i++) host_pwm[i] = 0;
}

// The seven motor pins the safe state must cover (the shared STBY once).
static const int kMotorPins[] = {STBY, PWMA, AIN1, AIN2, PWMB, BIN1, BIN2};

TEST(safe_boot_set_motors_safe_clears_bad_state) {
  resetPins();
  // Simulate a bad/strapping-pin boot: the direction pins and STBY are
  // left HIGH (the exact "motors spin at max" symptom from issue #44).
  digitalWrite(AIN1, HIGH);
  digitalWrite(AIN2, HIGH);
  digitalWrite(BIN1, HIGH);
  digitalWrite(BIN2, HIGH);
  digitalWrite(STBY, HIGH);

  setMotorsSafe();

  for (int pin : kMotorPins) {
    CHECK_EQ_INT(digitalRead(pin), LOW);
    CHECK_EQ_INT(host_pwm[pin], 0);
  }
}

TEST(safe_boot_setup_leaves_motors_safe) {
  resetPins();
  // Bad-boot starting state again.
  digitalWrite(AIN1, HIGH);
  digitalWrite(BIN1, HIGH);
  digitalWrite(STBY, HIGH);

  // setup() calls setMotorsSafe() as its first statement. Force the
  // LittleFS shim to succeed so setup() runs its FULL path (servos, OTA,
  // WiFi, web server) — the host shims make those no-ops, so it returns
  // cleanly and we can assert the motor pins stayed safe.
  host_littlefs_begin_ok = true;
  setup();
  host_littlefs_begin_ok = false;

  for (int pin : kMotorPins) {
    CHECK_EQ_INT(digitalRead(pin), LOW);
    CHECK_EQ_INT(host_pwm[pin], 0);
  }
}
