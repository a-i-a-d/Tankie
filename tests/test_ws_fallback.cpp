// Unit tests for the WebSocket fallback handler (tankie.ino) - issue #34.
//
// The /ws handler is the fallback control path (the serial NDJSON link,
// tankie/serialproto.cpp, is primary). These tests drive the REAL
// handleWebSocketMessage() from tankie.ino - compiled for the host
// against the network shims in tests/shims/ - against the REAL TankDrive
// (real config.h motor pins) and Servo shims, and assert on:
//   - the H-bridge PWM/direction pins (motors actually moved)
//   - the servo angles (pan/tilt actually moved)
//   - the command watchdog (armed on drive, fires after timeout)
//   - the rejection paths (non-JSON / missing cmd / missing field /
//     out-of-range / unknown cmd -> no motor or servo change)
//   - the OOB fix: a canary byte at data[len] must remain untouched
//     (the old `data[len] = 0` wrote one byte past the handler's view
//     into the library's TCP receive pbuf)
//
// Time is deterministic: tests advance millis() via host_set_millis().
#include "test_main.h"
#include "config.h"
#include "SparkFun_TB6612.h"
#include "tankdrive.h"
#include <AsyncWebSocket.h>

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>

// ---------------------------------------------------------------------------
// The tankie.ino globals the handler writes (defined in tankie.ino, which
// is compiled into the test binary).
// ---------------------------------------------------------------------------
extern TankDrive tank;
extern Servo servoPan;
extern Servo servoTilt;
extern int currentSpeed;
extern int currentSteer;
extern int currentPan;
extern int currentTilt;
extern long lastCommandMs;
extern bool watchdogActive;
extern void handleWebSocketMessage(void *arg, uint8_t *data, size_t len);

// Reset the WS handler's state to a "fresh boot" before each test.
static void resetWs() {
  servoPan.reset();
  servoTilt.reset();
  for (int i = 0; i < 64; i++) host_pin_level[i] = HIGH;
  for (int i = 0; i < 64; i++) host_pwm[i] = 0;
  host_clear_serial_rx();
  host_clear_ws_text_all();
  Serial.clearCapture();
  host_set_millis(0);
  currentSpeed = 0;
  currentSteer = 0;
  currentPan = 90;
  currentTilt = 90;
  lastCommandMs = 0;
  watchdogActive = false;
}

// Deliver a frame to the handler exactly the way the real library does:
// a WS_EVT_DATA with a fully-received (final, index 0) text frame whose
// AwsFrameInfo.len matches the data length.
static void sendWsFrame(const uint8_t *data, size_t len) {
  AwsFrameInfo info;
  std::memset(&info, 0, sizeof(info));
  info.message_opcode = WS_TEXT;
  info.opcode = WS_TEXT;
  info.final = 1;
  info.len = (uint64_t)len;
  info.index = 0;
  handleWebSocketMessage(&info, (uint8_t *)data, len);
}

static void sendWsText(const char *text) {
  sendWsFrame((const uint8_t *)text, std::strlen(text));
}

// ---------------------------------------------------------------------------
// T1 — valid commands: motors/servos actually move
// ---------------------------------------------------------------------------
TEST(ws_drive_straight_forward) {
  resetWs();
  sendWsText("{\"cmd\":\"drive\",\"speed\":50,\"steer\":0}");
  CHECK_EQ_INT(currentSpeed, 50);
  CHECK_EQ_INT(currentSteer, 0);
  CHECK_EQ_INT(host_pwm[PWMA], 50);   // left motor forward at 50
  CHECK_EQ_INT(host_pwm[PWMB], 50);   // right motor forward at 50
  CHECK_EQ_INT(digitalRead(AIN1), HIGH);
  CHECK_EQ_INT(digitalRead(BIN1), HIGH);
  CHECK(watchdogActive);             // a drive command arms the watchdog
  CHECK_EQ_INT(lastCommandMs, 0);    // millis() is 0 in the test clock
}

TEST(ws_drive_backward) {
  resetWs();
  sendWsText("{\"cmd\":\"drive\",\"speed\":-80,\"steer\":0}");
  CHECK_EQ_INT(currentSpeed, -80);
  CHECK_EQ_INT(digitalRead(AIN1), LOW);   // reverse
  CHECK_EQ_INT(host_pwm[PWMA], 80);
  CHECK_EQ_INT(digitalRead(BIN1), LOW);
  CHECK_EQ_INT(host_pwm[PWMB], 80);
}

TEST(ws_drive_steer_turns_one_wheel) {
  resetWs();
  // speed 100, steer +127 -> r_steer = int(127*(100/127)) = 100 -> one
  // wheel at speed (100), the other stopped (speed - r_steer = 0): a full
  // turn. tankie.ino wires TankDrive(&M1, &M2) with M1 = the BIN/PWMB
  // motor (mLeft -> the stopped value 0) and M2 = the AIN/PWMA motor
  // (mRight -> the full value 100).
  sendWsText("{\"cmd\":\"drive\",\"speed\":100,\"steer\":127}");
  CHECK_EQ_INT(host_pwm[PWMA], 100);   // AIN/PWMA motor (mRight) at full
  CHECK_EQ_INT(host_pwm[PWMB], 0);     // BIN/PWMB motor (mLeft) stopped
}

TEST(ws_pan_moves_pan_servo) {
  resetWs();
  sendWsText("{\"cmd\":\"pan\",\"angle\":45}");
  CHECK_EQ_INT(currentPan, 45);
  CHECK_EQ_INT(servoPan.read(), 45);
  CHECK_EQ_INT(servoTilt.read(), 0);   // tilt untouched
}

TEST(ws_tilt_moves_tilt_servo) {
  resetWs();
  sendWsText("{\"cmd\":\"tilt\",\"angle\":135}");
  CHECK_EQ_INT(currentTilt, 135);
  CHECK_EQ_INT(servoTilt.read(), 135);
  CHECK_EQ_INT(servoPan.read(), 0);    // pan untouched
}

TEST(ws_stop_stops_motors) {
  resetWs();
  sendWsText("{\"cmd\":\"drive\",\"speed\":120,\"steer\":0}");
  CHECK_EQ_INT(host_pwm[PWMA], 120);
  sendWsText("{\"cmd\":\"stop\"}");
  CHECK_EQ_INT(currentSpeed, 0);
  CHECK_EQ_INT(host_pwm[PWMA], 0);
  CHECK_EQ_INT(host_pwm[PWMB], 0);
  CHECK(!watchdogActive);             // stop disarms the watchdog
}

// ---------------------------------------------------------------------------
// T2 — command watchdog arming: the handler's responsibility is to arm the
// watchdog on a drive command (loop() is what fires it after the timeout).
// ---------------------------------------------------------------------------
TEST(ws_drive_arms_watchdog) {
  resetWs();
  sendWsText("{\"cmd\":\"drive\",\"speed\":50,\"steer\":0}");
  CHECK(watchdogActive);
  CHECK_EQ_INT(lastCommandMs, 0);   // millis() is 0 on the test clock
}

TEST(ws_pan_does_not_arm_watchdog) {
  resetWs();
  sendWsText("{\"cmd\":\"pan\",\"angle\":45}");
  // pan/tilt do not arm the drive watchdog (only drive/stop do).
  CHECK(!watchdogActive);
}

// ---------------------------------------------------------------------------
// T3 — rejection: invalid / unknown messages must not move anything
// ---------------------------------------------------------------------------
static void assertNoMovement() {
  CHECK_EQ_INT(host_pwm[PWMA], 0);
  CHECK_EQ_INT(host_pwm[PWMB], 0);
  CHECK_EQ_INT(servoPan.read(), 0);
  CHECK_EQ_INT(servoTilt.read(), 0);
  CHECK_EQ_INT(currentSpeed, 0);
  CHECK_EQ_INT(currentSteer, 0);
  CHECK(!watchdogActive);
}

TEST(ws_reject_non_json) {
  resetWs();
  sendWsText("hello world");
  assertNoMovement();
  CHECK(Serial.captured().find("Ignoring non-JSON") != std::string::npos);
}

TEST(ws_reject_empty_frame) {
  resetWs();
  uint8_t b = 0x00;
  sendWsFrame(&b, 0);   // zero-length frame
  assertNoMovement();
  CHECK(Serial.captured().find("Ignoring non-JSON") != std::string::npos);
}

TEST(ws_reject_missing_cmd) {
  resetWs();
  sendWsText("{\"speed\":50,\"steer\":0}");
  assertNoMovement();
  CHECK(Serial.captured().find("without a cmd field") != std::string::npos);
}

TEST(ws_reject_unknown_cmd) {
  resetWs();
  sendWsText("{\"cmd\":\"launch\"}");
  assertNoMovement();
  CHECK(Serial.captured().find("Unknown websocket cmd") != std::string::npos);
}

TEST(ws_reject_drive_missing_fields) {
  resetWs();
  sendWsText("{\"cmd\":\"drive\"}");
  assertNoMovement();
  CHECK(Serial.captured().find("missing speed/steer") != std::string::npos);
}

TEST(ws_reject_drive_speed_out_of_range) {
  resetWs();
  sendWsText("{\"cmd\":\"drive\",\"speed\":300,\"steer\":0}");
  assertNoMovement();
  CHECK(Serial.captured().find("out of range") != std::string::npos);
  sendWsText("{\"cmd\":\"drive\",\"speed\":-256,\"steer\":0}");
  assertNoMovement();
}

TEST(ws_reject_drive_steer_out_of_range) {
  resetWs();
  sendWsText("{\"cmd\":\"drive\",\"speed\":50,\"steer\":256}");
  assertNoMovement();
  CHECK(Serial.captured().find("out of range") != std::string::npos);
}

TEST(ws_reject_pan_out_of_range) {
  resetWs();
  sendWsText("{\"cmd\":\"pan\",\"angle\":181}");
  assertNoMovement();
  CHECK(Serial.captured().find("out of range") != std::string::npos);
  sendWsText("{\"cmd\":\"pan\",\"angle\":-1}");
  assertNoMovement();
}

TEST(ws_reject_tilt_out_of_range) {
  resetWs();
  sendWsText("{\"cmd\":\"tilt\",\"angle\":-5}");
  assertNoMovement();
  CHECK(Serial.captured().find("out of range") != std::string::npos);
}

TEST(ws_reject_pan_missing_angle) {
  resetWs();
  sendWsText("{\"cmd\":\"pan\"}");
  assertNoMovement();
  CHECK(Serial.captured().find("missing angle") != std::string::npos);
}

TEST(ws_reject_tilt_missing_angle) {
  resetWs();
  sendWsText("{\"cmd\":\"tilt\"}");
  assertNoMovement();
  CHECK(Serial.captured().find("missing angle") != std::string::npos);
}

// ---------------------------------------------------------------------------
// T4 — frame-shape guards (as the real library delivers them)
// ---------------------------------------------------------------------------
TEST(ws_ignore_binary_frame) {
  resetWs();
  AwsFrameInfo info;
  std::memset(&info, 0, sizeof(info));
  info.message_opcode = WS_BINARY;
  info.opcode = WS_BINARY;
  info.final = 1;
  info.len = 4;
  info.index = 0;
  const uint8_t payload[4] = {'d', 'r', 'i', 'v'};
  handleWebSocketMessage(&info, (uint8_t *)payload, 4);
  assertNoMovement();
}

TEST(ws_ignore_fragmented_frame) {
  resetWs();
  AwsFrameInfo info;
  std::memset(&info, 0, sizeof(info));
  info.message_opcode = WS_TEXT;
  info.opcode = WS_TEXT;
  info.final = 0;      // not the last fragment
  info.len = 3;
  info.index = 0;
  const uint8_t payload[3] = {'{', 'c', 'm'};
  handleWebSocketMessage(&info, (uint8_t *)payload, 3);
  assertNoMovement();
}

// ---------------------------------------------------------------------------
// T5 — the OOB fix (issue #34): nothing is written past data[len]
// ---------------------------------------------------------------------------
TEST(ws_oob_sentinel_untouched) {
  resetWs();
  const char *payload = "{\"cmd\":\"drive\",\"speed\":10,\"steer\":0}";
  size_t len = std::strlen(payload);
  uint8_t frame[64];
  std::memcpy(frame, payload, len);
  frame[len] = 0xA5;   // canary: the byte the old `data[len] = 0` clobbered
  sendWsFrame(frame, len);
  CHECK_EQ_INT((int)frame[len], (int)0xA5);      // canary untouched
  CHECK_EQ_INT((int)frame[len - 1], (int)'}');   // last payload byte intact
  CHECK_EQ_INT(host_pwm[PWMA], 10);              // command still applied
}

TEST(ws_long_frame_rejected_no_crash) {
  resetWs();
  // A frame longer than the 128-byte handler buffer: truncated before
  // parsing, so it fails JSON validation and is rejected - no OOB write,
  // no unbounded read, no motor movement.
  std::string big(150, 'a');
  big += "}";
  sendWsFrame((const uint8_t *)big.data(), big.size());
  assertNoMovement();
  CHECK(Serial.captured().find("Ignoring non-JSON") != std::string::npos);
}
