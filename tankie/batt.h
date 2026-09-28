#ifndef _TANKIE_BATT_H_
#define _TANKIE_BATT_H_

// Battery voltage measurement (extracted from tankie.ino so it can be
// unit-tested on the host - issue #5).
//
// The ESP8266 ADC reads the battery pack through a R1/R2 voltage divider
// (R1 = 330 kOhm, R2 = 33 kOhm on Tankie). getBatVoltage() averages 10 ADC
// samples and converts them back to the pack voltage. The math is
// byte-for-byte the code that used to live in tankie.ino.
float getBatVoltage(float R1, float R2);

#endif
