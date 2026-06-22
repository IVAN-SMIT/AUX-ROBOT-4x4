#ifndef BOBR4X4_H
#define BOBR4X4_H

#include "stdint.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

typedef enum {
    MOTOR_NOT_INIT = 0,
    MOTOR_WORK,
    MOTOR_LOW_VOLTAGE,
    VALUE_NOT_SET,
    DEVICE_CHARGE,
    MOTOR_ERROR = 0xff
} motor_state;

void start_motor_task(void *arg);
motor_state set_number_of_motor(uint8_t number);
motor_state motor_set_voltage(float *voltage);
motor_state motor_set_pwm(int16_t *pwm);
motor_state parse_data_from(uint8_t *pData);

float get_current_voltage(void);
float get_c_e(void);
uint8_t get_motor1_dir(void);
uint8_t get_motor2_dir(void);
uint8_t get_motor3_dir(void);
uint8_t get_motor4_dir(void);
float get_voltage_dead_zone(void);
uint8_t get_number_of_motor(void);
uint8_t check_config_update(void);
motor_state get_motor_state(void);

#endif