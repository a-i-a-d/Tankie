// Config for the serial control protocol (issue #29).
// Compile-time constants — change here and rebuild.

// Baud rate for the serial control link (UART0 / USB-serial bridge).
// 921600 is the target; fall back to 115200 if framing errors are seen.
#define SERIAL_PROTO_BAUD 921600

// Watchdog timeout in milliseconds. If no drive/stop command arrives
// within this window the motors stop and a watchdog event is emitted.
// Start at 1000 ms while testing; target 500 ms after the soak test.
#define SERIAL_WATCHDOG_MS 1000

// Protocol version. Bump on any breaking change to the NDJSON protocol.
// The Pi bridge verifies this during the hello handshake.
#define SERIAL_PROTO_VERSION 1

// Firmware version string, reported in the hello handshake.
#define TANKIE_FW_VERSION "v0.1-serial"

// Sweep defaults (issue #31). A `sweep` command runs for ~SERIAL_SWEEP_DEFAULT_MS
// total, divided into `steps` equally-spaced positions (1..SERIAL_SWEEP_MAX_STEPS).
// So `steps` controls the granularity, not the duration: 20 steps ≈ 100 ms apart.
#define SERIAL_SWEEP_DEFAULT_MS 2000
#define SERIAL_SWEEP_MIN_STEPS 1
#define SERIAL_SWEEP_MAX_STEPS 50
