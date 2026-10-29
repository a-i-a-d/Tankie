// Unit tests for the /ota/powersave handler (tankie.ino) - issue #133.
//
// The data-partition OTA fails in STA mode (random mid-upload reset, then
// a stuck core Updater) because the ESP8266 WiFi stack runs with modem-sleep
// ON by default: the radio's periodic beacon-poll to the external AP
// collides with the sustained ~2 MB flash erase+write and trips the
// interrupt watchdog. In AP mode (no external beacon-poll) it never fails.
//
// The fix is a tiny route on the OTA server:
//   GET /ota/powersave?on=false  -> WiFi.setSleep(false)  (disable modem-sleep)
//   GET /ota/powersave?on=true   -> WiFi.setSleep(true)   (restore it)
//
// These tests drive the REAL otaPowersaveHandler() from tankie.ino
// (compiled for the host against the shims in tests/shims/) and assert on:
//   - on=false  -> modem-sleep disabled (host_otaSleepDisabled) + HTTP 200
//   - on=true   -> modem-sleep restored + HTTP 200
//   - missing ?on=  -> HTTP 400 + state unchanged
//   - bad ?on= value -> HTTP 400 + state unchanged
//
// The shim's WiFi.setSleep() records its argument in host_otaSleepDisabled
// (tests/shims/ESP8266WiFi.h); the request's query param is injected via
// host_wsQueryParam (tests/shims/ESPAsyncWebServer.h).
#include "test_main.h"
#include <ESP8266WiFi.h>
#include <ESPAsyncWebServer.h>

// Defined in tankie.ino (compiled into the test binary via run_tests.sh).
extern void otaPowersaveHandler(AsyncWebServerRequest* request);

// Reset to a "fresh boot" state before each test: modem-sleep enabled
// (the core default), no query param injected, no recorded response.
static void resetOta() {
  host_otaSleepDisabled = false;
  host_wsQueryParam = "";
  host_lastSendCode = 0;
  host_lastSendBody = "";
}

// T1 — on=false: modem-sleep disabled + HTTP 200
TEST(ota_powersave_off_disables_sleep) {
  resetOta();
  host_wsQueryParam = "false";
  otaPowersaveHandler(new AsyncWebServerRequest());
  CHECK(host_otaSleepDisabled);
  CHECK_EQ_INT(host_lastSendCode, 200);
}

// T2 — on=true: modem-sleep restored + HTTP 200
TEST(ota_powersave_on_restores_sleep) {
  resetOta();
  host_otaSleepDisabled = true;   // start "disabled" (as after a T1 call)
  host_wsQueryParam = "true";
  otaPowersaveHandler(new AsyncWebServerRequest());
  CHECK(!host_otaSleepDisabled);
  CHECK_EQ_INT(host_lastSendCode, 200);
}

// T3 — missing ?on=: HTTP 400 + state unchanged
TEST(ota_powersave_missing_param_400) {
  resetOta();
  host_wsQueryParam = "";        // no query param
  otaPowersaveHandler(new AsyncWebServerRequest());
  CHECK(!host_otaSleepDisabled);
  CHECK_EQ_INT(host_lastSendCode, 400);
}

// T4 — bad ?on= value: HTTP 400 + state unchanged
TEST(ota_powersave_bad_param_400) {
  resetOta();
  host_wsQueryParam = "banana";
  otaPowersaveHandler(new AsyncWebServerRequest());
  CHECK(!host_otaSleepDisabled);
  CHECK_EQ_INT(host_lastSendCode, 400);
}

// T5 — toggle round-trip: off -> on -> off tracks the core state
TEST(ota_powersave_toggle_roundtrip) {
  resetOta();
  host_wsQueryParam = "false";
  otaPowersaveHandler(new AsyncWebServerRequest());
  CHECK(host_otaSleepDisabled);
  host_wsQueryParam = "true";
  otaPowersaveHandler(new AsyncWebServerRequest());
  CHECK(!host_otaSleepDisabled);
  host_wsQueryParam = "false";
  otaPowersaveHandler(new AsyncWebServerRequest());
  CHECK(host_otaSleepDisabled);
  // restore for any later tests
  host_wsQueryParam = "true";
  otaPowersaveHandler(new AsyncWebServerRequest());
  CHECK(!host_otaSleepDisabled);
}
