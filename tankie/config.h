// Set this to false and configure you wifi credentials below for client mode
#define AP_MODE true

// When running in Wifi Access Point mode 
#ifdef AP_MODE
#define APSSID "tankie-esp"
#define APPSK "secret"
#endif

// When running in Wifi Client mode
#ifndef AP_MODE
#define STASSID "your access point ssid"
#define STAPSK "your wifi password"
#endif

#define PWMA D1
#define AIN2 D2
#define AIN1 D3
#define STBY D4
#define BIN1 D8
#define BIN2 D7
#define PWMB D6
#define BAT A0

#define SERVO_PAN D0
#define SERVO_TILT D5