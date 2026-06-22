"""
Центральная конфигурация проекта BOBR 4x4.
Содержит только те параметры, которые используются в текущей версии (v12.1).
"""

import math

# ==================== Сеть ====================
HOST = "192.168.4.1"
PORT = 8888
PROXY_HOST = "0.0.0.0"
PROXY_PORT = 8000
TELEMETRY_INTERVAL = 0.5      # секунд между запросами телеметрии

# ==================== Параметры ESP32 ====================
MAX_PWM = 1023
MIN_PWM = 300                 # минимальный PWM для трогания с места

# ==================== Моторы ====================
MOTOR_REMAP = [3, 2, 1, 0]               # клиентские индексы → физические моторы
MOTOR_INVERT = [False, True, True, False] # инвертировать направление (до ремапа)

# ==================== Камера ====================
CAMERA_PARAMS = {
    "source": 1,              # 0 – встроенная, 1 – внешняя USB
    "fps": 30,
    "resolution": (640, 480),
}

# ==================== Детекция маркера ====================
DETECTION_PARAMS = {
    "color_lower": [25, 107, 110],
    "color_upper": [85, 255, 255],
    "min_area": 80,
    "min_triangle_ratio": 1.10,
    "morph_kernel": 3,
    "alpha_pos": 0.40,        # сглаживание позиции
    "alpha_theta": 0.30,      # сглаживание угла
    "alpha_dir": 0.5,         # сглаживание направления (вектор носа)
    "motion_lock_px": 6.0,    # порог движения для анти-флипа носа
}

# ==================== Планировщик ====================
PLANNER_PARAMS = {
    "dt": 0.05,               # шаг управления (20 Гц)
}

# ==================== Безопасность ====================
SAFETY = {
    "pose_timeout": 0.5,      # секунд без обновления позы → остановка
}

# ==================== Геометрия и радиусы ====================
MARKER_OFFSET_PX = (0, 0)     # смещение метки от центра вращения (dx, dy)
ARRIVAL_RADIUS_PX = 30        # радиус "точка достигнута" (пиксели)
SLOWDOWN_RADIUS_PX = 80       # начало плавного торможения (пиксели)


# ------------------- Настройки регулятора Turn-and-Go (начальные значения) -------------------
TURN_SPEED = 0.4
TURN_ACCURACY_DEG = 5.0
KP_TURN = 2.0                #не имеет ползунка, только константа
DRIVE_CORRECTION = 0.15
DRIVE_CORRECTION_THRESH = 5.0


# ==================== Вспомогательные функции ====================
def apply_motor_mapping(pwm_client: list) -> list:
    """Применяет маппинг и инверсию моторов."""
    remapped = [0, 0, 0, 0]
    for client_idx in range(4):
        phys_idx = MOTOR_REMAP[client_idx]
        pwm = pwm_client[client_idx]
        if MOTOR_INVERT[client_idx]:
            pwm = -pwm
        remapped[phys_idx] = pwm
    return remapped

def clamp_pwm(value: int) -> int:
    """Ограничение PWM в диапазоне [-MAX_PWM, MAX_PWM]."""
    return max(-MAX_PWM, min(MAX_PWM, value))

def normalize_angle(angle: float) -> float:
    """Нормализация угла в [-π, π]."""
    return (angle + math.pi) % (2 * math.pi) - math.pi

def angle_difference(target: float, current: float) -> float:
    """Кратчайшая разница между двумя углами."""
    return normalize_angle(target - current)