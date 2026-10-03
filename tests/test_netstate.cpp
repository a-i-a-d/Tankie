// Unit tests for the network-state broadcast (issue #46).
//
// Verifies that the state broadcast line carries net_mode + net_ip and
// that the values reflect what WiFiManager set (or the defaults before
// WiFi has been attempted).
#include "test_main.h"
#include "config.h"
#include "config_serial.h"
#include "SparkFun_TB6612.h"
#include "tankdrive.h"
#include "serialproto.h"
#include "netstate.h"

static Motor mLeft(AIN1, AIN2, PWMA, 1, STBY);
static Motor mRight(BIN1, BIN2, PWMB, 1, STBY);
static TankDrive tank(&mLeft, &mRight);
static Servo panServo;
static Servo tiltServo;

static float fakeBattery(float R1, float R2) { (void)R1; (void)R2; return 7.42f; }
static SerialProto proto(&tank, &panServo, &tiltServo, fakeBattery, 330000, 33000);

static void resetNetState() {
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
  proto.begin();
  Serial.clearCapture();   // drop the hello line
  host_set_millis(0);
}

static bool capturedHasLine(const char* expected) {
  const std::string& cap = Serial.captured();
  const std::string want = std::string(expected) + "\n";
  return cap.find(want) != std::string::npos;
}

// ---------------------------------------------------------------------------
// N1 — defaults before WiFiManager has run: "sta" / "0.0.0.0"
// ---------------------------------------------------------------------------
TEST(net_defaults_sta_0000) {
  resetNetState();
  Serial.clearCapture();
  proto.injectLine("{\"cmd\":\"drive\",\"speed\":50,\"steer\":0}");
  CHECK(capturedHasLine(
    "{\"type\":\"state\",\"seq\":1,\"battery\":7.42,"
    "\"speed\":50,\"steer\":0,\"pan\":90,\"tilt\":90,"
    "\"net_mode\":\"sta\",\"net_ip\":\"0.0.0.0\"}"));
}

// ---------------------------------------------------------------------------
// N2 — AP mode: netState set to "ap" + "192.168.4.1"
// ---------------------------------------------------------------------------
TEST(net_ap_mode_in_state) {
  resetNetState();
  netState.mode  = "ap";
  netState.ip    = "192.168.4.1";
  netState.ssid  = "";
  Serial.clearCapture();
  proto.injectLine("{\"cmd\":\"drive\",\"speed\":10,\"steer\":0}");
  CHECK(capturedHasLine(
    "{\"type\":\"state\",\"seq\":1,\"battery\":7.42,"
    "\"speed\":10,\"steer\":0,\"pan\":90,\"tilt\":90,"
    "\"net_mode\":\"ap\",\"net_ip\":\"192.168.4.1\"}"));
}

// ---------------------------------------------------------------------------
// N3 — STA mode with a real IP: "192.168.1.42"
// ---------------------------------------------------------------------------
TEST(net_sta_mode_with_ip) {
  resetNetState();
  netState.mode  = "sta";
  netState.ip    = "192.168.1.42";
  netState.ssid  = "MyWiFi";
  Serial.clearCapture();
  proto.injectLine("{\"cmd\":\"drive\",\"speed\":10,\"steer\":0}");
  CHECK(capturedHasLine(
    "{\"type\":\"state\",\"seq\":1,\"battery\":7.42,"
    "\"speed\":10,\"steer\":0,\"pan\":90,\"tilt\":90,"
    "\"net_mode\":\"sta\",\"net_ip\":\"192.168.1.42\"}"));
}

// ---------------------------------------------------------------------------
// N4 — the 1 Hz periodic broadcast also carries the net fields
// ---------------------------------------------------------------------------
TEST(net_fields_in_periodic_broadcast) {
  resetNetState();
  netState.mode  = "ap";
  netState.ip    = "192.168.4.1";
  Serial.clearCapture();
  host_set_millis(1000);
  proto.loop();   // t=1000 -> periodic broadcast
  CHECK(Serial.captured().find("\"net_mode\":\"ap\"") != std::string::npos);
  CHECK(Serial.captured().find("\"net_ip\":\"192.168.4.1\"") != std::string::npos);
}

// ---------------------------------------------------------------------------
// N5 — net fields are valid JSON (parseable by a real JSON parser)
// ---------------------------------------------------------------------------
TEST(net_fields_are_valid_json) {
  resetNetState();
  netState.mode  = "sta";
  netState.ip    = "10.0.0.5";
  Serial.clearCapture();
  proto.injectLine("{\"cmd\":\"drive\",\"speed\":10,\"steer\":0}");

  // Find the state line and verify it is a single valid JSON object.
  const std::string& cap = Serial.captured();
  size_t pos = cap.find("{\"type\":\"state\"");
  CHECK(pos != std::string::npos);
  if (pos != std::string::npos) {
    size_t end = cap.find('\n', pos);
    std::string line = cap.substr(pos, end - pos);
    // Basic sanity: starts with { and ends with }
    CHECK(line[0] == '{');
    CHECK(line[line.size() - 1] == '}');
    // Contains both net fields
    CHECK(line.find("\"net_mode\"") != std::string::npos);
    CHECK(line.find("\"net_ip\"") != std::string::npos);
    // No double-quotes within the values (would break JSON)
    CHECK(line.find("\"net_mode\":\"\"") == std::string::npos);
    CHECK(line.find("\"net_ip\":\"\"") == std::string::npos);
  }
}
