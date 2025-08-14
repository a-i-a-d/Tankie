#ifndef _TANKDRIVE_H_
#define _TANKDRIVE_H_

//#define DEBUG true

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
//#include "m8833.h"
#include "SparkFun_TB6612.h"

class TankDrive
{
  public:
    //TankDrive(M8833 *_mLeft, M8833 *_mRight);
    TankDrive(Motor *_mLeft, Motor *_mRight);
    ~TankDrive();
    void setSpeed(int speed);
    void setSteer(int _steer);

  private:
    int speed;
    int steer;
    int r_steer;
    void adjust();
    void updateMotors();
    void updateRelativeSteer();
    //M8833 *mLeft = nullptr;
    //M8833 *mRight = nullptr;
    Motor *mLeft = nullptr;
    Motor *mRight = nullptr;
};

#endif