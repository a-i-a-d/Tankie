// Config portal AP name and password.
// Used when the ESP cannot connect to a saved WiFi network,
// or when no WiFi network has been configured yet.
// The ESP starts as this AP and serves a web form at
// http://192.168.4.1/ to configure the WiFi connection.
#define APSSID "tankie-esp"
#define APPSK  "secret"

// Motor driver pins (SparkFun TB6612FNG)
#define PWMA D1
#define AIN2 D2
#define AIN1 D3
#define STBY D4
#define BIN1 D8
#define BIN2 D7
#define PWMB D6
#define BAT  A0

// Pan/tilt servo pins
#define SERVO_PAN  D0
#define SERVO_TILT D5
