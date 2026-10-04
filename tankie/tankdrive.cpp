#include "tankdrive.h"
#include "wheels.h"

TankDrive::TankDrive(Motor *_mLeft, Motor *_mRight)
{
  this->speed = 0;
  this->steer = 0;
  this->mLeft = _mLeft;
  this->mRight = _mRight;
}

TankDrive::~TankDrive()
{
}

void TankDrive::setSteer(int _steer) // -255...255
{
  this->steer = _steer;
  D_TD("steer: ");
  D_TDLN(this->steer);
  updateMotors();
}

void TankDrive::setSpeed(int _speed) // -255...255
{
  this->speed = _speed;
  D_TD("speed: ");
  D_TDLN(this->speed);
  updateMotors();
}

void TankDrive::updateMotors()
{
  // The wheel math is the pure, host-testable computeWheels() (issue #14):
  // 2-line differential formula, outputs clamped to [-255, 255], and
  // spin-in-place support at speed == 0.
  WheelOutputs out = computeWheels(this->speed, this->steer);
  D_TD("[L] ");
  D_TDLN(out.left);
  D_TD("[R] ");
  D_TDLN(out.right);
  this->mLeft->drive(out.left);
  this->mRight->drive(out.right);
}
