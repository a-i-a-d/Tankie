// Host unit tests for the set_wifi / get_wifi / reboot serial commands
// (tankie/serialproto.cpp) - issue #56.
//
// The Pi (or a Pi-only agent) pushes the WiFi config over the serial NDJSON
// link; the ESP stores it in the reserved EEPROM sector (wificfg.h, issue
// #54 - the SAME path the config portal uses) and reboots to apply. These
// tests drive the REAL SerialProto against the REAL wificfg codec + the
// EEPROM shim (tests/shims/EEPROM.h, a 4 KB 0xFF-backed sector image) and
// assert:
//
//   - set_wifi (save) -> ack + the config actually landed in the EEPROM
//     sector (verified by reading it back with wifiCfgLoad) + reboot pending
//   - set_wifi (reset) -> the sector is wiped (wifiCfgLoad fails) + reboot
//   - malformed input (missing ssid, oversized ssid/pass, bad ip/gateway)
//     -> an error line + NO save + no reboot
//   - a full-cap (64-char ssid + 64-char pass) line is NOT truncated
//   - get_wifi -> returns ssid/mode/ip (no password), no reboot pending
//   - the reboot command -> ack + reboot pending
//   - the line reader still accepts a ~217-byte full-cap set_wifi line
//     (lineBuf_ was grown 128 -> 256, issue #56 Q4)
//
// On the host the ESP.restart() shim is a no-op, so the pending-reboot path
// never actually restarts the test process.
#include "test_main.h"
#include "config.h"
#include "config_serial.h"
#include "SparkFun_TB6612.h"
#include "tankdrive.h"
#include "serialproto.h"
#include "wificfg.h"
#include "netstate.h"
#include <EEPROM.h>

#include <cstdio>
#include <cstring>
#include <string>

// The two motors, exactly as tankie.ino wires them (shared STBY).
static Motor mLeft(AIN1, AIN2, PWMA, STBY);
static Motor mRight(BIN1, BIN2, PWMB, STBY);
static TankDrive tank(&mLeft, &mRight);
static Servo panServo;
static Servo tiltServo;

static float fakeBattery(float R1, float R2) { (void)R1; (void)R2; return 7.42f; }

static SerialProto proto(&tank, &panServo, &tiltServo, fakeBattery, 330000, 33000);

// Reset to a "fresh boot" state: blank EEPROM sector, clear capture, drop
// the hello line, and make sure no reboot is pending.
static void resetWifi() {
  host_eeprom_reset();
  netState.mode  = "";
  netState.ip    = "";
  netState.ssid  = "";
  panServo.reset();
  tiltServo.reset();
  for (int i = 0; i < 64; i++) host_pin_level[i] = HIGH;
  for (int i = 0; i < 64; i++) host_pwm[i] = 0;
  host_clear_serial_rx();
  Serial.clearCapture();
  host_set_millis(0);
  proto.begin();
  Serial.clearCapture();   // drop the hello line
  host_set_millis(0);
}

static bool capturedHasLine(const char* expected) {
  const std::string& cap = Serial.captured();
  return cap.find(std::string(expected) + "\n") != std::string::npos;
}
static bool capturedContains(const char* needle) {
  return Serial.captured().find(needle) != std::string::npos;
}

// ---------------------------------------------------------------------------
// T1 — set_wifi (save): ack + config landed in the EEPROM sector + reboot
// ---------------------------------------------------------------------------
TEST(set_wifi_save_lands_in_eeprom_and_reboots) {
  resetWifi();
  proto.injectLine("{\"cmd\":\"set_wifi\",\"ssid\":\"tankie-lan\",\"pass\":\"hunter2\","
                   "\"ip\":\"192.168.178.126\",\"gateway\":\"192.168.178.1\"}");
  CHECK(capturedHasLine("{\"type\":\"ack\",\"seq\":1}"));
  CHECK(proto.rebootPending());

  // The config must actually be in the EEPROM sector (the single source of
  // truth), readable back via the codec.
  WifiCfg out;
  CHECK(wifiCfgLoad(&out));
  CHECK(out.ssid == "tankie-lan");
  CHECK(out.pass == "hunter2");
  CHECK(out.ip == "192.168.178.126");
  CHECK(out.gateway == "192.168.178.1");
}

TEST(set_wifi_dhcp_no_gateway) {
  resetWifi();
  proto.injectLine("{\"cmd\":\"set_wifi\",\"ssid\":\"tankie-lan\",\"pass\":\"hunter2\","
                   "\"ip\":\"dhcp\"}");
  CHECK(capturedHasLine("{\"type\":\"ack\",\"seq\":1}"));
  CHECK(proto.rebootPending());
  WifiCfg out;
  CHECK(wifiCfgLoad(&out));
  CHECK(out.ssid == "tankie-lan");
  CHECK(out.pass == "hunter2");
  CHECK(out.ip == "dhcp");
  CHECK(out.gateway.empty());
}

TEST(set_wifi_empty_ip_is_dhcp) {
  resetWifi();
  proto.injectLine("{\"cmd\":\"set_wifi\",\"ssid\":\"tankie-lan\",\"pass\":\"hunter2\"}");
  CHECK(proto.rebootPending());
  WifiCfg out;
  CHECK(wifiCfgLoad(&out));
  CHECK(out.ssid == "tankie-lan");
  CHECK(out.ip.empty());
}

// ---------------------------------------------------------------------------
// T2 — set_wifi (reset): sector wiped + reboot
// ---------------------------------------------------------------------------
TEST(set_wifi_reset_wipes_sector_and_reboots) {
  resetWifi();
  // Seed a config first, then reset it.
  proto.injectLine("{\"cmd\":\"set_wifi\",\"ssid\":\"tankie-lan\",\"pass\":\"hunter2\"}");
  WifiCfg out;
  CHECK(wifiCfgLoad(&out));          // present
  CHECK(out.ssid == "tankie-lan");

  proto.injectLine("{\"cmd\":\"set_wifi\",\"reset\":true}");
  CHECK(proto.rebootPending());
  WifiCfg out2;
  CHECK(!wifiCfgLoad(&out2));        // wiped -> blank again
}

TEST(set_wifi_reset_on_blank_sector_still_acks) {
  resetWifi();
  proto.injectLine("{\"cmd\":\"set_wifi\",\"reset\":true}");
  CHECK(capturedHasLine("{\"type\":\"ack\",\"seq\":1}"));
  CHECK(proto.rebootPending());
}

// ---------------------------------------------------------------------------
// T3 — malformed input: error + NO save + no reboot
// ---------------------------------------------------------------------------
TEST(set_wifi_missing_ssid_is_error) {
  resetWifi();
  proto.injectLine("{\"cmd\":\"set_wifi\",\"pass\":\"hunter2\"}");
  CHECK(capturedContains("\"code\":\"missing\""));
  CHECK(capturedContains("\"field\":\"ssid\""));
  CHECK(!proto.rebootPending());
  WifiCfg out;
  CHECK(!wifiCfgLoad(&out));         // nothing saved
}

TEST(set_wifi_empty_ssid_is_error) {
  resetWifi();
  proto.injectLine("{\"cmd\":\"set_wifi\",\"ssid\":\"\",\"pass\":\"hunter2\"}");
  CHECK(capturedContains("\"code\":\"missing\""));
  CHECK(capturedContains("\"field\":\"ssid\""));
  CHECK(!proto.rebootPending());
}

TEST(set_wifi_oversized_ssid_is_error) {
  resetWifi();
  // 65 chars: one over WIFICFG_MAX_SSID (64).
  char ssid65[WIFICFG_MAX_SSID + 2];
  memset(ssid65, 'a', WIFICFG_MAX_SSID + 1); ssid65[WIFICFG_MAX_SSID + 1] = '\0';
  char line[256];
  std::snprintf(line, sizeof(line), "{\"cmd\":\"set_wifi\",\"ssid\":\"%s\"}", ssid65);
  proto.injectLine(line);
  CHECK(capturedContains("\"code\":\"range\""));
  CHECK(capturedContains("\"field\":\"ssid\""));
  CHECK(!proto.rebootPending());
  WifiCfg out;
  CHECK(!wifiCfgLoad(&out));
}

TEST(set_wifi_oversized_pass_is_error) {
  resetWifi();
  char pass65[WIFICFG_MAX_PASS + 2];
  memset(pass65, 'b', WIFICFG_MAX_PASS + 1); pass65[WIFICFG_MAX_PASS + 1] = '\0';
  char line[256];
  std::snprintf(line, sizeof(line), "{\"cmd\":\"set_wifi\",\"ssid\":\"tankie\",\"pass\":\"%s\"}", pass65);
  proto.injectLine(line);
  CHECK(capturedContains("\"code\":\"range\""));
  CHECK(capturedContains("\"field\":\"pass\""));
  CHECK(!proto.rebootPending());
  WifiCfg out;
  CHECK(!wifiCfgLoad(&out));
}

TEST(set_wifi_bad_ip_is_error) {
  resetWifi();
  proto.injectLine("{\"cmd\":\"set_wifi\",\"ssid\":\"tankie-lan\",\"pass\":\"hunter2\",\"ip\":\"999.1.1.1\"}");
  CHECK(capturedContains("\"code\":\"range\""));
  CHECK(capturedContains("\"field\":\"ip\""));
  CHECK(!proto.rebootPending());
  WifiCfg out;
  CHECK(!wifiCfgLoad(&out));
}

TEST(set_wifi_bad_gateway_is_error) {
  resetWifi();
  proto.injectLine("{\"cmd\":\"set_wifi\",\"ssid\":\"tankie-lan\",\"pass\":\"hunter2\","
                   "\"ip\":\"192.168.1.10\",\"gateway\":\"not-an-ip\"}");
  CHECK(capturedContains("\"code\":\"range\""));
  CHECK(capturedContains("\"field\":\"gateway\""));
  CHECK(!proto.rebootPending());
  WifiCfg out;
  CHECK(!wifiCfgLoad(&out));
}

// ---------------------------------------------------------------------------
// T4 — full-cap line is NOT truncated (lineBuf_ 128 -> 256, issue #56 Q4)
// ---------------------------------------------------------------------------
TEST(set_wifi_full_cap_line_is_not_truncated) {
  resetWifi();
  char ssid64[WIFICFG_MAX_SSID + 1];
  char pass64[WIFICFG_MAX_PASS + 1];
  memset(ssid64, 'a', WIFICFG_MAX_SSID); ssid64[WIFICFG_MAX_SSID] = '\0';
  memset(pass64, 'b', WIFICFG_MAX_PASS); pass64[WIFICFG_MAX_PASS] = '\0';

  char line[320];
  std::snprintf(line, sizeof(line),
                "{\"cmd\":\"set_wifi\",\"ssid\":\"%s\",\"pass\":\"%s\"}", ssid64, pass64);
  // The line is ~217 bytes - it would have been truncated by the old 128-byte
  // buffer. It must be accepted and stored in full.
  CHECK(strlen(line) > 128);
  proto.injectLine(line);
  CHECK(proto.rebootPending());
  WifiCfg out;
  CHECK(wifiCfgLoad(&out));
  CHECK(out.ssid.length() == 64);
  CHECK(out.pass.length() == 64);
}

// A full-cap line fed byte-by-byte through the REAL serial reader must also
// arrive intact (exercises readSerial() against the grown lineBuf_).
TEST(set_wifi_full_cap_line_via_serial_reader) {
  resetWifi();
  char ssid64[WIFICFG_MAX_SSID + 1];
  char pass64[WIFICFG_MAX_PASS + 1];
  memset(ssid64, 'c', WIFICFG_MAX_SSID); ssid64[WIFICFG_MAX_SSID] = '\0';
  memset(pass64, 'd', WIFICFG_MAX_PASS); pass64[WIFICFG_MAX_PASS] = '\0';

  char line[320];
  std::snprintf(line, sizeof(line),
                "{\"cmd\":\"set_wifi\",\"ssid\":\"%s\",\"pass\":\"%s\"}\n", ssid64, pass64);
  host_feed_serial(line);
  proto.loop();
  CHECK(proto.rebootPending());
  WifiCfg out;
  CHECK(wifiCfgLoad(&out));
  CHECK(out.ssid.length() == 64);
  CHECK(out.pass.length() == 64);
}

// ---------------------------------------------------------------------------
// T5 — get_wifi: returns ssid/mode/ip (no password), no reboot pending
// ---------------------------------------------------------------------------
TEST(get_wifi_returns_ssid_mode_ip) {
  resetWifi();
  netState.mode  = "sta";
  netState.ip    = "192.168.178.126";
  netState.ssid  = "tankie-lan";
  proto.injectLine("{\"cmd\":\"get_wifi\"}");
  CHECK(capturedHasLine(
    "{\"type\":\"wifi\",\"ssid\":\"tankie-lan\",\"mode\":\"sta\",\"ip\":\"192.168.178.126\"}"));
  CHECK(!proto.rebootPending());
  // The response must NOT echo the password.
  CHECK(!capturedContains("hunter2"));
}

TEST(get_wifi_defaults_when_no_network) {
  resetWifi();   // netState blank
  proto.injectLine("{\"cmd\":\"get_wifi\"}");
  CHECK(capturedHasLine(
    "{\"type\":\"wifi\",\"ssid\":\"\",\"mode\":\"sta\",\"ip\":\"0.0.0.0\"}"));
  CHECK(!proto.rebootPending());
}

// ---------------------------------------------------------------------------
// T6 — reboot command: ack + reboot pending
// ---------------------------------------------------------------------------
TEST(reboot_command_sets_pending) {
  resetWifi();
  CHECK(!proto.rebootPending());
  proto.injectLine("{\"cmd\":\"reboot\"}");
  CHECK(capturedHasLine("{\"type\":\"ack\",\"seq\":1}"));
  CHECK(proto.rebootPending());
}

// A fresh begin() clears a stale reboot-pending flag (so a reboot that was
// "pending" but the test process kept running does not leak into the next
// test's state).
TEST(begin_clears_reboot_pending) {
  resetWifi();
  proto.injectLine("{\"cmd\":\"reboot\"}");
  CHECK(proto.rebootPending());
  proto.begin();
  CHECK(!proto.rebootPending());
}
