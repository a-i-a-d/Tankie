// Serial control protocol (NDJSON over UART0) — issue #29.
//
// Implementation of the SerialProto class declared in serialproto.h.
// All command validation + clamping happens here, so the ESP8266 is the
// single, hardened entry point for motor/servo control (issue #29 §3).
//
// Design notes:
//   * Non-blocking: loop() only reads whatever bytes are available on
//     Serial and accumulates them into lineBuf_. No delay() calls.
//   * JSON parsing is a small, self-contained field extractor (no
//     ArduinoJson dependency) — see jsonGetString/jsonGetInt below.
//   * All emission goes through emit() which appends a newline, so every
//     message is a complete NDJSON line.

#include "serialproto.h"

#include <string.h>
#include <stdio.h>

#include "batt.h"

#include <Servo.h>   // full Servo definition (ESP core / tests/shims)

// ---------------------------------------------------------------------------
// Construction / begin / loop
// ---------------------------------------------------------------------------

SerialProto::SerialProto(TankDrive* tank, Servo* panServo, Servo* tiltServo,
                         float (*getBattery)(float R1, float R2),
                         float R1, float R2)
{
  tank_       = tank;
  panServo_   = panServo;
  tiltServo_  = tiltServo;
  getBattery_ = getBattery;
  R1_         = R1;
  R2_         = R2;

  lineLen_ = 0;
  seq_ = 0;
  watchdogFired_ = false;
  lastDriveCmdMs_ = 0;
  lastStateBroadcastMs_ = 0;
  speed_ = 0;
  steer_ = 0;
  pan_ = 90;
  tilt_ = 90;
}

void SerialProto::begin() {
  lineLen_ = 0;
  seq_ = 0;
  watchdogFired_ = false;
  lastDriveCmdMs_ = millis();
  lastStateBroadcastMs_ = millis();
  speed_ = 0;
  steer_ = 0;
  pan_ = 90;
  tilt_ = 90;
  emitHello();
}

void SerialProto::loop() {
  readSerial();
  checkWatchdog();
  // ~1 Hz state broadcast (reuses the existing 1 s battery interval idea).
  unsigned long now = millis();
  if (now - lastStateBroadcastMs_ >= 1000UL) {
    lastStateBroadcastMs_ = now;
    emitState();
  }
}

// ---------------------------------------------------------------------------
// Line reader
// ---------------------------------------------------------------------------

void SerialProto::readSerial() {
  while (Serial.available() > 0) {
    int c = Serial.read();
    if (c < 0) break;
    char ch = (char)c;
    if (ch == '\n') {
      if (lineLen_ > 0) {
        lineBuf_[lineLen_] = '\0';
        // Trim a trailing '\r' (CRLF tolerance).
        size_t n = lineLen_;
        if (n > 0 && lineBuf_[n - 1] == '\r') {
          lineBuf_[n - 1] = '\0';
          n--;
        }
        handleLine(lineBuf_, n);
        lineLen_ = 0;
      }
    } else {
      // Ignore anything that would overflow the buffer (defensive).
      if (lineLen_ < sizeof(lineBuf_) - 1) {
        lineBuf_[lineLen_++] = ch;
      }
    }
  }
}

void SerialProto::injectLine(const char* line) {
  if (!line) return;
  size_t n = strlen(line);
  if (n > 0 && line[n - 1] == '\r') n--;
  if (n > 0 && n < sizeof(lineBuf_)) {
    memcpy(lineBuf_, line, n);
    lineBuf_[n] = '\0';
    handleLine(lineBuf_, n);
  }
}

// ---------------------------------------------------------------------------
// Command dispatch
// ---------------------------------------------------------------------------

void SerialProto::handleLine(const char* line, size_t len) {
  (void)len;
  // Only accept lines that look like a JSON object.
  if (line[0] != '{') return;

  char cmd[16];
  if (!jsonGetString(line, "cmd", cmd, sizeof(cmd))) return;

  if (strcmp(cmd, "drive") == 0) {
    int speed = 0, steer = 0;
    bool hasSpeed = jsonGetInt(line, "speed", &speed);
    bool hasSteer = jsonGetInt(line, "steer", &steer);
    if (!hasSpeed && !hasSteer) {
      emitError("missing", "speed");
      return;
    }
    handleDrive(speed, steer);
  } else if (strcmp(cmd, "pan") == 0) {
    int angle = 0;
    if (!jsonGetInt(line, "angle", &angle)) {
      emitError("missing", "angle");
      return;
    }
    handlePan(angle);
  } else if (strcmp(cmd, "tilt") == 0) {
    int angle = 0;
    if (!jsonGetInt(line, "angle", &angle)) {
      emitError("missing", "angle");
      return;
    }
    handleTilt(angle);
  } else if (strcmp(cmd, "stop") == 0) {
    handleStop();
  }
  // Unknown cmd -> silently ignore (non-JSON / foreign lines coexist).
}

void SerialProto::handleDrive(int speed, int steer) {
  // Validation + clamping (issue #29 §3): speed/steer in [-255, 255].
  if (speed < -255 || speed > 255) { emitError("range", "speed"); return; }
  if (steer < -255 || steer > 255) { emitError("range", "steer"); return; }

  tank_->setSpeed(speed);
  tank_->setSteer(steer);
  speed_ = speed;
  steer_ = steer;

  seq_++;
  lastDriveCmdMs_ = millis();
  if (watchdogFired_) watchdogFired_ = false;   // re-arm on next command
  emitAck();
  emitState();   // state changed -> broadcast immediately
}

void SerialProto::handlePan(int angle) {
  if (angle < 0 || angle > 180) { emitError("range", "pan"); return; }
  panServo_->write(angle);
  pan_ = angle;
  seq_++;
  emitAck();
  emitState();
}

void SerialProto::handleTilt(int angle) {
  if (angle < 0 || angle > 180) { emitError("range", "tilt"); return; }
  tiltServo_->write(angle);
  tilt_ = angle;
  seq_++;
  emitAck();
  emitState();
}

void SerialProto::handleStop() {
  tank_->setSpeed(0);
  tank_->setSteer(0);
  speed_ = 0;
  steer_ = 0;
  seq_++;
  lastDriveCmdMs_ = millis();
  if (watchdogFired_) watchdogFired_ = false;   // re-arm on next command
  emitAck();
  emitState();
}

// ---------------------------------------------------------------------------
// Watchdog
// ---------------------------------------------------------------------------

void SerialProto::checkWatchdog() {
  if (watchdogFired_) return;
  if (millis() - lastDriveCmdMs_ > SERIAL_WATCHDOG_MS) {
    tank_->setSpeed(0);
    tank_->setSteer(0);
    speed_ = 0;
    steer_ = 0;
    watchdogFired_ = true;
    emitWatchdog();
  }
}

// ---------------------------------------------------------------------------
// Emission (ESP → Pi)
// ---------------------------------------------------------------------------

void SerialProto::emit(const char* s) {
  Serial.print(s);
  Serial.print('\n');
}

void SerialProto::emitHello() {
  char buf[96];
  snprintf(buf, sizeof(buf),
                "{\"type\":\"hello\",\"proto\":%d,\"fw\":\"%s\"}",
                (int)SERIAL_PROTO_VERSION, TANKIE_FW_VERSION);
  emit(buf);
}

void SerialProto::emitAck() {
  char buf[48];
  snprintf(buf, sizeof(buf), "{\"type\":\"ack\",\"seq\":%lu}", seq_);
  emit(buf);
}

void SerialProto::emitError(const char* code, const char* field) {
  char buf[96];
  snprintf(buf, sizeof(buf),
                "{\"type\":\"error\",\"seq\":%lu,\"code\":\"%s\",\"field\":\"%s\"}",
                seq_, code, field);
  emit(buf);
}

void SerialProto::emitWatchdog() {
  emit("{\"type\":\"watchdog\"}");
}

void SerialProto::emitState() {
  float battery = getBattery_(R1_, R2_);
  char buf[160];
  snprintf(buf, sizeof(buf),
                "{\"type\":\"state\",\"seq\":%lu,\"battery\":%.2f,"
                "\"speed\":%d,\"steer\":%d,\"pan\":%d,\"tilt\":%d}",
                seq_, battery, speed_, steer_, pan_, tilt_);
  emit(buf);
}

// ---------------------------------------------------------------------------
// Minimal JSON field extractors
//
// These are intentionally tiny and dependency-free (no ArduinoJson).
// They handle the flat, single-line JSON objects used by this protocol:
//   {"cmd":"drive","speed":50,"steer":0}
// They do NOT handle nested objects, arrays, or escaped strings — none of
// which appear in the protocol. See issue #29 §3 for the message shapes.
// ---------------------------------------------------------------------------

// Find "key" and copy the following string value into out.
bool SerialProto::jsonGetString(const char* json, const char* key,
                                char* out, size_t outSize) {
  if (!json || !key || !out || outSize == 0) return false;

  char pat[32];
  size_t klen = strlen(key);
  if (klen == 0 || klen >= sizeof(pat) - 2) return false;
  pat[0] = '"';
  memcpy(pat + 1, key, klen);
  pat[1 + klen] = '"';
  pat[2 + klen] = '\0';

  const char* p = strstr(json, pat);
  if (!p) return false;
  p += klen + 2;   // past "key"

  while (*p == ' ' || *p == '\t') p++;
  if (*p != ':') return false;
  p++;
  while (*p == ' ' || *p == '\t') p++;
  if (*p != '"') return false;   // must be a string value
  p++;

  size_t i = 0;
  while (*p && *p != '"' && i < outSize - 1) {
    if (*p == '\\' && *(p + 1)) { p++; }   // skip escape
    out[i++] = *p++;
  }
  out[i] = '\0';
  return (*p == '"');
}

// Find "key" and parse the following integer value into *out.
bool SerialProto::jsonGetInt(const char* json, const char* key, int* out) {
  if (!json || !key || !out) return false;

  char pat[32];
  size_t klen = strlen(key);
  if (klen == 0 || klen >= sizeof(pat) - 2) return false;
  pat[0] = '"';
  memcpy(pat + 1, key, klen);
  pat[1 + klen] = '"';
  pat[2 + klen] = '\0';

  const char* p = strstr(json, pat);
  if (!p) return false;
  p += klen + 2;
  while (*p == ' ' || *p == '\t') p++;
  if (*p != ':') return false;
  p++;
  while (*p == ' ' || *p == '\t') p++;

  bool neg = false;
  if (*p == '-') { neg = true; p++; }
  else if (*p == '+') { p++; }
  if (*p < '0' || *p > '9') return false;
  long v = 0;
  while (*p >= '0' && *p <= '9') {
    v = v * 10 + (*p - '0');
    p++;
  }
  *out = neg ? -(int)v : (int)v;
  return true;
}
