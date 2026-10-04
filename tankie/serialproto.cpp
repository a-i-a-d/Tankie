// Serial control protocol (NDJSON over UART0) — issue #29.
//
// Implementation of the SerialProto class declared in serialproto.h.
// All command validation + clamping happens here, so the ESP8266 is the
// single, hardened entry point for motor/servo control (issue #29 §3).
//
// Design notes:
//   * Non-blocking: loop() only reads whatever bytes are available on
//     Serial and accumulates them into lineBuf_. No delay() calls. The
//     sweep (issue #31) is stepped from loop() the same way.
//   * JSON parsing is a small, self-contained field extractor (no
//     ArduinoJson dependency) — see jsonGetString/jsonGetInt below.
//   * All emission goes through emit() which appends a newline, so every
//     message is a complete NDJSON line.

#include "serialproto.h"
#include "netstate.h"
#include "streaminfo.h"

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
  sweepActive_ = false;
  sweepAxis_ = 0;
  from_ = 0;
  to_ = 0;
  steps_ = 0;
  stepIdx_ = 0;
  nextStepMs_ = 0;
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
  sweepActive_ = false;
  emitHello();
}

void SerialProto::loop() {
  readSerial();
  checkWatchdog();
  stepSweep();
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
  } else if (strcmp(cmd, "pan-rel") == 0) {
    int delta = 0;
    if (!jsonGetInt(line, "delta", &delta)) {
      emitError("missing", "delta");
      return;
    }
    handlePanRel(delta);
  } else if (strcmp(cmd, "tilt-rel") == 0) {
    int delta = 0;
    if (!jsonGetInt(line, "delta", &delta)) {
      emitError("missing", "delta");
      return;
    }
    handleTiltRel(delta);
  } else if (strcmp(cmd, "center") == 0) {
    handleCenter();
  } else if (strcmp(cmd, "sweep") == 0) {
    char axis[8];
    if (!jsonGetString(line, "axis", axis, sizeof(axis))) {
      emitError("missing", "axis");
      return;
    }
    int from = 0, to = 0, steps = 0;
    if (!jsonGetInt(line, "from", &from)) { emitError("missing", "from"); return; }
    if (!jsonGetInt(line, "to", &to))     { emitError("missing", "to");   return; }
    if (!jsonGetInt(line, "steps", &steps)) { emitError("missing", "steps"); return; }
    if (steps < SERIAL_SWEEP_MIN_STEPS || steps > SERIAL_SWEEP_MAX_STEPS) {
      emitError("range", "steps");
      return;
    }
    if (from < 0 || from > 180) { emitError("range", "from"); return; }
    if (to < 0 || to > 180)     { emitError("range", "to");   return; }
    if (strcmp(axis, "pan") == 0) {
      handleSweep(0, from, to, steps);
    } else if (strcmp(axis, "tilt") == 0) {
      handleSweep(1, from, to, steps);
    } else {
      emitError("range", "axis");
    }
  } else if (strcmp(cmd, "stop") == 0) {
    handleStop();
  } else if (strcmp(cmd, "set_stream") == 0) {
    handleSetStream(line);
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
  sweepActive_ = false;   // an explicit pan command cancels any running sweep
  panServo_->write(angle);
  pan_ = angle;
  seq_++;
  emitAck();
  emitState();
}

void SerialProto::handleTilt(int angle) {
  if (angle < 0 || angle > 180) { emitError("range", "tilt"); return; }
  sweepActive_ = false;   // an explicit tilt command cancels any running sweep
  tiltServo_->write(angle);
  tilt_ = angle;
  seq_++;
  emitAck();
  emitState();
}

void SerialProto::handlePanRel(int delta) {
  // Relative move from the current pan (issue #31), clamped to 0..180.
  // Deltas are accepted in [-180, 180] (the full servo range) — anything
  // larger is a range error rather than a silent clamp.
  if (delta < -180 || delta > 180) { emitError("range", "delta"); return; }
  int target = pan_ + delta;
  if (target < 0) target = 0;
  if (target > 180) target = 180;
  sweepActive_ = false;   // an explicit relative move cancels any running sweep
  panServo_->write(target);
  pan_ = target;
  seq_++;
  emitAck();
  emitState();
}

void SerialProto::handleTiltRel(int delta) {
  if (delta < -180 || delta > 180) { emitError("range", "delta"); return; }
  int target = tilt_ + delta;
  if (target < 0) target = 0;
  if (target > 180) target = 180;
  sweepActive_ = false;
  tiltServo_->write(target);
  tilt_ = target;
  seq_++;
  emitAck();
  emitState();
}

void SerialProto::handleCenter() {
  // Center the camera: pan=90 AND tilt=90 in one ack (issue #31).
  sweepActive_ = false;
  panServo_->write(90);
  tiltServo_->write(90);
  pan_ = 90;
  tilt_ = 90;
  seq_++;
  emitAck();
  emitState();
}

void SerialProto::handleSweep(int axis, int from, int to, int steps) {
  // Time-based sweep (issue #31): step from `from` to `to` across `steps`
  // equally-spaced positions, spread over ~SERIAL_SWEEP_DEFAULT_MS.
  // Non-blocking: stepSweep() in loop() applies one position at a time.
  if (steps < SERIAL_SWEEP_MIN_STEPS || steps > SERIAL_SWEEP_MAX_STEPS) {
    emitError("range", "steps");
    return;
  }
  if (from < 0 || from > 180) { emitError("range", "from"); return; }
  if (to < 0 || to > 180)     { emitError("range", "to");   return; }

  sweepActive_ = true;
  sweepAxis_ = axis;
  from_ = from;
  to_ = to;
  steps_ = steps;
  stepIdx_ = 0;
  unsigned long interval = SERIAL_SWEEP_DEFAULT_MS / steps_;
  if (interval < 1) interval = 1;
  // First position applies on the very next loop() iteration (immediate),
  // the rest are spread over ~SERIAL_SWEEP_DEFAULT_MS.
  nextStepMs_ = millis();
  seq_++;
  emitAck();
}

void SerialProto::handleStop() {
  tank_->setSpeed(0);
  tank_->setSteer(0);
  speed_ = 0;
  steer_ = 0;
  sweepActive_ = false;   // stop cancels a running sweep (issue #31)
  seq_++;
  lastDriveCmdMs_ = millis();
  if (watchdogFired_) watchdogFired_ = false;   // re-arm on next command
  emitAck();
  emitState();
}

// ---------------------------------------------------------------------------
// set_stream (issue #51) — the Pi pushes its own stream endpoint
// ---------------------------------------------------------------------------

// Validate an IPv4 dotted-quad (the only host form we accept). Restricting
// the ip to digits + dots guarantees the assembled URL contains no
// JSON-breaking characters (it is embedded verbatim in the state line).
static bool validIpv4(const char* ip) {
  if (!ip) return false;
  int octets = 0;
  int cur = 0;
  bool inNum = false;
  for (const char* p = ip; ; p++) {
    if (*p >= '0' && *p <= '9') {
      cur = cur * 10 + (*p - '0');
      inNum = true;
      if (cur > 255) return false;
    } else if (*p == '.') {
      if (!inNum) return false;      // empty octet (leading/trailing/double .)
      octets++;
      if (octets > 3) return false;  // more than 4 octets
      cur = 0;
      inNum = false;
    } else if (*p == '\0') {
      if (!inNum) return false;      // trailing dot / empty
      octets++;
      return octets == 4;
    } else {
      return false;                  // any other character (space, quote, ...)
    }
  }
}

// The stream path must start with '/' and contain only safe, printable,
// non-JSON-breaking characters (it is embedded verbatim in the state line).
static bool validStreamPath(const char* path) {
  if (!path || path[0] != '/') return false;
  size_t n = strlen(path);
  if (n > 96) return false;
  for (size_t i = 0; i < n; i++) {
    char c = path[i];
    if (c < 0x21 || c > 0x7e) return false;   // printable ASCII only
    if (c == '"' || c == '\\' || c == '{' || c == '}' || c == ',') return false;
  }
  return true;
}

void SerialProto::handleSetStream(const char* line) {
  char ip[32] = "";
  char path[48] = "";
  int port = 0;
  bool hasIp = jsonGetString(line, "ip", ip, sizeof(ip));
  bool hasPort = jsonGetInt(line, "port", &port);
  bool hasPath = jsonGetString(line, "path", path, sizeof(path));

  if (!hasIp || !hasPort || !hasPath) {
    emitError("missing", "ip");
    return;
  }
  if (!validIpv4(ip)) {
    emitError("range", "ip");
    return;
  }
  if (port < 1 || port > 65535) {
    emitError("range", "port");
    return;
  }
  if (!validStreamPath(path)) {
    emitError("range", "path");
    return;
  }

  char url[160];
  snprintf(url, sizeof(url), "http://%s:%d%s", ip, port, path);

  streamInfo.url = url;
  seq_++;
  emitAck();
  emitState();   // state changed -> broadcast immediately (now with stream_url)
}

// ---------------------------------------------------------------------------
// Sweep stepper (issue #31)
// ---------------------------------------------------------------------------

void SerialProto::stepSweep() {
  if (!sweepActive_) return;
  unsigned long now = millis();
  if (now < nextStepMs_) return;

  // Apply the next position.
  int pos = from_ + (to_ - from_) * stepIdx_ / steps_;
  if (sweepAxis_ == 0) {
    panServo_->write(pos);
    pan_ = pos;
  } else {
    tiltServo_->write(pos);
    tilt_ = pos;
  }
  stepIdx_++;

  if (stepIdx_ >= steps_) {
    // Sweep complete: emit the done line and stop.
    sweepActive_ = false;
    emitSweepDone();
    emitState();
  } else {
    nextStepMs_ = now + (SERIAL_SWEEP_DEFAULT_MS / steps_);
  }
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

void SerialProto::emitSweepDone() {
  emit(sweepAxis_ == 0
       ? "{\"type\":\"sweep\",\"axis\":\"pan\",\"done\":true}"
       : "{\"type\":\"sweep\",\"axis\":\"tilt\",\"done\":true}");
}

void SerialProto::emitState() {
  float battery = getBattery_(R1_, R2_);

  // Issue #46: include the network mode + IP in the state broadcast so a
  // client can determine the ESP's IP and mode (AP vs STA) without needing
  // the boot console message. netState is set by WiFiManager on connect
  // (STA) or when the config portal AP starts; defaults to "sta"/"0.0.0.0"
  // before WiFi has been attempted. The fields are bounded strings
  // (mode in {"sta","ap"}, ip is a dotted quad) so the line always fits
  // and stays a single valid NDJSON object.
  const char* mode = "sta";
  const char* ip   = "0.0.0.0";
  if (netState.mode.length() > 0) mode = netState.mode.c_str();
  if (netState.ip.length() > 0)   ip   = netState.ip.c_str();

  // Issue #51: append the stream URL the Pi pushed via set_stream (if any).
  // The field is omitted until the Pi has provided one, so the line shape
  // is unchanged for firmware/bridge pairs that do not use it.
  // (buf is 256: a realistic stream_url like "http://192.168.178.134:8889/cam/"
  //  makes the full line 171 bytes — 160 would truncate it into invalid JSON.)
  char buf[256];
  if (streamInfo.url.length() > 0) {
    snprintf(buf, sizeof(buf),
             "{\"type\":\"state\",\"seq\":%lu,\"battery\":%.2f,"
             "\"speed\":%d,\"steer\":%d,\"pan\":%d,\"tilt\":%d,"
             "\"net_mode\":\"%s\",\"net_ip\":\"%s\","
             "\"stream_url\":\"%s\"}",
             seq_, battery, speed_, steer_, pan_, tilt_, mode, ip,
             streamInfo.url.c_str());
  } else {
    snprintf(buf, sizeof(buf),
             "{\"type\":\"state\",\"seq\":%lu,\"battery\":%.2f,"
             "\"speed\":%d,\"steer\":%d,\"pan\":%d,\"tilt\":%d,"
             "\"net_mode\":\"%s\",\"net_ip\":\"%s\"}",
             seq_, battery, speed_, steer_, pan_, tilt_, mode, ip);
  }
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
