// Serial control protocol (NDJSON over UART0) — issue #29.
//
// Line-oriented JSON for realtime tank control over the USB-serial link.
// The ESP8266 is the single entry point for motor/servo commands; all
// validation + clamping happens here (the only entry point, per issue #29).
//
// Pi → ESP (commands), one object per line:
//   {"cmd":"drive","speed":50,"steer":0}
//   {"cmd":"pan","angle":90}
//   {"cmd":"tilt","angle":30}
//   {"cmd":"stop"}
//   {"cmd":"set_stream","ip":"192.168.1.42","port":8889,"path":"/cam/"}
//        (issue #51: the Pi pushes its own stream endpoint; the ESP stores
//         it and adds stream_url to every state broadcast)
//
// ESP → Pi (responses), one object per line:
//   {"type":"hello","proto":1,"fw":"v0.1-serial"}
//   {"type":"ack","seq":1}
//   {"type":"error","seq":2,"code":"range","field":"speed"}
//   {"type":"watchdog"}
//   {"type":"state","seq":1,"battery":7.42,"speed":50,"steer":0,"pan":90,"tilt":90,
//    "net_mode":"sta"|"ap","net_ip":"192.168.x.y",
//    "stream_url":"http://192.168.1.42:8889/cam/"}   (issue #46, #51)
//    (stream_url is present only once the Pi has sent set_stream)
//
// Non-JSON lines (e.g. Serial.println debug output) are ignored, so the
// protocol coexists safely with console logging (issue #29, T7).
//
// See issue #29 for the full protocol specification.

#ifndef _SERIALPROTO_H_
#define _SERIALPROTO_H_

#include "Arduino.h"
#include "config_serial.h"
#include "tankdrive.h"
#include "streaminfo.h"

class Servo;  // full definition in <Servo.h> (esp8266 core) or tests/shims

class SerialProto {
public:
  // tank        - the drive controller (motors)
  // panServo    - pan servo (SG90)
  // tiltServo   - tilt servo (SG90)
  // getBattery  - battery voltage reader (batt.cpp getBatVoltage)
  // R1, R2      - the voltage-divider resistors passed to getBattery
  SerialProto(TankDrive* tank, Servo* panServo, Servo* tiltServo,
              float (*getBattery)(float R1, float R2), float R1, float R2);

  void begin();   // reset state + emit the hello handshake
  void loop();    // non-blocking: read lines, watchdog, 1 Hz state broadcast

  // --- State accessors ---
  unsigned long seq() const { return seq_; }
  bool          watchdogFired() const { return watchdogFired_; }
  int           speed() const { return speed_; }
  int           steer() const { return steer_; }
  int           pan()   const { return pan_; }
  int           tilt()  const { return tilt_; }
  // The stream URL the Pi pushed via set_stream (issue #51); empty until set.
  String        streamUrl() const { return streamInfo.url; }

  // Test hook: inject a command line directly (bypasses Serial).
  void injectLine(const char* line);

private:
  // Line reader state
  char   lineBuf_[128];
  size_t lineLen_;

  // Protocol state
  unsigned long seq_;
  bool          watchdogFired_;
  unsigned long lastDriveCmdMs_;
  unsigned long lastStateBroadcastMs_;

  // Current state (for state broadcasts)
  int speed_;
  int steer_;
  int pan_;
  int tilt_;

  // Dependencies
  TankDrive* tank_;
  Servo*     panServo_;
  Servo*     tiltServo_;
  float (*getBattery_)(float R1, float R2);
  float R1_;
  float R2_;

  // Emission (ESP → Pi)
  void emit(const char* s);
  void emitHello();
  void emitAck();
  void emitError(const char* code, const char* field);
  void emitWatchdog();
  void emitState();

  // Command handling (Pi → ESP)
  void handleLine(const char* line, size_t len);
  void handleDrive(int speed, int steer);
  void handlePan(int angle);
  void handleTilt(int angle);
  void handleStop();
  void handleSetStream(const char* line);   // issue #51
  void checkWatchdog();
  void readSerial();

  // Minimal JSON field extractors (no external dependency).
  static bool jsonGetString(const char* json, const char* key,
                            char* out, size_t outSize);
  static bool jsonGetInt(const char* json, const char* key, int* out);
};

#endif
