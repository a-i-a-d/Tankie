// Unit tests for the battery voltage measurement (tankie/batt.cpp,
// extracted from tankie.ino) - issue #5.
//
// getBatVoltage() averages 10 ADC samples from the BAT pin (A0), converts
// the average to the divider voltage (V = adc/1023 * 3.3) and scales it up
// by the R1/R2 divider ratio:
//
//   Tvoltage = (adc_avg/1023 * 3.3) / (R2 / (R1 + R2))
//            = (adc_avg/1023 * 3.3) * (R1 + R2) / R2
//
// Tankie's divider is R1 = 330 kOhm, R2 = 33 kOhm -> a 11x multiplier
// ((330k + 33k) / 33k). Note: the code scales by (R1+R2)/R2, i.e. it treats
// the ADC reading as the voltage across R2 and adds the R1 drop on top.
// The tests lock in the CURRENT behavior of the firmware code, whatever
// that is - if the math is ever "fixed", these tests must be updated.
#include "test_main.h"
#include <Arduino.h>
#include "config.h"
#include "batt.h"

// The divider the tank actually uses.
static const float R1 = 330000.0f;
static const float R2 = 33000.0f;

static void resetAdc(int value) {
  for (int i = 0; i < 64; i++) host_adc[i] = 0;
  host_adc_reads = 0;
  host_set_adc(BAT, value);
}

TEST(batt_zero_adc_is_zero_volts) {
  resetAdc(0);
  CHECK_NEAR(getBatVoltage(R1, R2), 0.0, 0.001);
}

TEST(batt_full_scale_adc) {
  // 1023/1023 * 3.3 = 3.3 V on the rail, * 11 (divider) = 36.3 V.
  resetAdc(1023);
  CHECK_NEAR(getBatVoltage(R1, R2), 36.3, 0.01);
}

TEST(batt_half_scale_adc) {
  // 511/1023 * 3.3 = 1.64839... V on the rail, * 11 = 18.13226... V.
  resetAdc(511);
  CHECK_NEAR(getBatVoltage(R1, R2), 18.1323, 0.01);
}

TEST(batt_typical_charged_pack) {
  // A 12 V pack (6x 2.0 V cells) puts 12/11 = 1.0909... V on the rail ->
  // adc ~= 1.0909/3.3*1023 = 337.8, use 338.
  resetAdc(338);
  CHECK_NEAR(getBatVoltage(R1, R2), 11.9935, 0.01);
}

TEST(batt_averages_ten_samples) {
  // The function must take exactly 10 samples (the average is /10.0).
  resetAdc(500);
  host_adc_reads = 0;
  getBatVoltage(R1, R2);
  CHECK_EQ_INT(host_adc_reads, 10);
}

TEST(batt_reads_the_bat_pin) {
  // The samples must come from the BAT pin (A0), not some other pin.
  resetAdc(0);
  host_set_adc(0, 999);  // a different pin
  host_adc_reads = 0;
  float v = getBatVoltage(R1, R2);
  CHECK_NEAR(v, 0.0, 0.001);  // still zero -> BAT pin was read, not pin 0
}

TEST(batt_custom_divider_ratio) {
  // With R1 == R2 the ratio is 2, not 10: 511/1023*3.3*2 = 3.299.
  resetAdc(511);
  CHECK_NEAR(getBatVoltage(1000.0f, 1000.0f), 3.299, 0.01);
}
