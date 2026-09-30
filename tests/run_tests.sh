#!/usr/bin/env bash
# Host test runner for the Tankie firmware modules (issue #5).
#
# Compiles the pure/deterministic modules with g++ (no ESP8266 toolchain
# needed) against the Arduino shim in tests/shims/, links them with the
# test harness, and runs the resulting binary. Non-zero exit on any
# failure, so it can be used as-is in CI (.github/workflows/tests.yml).
#
# Usage: bash tests/run_tests.sh
#
# To add a module: add its .cpp to MODULES and its test file to TESTS.
# The shims in tests/shims/ grow the same way (Arduino.h + the network
# stack shims for tankie.ino's WS fallback handler - issue #34).
set -euo pipefail

cd "$(dirname "$0")/.."   # repo root

CXX="${CXX:-g++}"
CXXFLAGS="-std=c++17 -Wall -Wextra -I tests/shims -I tankie"
OUT=tests/build/test_bin
mkdir -p tests/build

# Modules under test (the firmware .cpp files, compiled for the host).
MODULES=(
  tankie/tankdrive.cpp
  tankie/SparkFun_TB6612.cpp
  tankie/batt.cpp
  tankie/serialproto.cpp
  tankie/wifimanager.cpp
)

# The sketch under test (issue #34): tankie.ino is a .ino, which g++ would
# otherwise hand to the linker as a "linker script" (unrecognized extension).
# Compile it explicitly as C++ with -x c++.
INO=(
  tankie/tankie.ino
)

# Test files (one per module, plus the harness).
TESTS=(
  tests/test_main.cpp
  tests/test_tankdrive.cpp
  tests/test_motor.cpp
  tests/test_bat_voltage.cpp
  tests/test_serialproto.cpp
  tests/test_ws_fallback.cpp
)

echo "[run_tests] compiling: ${MODULES[*]}"
echo "[run_tests] sketch:    ${INO[*]}"
echo "[run_tests] tests:     ${TESTS[*]}"
"$CXX" $CXXFLAGS "${TESTS[@]}" "${MODULES[@]}" -x c++ "${INO[@]}" -o "$OUT"

echo "[run_tests] running:   $OUT"
"$OUT"
