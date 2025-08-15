#include "tankdrive.h"

//TankDrive::TankDrive(M8833 *_mLeft, M8833 *_mRight)
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
  updateRelativeSteer();
  updateMotors();
}

void TankDrive::updateRelativeSteer()
{
  this->r_steer = int(this->steer * (float(abs(this->speed)) / 127));
  //this->r_steer = this->steer;
  D_TD("relative steer: ");
  D_TDLN(this->r_steer);
}

void TankDrive::setSpeed(int _speed) // -255...255
{
  this->speed = _speed;
  D_TD("speed: ");
  D_TDLN(this->speed);
  updateRelativeSteer();
  updateMotors();
}

void TankDrive::updateMotors()
{
  // stop
  if (this->speed == 0){
    D_TD("[stop] L: ");
    D_TDLN("0");
    D_TD("[stop] R: ");
    D_TDLN("0");
    //this->mLeft->setSpeed(0);
    //this->mRight->setSpeed(0);
    this->mLeft->drive(0);
    this->mRight->drive(0);
  }
  // fw straight and left
  else if (this->r_steer >= 0 && this->speed > 0){
    D_TD("[fw straight/left] L: ");
    D_TDLN(this->speed - this->r_steer);
    D_TD("[fw straight/left] R: ");
    D_TDLN(this->speed);
    //this->mLeft->setSpeed(this->speed - this->r_steer);
    //this->mRight->setSpeed(this->speed);
    this->mLeft->drive(this->speed - this->r_steer);
    this->mRight->drive(this->speed);
  }
  // fw right
  else if (this->r_steer < 0 && this->speed > 0){
    D_TD("[fw right] L: ");
    D_TDLN(this->speed);
    D_TD("[fw right] R: ");
    D_TDLN(this->speed + this->r_steer);
    //this->mLeft->setSpeed(this->speed);
    //this->mRight->setSpeed(this->speed + this->r_steer);
    this->mLeft->drive(this->speed);
    this->mRight->drive(this->speed + this->r_steer);
  }
  //bw straigt and left
  else if (this->r_steer >= 0 && this->speed < 0){
    D_TD("[bw straight/left] L: ");
    D_TDLN(this->speed + this->r_steer);
    D_TD("[bw straight/left] R: ");
    D_TDLN(this->speed);
    //this->mLeft->setSpeed(this->speed + this->r_steer);
    //this->mRight->setSpeed(this->speed);
    this->mLeft->drive(this->speed + this->r_steer);
    this->mRight->drive(this->speed);
  }
  // bw right
  else if (this->r_steer < 0 && this->speed < 0){
    D_TD("[bw right] L: ");
    D_TDLN(this->speed);
    D_TD("[bw right] R: ");
    D_TDLN(this->speed - this->r_steer);
    //this->mLeft->setSpeed(this->speed);
    //this->mRight->setSpeed(this->speed - this->r_steer);
    this->mLeft->drive(this->speed);
    this->mRight->drive(this->speed - this->r_steer);
  }
}