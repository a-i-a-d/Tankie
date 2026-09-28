// Host test framework for the Tankie firmware modules (issue #5).
//
// A tiny, dependency-free assertion framework (same pattern as the
// TamAIgotchi host tests):
//
//   TEST(name)          - define + auto-register a test
//   CHECK(cond)         - assert a boolean
//   CHECK_EQ_INT(a, b)  - assert two integers are equal
//   CHECK_NEAR(a, b, e) - assert two floats are within epsilon of each other
//
// The implementation (the check functions, register_test, the pass/fail
// counters, and main()) lives in tests/test_main.cpp. Test files
// (test_tankdrive.cpp, test_motor.cpp, ...) only include this header and
// use the macros above - no external test library.
//
// Tests run in registration order (link order of the test translation
// units). main() returns non-zero if any check failed.
#pragma once

#include <cstddef>

// --- framework API (defined in test_main.cpp) ---
void register_test(const char* name, void (*fn)());
void check(bool cond, const char* what, const char* file, int line);
void check_eq_long(long a, long b, const char* what, const char* file,
                   int line);
void check_near(double a, double b, double eps, const char* what,
                const char* file, int line);

// --- macros used by the test files ---
#define CHECK(cond) check((cond), #cond, __FILE__, __LINE__)
#define CHECK_EQ_INT(a, b) check_eq_long((long)(a), (long)(b), #a " == " #b, __FILE__, __LINE__)
#define CHECK_NEAR(a, b, eps) check_near((double)(a), (double)(b), (double)(eps), #a " ~= " #b, __FILE__, __LINE__)

// Define a test function and register it at static-init time. The
// registration runs before main(), so the order of TEST() blocks across the
// linked test files is deterministic (link order).
#define TEST(name)                                                       \
  static void test_##name();                                             \
  static const bool reg_##name =                                         \
      (register_test(#name, test_##name), true);                         \
  static void test_##name()
