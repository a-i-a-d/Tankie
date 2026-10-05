// Unit tests for the set_stream command + stream_url broadcast (issue #51).
//
// The Pi (which owns the camera + mediamtx) pushes its own stream endpoint to
// the ESP over the serial link via the set_stream command. The ESP stores it
// and appends it as stream_url to every state broadcast, so the human web UI
// can point its video iframe at the correct (dynamic) Pi IP instead of a
// hard-coded, stale address.
//
// Covers:
//   - set_stream accepted -> ack + state carries the stream_url field
//   - malformed (missing ip / bad port / bad path) -> error + no state change
//   - pre-set state is unchanged (stream_url field absent)
//   - the 1 Hz periodic broadcast also carries stream_url once set
#include "test_main.h"
#include "config.h"
#include "config_serial.h"
#include "SparkFun_TB6612.h"
#include "tankdrive.h"
#include "serialproto.h"
#include "streaminfo.h"
#include "netstate.h"

static Motor mLeft(AIN1, AIN2, PWMA, 1, STBY);
static Motor mRight(BIN1, BIN2, PWMB, 1, STBY);
static TankDrive tank(&mLeft, &mRight);
static Servo panServo;
static Servo tiltServo;

static float fakeBattery(float R1, float R2) { (void)R1; (void)R2; return 7.42f; }
static SerialProto proto(&tank, &panServo, &tiltServo, fakeBattery, 330000, 33000);

static void resetStream() {
  panServo.reset();
  tiltServo.reset();
  for (int i = 0; i < 64; i++) host_pin_level[i] = HIGH;
  for (int i = 0; i < 64; i++) host_pwm[i] = 0;
  host_clear_serial_rx();
  Serial.clearCapture();
  host_set_millis(0);
  netState.mode  = "";
  netState.ip    = "";
  netState.ssid  = "";
  streamInfo.url = "";
  proto.begin();
  Serial.clearCapture();   // drop the hello line
  host_set_millis(0);
}

static bool capturedHasLine(const char* expected) {
  const std::string& cap = Serial.captured();
  const std::string want = std::string(expected) + "\n";
  return cap.find(want) != std::string::npos;
}
static bool capturedContains(const char* needle) {
  return Serial.captured().find(needle) != std::string::npos;
}

// ---------------------------------------------------------------------------
// S1 — before any set_stream, the state line is unchanged (no stream_url)
// ---------------------------------------------------------------------------
TEST(stream_state_absent_before_set) {
  resetStream();
  proto.injectLine("{\"cmd\":\"drive\",\"speed\":50,\"steer\":0}");
  CHECK(capturedHasLine(
    "{\"type\":\"state\",\"seq\":1,\"battery\":7.42,"
    "\"speed\":50,\"steer\":0,\"pan\":90,\"tilt\":90,"
    "\"net_mode\":\"sta\",\"net_ip\":\"0.0.0.0\"}"));
  CHECK(!capturedContains("\"stream_url\""));
  CHECK(streamInfo.url.length() == 0);
}

// ---------------------------------------------------------------------------
// S2 — set_stream accepted -> ack + state carries the stream_url field
// ---------------------------------------------------------------------------
TEST(stream_set_then_state_has_url) {
  resetStream();
  proto.injectLine("{\"cmd\":\"set_stream\",\"ip\":\"192.168.1.42\",\"port\":8889,\"path\":\"/cam/\"}");
  CHECK(proto.seq() == 1);
  CHECK(capturedHasLine("{\"type\":\"ack\",\"seq\":1}"));
  CHECK(streamInfo.url.length() > 0);
  CHECK(streamInfo.url == "http://192.168.1.42:8889/cam/");
  CHECK(capturedHasLine(
    "{\"type\":\"state\",\"seq\":1,\"battery\":7.42,"
    "\"speed\":0,\"steer\":0,\"pan\":90,\"tilt\":90,"
    "\"net_mode\":\"sta\",\"net_ip\":\"0.0.0.0\","
    "\"stream_url\":\"http://192.168.1.42:8889/cam/\"}"));
}

// ---------------------------------------------------------------------------
// S3 — a subsequent command's state line also carries the stream_url
// ---------------------------------------------------------------------------
TEST(stream_url_persists_across_commands) {
  resetStream();
  proto.injectLine("{\"cmd\":\"set_stream\",\"ip\":\"10.0.0.5\",\"port\":8889,\"path\":\"/cam/\"}");
  proto.injectLine("{\"cmd\":\"drive\",\"speed\":30,\"steer\":0}");
  CHECK(proto.seq() == 2);
  CHECK(capturedHasLine(
    "{\"type\":\"state\",\"seq\":2,\"battery\":7.42,"
    "\"speed\":30,\"steer\":0,\"pan\":90,\"tilt\":90,"
    "\"net_mode\":\"sta\",\"net_ip\":\"0.0.0.0\","
    "\"stream_url\":\"http://10.0.0.5:8889/cam/\"}"));
}

// ---------------------------------------------------------------------------
// S4 — the 1 Hz periodic broadcast also carries stream_url once set
// ---------------------------------------------------------------------------
TEST(stream_url_in_periodic_broadcast) {
  resetStream();
  proto.injectLine("{\"cmd\":\"set_stream\",\"ip\":\"192.168.1.42\",\"port\":8889,\"path\":\"/cam/\"}");
  Serial.clearCapture();
  host_set_millis(1000);
  proto.loop();   // t=1000 -> periodic broadcast
  CHECK(capturedContains("\"stream_url\":\"http://192.168.1.42:8889/cam/\""));
}

// ---------------------------------------------------------------------------
// S5 — set_stream updates the URL (re-push on IP change)
// ---------------------------------------------------------------------------
TEST(stream_set_updates_url) {
  resetStream();
  proto.injectLine("{\"cmd\":\"set_stream\",\"ip\":\"192.168.1.42\",\"port\":8889,\"path\":\"/cam/\"}");
  CHECK(streamInfo.url == "http://192.168.1.42:8889/cam/");
  proto.injectLine("{\"cmd\":\"set_stream\",\"ip\":\"192.168.1.99\",\"port\":9999,\"path\":\"/cam/\"}");
  CHECK(streamInfo.url == "http://192.168.1.99:9999/cam/");
}

// ---------------------------------------------------------------------------
// S6 — missing ip -> error + no state change
// ---------------------------------------------------------------------------
TEST(stream_missing_ip_is_error) {
  resetStream();
  proto.injectLine("{\"cmd\":\"set_stream\",\"port\":8889,\"path\":\"/cam/\"}");
  CHECK(capturedContains("\"code\":\"missing\""));
  CHECK(capturedContains("\"field\":\"ip\""));
  CHECK(streamInfo.url.length() == 0);
  CHECK(proto.seq() == 0);   // no ack, no seq increment
}

// ---------------------------------------------------------------------------
// S7 — bad ip (not a dotted quad) -> error + no state change
// ---------------------------------------------------------------------------
TEST(stream_bad_ip_is_error) {
  resetStream();
  proto.injectLine("{\"cmd\":\"set_stream\",\"ip\":\"not.an.ip.here\",\"port\":8889,\"path\":\"/cam/\"}");
  CHECK(capturedContains("\"code\":\"range\""));
  CHECK(capturedContains("\"field\":\"ip\""));
  CHECK(streamInfo.url.length() == 0);

  resetStream();
  proto.injectLine("{\"cmd\":\"set_stream\",\"ip\":\"1.2.3.4.5\",\"port\":8889,\"path\":\"/cam/\"}");
  CHECK(capturedContains("\"field\":\"ip\""));
  CHECK(streamInfo.url.length() == 0);
}

// ---------------------------------------------------------------------------
// S8 — out-of-range port -> error + no state change
// ---------------------------------------------------------------------------
TEST(stream_bad_port_is_error) {
  resetStream();
  proto.injectLine("{\"cmd\":\"set_stream\",\"ip\":\"192.168.1.42\",\"port\":0,\"path\":\"/cam/\"}");
  CHECK(capturedContains("\"code\":\"range\""));
  CHECK(capturedContains("\"field\":\"port\""));
  CHECK(streamInfo.url.length() == 0);

  resetStream();
  proto.injectLine("{\"cmd\":\"set_stream\",\"ip\":\"192.168.1.42\",\"port\":70000,\"path\":\"/cam/\"}");
  CHECK(capturedContains("\"field\":\"port\""));
  CHECK(streamInfo.url.length() == 0);
}

// ---------------------------------------------------------------------------
// S9 — bad path (no leading slash) -> error + no state change
// ---------------------------------------------------------------------------
TEST(stream_bad_path_is_error) {
  resetStream();
  proto.injectLine("{\"cmd\":\"set_stream\",\"ip\":\"192.168.1.42\",\"port\":8889,\"path\":\"cam\"}");
  CHECK(capturedContains("\"code\":\"range\""));
  CHECK(capturedContains("\"field\":\"path\""));
  CHECK(streamInfo.url.length() == 0);
}

// ---------------------------------------------------------------------------
// S10 — the state line with a realistic stream_url is valid JSON (not
//       truncated). A full URL like http://192.168.178.134:8889/cam/ makes
//       the line 171 bytes — the old buf[160] would have cut it off.
// ---------------------------------------------------------------------------
TEST(stream_line_with_realistic_url_is_valid) {
  resetStream();
  proto.injectLine("{\"cmd\":\"set_stream\",\"ip\":\"192.168.178.134\",\"port\":8889,\"path\":\"/cam/\"}");
  const std::string& cap = Serial.captured();
  size_t pos = cap.find("\"stream_url\":\"http://192.168.178.134:8889/cam/\"}");
  CHECK(pos != std::string::npos);
  // The line must end with the closing } right after the URL (no truncation).
  if (pos != std::string::npos) {
    size_t end = cap.find('\n', pos);
    std::string line = cap.substr(cap.rfind('{', pos), end - cap.rfind('{', pos));
    CHECK(line[line.size() - 1] == '}');
    CHECK(line.find("\"stream_url\"") != std::string::npos);
  }
}
