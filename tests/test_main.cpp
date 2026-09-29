// Host test harness for the Tankie firmware modules (issue #5).
//
// Provides:
//   - the state behind the shims: host_now_ms (millis()), host_pin_level[]
//     (digitalRead/Write), host_pwm[] (analogWrite), host_adc[] (analogRead)
//     and the Serial object
//   - the assertion-framework implementation (see test_main.h)
//   - main(): runs every registered test, prints a summary, non-zero exit
//     on any failure
#include "test_main.h"
#include <Arduino.h>

#include <cstdio>
#include <utility>
#include <vector>

// ---------------------------------------------------------------------------
// Shim state (declared extern in tests/shims/Arduino.h)
// ---------------------------------------------------------------------------
unsigned long host_now_ms = 0;
int host_pin_level[64];
int host_pwm[64];
int host_adc[64];
unsigned long host_adc_reads = 0;
std::string host_serial_rx;   // Serial receive buffer (issue #29)
SerialClass Serial;

// ---------------------------------------------------------------------------
// Assertion-framework implementation
// ---------------------------------------------------------------------------
static long g_checks_passed = 0;
static long g_checks_failed = 0;
static std::string g_current_test;

void check(bool cond, const char* what, const char* file, int line) {
  if (cond) {
    g_checks_passed++;
  } else {
    g_checks_failed++;
    std::printf("  FAIL [%s] %s\n        at %s:%d\n", g_current_test.c_str(),
                what, file, line);
  }
}

void check_eq_long(long a, long b, const char* what, const char* file, int line) {
  if (a == b) {
    g_checks_passed++;
  } else {
    g_checks_failed++;
    std::printf("  FAIL [%s] %s: %ld != %ld\n        at %s:%d\n",
                g_current_test.c_str(), what, a, b, file, line);
  }
}

void check_near(double a, double b, double eps, const char* what,
                const char* file, int line) {
  double diff = a - b;
  if (diff < 0) diff = -diff;
  if (diff <= eps) {
    g_checks_passed++;
  } else {
    g_checks_failed++;
    std::printf("  FAIL [%s] %s: %f !~= %f (diff %f > %f)\n        at %s:%d\n",
                g_current_test.c_str(), what, a, b, diff, eps, file, line);
  }
}

using TestFn = void (*)();
static std::vector<std::pair<std::string, TestFn>> g_tests;

void register_test(const char* name, TestFn fn) { g_tests.emplace_back(name, fn); }

// ---------------------------------------------------------------------------
// main: run every registered test, print a summary, exit non-zero on failure
// ---------------------------------------------------------------------------
int main() {
  // Pin table defaults to HIGH (released / pull-up), like the real hardware.
  for (int i = 0; i < 64; i++) host_pin_level[i] = HIGH;
  for (int i = 0; i < 64; i++) host_pwm[i] = 0;
  for (int i = 0; i < 64; i++) host_adc[i] = 0;

  std::printf("== Tankie host tests (%zu tests) ==\n", g_tests.size());
  for (const auto& t : g_tests) {
    g_current_test = t.first;
    long before_failed = g_checks_failed;
    t.second();
    std::printf("%s %s\n", (g_checks_failed == before_failed) ? "PASS" : "FAIL",
                t.first.c_str());
  }

  std::printf("== %ld checks passed, %ld failed ==\n", g_checks_passed,
              g_checks_failed);
  if (g_checks_failed) {
    std::printf("RESULT: FAIL\n");
    return 1;
  }
  std::printf("RESULT: OK\n");
  return 0;
}
