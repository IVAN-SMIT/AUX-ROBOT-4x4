#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_system.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "nvs_flash.h"
#include "lwip/sockets.h"
#include "driver/gpio.h"
#include "esp_adc/adc_oneshot.h"
#include "driver/ledc.h"
#include <string.h>
#include <math.h>

#define TAG "BOBR"

// ==================== Конфигурация пинов ====================
#define M1_PWM      GPIO_NUM_14
#define M1_DIR      GPIO_NUM_27
#define M2_DIR      GPIO_NUM_26
#define M2_PWM      GPIO_NUM_25
#define STBY1       GPIO_NUM_23

#define M3_DIR      GPIO_NUM_16
#define M3_PWM      GPIO_NUM_17
#define M4_DIR      GPIO_NUM_18
#define M4_PWM      GPIO_NUM_19
#define STBY2       GPIO_NUM_21

#define ADC_PIN     GPIO_NUM_34
#define ADC_CHANNEL ADC_CHANNEL_6

// ==================== Параметры ШИМ ====================
#define LEDC_TIMER      LEDC_TIMER_0
#define LEDC_MODE       LEDC_LOW_SPEED_MODE
#define LEDC_DUTY_RES   LEDC_TIMER_10_BIT
#define LEDC_FREQ       20000
#define MAX_DUTY        ((1 << LEDC_DUTY_RES) - 1)  // 1023

#define TCP_PORT        8888
#define MAX_VOLTAGE     8.4f

// ==================== Структура мотора ====================
typedef struct {
    gpio_num_t pwm_pin;
    gpio_num_t dir_pin;
    ledc_channel_t pwm_channel;   // Основной PWM канал
    ledc_channel_t dir_channel;   // PWM канал для DIR пина (для реверса)
    const char *name;
} motor_t;

// Каналы: 0-3 = PWM основные, 4-7 = DIR для реверса
static motor_t motors[4] = {
    {M1_PWM, M1_DIR, LEDC_CHANNEL_0, LEDC_CHANNEL_4, "M1"},
    {M2_PWM, M2_DIR, LEDC_CHANNEL_1, LEDC_CHANNEL_5, "M2"},
    {M3_PWM, M3_DIR, LEDC_CHANNEL_2, LEDC_CHANNEL_6, "M3"},
    {M4_PWM, M4_DIR, LEDC_CHANNEL_3, LEDC_CHANNEL_7, "M4"},
};

static float battery_voltage = 7.4f;
static int16_t motor_pwm[4] = {0, 0, 0, 0};
static uint8_t num_motors = 4;

// ==================== Инициализация драйверов ====================
static void init_drivers(void) {
    gpio_reset_pin(STBY1);
    gpio_set_direction(STBY1, GPIO_MODE_OUTPUT);
    gpio_set_level(STBY1, 1);
    
    gpio_reset_pin(STBY2);
    gpio_set_direction(STBY2, GPIO_MODE_OUTPUT);
    gpio_set_level(STBY2, 1);
    
    gpio_num_t dir_pins[] = {M1_DIR, M2_DIR, M3_DIR, M4_DIR};
    for (int i = 0; i < 4; i++) {
        gpio_reset_pin(dir_pins[i]);
        gpio_set_direction(dir_pins[i], GPIO_MODE_OUTPUT);
        gpio_set_level(dir_pins[i], 0);
    }
    
    ESP_LOGI(TAG, "Drivers initialized");
}

// ==================== Инициализация ШИМ ====================
static void init_pwm(void) {
    ledc_timer_config_t tmr = {
        .speed_mode = LEDC_MODE,
        .duty_resolution = LEDC_DUTY_RES,
        .timer_num = LEDC_TIMER,
        .freq_hz = LEDC_FREQ,
        .clk_cfg = LEDC_AUTO_CLK
    };
    ESP_ERROR_CHECK(ledc_timer_config(&tmr));
    
    // Настраиваем 8 каналов: 0-3 для PWM пинов, 4-7 для DIR пинов
    for (int i = 0; i < 4; i++) {
        // Основной PWM канал
        ledc_channel_config_t ch_pwm = {
            .gpio_num = motors[i].pwm_pin,
            .speed_mode = LEDC_MODE,
            .channel = motors[i].pwm_channel,
            .timer_sel = LEDC_TIMER,
            .intr_type = LEDC_INTR_DISABLE,
            .duty = 0,
            .hpoint = 0
        };
        ESP_ERROR_CHECK(ledc_channel_config(&ch_pwm));
        
        // DIR канал (для реверса)
        ledc_channel_config_t ch_dir = {
            .gpio_num = motors[i].dir_pin,
            .speed_mode = LEDC_MODE,
            .channel = motors[i].dir_channel,
            .timer_sel = LEDC_TIMER,
            .intr_type = LEDC_INTR_DISABLE,
            .duty = 0,
            .hpoint = 0
        };
        ESP_ERROR_CHECK(ledc_channel_config(&ch_dir));
        
        ESP_LOGI(TAG, "Motor %d (%s): PWM=GPIO%d (ch%d), DIR=GPIO%d (ch%d)", 
                 i, motors[i].name, 
                 motors[i].pwm_pin, motors[i].pwm_channel,
                 motors[i].dir_pin, motors[i].dir_channel);
    }
    
    ESP_LOGI(TAG, "PWM ready, MAX_DUTY=%d", MAX_DUTY);
}

// ==================== Применение ШИМ (с реверсом!) ====================
static void apply_pwm(void) {
    for (int i = 0; i < 4; i++) {
        if (i >= num_motors) {
            ledc_set_duty(LEDC_MODE, motors[i].pwm_channel, 0);
            ledc_update_duty(LEDC_MODE, motors[i].pwm_channel);
            ledc_set_duty(LEDC_MODE, motors[i].dir_channel, 0);
            ledc_update_duty(LEDC_MODE, motors[i].dir_channel);
            continue;
        }
        
        int16_t pwm = motor_pwm[i];
        
        if (pwm > 0) {
            // Вперёд: PWM=ШИМ, DIR=HIGH
            if (pwm > MAX_DUTY) pwm = MAX_DUTY;
            ledc_set_duty(LEDC_MODE, motors[i].pwm_channel, pwm);
            ledc_set_duty(LEDC_MODE, motors[i].dir_channel, MAX_DUTY);  // DIR=HIGH
        } else if (pwm < 0) {
            // Назад: PWM=HIGH, DIR=ШИМ (меняем роли!)
            int16_t abs_pwm = -pwm;
            if (abs_pwm > MAX_DUTY) abs_pwm = MAX_DUTY;
            ledc_set_duty(LEDC_MODE, motors[i].pwm_channel, MAX_DUTY);  // PWM=HIGH
            ledc_set_duty(LEDC_MODE, motors[i].dir_channel, abs_pwm);   // DIR=ШИМ
        } else {
            // Стоп: оба LOW
            ledc_set_duty(LEDC_MODE, motors[i].pwm_channel, 0);
            ledc_set_duty(LEDC_MODE, motors[i].dir_channel, 0);
        }
        
        ledc_update_duty(LEDC_MODE, motors[i].pwm_channel);
        ledc_update_duty(LEDC_MODE, motors[i].dir_channel);
    }
}

// ==================== Измерение напряжения ====================
static void adc_task(void *arg) {
    adc_oneshot_unit_handle_t adc_handle;
    adc_oneshot_unit_init_cfg_t init_cfg = {
        .unit_id = ADC_UNIT_1,
        .clk_src = ADC_RTC_CLK_SRC_DEFAULT,
        .ulp_mode = ADC_ULP_MODE_DISABLE,
    };
    ESP_ERROR_CHECK(adc_oneshot_new_unit(&init_cfg, &adc_handle));
    
    adc_oneshot_chan_cfg_t chan_cfg = {
        .atten = ADC_ATTEN_DB_11,
        .bitwidth = ADC_BITWIDTH_12,
    };
    ESP_ERROR_CHECK(adc_oneshot_config_channel(adc_handle, ADC_CHANNEL, &chan_cfg));

    ESP_LOGI(TAG, "ADC initialized on GPIO34");

    while (1) {
        int sum = 0;
        for (int i = 0; i < 16; i++) {
            int raw;
            adc_oneshot_read(adc_handle, ADC_CHANNEL, &raw);
            sum += raw;
            vTaskDelay(pdMS_TO_TICKS(1));
        }
        int avg = sum / 16;
        
        float pin_voltage = (float)avg * 3.3f / 4095.0f;
        battery_voltage = pin_voltage * 2.0f;
        
        static uint32_t last_log = 0;
        uint32_t now = xTaskGetTickCount() * portTICK_PERIOD_MS;
        if (now - last_log > 5000) {
            ESP_LOGI(TAG, "ADC: raw=%d, pin=%.3fV, battery=%.2fV", avg, pin_voltage, battery_voltage);
            last_log = now;
        }
        
        vTaskDelay(pdMS_TO_TICKS(500));
    }
    vTaskDelete(NULL);
}

// ==================== Задача моторов ====================
static void motor_task(void *arg) {
    init_drivers();
    init_pwm();
    
    ESP_LOGI(TAG, "Motor task started");
    
    while (1) {
        apply_pwm();
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    vTaskDelete(NULL);
}

// ==================== WiFi ====================
static void wifi_init_ap(void) {
    wifi_config_t ap_cfg = {
        .ap = {
            .ssid = "BOBR_4x4",
            .ssid_len = 8,
            .password = "12345678",
            .max_connection = 4,
            .authmode = WIFI_AUTH_WPA2_PSK
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap_cfg));
    ESP_LOGI(TAG, "WiFi: BOBR_4x4 / 12345678");
}

// ==================== Парсер команд ====================
static void parse_cmd(const char *cmd) {
    if (!cmd || cmd[0] == '\0') return;
    
    float vals[10];
    int cnt = 0;
    char *tok = strtok((char*)cmd, ",: ");
    while (tok && cnt < 10) {
        char *ep;
        float f = strtof(tok, &ep);
        if (ep != tok) vals[cnt++] = f;
        tok = strtok(NULL, ",: ");
    }
    if (!cnt) return;
    
    switch (cmd[0]) {
        case 'v':
            for (int i = 0; i < cnt && i < 4; i++) {
                float dc = vals[i] / battery_voltage;
                if (dc > 1.0f) dc = 1.0f;
                else if (dc < -1.0f) dc = -1.0f;
                motor_pwm[i] = (int16_t)(MAX_DUTY * dc);
            }
            break;
            
        case 'm':
            for (int i = 0; i < cnt && i < 4; i++) {
                motor_pwm[i] = (int16_t)vals[i];
                if (motor_pwm[i] > MAX_DUTY) motor_pwm[i] = MAX_DUTY;
                if (motor_pwm[i] < -MAX_DUTY) motor_pwm[i] = -MAX_DUTY;
            }
            ESP_LOGI(TAG, "PWM: [%d, %d, %d, %d]", 
                     motor_pwm[0], motor_pwm[1], motor_pwm[2], motor_pwm[3]);
            break;
            
        case 'N':
            if (cnt >= 1 && vals[0] >= 1 && vals[0] <= 4) {
                num_motors = (uint8_t)vals[0];
                ESP_LOGI(TAG, "Motors: %d", num_motors);
            }
            break;
            
        case 'S':
            if (cnt >= 1) gpio_set_level(STBY1, vals[0] > 0.5f ? 1 : 0);
            if (cnt >= 2) gpio_set_level(STBY2, vals[1] > 0.5f ? 1 : 0);
            break;
            
        case 's':
            gpio_set_level(STBY1, 0);
            gpio_set_level(STBY2, 0);
            break;
            
        case 'w':
            gpio_set_level(STBY1, 1);
            gpio_set_level(STBY2, 1);
            break;
    }
}

// ==================== TCP сервер ====================
static void tcp_server_task(void *arg) {
    char buf[128];
    struct sockaddr_in addr = {
        .sin_family = AF_INET,
        .sin_port = htons(TCP_PORT),
        .sin_addr.s_addr = htonl(INADDR_ANY)
    };
    
    int ls = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (ls < 0) { vTaskDelete(NULL); return; }
    
    int opt = 1;
    setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
    
    if (bind(ls, (struct sockaddr*)&addr, sizeof(addr)) < 0 || listen(ls, 1) < 0) {
        ESP_LOGE(TAG, "TCP bind/listen failed");
        close(ls);
        vTaskDelete(NULL);
        return;
    }
    
    ESP_LOGI(TAG, "TCP: 192.168.4.1:%d", TCP_PORT);
    
    while (1) {
        int cs = accept(ls, NULL, NULL);
        
        if (cs > 0) {
            ESP_LOGI(TAG, "Client connected");
            
            struct timeval tv = { .tv_sec = 5, .tv_usec = 0 };
            setsockopt(cs, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
            
            while (1) {
                int len = recv(cs, buf, sizeof(buf) - 1, 0);
                if (len <= 0) break;
                
                buf[len] = 0;
                while (len > 0 && (buf[len-1] == '\n' || buf[len-1] == '\r')) {
                    buf[--len] = 0;
                }
                
                if (buf[0] == '?') {
                    char resp[100];
                    snprintf(resp, sizeof(resp), 
                             "V:%.2f|N:%d|PWM:%d,%d,%d,%d\n",
                             battery_voltage, num_motors,
                             motor_pwm[0], motor_pwm[1], 
                             motor_pwm[2], motor_pwm[3]);
                    send(cs, resp, strlen(resp), 0);
                } else {
                    parse_cmd(buf);
                    send(cs, "OK\n", 3, 0);
                }
            }
            
            ESP_LOGI(TAG, "Client disconnected");
            close(cs);
            memset(motor_pwm, 0, sizeof(motor_pwm));
        }
    }
    
    vTaskDelete(NULL);
}

// ==================== Main ====================
void app_main(void) {
    ESP_LOGI(TAG, "=== BOBR 4x4 Starting ===");
    
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        nvs_flash_init();
    }
    
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_ap();
    
    wifi_init_config_t wifi_cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&wifi_cfg));
    wifi_init_ap();
    ESP_ERROR_CHECK(esp_wifi_start());
    
    xTaskCreate(adc_task, "adc", 2048, NULL, 3, NULL);
    xTaskCreate(motor_task, "motor", 3072, NULL, 5, NULL);
    xTaskCreate(tcp_server_task, "tcp", 3584, NULL, 4, NULL);
    
    ESP_LOGI(TAG, "Ready! WiFi: BOBR_4x4 / 12345678 | TCP: %d", TCP_PORT);
}