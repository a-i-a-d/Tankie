// NOTE: This sketch uses the async web stack (AsyncWebServer / AsyncWebSocket).
// ElegantOTA must therefore be built in async mode. That is a per-library
// compile flag: build with -DELEGANTOTA_USE_ASYNC_WEBSERVER=1 (see README /
// arduino-cli --build-property "compiler.cpp.extra_flags=-DELEGANTOTA_USE_ASYNC_WEBSERVER=1").
#include <ElegantOTA.h>
//#include "m8833.h"
#include "tankdrive.h"
#include "config.h"
#include "batt.h"
#include "serialproto.h"
#include "wifimanager.h"
#include "netstate.h"
#include "streaminfo.h"
#include <Servo.h>
#include <ESP8266WiFi.h>
#include <ESPAsyncTCP.h>
#include <ESPAsyncWebServer.h>
#include <FS.h>
#include "LittleFS.h"
#include <time.h>

Motor M2 = Motor(AIN1, AIN2, PWMA, STBY);
Motor M1 = Motor(BIN1, BIN2, PWMB, STBY);

AsyncWebServer server(80);
AsyncWebServer configServer(8080);
AsyncWebSocket ws("/ws");
//M8833 M1(D1,D2);
//M8833 M2(D4,D3);
//TankDrive tank(&M1, &M2);
TankDrive tank(&M1, &M2);
Servo servoPan;
Servo servoTilt;

// WiFi configuration (STA with saved credentials, fallback config portal).
// See wifimanager.h/.cpp - replaces the WiFiManager library, which
// collides with ESPAsyncWebServer / ElegantOTA (see issue #21).
WiFiManager wifiManager;


float vin = 0.0;
float R1 = 330000;
float R2 = 33000;

// Serial control protocol (issue #29) — the single hardened entry
// point for motor/servo commands over the USB-serial link.
SerialProto serialProto(&tank, &servoPan, &servoTilt, getBatVoltage, R1, R2);
long batInterval = 1000;
long batTimer;

// Current state (updated by the websocket handler, broadcast to clients)
int currentSpeed = 0;
int currentSteer = 0;
int currentPan = 90;
int currentTilt = 90;
long lastCommandMs = 0;

// Safety watchdog: if no command arrives within this window the motors are
// stopped and the servos return to center, so a lost/disconnected client can
// never leave the tank driving unattended.
const unsigned long COMMAND_TIMEOUT_MS = 5000;
bool watchdogActive = false;

// Forward declarations: these helpers are defined later in the sketch but
// are called from setup()/loop() (the Arduino preprocessor concatenates the
// whole .ino before compiling, so the order is legal on the ESP; the host
// test build compiles the file as plain C++ and needs the declarations).
void setMotorsSafe();   // issue #44 C: safe motor state at the top of setup()
void broadcastState();
void broadcastWatchdog();
void listDir(const char *dirname);
String processor(const String &var);
void notFound(AsyncWebServerRequest *request);
void eventHandler(AsyncWebSocket *server, AsyncWebSocketClient *client,
                  AwsEventType type, void *arg, uint8_t *data, size_t len);


// Force the TB6612FNG into a known, non-driving state before anything else
// runs, so a bad/strapping-pin boot can never leave the motors spinning at
// max speed (issue #44 C). STBY=LOW puts both H-bridges in standby and the
// direction + PWM pins LOW = no drive. drive() re-asserts STBY=HIGH on every
// command, so normal operation is unaffected.
void setMotorsSafe() {
  pinMode(STBY, OUTPUT);  digitalWrite(STBY, LOW);
  pinMode(PWMA, OUTPUT);  digitalWrite(PWMA, LOW);
  pinMode(AIN1, OUTPUT);  digitalWrite(AIN1, LOW);
  pinMode(AIN2, OUTPUT);  digitalWrite(AIN2, LOW);
  pinMode(PWMB, OUTPUT);  digitalWrite(PWMB, LOW);
  pinMode(BIN1, OUTPUT);  digitalWrite(BIN1, LOW);
  pinMode(BIN2, OUTPUT);  digitalWrite(BIN2, LOW);
}

void setup() {
  setMotorsSafe();   // issue #44 C: first statement, before Serial/LittleFS/servos

  Serial.begin(SERIAL_PROTO_BAUD);  // 921600 8N1 — issue #29

  if(!LittleFS.begin()){
    Serial.println("An Error has occurred while mounting LittleFS");
    return;
  }

  ElegantOTA.begin(&server);

  ws.onEvent(eventHandler);
  server.addHandler(&ws);

  servoPan.attach(SERVO_PAN);
  servoTilt.attach(SERVO_TILT);
  servoPan.write(90);
  servoTilt.write(90);

  // Serial control protocol: emit the hello handshake (issue #29).
  serialProto.begin();

  Serial.println();
  Serial.println();

  // WiFi: connect to the saved network (ssid.txt/pass.txt on LittleFS),
  // or open the "tankie-esp" config portal AP (http://192.168.4.1:8080)
  // when nothing is saved or the network is unreachable.
  wifiManager.begin(APSSID, APPSK);

  // Port 80 always serves the tank control page (even in config mode, so the
  // page can hint at the portal URL). The WiFi setup form itself lives on
  // the dedicated config server below (port 8080).
  server.on("/", HTTP_GET, [](AsyncWebServerRequest *request)
  {
    Serial.println("requested /");
    request->send(LittleFS, "/index.html", String(), false, processor);
  });

  // Config portal server (only meaningful while wifiManager.inConfigMode()):
  // the WiFi setup form on a dedicated port, so it never collides with the
  // tank control page or the ElegantOTA routes on port 80.
  configServer.on("/", HTTP_GET, [](AsyncWebServerRequest *request)
  {
    Serial.println("config portal: requested /");
    request->send(LittleFS, "/wifimanager.html", "text/html");
  });

  configServer.on("/", HTTP_POST, [](AsyncWebServerRequest *request)
  {
    wifiManager.handleConfigPost(request);
  });

  configServer.onNotFound(notFound);

  server.on("/style.css", HTTP_GET, [](AsyncWebServerRequest *request){
    request->send(LittleFS, "/style.css", "text/css");
  });

  server.on("/joy.js", HTTP_GET, [](AsyncWebServerRequest *request){
    request->send(LittleFS, "/joy.js", "text/js");
  });

  server.on("/script.js", HTTP_GET, [](AsyncWebServerRequest *request){
    request->send(LittleFS, "/script.js", "text/js");
  });

  server.on("/tankie.png", HTTP_GET, [](AsyncWebServerRequest *request){
    request->send(LittleFS, "/tankie.png", "image/png");
  });

  server.onNotFound(notFound);
  server.begin();
  configServer.begin();

  listDir("/");
}

void loop()
{
  serialProto.loop();   // NDJSON control link (issue #29)
  ElegantOTA.loop();
  wifiManager.loop();
  ws.cleanupClients();
  if (millis() > batInterval + batTimer ) {
    batTimer = millis();
    vin = getBatVoltage(R1, R2);
    // send battery Value to server
    Serial.print("Battery Voltage: ");
    Serial.println(vin);
    // State feedback: battery + current speed/steer/pan/tilt, so a client
    // (e.g. the AI) always knows the actual tank state. Contract shape
    // (issue #32): {"type":"state","seq":N,"battery":...,"speed":...,...}.
    broadcastState();
  }

  // Safety watchdog: stop the motors and recenter the camera if no command
  // has been received for COMMAND_TIMEOUT_MS.
  if (watchdogActive && (millis() - lastCommandMs) > COMMAND_TIMEOUT_MS) {
    watchdogActive = false;
    Serial.println("Watchdog: no command received, stopping motors and centering camera");
    if (currentSpeed != 0 || currentSteer != 0) {
      currentSpeed = 0;
      currentSteer = 0;
      tank.setSpeed(0);
      tank.setSteer(0);
    }
    if (currentPan != 90 || currentTilt != 90) {
      currentPan = 90;
      currentTilt = 90;
      servoPan.write(90);
      servoTilt.write(90);
    }
    broadcastWatchdog();
  }
}


void notFound(AsyncWebServerRequest *request) {
    request->send(404, "text/plain", "Not found");
}

String processor(const String &var)
{
  return String("unknown");
}

// ---------------------------------------------------------------------------
// WebSocket command handling (issue #32)
//
// The ESP /ws endpoint now speaks the JSON protocol contract (the same NDJSON
// shapes as the serial link, tankie/serialproto.h) instead of the legacy
// key=value dialect. The web UI (tankie/data/script.js) and the Pi bridge
// both send:
//   {"cmd":"drive","speed":N,"steer":M}
//   {"cmd":"pan","angle":N}
//   {"cmd":"tilt","angle":N}
//   {"cmd":"stop"}
// Non-JSON / unknown messages are logged and ignored (coexistence rule).
// ---------------------------------------------------------------------------

// Minimal JSON field extractor (dependency-free, same approach as
// serialproto.cpp): find "key" and parse the following integer value.
static bool wsJsonGetInt(const char* json, const char* key, int* out) {
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
  p += klen + 2;   // past "key"
  while (*p == ' ' || *p == '\t') p++;
  if (*p != ':') return false;
  p++;
  while (*p == ' ' || *p == '\t') p++;
  bool neg = false;
  if (*p == '-') { neg = true; p++; }
  else if (*p == '+') { p++; }
  if (*p < '0' || *p > '9') return false;
  long v = 0;
  while (*p >= '0' && *p <= '9') { v = v * 10 + (*p - '0'); p++; }
  *out = neg ? -(int)v : (int)v;
  return true;
}

// Minimal JSON string-field extractor (for the "cmd" dispatch).
static bool wsJsonGetString(const char* json, const char* key,
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
  p += klen + 2;
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

// Apply a validated drive command (speed/steer) to the tank.
static void wsApplyDrive(int speed, int steer) {
  currentSpeed = speed;
  currentSteer = steer;
  tank.setSpeed(speed);
  tank.setSteer(steer);
  lastCommandMs = millis();
  watchdogActive = (speed != 0 || steer != 0);
}

// Apply a validated pan/tilt angle to the servo (clamped to 0-180).
static void wsApplyPan(int angle) {
  if (angle < 0) angle = 0;
  if (angle > 180) angle = 180;
  currentPan = angle;
  servoPan.write(angle);
  lastCommandMs = millis();
}

static void wsApplyTilt(int angle) {
  if (angle < 0) angle = 0;
  if (angle > 180) angle = 180;
  currentTilt = angle;
  servoTilt.write(angle);
  lastCommandMs = millis();
}

// Maximum size of a single WebSocket command frame we accept.
// Matches the serial line buffer (tankie/serialproto.cpp, lineBuf_[128]).
// Longer frames are truncated before parsing; a truncated command fails
// JSON parsing and is rejected (no unbounded stack use, no OOB writes).
static const size_t WS_MSG_MAX = 128;

void handleWebSocketMessage(void *arg, uint8_t *data, size_t len) {
  AwsFrameInfo *info = (AwsFrameInfo*)arg;
  if (info->final && info->index == 0 && info->len == len && info->opcode == WS_TEXT) {
    if (data != NULL) {
      // Copy the frame into a bounded, NUL-terminated buffer and parse
      // THAT (issue #34). ESPAsyncWebSocket hands us a pointer into the
      // TCP receive pbuf with no NUL-terminator guarantee - it even
      // restores data[datalen] after the handler returns - so we must
      // not write past `len` and must not read `data` as a C string.
      size_t n = (len < WS_MSG_MAX) ? len : WS_MSG_MAX;
      char buf[WS_MSG_MAX + 1];
      memcpy(buf, data, n);
      buf[n] = '\0';
      const char* msg = buf;
      Serial.print("Data received: ");
      Serial.println(msg);

      // Only accept JSON objects (the protocol contract).
      if (msg[0] != '{') {
        Serial.println("Ignoring non-JSON websocket message");
        return;
      }

      char cmd[16];
      if (!wsJsonGetString(msg, "cmd", cmd, sizeof(cmd))) {
        Serial.println("Ignoring websocket message without a cmd field");
        return;
      }

      if (strcmp(cmd, "drive") == 0) {
        int speed = 0, steer = 0;
        bool hasSpeed = wsJsonGetInt(msg, "speed", &speed);
        bool hasSteer = wsJsonGetInt(msg, "steer", &steer);
        if (!hasSpeed && !hasSteer) {
          Serial.println("drive: missing speed/steer");
          return;
        }
        if (speed < -255 || speed > 255 || steer < -255 || steer > 255) {
          Serial.println("drive: speed/steer out of range [-255, 255]");
          return;
        }
        wsApplyDrive(speed, steer);
      } else if (strcmp(cmd, "pan") == 0) {
        int angle = 0;
        if (!wsJsonGetInt(msg, "angle", &angle)) {
          Serial.println("pan: missing angle");
          return;
        }
        if (angle < 0 || angle > 180) {
          Serial.println("pan: angle out of range [0, 180]");
          return;
        }
        wsApplyPan(angle);
      } else if (strcmp(cmd, "tilt") == 0) {
        int angle = 0;
        if (!wsJsonGetInt(msg, "angle", &angle)) {
          Serial.println("tilt: missing angle");
          return;
        }
        if (angle < 0 || angle > 180) {
          Serial.println("tilt: angle out of range [0, 180]");
          return;
        }
        wsApplyTilt(angle);
      } else if (strcmp(cmd, "stop") == 0) {
        wsApplyDrive(0, 0);
      } else {
        Serial.print("Unknown websocket cmd: ");
        Serial.println(cmd);
      }
    }
  }
}

// ---------------------------------------------------------------------------
// State / watchdog broadcast (issue #32)
//
// The ESP now broadcasts the contract shapes so the web UI (and any other
// JSON client) can parse a single, well-defined message type:
//   {"type":"state","seq":N,"battery":B,"speed":S,"steer":T,"pan":P,"tilt":U}
//   {"type":"watchdog"}
// ---------------------------------------------------------------------------
static unsigned long wsSeq = 0;

void broadcastState() {
  float battery = getBatVoltage(R1, R2);
  wsSeq++;
  // Issue #46: include the network mode + IP in the websocket state
  // broadcast so the web UI can display it without the boot console.
  const char* mode = "sta";
  const char* ip   = "0.0.0.0";
  if (netState.mode.length() > 0) mode = netState.mode.c_str();
  if (netState.ip.length() > 0)   ip   = netState.ip.c_str();

  // Issue #51: append the stream URL the Pi pushed via set_stream (if any).
  // The field is omitted until the Pi has provided one, so the line shape is
  // unchanged for firmware/bridge pairs that do not use it. buf is 256: a
  // realistic stream_url makes the full line 171 bytes (160 would truncate it
  // into invalid JSON).
  char buf[256];
  if (streamInfo.url.length() > 0) {
    snprintf(buf, sizeof(buf),
             "{\"type\":\"state\",\"seq\":%lu,\"battery\":%.2f,"
             "\"speed\":%d,\"steer\":%d,\"pan\":%d,\"tilt\":%d,"
             "\"net_mode\":\"%s\",\"net_ip\":\"%s\","
             "\"stream_url\":\"%s\"}",
             wsSeq, battery, currentSpeed, currentSteer, currentPan, currentTilt,
             mode, ip, streamInfo.url.c_str());
  } else {
    snprintf(buf, sizeof(buf),
             "{\"type\":\"state\",\"seq\":%lu,\"battery\":%.2f,"
             "\"speed\":%d,\"steer\":%d,\"pan\":%d,\"tilt\":%d,"
             "\"net_mode\":\"%s\",\"net_ip\":\"%s\"}",
             wsSeq, battery, currentSpeed, currentSteer, currentPan, currentTilt,
             mode, ip);
  }
  ws.textAll(buf);
}

void broadcastWatchdog() {
  ws.textAll("{\"type\":\"watchdog\"}");
}

void eventHandler(AsyncWebSocket *server, AsyncWebSocketClient *client, AwsEventType type, void *arg, uint8_t *data, size_t len) {
  switch (type) {
    case WS_EVT_CONNECT:
      Serial.printf("WebSocket client #%u connected from %s\n", client->id(), client->remoteIP().toString().c_str());
      break;
    case WS_EVT_DISCONNECT:
      Serial.printf("WebSocket client #%u disconnected\n", client->id());
      break;
    case WS_EVT_DATA:
      handleWebSocketMessage(arg, data, len);
      break;
    case WS_EVT_PONG:
    case WS_EVT_ERROR:
      break;
  }
}

void listDir(const char *dirname) {
  Serial.printf("Listing directory: %s\n", dirname);

  Dir root = LittleFS.openDir(dirname);

  while (root.next()) {
    File file = root.openFile("r");
    Serial.print("  FILE: ");
    Serial.print(root.fileName());
    Serial.print("  SIZE: ");
    Serial.print(file.size());
    time_t cr = file.getCreationTime();
    time_t lw = file.getLastWrite();
    file.close();
    struct tm *tmstruct = localtime(&cr);
    Serial.printf("    CREATION: %d-%02d-%02d %02d:%02d:%02d\n", (tmstruct->tm_year) + 1900, (tmstruct->tm_mon) + 1, tmstruct->tm_mday, tmstruct->tm_hour, tmstruct->tm_min, tmstruct->tm_sec);
    tmstruct = localtime(&lw);
    Serial.printf("  LAST WRITE: %d-%02d-%02d %02d:%02d:%02d\n", (tmstruct->tm_year) + 1900, (tmstruct->tm_mon) + 1, tmstruct->tm_mday, tmstruct->tm_hour, tmstruct->tm_min, tmstruct->tm_sec);
  }
}
