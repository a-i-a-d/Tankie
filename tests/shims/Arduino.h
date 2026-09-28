// Host-side Arduino API shim for the Tankie unit tests (issue #5).
//
// The firmware modules under test do `#include <Arduino.h>`; on the host
// build this file provides just enough of the Arduino API for them to
// compile and run under g++ (no ESP8266 toolchain needed):
//
//   - String   (small class over std::string)
//   - millis() (deterministic: tests drive it via host_set_millis())
//   - digitalRead() / digitalWrite() (backed by the host_pin_level[] table)
//   - analogRead() / analogWrite()   (backed by the host_adc[] / host_pwm[]
//                                      tables, so tests can assert on the
//                                      PWM output and feed the ADC input)
//   - Serial   (print/println -> stdout AND a capture buffer tests can assert on)
//   - F(x), HIGH, LOW, INPUT, OUTPUT, INPUT_PULLUP
//
// It is deliberately NOT a general Arduino emulation: only what the modules
// listed in tests/run_tests.sh actually use is provided.
//
// NOTE: the file must stay named `Arduino.h` with a capital A - the modules
// include it as <Arduino.h> and Linux is case-sensitive.
#pragma once

#include <cstdarg>
#include <cstddef>
#include <cstdio>
#include <cstring>
#include <string>

// ---------------------------------------------------------------------------
// Constants + string literal macro
// ---------------------------------------------------------------------------
#define HIGH 1
#define LOW  0
#define INPUT 0x0
#define OUTPUT 0x1
#define INPUT_PULLUP 0x2
#define F(x) x

// ---------------------------------------------------------------------------
// Pin names: the Wemos D1 Mini (ESP8266) GPIO mapping, exactly as the
// esp8266:esp8266:d1_mini core defines them. config.h refers to the motor
// pins by these names, so the tests assert on the REAL hardware pins.
// ---------------------------------------------------------------------------
#define D0 4
#define D1 5
#define D2 16
#define D3 0
#define D4 2
#define D5 14
#define D6 12
#define D7 13
#define D8 15
#define A0 16  // D1 Mini: A0 is GPIO16 (== D2)

// ---------------------------------------------------------------------------
// Deterministic time: millis() reads host_now_ms; tests advance it with
// host_set_millis(). (State defined in tests/test_main.cpp.)
// ---------------------------------------------------------------------------
extern unsigned long host_now_ms;
inline unsigned long millis() { return host_now_ms; }
inline void host_set_millis(unsigned long ms) { host_now_ms = ms; }

// delay(): the firmware uses it between the battery ADC samples. On the
// host it is a no-op (the samples are driven by the host_adc[] table).
inline void delay(unsigned long ms) { (void)ms; }

// ---------------------------------------------------------------------------
// Deterministic GPIO: digitalRead() / digitalWrite() read/write a pin table
// that defaults to HIGH (released / pull-up), like the real hardware.
// (Tables defined in tests/test_main.cpp.)
// ---------------------------------------------------------------------------
extern int host_pin_level[64];
inline int digitalRead(int pin) {
  return (pin >= 0 && pin < 64) ? host_pin_level[pin] : HIGH;
}
inline void digitalWrite(int pin, int level) {
  if (pin >= 0 && pin < 64) host_pin_level[pin] = level;
}
inline void pinMode(int pin, int mode) { (void)pin; (void)mode; }  // no-op on host

// ---------------------------------------------------------------------------
// Deterministic analog I/O: analogWrite() stores the PWM value per pin,
// analogRead() returns the value the test set with host_set_adc().
// (Tables defined in tests/test_main.cpp.)
// ---------------------------------------------------------------------------
extern int host_pwm[64];
extern int host_adc[64];
extern unsigned long host_adc_reads;   // analogRead() call counter (test hook)
inline void analogWrite(int pin, int value) {
  if (pin >= 0 && pin < 64) host_pwm[pin] = value;
}
inline int analogRead(int pin) {
  host_adc_reads++;
  return (pin >= 0 && pin < 64) ? host_adc[pin] : 0;
}
// Test hook: feed an ADC value (0..1023) to a pin.
inline void host_set_adc(int pin, int value) {
  if (pin >= 0 && pin < 64) host_adc[pin] = value;
}

// ---------------------------------------------------------------------------
// String: the subset of the Arduino String API the firmware uses.
// ---------------------------------------------------------------------------
class String {
 public:
  String() = default;
  String(const char* s) : s_(s ? s : "") {}
  String(int v) { s_ = std::to_string(v); }
  String(unsigned v) { s_ = std::to_string(v); }
  String(long v) { s_ = std::to_string(v); }
  String(unsigned long v) { s_ = std::to_string(v); }
  String(float v) { char b[32]; std::snprintf(b, sizeof b, "%g", v); s_ = b; }
  String(double v) { char b[32]; std::snprintf(b, sizeof b, "%g", v); s_ = b; }

  bool empty() const { return s_.empty(); }
  size_t length() const { return s_.size(); }
  char charAt(size_t i) const { return (i < s_.size()) ? s_[i] : '\0'; }
  const char* c_str() const { return s_.c_str(); }

  String& operator+=(char c) { s_ += c; return *this; }
  String& operator+=(const char* s) { if (s) s_ += s; return *this; }
  String& operator+=(const String& s) { s_ += s.s_; return *this; }

  String operator+(const String& o) const { return String((s_ + o.s_).c_str()); }
  String operator+(const char* o) const { return String((s_ + (o ? o : "")).c_str()); }
  String operator+(char o) const { std::string r = s_; r += o; return String(r.c_str()); }

  bool equals(const char* o) const { return s_ == (o ? o : ""); }
  bool operator==(const char* o) const { return s_ == (o ? o : ""); }
  bool operator!=(const char* o) const { return !(*this == o); }

 private:
  std::string s_;
};
// Free operator+ for (const char* + String): the battery broadcast builds
// String("{\n\"battery\":") + String(vin) + String("\n}").
inline String operator+(const char* a, const String& b) {
  return String((std::string(a ? a : "") + b.c_str()).c_str());
}

// ---------------------------------------------------------------------------
// Serial: print/println go to stdout (so a human running the tests sees the
// same trace as the firmware) AND into a capture buffer tests can assert on.
// (The Serial object is defined in tests/test_main.cpp.)
// ---------------------------------------------------------------------------
class SerialClass {
 public:
  void begin(unsigned long baud) { (void)baud; }
  void end() {}

  size_t print(const char* s) { return write(s ? s : ""); }
  size_t print(const String& s) { return write(s.c_str()); }
  size_t print(char c) { return write(std::string(1, c)); }
  size_t print(int v) { char b[24]; std::snprintf(b, sizeof b, "%d", v); return write(b); }
  size_t print(unsigned long v) { char b[24]; std::snprintf(b, sizeof b, "%lu", v); return write(b); }
  size_t print(float v) { char b[32]; std::snprintf(b, sizeof b, "%g", v); return write(b); }
  size_t print(double v) { char b[32]; std::snprintf(b, sizeof b, "%g", v); return write(b); }

  size_t println() { return write("\n"); }
  size_t println(const char* s) { return print(s) + write("\n"); }
  size_t println(const String& s) { return print(s) + write("\n"); }
  size_t println(int v) { return print(v) + write("\n"); }
  size_t println(unsigned long v) { char b[24]; std::snprintf(b, sizeof b, "%lu", v); return write(b) + write("\n"); }
  size_t println(float v) { char b[32]; std::snprintf(b, sizeof b, "%g", v); return write(b) + write("\n"); }
  size_t println(double v) { char b[32]; std::snprintf(b, sizeof b, "%g", v); return write(b) + write("\n"); }

  size_t printf(const char* fmt, ...) {
    char b[256];
    va_list ap;
    va_start(ap, fmt);
    int n = std::vsnprintf(b, sizeof b, fmt, ap);
    va_end(ap);
    if (n < 0) return 0;
    return write(n < (int)sizeof b ? b : "");
  }

  // Test hooks
  void clearCapture() { captured_.clear(); }
  const std::string& captured() const { return captured_; }

 private:
  size_t write(const std::string& s) {
    captured_ += s;
    std::fputs(s.c_str(), stdout);
    return s.size();
  }
  std::string captured_;
};

extern SerialClass Serial;
