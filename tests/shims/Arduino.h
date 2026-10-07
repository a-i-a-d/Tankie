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
#include <cstdint>
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
extern std::string host_serial_rx;   // Serial receive buffer (issue #29)
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
  void clear() { s_.clear(); }

  size_t length() const { return s_.size(); }
  char charAt(size_t i) const { return (i < s_.size()) ? s_[i] : '\0'; }
  const char* c_str() const { return s_.c_str(); }

  String& operator+=(char c) { s_ += c; return *this; }
  String& operator+=(const char* s) { if (s) s_ += s; return *this; }
  String& operator+=(const String& s) { s_ += s.s_; return *this; }

  String operator+(const String& o) const { return String((s_ + o.s_).c_str()); }
  String operator+(const char* o) const { return String((s_ + (o ? o : "")).c_str()); }
  String operator+(char o) const { std::string r = s_; r += o; return String(r.c_str()); }

  void trim() {
    while (!s_.empty() && (s_.front() == ' ' || s_.front() == '\t' ||
                            s_.front() == '\r' || s_.front() == '\n'))
      s_.erase(0, 1);
    while (!s_.empty() && (s_.back() == ' ' || s_.back() == '\t' ||
                            s_.back() == '\r' || s_.back() == '\n'))
      s_.pop_back();
  }

  bool equals(const char* o) const { return s_ == (o ? o : ""); }
  bool operator==(const char* o) const { return s_ == (o ? o : ""); }
  bool operator==(const String& o) const { return s_ == o.s_; }
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
// IPAddress: the subset of the esp8266 core's IPAddress API the firmware
// uses (wifimanager.cpp config logging + the WiFi shim). Defined here so
// the SerialClass below can offer print/println overloads for it.
// ---------------------------------------------------------------------------
class IPAddress {
 public:
  IPAddress() : o_{0, 0, 0, 0} {}
  IPAddress(uint8_t a, uint8_t b, uint8_t c, uint8_t d) : o_{a, b, c, d} {}
  bool fromString(const char* s) { (void)s; return false; }
  bool operator==(const IPAddress& o) const {
    return o_[0] == o.o_[0] && o_[1] == o.o_[1] &&
           o_[2] == o.o_[2] && o_[3] == o.o_[3];
  }
  uint8_t& operator[](int i) { return o_[i]; }
  uint8_t operator[](int i) const { return o_[i]; }
  String toString() const {
    char b[24];
    std::snprintf(b, sizeof b, "%u.%u.%u.%u", o_[0], o_[1], o_[2], o_[3]);
    return String(b);
  }
 private:
  uint8_t o_[4];
};

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
  // wifimanager.cpp logs the AP IP this way.
  size_t print(const IPAddress& ip) { return write(ip.toString().c_str()); }
  size_t println(const IPAddress& ip) { return print(ip) + write("\n"); }

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
  // Receive-side (issue #29): available()/read() from host_serial_rx.
 public:
  int available() { return (int)host_serial_rx.size(); }
  int read() {
    if (host_serial_rx.empty()) return -1;
    int c = (unsigned char)host_serial_rx[0];
    host_serial_rx.erase(0, 1);
    return c;
  }
 private:
};

extern SerialClass Serial;

// ---------------------------------------------------------------------------
// Servo: the subset of the esp8266 core's Servo API the firmware uses
// (attach/write/read). Backed by a per-instance "last written angle" table
// so tests can assert the servo actually moved (issue #29 pan/tilt).
// ---------------------------------------------------------------------------
class Servo {
 public:
  Servo() : pin_(-1), value_(0) {}
  int attach(int pin) { pin_ = pin; return pin_ >= 0 ? 1 : 0; }
  void write(int value) { value_ = value; }
  int read() const { return value_; }
  bool attached() const { return pin_ >= 0; }
  void reset() { pin_ = -1; value_ = 0; }
  int pin() const { return pin_; }

 private:
  int pin_;
  int value_;
};

// ---------------------------------------------------------------------------
// Serial receive: the SerialClass shim gains a small receive buffer +
// available()/read() so SerialProto::readSerial() can be exercised. Tests
// feed bytes with host_feed_serial() (defined below).
// ---------------------------------------------------------------------------
inline void host_feed_serial(const std::string& s) { host_serial_rx += s; }
inline void host_clear_serial_rx() { host_serial_rx.clear(); }
