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
#include <Servo.h>
#include <ESP8266WiFi.h>
#include <ESPAsyncTCP.h>
#include <ESPAsyncWebServer.h>
#include <FS.h>
#include "LittleFS.h"
#include <time.h>

const char* PARAM_COMMAND = "command";
const char* PARAM_SPEED = "speed";
const char* PARAM_STEER = "steer";
const char* PARAM_PAN = "pan";
const char* PARAM_TILT = "tilt";

const int offsetA = 1;
const int offsetB = 1;

Motor M2 = Motor(AIN1, AIN2, PWMA, offsetA, STBY);
Motor M1 = Motor(BIN1, BIN2, PWMB, offsetB, STBY);

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


void setup() {

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
    // (e.g. the AI) always knows the actual tank state.
    String state = String("{\"battery\":") + String(vin)
      + String(",\"speed\":") + String(currentSpeed)
      + String(",\"steer\":") + String(currentSteer)
      + String(",\"pan\":") + String(currentPan)
      + String(",\"tilt\":") + String(currentTilt)
      + String("}");
    ws.textAll(state);
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
    ws.textAll("{\"watchdog\":true,\"speed\":0,\"steer\":0,\"pan\":90,\"tilt\":90}");
  }
}


void notFound(AsyncWebServerRequest *request) {
    request->send(404, "text/plain", "Not found");
}

String processor(const String &var)
{
  return String("unknown");
}

void handleWebSocketMessage(void *arg, uint8_t *data, size_t len) {
  AwsFrameInfo *info = (AwsFrameInfo*)arg;
  if (info->final && info->index == 0 && info->len == len && info->opcode == WS_TEXT) {

    if (data != NULL)
    {
      data[len] = 0;
      const char s[2] = "=";
      Serial.print("Data received: ");
      Serial.println((char*)data);

      String message = String( (char *) data );
      Serial.println(message);

      char *token = strtok((char*)data, s);
      if (token != NULL)
      {
        if (strcmp(token, "speed") == 0)
        {
          //Serial.print("Set speed to");
          token = strtok(NULL, s);
          //Serial.println(token);
          currentSpeed = atoi(token);
          lastCommandMs = millis();
          watchdogActive = (currentSpeed != 0);
          tank.setSpeed(currentSpeed);
        }
        else if (strcmp(token, "steer") == 0)
        {
          //Serial.print("Set steer to");
          token = strtok(NULL, s);
          //Serial.println(token);
          currentSteer = atoi(token);
          lastCommandMs = millis();
          watchdogActive = (currentSpeed != 0);
          tank.setSteer(currentSteer);
        }
        else if (strcmp(token, "pan") == 0)
        {
          token = strtok(NULL, s);
          //Serial.println(token);
          // Clamp to the servo range so out-of-range values cannot stall the servo
          int pan = atoi(token);
          if (pan < 0) pan = 0;
          if (pan > 180) pan = 180;
          currentPan = pan;
          lastCommandMs = millis();
          servoPan.write(currentPan);
        }
        else if (strcmp(token, "tilt") == 0)
        {
          token = strtok(NULL, s);
          //Serial.println(token);
          // Clamp to the servo range so out-of-range values cannot stall the servo
          int tilt = atoi(token);
          if (tilt < 0) tilt = 0;
          if (tilt > 180) tilt = 180;
          currentTilt = tilt;
          lastCommandMs = millis();
          servoTilt.write(currentTilt);
        }
        else
        {
          Serial.print("Unknown command: ");
          token = strtok(NULL, s);
          Serial.println(token);
        }
      }
    }
  }
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
