#ifndef _TANKDRIVE_H_
#define _TANKDRIVE_H_


#ifdef DEBUG
#define D_TD(x) Serial.print("[TANKDRIVE] "); Serial.print(x)
#define D_TDDEC(x) Serial.print("[TANKDRIVE] "); Serial.print(x, DEC)
#define D_TDLN(x) Serial.print("[TANKDRIVE] "); Serial.println(x)
#else
#define D_TD(x)
#define D_TDDEC(x)
#define D_TDLN(x)
#endif

#include "Arduino.h"
#include "SparkFun_TB6612.h"

// TankDrive: turns a (speed, steer) command into per-motor drive outputs.
// The wheel math itself lives in the pure, Arduino-free tankie/wheels.cpp
// (issue #14) so it is unit-testable on the host; this class only stores
// the command and delegates to computeWheels().
class TankDrive
{
  public:
    TankDrive(Motor *_mLeft, Motor *_mRight);
    ~TankDrive();
    void setSpeed(int speed);
    void setSteer(int _steer);

  private:
    int speed;
    int steer;
    void updateMotors();
    Motor *mLeft = nullptr;
    Motor *mRight = nullptr;
};

#endif
