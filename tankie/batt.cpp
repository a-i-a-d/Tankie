#include "batt.h"

#include "Arduino.h"
#include "config.h"

// The timestamp of the last ADC sample (used to pace the sampling loop).
// File-local: only the battery code touches it.
static long readTimer = 0;

float getBatVoltage(float R1, float R2)
{
  float Tvoltage=0.0;
  float Vvalue=0.0,Rvalue=0.0;

  for(unsigned int i=0;i<10;i++)
  {
    readTimer = millis();
    Vvalue=Vvalue+analogRead(BAT);         //Read analog Voltage
    delay(1);
  }
  Vvalue=(float)Vvalue/10.0;
  Rvalue = (Vvalue * 3.3) / 1023.0;
  Tvoltage = Rvalue / (R2/(R1+R2));
  return(Tvoltage);
}
