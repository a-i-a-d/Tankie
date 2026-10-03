// Unit tests for the serial control protocol (tankie/serialproto.cpp) - issue #29.
//
// Covers the NDJSON command dispatch, validation + clamping, seq/ack
// correlation, the command watchdog (fire + re-arm), the hello handshake,
// the 1 Hz state broadcast, and the line reader (partial lines, CRLF,
// non-JSON garbage).
//
// The tests drive the REAL SerialProto class against the REAL TankDrive
// (real config.h motor pins) and a Servo shim, and assert on:
//   - the H-bridge PWM/direction pins (motors actually moved)
//   - the servo angle (pan/tilt actually moved)
//   - the Serial capture buffer (the exact NDJSON lines emitted)
//
// Time is deterministic: tests advance millis() via host_set_millis().
#include "test_main.h"
#include "config.h"
#include "config_serial.h"
#include "SparkFun_TB6612.h"
#include "tankdrive.h"
#include "serialproto.h"

#include <cstdio>
#include <string>

// The two motors, exactly as tankie.ino wires them (offset 1, shared STBY).
static Motor mLeft(AIN1, AIN2, PWMA, 1, STBY);
static Motor mRight(BIN1, BIN2, PWMB, 1, STBY);
static TankDrive tank(&mLeft, &mRight);
static Servo panServo;
static Servo tiltServo;

// A battery reader that returns a fixed, known voltage so the state
// broadcast is deterministic (the real getBatVoltage() reads the ADC).
static float fakeBattery(float R1, float R2) { (void)R1; (void)R2; return 7.42f; }

static SerialProto proto(&tank, &panServo, &tiltServo, fakeBattery, 330000, 33000);

// Reset everything to a "fresh boot" state before each test.
static void resetProto() {
  panServo.reset();
  tiltServo.reset();
  for (int i = 0; i < 64; i++) host_pin_level[i] = HIGH;
  for (int i = 0; i < 64; i++) host_pwm[i] = 0;
  host_clear_serial_rx();
  Serial.clearCapture();
  host_set_millis(0);
  proto.begin();
  // begin() emits the hello line; drop it from the capture so tests can
  // assert on exactly the lines a command produces.
  Serial.clearCapture();
  host_set_millis(0);
}

// True if the capture contains a line that is exactly `expected`.
static bool capturedHasLine(const char* expected) {
  const std::string& cap = Serial.captured();
  const std::string want = std::string(expected) + "\n";
  return cap.find(want) != std::string::npos;
}

static bool capturedContains(const char* needle) {
  return Serial.captured().find(needle) != std::string::npos;
}

// ---------------------------------------------------------------------------
// T1 — hello handshake
// ---------------------------------------------------------------------------
TEST(hello_on_begin) {
  resetProto();
  Serial.clearCapture();
  proto.begin();
  char want[96];
  std::snprintf(want, sizeof(want),
                "{\"type\":\"hello\",\"proto\":%d,\"fw\":\"%s\"}",
                (int)SERIAL_PROTO_VERSION, TANKIE_FW_VERSION);
  CHECK(capturedHasLine(want));
}

TEST(hello_proto_version_is_one) {
  resetProto();
  Serial.clearCapture();
  proto.begin();
  CHECK(capturedContains("\"proto\":1"));
}

// ---------------------------------------------------------------------------
// T2 — drive command: ack + seq + motors actually move
// ---------------------------------------------------------------------------
TEST(drive_straight_forward) {
  resetProto();
  proto.injectLine("{\"cmd\":\"drive\",\"speed\":50,\"steer\":0}");
  CHECK(proto.seq() == 1);
  CHECK(proto.speed() == 50);
  CHECK(proto.steer() == 0);
  CHECK(capturedHasLine("{\"type\":\"ack\",\"seq\":1}"));
  // steer 0 -> r_steer 0 -> both motors forward at 50.
  CHECK_EQ_INT(digitalRead(AIN1), HIGH);
  CHECK_EQ_INT(host_pwm[PWMA], 50);
  CHECK_EQ_INT(digitalRead(BIN1), HIGH);
  CHECK_EQ_INT(host_pwm[PWMB], 50);
}

TEST(drive_backward) {
  resetProto();
  proto.injectLine("{\"cmd\":\"drive\",\"speed\":-80,\"steer\":0}");
  CHECK(proto.speed() == -80);
  CHECK_EQ_INT(digitalRead(AIN1), LOW);   // reverse
  CHECK_EQ_INT(host_pwm[PWMA], 80);
}

TEST(drive_left_steer) {
  resetProto();
  // speed 100, steer +127 -> r_steer = 127*(100/127)=100 -> L=0, R=100
  proto.injectLine("{\"cmd\":\"drive\",\"speed\":100,\"steer\":127}");
  CHECK(proto.steer() == 127);
  CHECK_EQ_INT(host_pwm[PWMA], 0);    // left motor stopped (full left)
  CHECK_EQ_INT(host_pwm[PWMB], 100);  // right motor at full
}

TEST(drive_acks_increment_seq) {
  resetProto();
  proto.injectLine("{\"cmd\":\"drive\",\"speed\":10,\"steer\":0}");
  CHECK(proto.seq() == 1);
  proto.injectLine("{\"cmd\":\"drive\",\"speed\":20,\"steer\":0}");
  CHECK(proto.seq() == 2);
  CHECK(capturedHasLine("{\"type\":\"ack\",\"seq\":2}"));
}

// ---------------------------------------------------------------------------
// T3 — pan / tilt: servo moves + state reflects it
// ---------------------------------------------------------------------------
TEST(pan_moves_servo) {
  resetProto();
  proto.injectLine("{\"cmd\":\"pan\",\"angle\":90}");
  CHECK(proto.pan() == 90);
  CHECK_EQ_INT(panServo.read(), 90);
  CHECK(capturedHasLine("{\"type\":\"ack\",\"seq\":1}"));
}

TEST(tilt_moves_servo) {
  resetProto();
  proto.injectLine("{\"cmd\":\"tilt\",\"angle\":30}");
  CHECK(proto.tilt() == 30);
  CHECK_EQ_INT(tiltServo.read(), 30);
}

TEST(pan_zero_and_180_are_valid) {
  resetProto();
  proto.injectLine("{\"cmd\":\"pan\",\"angle\":0}");
  CHECK_EQ_INT(panServo.read(), 0);
  proto.injectLine("{\"cmd\":\"pan\",\"angle\":180}");
  CHECK_EQ_INT(panServo.read(), 180);
}

// ---------------------------------------------------------------------------
// T4 — stop: motors off immediately, state.speed = 0
// ---------------------------------------------------------------------------
TEST(stop_turns_motors_off) {
  resetProto();
  proto.injectLine("{\"cmd\":\"drive\",\"speed\":120,\"steer\":0}");
  CHECK_EQ_INT(host_pwm[PWMA], 120);
  proto.injectLine("{\"cmd\":\"stop\"}");
  CHECK(proto.speed() == 0);
  CHECK(proto.steer() == 0);
  CHECK_EQ_INT(host_pwm[PWMA], 0);
  CHECK_EQ_INT(host_pwm[PWMB], 0);
  CHECK(capturedHasLine("{\"type\":\"ack\",\"seq\":2}"));
}

// ---------------------------------------------------------------------------
// T5 — out-of-range -> error + NO state change
// ---------------------------------------------------------------------------
TEST(drive_speed_999_is_range_error) {
  resetProto();
  proto.injectLine("{\"cmd\":\"drive\",\"speed\":999,\"steer\":0}");
  CHECK(capturedHasLine("{\"type\":\"error\",\"seq\":0,\"code\":\"range\",\"field\":\"speed\"}"));
  CHECK(proto.speed() == 0);          // no state change
  CHECK_EQ_INT(host_pwm[PWMA], 0);    // motors untouched
}

TEST(drive_steer_999_is_range_error) {
  resetProto();
  proto.injectLine("{\"cmd\":\"drive\",\"speed\":50,\"steer\":999}");
  CHECK(capturedContains("\"code\":\"range\""));
  CHECK(capturedContains("\"field\":\"steer\""));
  CHECK(proto.speed() == 0);
}

TEST(pan_181_is_range_error) {
  resetProto();
  proto.injectLine("{\"cmd\":\"pan\",\"angle\":181}");
  CHECK(capturedContains("\"field\":\"pan\""));
  CHECK_EQ_INT(panServo.read(), 0);   // servo untouched (shim default)
}

TEST(tilt_negative_is_range_error) {
  resetProto();
  proto.injectLine("{\"cmd\":\"tilt\",\"angle\":-1}");
  CHECK(capturedContains("\"field\":\"tilt\""));
}

TEST(drive_boundary_values_are_accepted) {
  resetProto();
  proto.injectLine("{\"cmd\":\"drive\",\"speed\":255,\"steer\":255}");
  CHECK(proto.speed() == 255);
  CHECK(proto.steer() == 255);
  proto.injectLine("{\"cmd\":\"drive\",\"speed\":-255,\"steer\":-255}");
  CHECK(proto.speed() == -255);
  CHECK(proto.steer() == -255);
}

// ---------------------------------------------------------------------------
// T6 — watchdog: fires after SERIAL_WATCHDOG_MS, re-arms on next command
// ---------------------------------------------------------------------------
TEST(watchdog_fires_after_timeout) {
  resetProto();
  proto.injectLine("{\"cmd\":\"drive\",\"speed\":100,\"steer\":0}");
  CHECK_EQ_INT(host_pwm[PWMA], 100);
  CHECK(!proto.watchdogFired());

  host_set_millis(SERIAL_WATCHDOG_MS + 1);   // just past the timeout
  proto.loop();

  CHECK(proto.watchdogFired());
  CHECK(proto.speed() == 0);
  CHECK_EQ_INT(host_pwm[PWMA], 0);           // motors stopped
  CHECK_EQ_INT(host_pwm[PWMB], 0);
  CHECK(capturedHasLine("{\"type\":\"watchdog\"}"));
}

TEST(watchdog_does_not_fire_before_timeout) {
  resetProto();
  proto.injectLine("{\"cmd\":\"drive\",\"speed\":100,\"steer\":0}");
  host_set_millis(SERIAL_WATCHDOG_MS);       // exactly at the timeout (not past)
  proto.loop();
  CHECK(!proto.watchdogFired());
  CHECK_EQ_INT(host_pwm[PWMA], 100);
}

TEST(watchdog_rearms_on_next_command) {
  resetProto();
  proto.injectLine("{\"cmd\":\"drive\",\"speed\":100,\"steer\":0}");
  host_set_millis(SERIAL_WATCHDOG_MS + 1);
  proto.loop();
  CHECK(proto.watchdogFired());
  CHECK(capturedHasLine("{\"type\":\"watchdog\"}"));

  // A new command re-arms the watchdog and the motors run again.
  Serial.clearCapture();
  host_set_millis(SERIAL_WATCHDOG_MS + 2);
  proto.injectLine("{\"cmd\":\"drive\",\"speed\":60,\"steer\":0}");
  CHECK(!proto.watchdogFired());
  CHECK_EQ_INT(host_pwm[PWMA], 60);

  // And it fires again after the next timeout.
  host_set_millis(2 * SERIAL_WATCHDOG_MS + 3);
  proto.loop();
  CHECK(proto.watchdogFired());
}

TEST(stop_rearms_watchdog) {
  resetProto();
  proto.injectLine("{\"cmd\":\"drive\",\"speed\":100,\"steer\":0}");
  host_set_millis(SERIAL_WATCHDOG_MS + 1);
  proto.loop();
  CHECK(proto.watchdogFired());

  // stop re-arms: no new watchdog event immediately after.
  Serial.clearCapture();
  host_set_millis(SERIAL_WATCHDOG_MS + 2);
  proto.injectLine("{\"cmd\":\"stop\"}");
  CHECK(!proto.watchdogFired());
  CHECK(!capturedContains("\"type\":\"watchdog\""));
}

// ---------------------------------------------------------------------------
// T7 — non-JSON garbage is ignored, link stays up
// ---------------------------------------------------------------------------
TEST(garbage_lines_are_ignored) {
  resetProto();
  proto.injectLine("Battery Voltage: 7.42");
  proto.injectLine("this is not json at all");
  proto.injectLine("");
  CHECK(proto.seq() == 0);
  CHECK(!capturedContains("\"type\":\"ack\""));
  CHECK(!capturedContains("\"type\":\"error\""));
  // The link is still alive: a valid command is still processed.
  proto.injectLine("{\"cmd\":\"drive\",\"speed\":10,\"steer\":0}");
  CHECK(proto.seq() == 1);
}

TEST(unknown_cmd_is_ignored) {
  resetProto();
  proto.injectLine("{\"cmd\":\"fly\",\"speed\":50}");
  CHECK(proto.seq() == 0);
  CHECK(proto.speed() == 0);
}

// ---------------------------------------------------------------------------
// Line reader: partial lines, CRLF, multi-line
// ---------------------------------------------------------------------------
TEST(line_reader_handles_partial_lines) {
  resetProto();
  // Feed a line in two chunks (simulates serial byte-by-byte arrival).
  host_feed_serial("{\"cmd\":\"drive\",\"speed\":4");
  proto.loop();
  CHECK(proto.seq() == 0);   // not a complete line yet
  host_feed_serial("0,\"steer\":0}\n");
  proto.loop();
  CHECK(proto.seq() == 1);
  CHECK(proto.speed() == 40);
}

TEST(line_reader_handles_crlf) {
  resetProto();
  host_feed_serial("{\"cmd\":\"pan\",\"angle\":45}\r\n");
  proto.loop();
  CHECK(proto.pan() == 45);
}

TEST(line_reader_handles_multiple_lines) {
  resetProto();
  host_feed_serial("{\"cmd\":\"pan\",\"angle\":10}\n{\"cmd\":\"tilt\",\"angle\":20}\n");
  proto.loop();
  CHECK(proto.pan() == 10);
  CHECK(proto.tilt() == 20);
  CHECK(proto.seq() == 2);
}

// ---------------------------------------------------------------------------
// State broadcast
// ---------------------------------------------------------------------------
TEST(state_broadcast_on_change) {
  resetProto();
  Serial.clearCapture();
  proto.injectLine("{\"cmd\":\"drive\",\"speed\":50,\"steer\":0}");
  // Issue #46: the state line now carries net_mode + net_ip (defaults
  // before WiFiManager has run: "sta" / "0.0.0.0").
  CHECK(capturedHasLine(
    "{\"type\":\"state\",\"seq\":1,\"battery\":7.42,\"speed\":50,\"steer\":0,\"pan\":90,\"tilt\":90,\"net_mode\":\"sta\",\"net_ip\":\"0.0.0.0\"}"));
}

TEST(state_broadcast_at_1hz) {
  resetProto();
  Serial.clearCapture();
  host_set_millis(0);
  proto.loop();   // t=0, no broadcast (just started)
  CHECK(!capturedContains("\"type\":\"state\""));
  host_set_millis(1000);
  proto.loop();   // t=1000 -> broadcast
  CHECK(capturedContains("\"type\":\"state\""));
  CHECK(capturedContains("\"battery\":7.42"));
}

// ---------------------------------------------------------------------------
// JSON extractor edge cases
// ---------------------------------------------------------------------------
TEST(json_int_handles_negative) {
  resetProto();
  proto.injectLine("{\"cmd\":\"drive\",\"speed\":-50,\"steer\":0}");
  CHECK(proto.speed() == -50);
}

TEST(json_int_handles_whitespace) {
  resetProto();
  proto.injectLine("{ \"cmd\" : \"drive\" , \"speed\" : 33 , \"steer\" : 0 }");
  CHECK(proto.speed() == 33);
}
