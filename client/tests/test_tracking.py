"""
test_tracking.py — Интерактивный инструмент для настройки параметров трекинга.
ОПРЕДЕЛЯЕТ НОС по самой острой вершине треугольника (геометрия 85-85-65).

Позволяет:
  - Настраивать HSV-диапазон через трекбары
  - Настраивать параметры детекции (min_area, min_ratio, morph_kernel)
  - Настраивать сглаживание (alpha_pos, alpha_theta)
  - Видеть результат в реальном времени
  - Сохранять параметры в config.py одной кнопкой
  - ОПРЕДЕЛЯЕТ НОС по геометрии треугольника (самая острая вершина)

Управление:
  - Трекбары в окне "Controls" — настройка параметров
  - Клавиша 's' — сохранить параметры в config.py
  - Клавиша 'q' — выход
  - Клавиша 'r' — сброс к заводским настройкам
  - Клавиша 'c' — переключение цветовой маски (HSV/BGR)
  - Клавиша 'm' — переключение режима отладки (маска/контуры/результат)
  - Клавиша 'f' — ручной флип направления (если нос определился неверно)

Зависимости:
  pip install opencv-python numpy
"""

import cv2
import numpy as np
import os
import json
from datetime import datetime

# ==================== Конфигурация по умолчанию ====================

DEFAULT_CONFIG = {
    "color_lower": [25, 107, 110],
    "color_upper": [85, 255, 255],
    "min_area": 150,
    "min_triangle_ratio": 1.15,
    "morph_kernel": 3,
    "alpha_pos": 0.4,
    "alpha_theta": 0.3,
}

# Путь к config.py
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.py")

# Загружаем текущие параметры из config.py (если есть)
try:
    from config import DETECTION_PARAMS
    current_config = DEFAULT_CONFIG.copy()
    for key in current_config:
        if key in DETECTION_PARAMS:
            current_config[key] = DETECTION_PARAMS[key]
    print("[OK] Параметры загружены из config.py")
except ImportError:
    current_config = DEFAULT_CONFIG.copy()
    print("[!] config.py не найден, используются значения по умолчанию")


# ==================== Класс для отладки ====================

class TrackingDebugger:
    """Интерактивный отладчик параметров трекинга с определением НОСА."""
    
    def __init__(self, camera_id=1):
        self.camera_id = camera_id
        self.config = current_config.copy()
        
        # Открываем камеру
        self.cap = cv2.VideoCapture(camera_id)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        
        if not self.cap.isOpened():
            raise RuntimeError(f"Не удалось открыть камеру #{camera_id}")
        
        # Режимы отладки
        self.debug_mode = "result"  # "mask", "contours", "result"
        self.show_color_mask = True
        
        # Ручной флип направления
        self.manual_flip = False
        
        # Для сглаживания
        self._smooth_cx = None
        self._smooth_cy = None
        self._smooth_theta = None
        self._prev_theta = None
        self._prev_cx = None
        self._prev_cy = None
        
        # Статистика
        self._frame_count = 0
        self._detection_count = 0
        self._fps = 0.0
        self._fps_timer = cv2.getTickCount()
        
        # Создаём окна
        cv2.namedWindow("Controls", cv2.WINDOW_NORMAL)
        cv2.resizeWindow("Controls", 600, 400)
        cv2.namedWindow("Debug", cv2.WINDOW_NORMAL)
        cv2.resizeWindow("Debug", 800, 450)
        
        # Создаём трекбары
        self._create_trackbars()
        
        print("\n" + "=" * 60)
        print("  Инструмент настройки параметров трекинга")
        print("  NOSE DETECTION: самая острая вершина треугольника")
        print("=" * 60)
        print("  Трекбары — в окне 'Controls'")
        print("  s — сохранить в config.py")
        print("  r — сброс к заводским")
        print("  c — показать/скрыть маску")
        print("  m — режим отладки (mask/contours/result)")
        print("  f — ручной флип направления (если нос неверно)")
        print("  q — выход")
        print("=" * 60)
    
    def _create_trackbars(self):
        """Создаёт трекбары для настройки параметров."""
        # HSV нижняя граница
        cv2.createTrackbar("H Low", "Controls", self.config["color_lower"][0], 180, self._on_trackbar)
        cv2.createTrackbar("S Low", "Controls", self.config["color_lower"][1], 255, self._on_trackbar)
        cv2.createTrackbar("V Low", "Controls", self.config["color_lower"][2], 255, self._on_trackbar)
        
        # HSV верхняя граница
        cv2.createTrackbar("H High", "Controls", self.config["color_upper"][0], 180, self._on_trackbar)
        cv2.createTrackbar("S High", "Controls", self.config["color_upper"][1], 255, self._on_trackbar)
        cv2.createTrackbar("V High", "Controls", self.config["color_upper"][2], 255, self._on_trackbar)
        
        # Параметры детекции
        cv2.createTrackbar("Min Area", "Controls", self.config["min_area"], 2000, self._on_trackbar)
        cv2.createTrackbar("Min Ratio x10", "Controls", int(self.config["min_triangle_ratio"] * 10), 50, self._on_trackbar)
        cv2.createTrackbar("Morph Kernel", "Controls", self.config["morph_kernel"], 15, self._on_trackbar)
        
        # Параметры сглаживания
        cv2.createTrackbar("Alpha Pos x100", "Controls", int(self.config["alpha_pos"] * 100), 100, self._on_trackbar)
        cv2.createTrackbar("Alpha Theta x100", "Controls", int(self.config["alpha_theta"] * 100), 100, self._on_trackbar)
    
    def _on_trackbar(self, value):
        """Callback при изменении трекбара (обновляет config)."""
        self.config["color_lower"] = [
            cv2.getTrackbarPos("H Low", "Controls"),
            cv2.getTrackbarPos("S Low", "Controls"),
            cv2.getTrackbarPos("V Low", "Controls"),
        ]
        self.config["color_upper"] = [
            cv2.getTrackbarPos("H High", "Controls"),
            cv2.getTrackbarPos("S High", "Controls"),
            cv2.getTrackbarPos("V High", "Controls"),
        ]
        self.config["min_area"] = cv2.getTrackbarPos("Min Area", "Controls")
        self.config["min_triangle_ratio"] = cv2.getTrackbarPos("Min Ratio x10", "Controls") / 10.0
        self.config["morph_kernel"] = max(1, cv2.getTrackbarPos("Morph Kernel", "Controls"))
        self.config["alpha_pos"] = cv2.getTrackbarPos("Alpha Pos x100", "Controls") / 100.0
        self.config["alpha_theta"] = cv2.getTrackbarPos("Alpha Theta x100", "Controls") / 100.0
    
    def _find_triangle_vertices(self, contour):
        """
        Находит 3 вершины треугольника.
        Возвращает (nose, base1, base2) где nose — самая острая вершина.
        """
        # Аппроксимируем контур до многоугольника
        epsilon = 0.03 * cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, epsilon, True)
        
        if len(approx) < 3:
            # Если не получилось — берём extreme points
            M = cv2.moments(contour)
            if M["m00"] == 0:
                return None
            cx = M["m10"] / M["m00"]
            cy = M["m01"] / M["m00"]
            
            pts = contour.reshape(-1, 2)
            
            # Самая удалённая от центра
            dists = np.hypot(pts[:, 0] - cx, pts[:, 1] - cy)
            idx1 = np.argmax(dists)
            p1 = pts[idx1]
            
            # Самая удалённая от p1
            dists = np.hypot(pts[:, 0] - p1[0], pts[:, 1] - p1[1])
            idx2 = np.argmax(dists)
            p2 = pts[idx2]
            
            # Самая удалённая от линии p1-p2
            max_dist = 0
            p3 = pts[0]
            for pt in pts:
                d = abs((p2[0]-p1[0])*(p1[1]-pt[1]) - (p1[0]-pt[0])*(p2[1]-p1[1]))
                d /= np.hypot(p2[0]-p1[0], p2[1]-p1[1]) + 1e-6
                if d > max_dist:
                    max_dist = d
                    p3 = pt
            
            vertices = np.array([p1, p2, p3], dtype=np.float32)
        else:
            vertices = approx.reshape(-1, 2).astype(np.float32)
        
        if len(vertices) != 3:
            return None
        
        # Вычисляем длины сторон
        sides = []
        for i in range(3):
            p1 = vertices[i]
            p2 = vertices[(i + 1) % 3]
            length = np.hypot(p2[0] - p1[0], p2[1] - p1[1])
            sides.append((length, i, (i + 1) % 3))
        
        # Самая короткая сторона = основание (65 мм)
        sides.sort(key=lambda x: x[0])
        short_side = sides[0]
        
        # Вершина НАПРОТИВ короткой стороны = НОС
        # Индексы: 0,1,2. Короткая сторона соединяет вершины A и B.
        # Вершина C (не A и не B) = нос.
        used_vertices = {short_side[1], short_side[2]}
        nose_idx = ({0, 1, 2} - used_vertices).pop()
        
        nose = vertices[nose_idx]
        base1 = vertices[short_side[1]]
        base2 = vertices[short_side[2]]
        
        return nose, base1, base2
    
    def _detect(self, frame):
        """Детекция зелёного треугольника с определением НОСА."""
        hsv_lower = np.array(self.config["color_lower"], dtype=np.uint8)
        hsv_upper = np.array(self.config["color_upper"], dtype=np.uint8)
        min_area = self.config["min_area"]
        min_ratio = self.config["min_triangle_ratio"]
        kernel_size = self.config["morph_kernel"]
        
        kernel_erode = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
        kernel_dilate = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size + 2, kernel_size + 2))
        
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, hsv_lower, hsv_upper)
        
        # Расширенный диапазон при плохом освещении
        if cv2.countNonZero(mask) < 200:
            lower = np.array([20, 80, 80], dtype=np.uint8)
            upper = np.array([90, 255, 255], dtype=np.uint8)
            mask = cv2.inRange(hsv, lower, upper)
        
        mask = cv2.erode(mask, kernel_erode, iterations=1)
        mask = cv2.dilate(mask, kernel_dilate, iterations=2)
        
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        if not contours:
            return None, mask
        
        # Лучший контур
        best = None
        best_area = 0
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < min_area:
                continue
            x, y, w, h = cv2.boundingRect(cnt)
            ratio = max(w, h) / (min(w, h) + 1e-6)
            if ratio < min_ratio:
                continue
            if area > best_area:
                best_area = area
                best = cnt
        
        if best is None:
            return None, mask
        
        # Центр масс
        M = cv2.moments(best)
        if M["m00"] == 0:
            return None, mask
        
        cx = M["m10"] / M["m00"]
        cy = M["m01"] / M["m00"]
        
        # Находим вершины треугольника
        vertices = self._find_triangle_vertices(best)
        
        if vertices is not None:
            nose, base1, base2 = vertices
            
            # Направление: от центра основания к носу
            base_center = (base1 + base2) / 2
            theta = np.arctan2(nose[1] - base_center[1], nose[0] - base_center[0])
            
            # Ручной флип
            if self.manual_flip:
                theta += np.pi
            
            # Проверка по движению (если есть история)
            if self._prev_cx is not None and self._prev_theta is not None:
                dx = cx - self._prev_cx
                dy = cy - self._prev_cy
                move_dist = np.hypot(dx, dy)
                
                if move_dist > 3:  # значимое движение
                    move_angle = np.arctan2(dy, dx)
                    
                    # Проверяем оба варианта
                    diff1 = abs(np.arctan2(np.sin(move_angle - theta), np.cos(move_angle - theta)))
                    diff2 = abs(np.arctan2(np.sin(move_angle - (theta + np.pi)), np.cos(move_angle - (theta + np.pi))))
                    
                    if diff2 < diff1:
                        theta += np.pi
                        # Обновляем ручной флип
                        self.manual_flip = not self.manual_flip
        else:
            # Fallback: моменты инерции
            mu20 = mu02 = mu11 = 0.0
            for point in best:
                px, py = point[0]
                dx, dy = px - cx, py - cy
                mu20 += dx * dx
                mu02 += dy * dy
                mu11 += dx * dy
            
            theta = 0.5 * np.arctan2(2 * mu11, mu20 - mu02) if abs(mu20 - mu02) > 1e-6 else 0.0
            
            # Направление по асимметрии
            fwd_sum = bwd_sum = 0.0
            for point in best:
                px, py = point[0]
                proj = (px - cx) * np.cos(theta) + (py - cy) * np.sin(theta)
                if proj > 0:
                    fwd_sum += proj
                else:
                    bwd_sum += abs(proj)
            
            if bwd_sum > fwd_sum:
                theta += np.pi
        
        theta = np.arctan2(np.sin(theta), np.cos(theta))
        
        # Стабилизация угла
        if self._prev_theta is not None:
            dtheta = theta - self._prev_theta
            dtheta = np.arctan2(np.sin(dtheta), np.cos(dtheta))
            
            # Ограничение поворота
            max_rotation = np.radians(30)
            if abs(dtheta) > max_rotation:
                dtheta = np.sign(dtheta) * max_rotation
            
            theta = self._prev_theta + 0.3 * dtheta
        
        self._prev_theta = theta
        self._prev_cx = cx
        self._prev_cy = cy
        
        return {
            'cx': cx, 'cy': cy,
            'area': best_area,
            'theta': theta,
            'contour': best,
            'vertices': vertices
        }, mask
    
    def _draw_debug(self, frame, detection, mask):
        """Рисует отладочную информацию."""
        h, w = frame.shape[:2]
        
        if self.debug_mode == "mask":
            debug = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
        elif self.debug_mode == "contours":
            debug = frame.copy()
            if detection and 'contour' in detection:
                cv2.drawContours(debug, [detection['contour']], -1, (0, 255, 0), 2)
        else:
            debug = frame.copy()
            
            if self.show_color_mask:
                mask_colored = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
                mask_colored[:, :, 0] = 0
                mask_colored[:, :, 2] = 0
                debug = cv2.addWeighted(debug, 0.7, mask_colored, 0.3, 0)
            
            if detection:
                cx, cy = int(detection['cx']), int(detection['cy'])
                theta = detection['theta']
                
                # Вершины треугольника
                if detection.get('vertices'):
                    nose, base1, base2 = detection['vertices']
                    n = tuple(map(int, nose))
                    b1 = tuple(map(int, base1))
                    b2 = tuple(map(int, base2))
                    
                    # Основание — синяя линия
                    cv2.line(debug, b1, b2, (255, 150, 0), 2)
                    
                    # Нос — красная точка
                    cv2.circle(debug, n, 8, (0, 0, 255), -1)
                    cv2.putText(debug, "NOSE", (n[0] + 10, n[1]), 
                               cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
                    
                    # Вершины основания — синие точки
                    cv2.circle(debug, b1, 5, (255, 150, 0), -1)
                    cv2.circle(debug, b2, 5, (255, 150, 0), -1)
                    
                    # Линия от основания к носу
                    base_center = ((b1[0]+b2[0])//2, (b1[1]+b2[1])//2)
                    cv2.line(debug, base_center, n, (0, 255, 255), 1, cv2.LINE_AA)
                
                # Сглаженная позиция
                if self._smooth_cx is not None:
                    scx, scy = int(self._smooth_cx), int(self._smooth_cy)
                    cv2.circle(debug, (scx, scy), 5, (255, 0, 0), -1)
                
                # Центр
                cv2.circle(debug, (cx, cy), 8, (0, 255, 0), -1)
                cv2.circle(debug, (cx, cy), 12, (0, 255, 0), 2)
                
                # Направление
                arrow_len = 60
                dx = int(arrow_len * np.cos(theta))
                dy = int(arrow_len * np.sin(theta))
                cv2.arrowedLine(debug, (cx, cy), (cx + dx, cy + dy), (0, 255, 255), 3, tipLength=0.3)
                
                # Контур
                if 'contour' in detection:
                    cv2.drawContours(debug, [detection['contour']], -1, (0, 255, 0), 1)
        
        # Статистика
        cv2.putText(debug, f"FPS: {self._fps:.1f}", (10, 24),
                   cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        cv2.putText(debug, f"Found: {self._detection_count}/{self._frame_count}", (10, 48),
                   cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        cv2.putText(debug, f"Flip: {'ON' if self.manual_flip else 'OFF'} | f=flip s=save r=reset m=mode", 
                   (10, h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (128, 128, 128), 1)
        
        if detection:
            cv2.putText(debug, f"Area={detection['area']:.0f} θ={np.rad2deg(detection['theta']):.0f}°",
                       (10, 72), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        
        return debug
    
    def _draw_controls(self):
        """Рисует панель с текущими значениями параметров."""
        panel = np.ones((400, 600, 3), dtype=np.uint8) * 40
        
        y = 30
        cv2.putText(panel, "CURRENT PARAMETERS", (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        
        params_text = [
            f"HSV Lower: {self.config['color_lower']}",
            f"HSV Upper: {self.config['color_upper']}",
            f"Min Area: {self.config['min_area']}",
            f"Min Ratio: {self.config['min_triangle_ratio']:.2f}",
            f"Morph Kernel: {self.config['morph_kernel']}",
            f"Alpha Pos: {self.config['alpha_pos']:.2f}",
            f"Alpha Theta: {self.config['alpha_theta']:.2f}",
            f"Manual Flip: {'ON' if self.manual_flip else 'OFF'}",
            "",
            f"Detections: {self._detection_count}/{self._frame_count}",
            f"Rate: {self._detection_count/max(1,self._frame_count)*100:.1f}%",
            f"FPS: {self._fps:.1f}",
        ]
        
        for i, text in enumerate(params_text):
            cv2.putText(panel, text, (10, y + 25 * (i + 1)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 200, 200), 1)
        
        cv2.imshow("Controls", panel)
    
    def save_config(self):
        """Сохраняет текущие параметры в config.py."""
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                content = f.read()
            
            import re
            
            pattern = r'(DETECTION_PARAMS\s*=\s*\{).*?(\n\})'
            
            new_params = f'''DETECTION_PARAMS = {{
    "color_lower": {self.config['color_lower']},
    "color_upper": {self.config['color_upper']},
    "min_area": {self.config['min_area']},
    "min_triangle_ratio": {self.config['min_triangle_ratio']:.2f},
    "morph_kernel": {self.config['morph_kernel']},
    "alpha_pos": {self.config['alpha_pos']:.2f},
    "alpha_theta": {self.config['alpha_theta']:.2f},
    # Сохранено из test_tracking.py {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
}}'''
            
            new_content = re.sub(pattern, new_params, content, flags=re.DOTALL)
            
            backup_path = CONFIG_PATH + ".backup"
            with open(backup_path, 'w', encoding='utf-8') as f:
                f.write(content)
            
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                f.write(new_content)
            
            print(f"\n[✓] Параметры сохранены в {CONFIG_PATH}")
            print(f"[✓] Бэкап сохранён в {backup_path}")
            return True
        
        except Exception as e:
            print(f"\n[✗] Ошибка сохранения: {e}")
            return False
    
    def reset_config(self):
        """Сбрасывает параметры к заводским."""
        self.config = DEFAULT_CONFIG.copy()
        self.manual_flip = False
        
        cv2.setTrackbarPos("H Low", "Controls", self.config["color_lower"][0])
        cv2.setTrackbarPos("S Low", "Controls", self.config["color_lower"][1])
        cv2.setTrackbarPos("V Low", "Controls", self.config["color_lower"][2])
        cv2.setTrackbarPos("H High", "Controls", self.config["color_upper"][0])
        cv2.setTrackbarPos("S High", "Controls", self.config["color_upper"][1])
        cv2.setTrackbarPos("V High", "Controls", self.config["color_upper"][2])
        cv2.setTrackbarPos("Min Area", "Controls", self.config["min_area"])
        cv2.setTrackbarPos("Min Ratio x10", "Controls", int(self.config["min_triangle_ratio"] * 10))
        cv2.setTrackbarPos("Morph Kernel", "Controls", self.config["morph_kernel"])
        cv2.setTrackbarPos("Alpha Pos x100", "Controls", int(self.config["alpha_pos"] * 100))
        cv2.setTrackbarPos("Alpha Theta x100", "Controls", int(self.config["alpha_theta"] * 100))
        
        self._smooth_cx = None
        self._smooth_cy = None
        self._smooth_theta = None
        self._prev_theta = None
        self._prev_cx = None
        self._prev_cy = None
        
        print("\n[✓] Параметры сброшены к заводским")
    
    def run(self):
        """Главный цикл."""
        try:
            while True:
                ret, frame = self.cap.read()
                if not ret:
                    break
                
                self._frame_count += 1
                
                # Детекция
                detection, mask = self._detect(frame)
                
                if detection is not None:
                    self._detection_count += 1
                    
                    # Сглаживание
                    alpha_pos = self.config['alpha_pos']
                    alpha_theta = self.config['alpha_theta']
                    
                    if self._smooth_cx is None:
                        self._smooth_cx = detection['cx']
                        self._smooth_cy = detection['cy']
                        self._smooth_theta = detection['theta']
                    else:
                        self._smooth_cx += alpha_pos * (detection['cx'] - self._smooth_cx)
                        self._smooth_cy += alpha_pos * (detection['cy'] - self._smooth_cy)
                        
                        dtheta = detection['theta'] - self._smooth_theta
                        dtheta = np.arctan2(np.sin(dtheta), np.cos(dtheta))
                        self._smooth_theta += alpha_theta * dtheta
                    
                    detection['cx'] = self._smooth_cx
                    detection['cy'] = self._smooth_cy
                    detection['theta'] = self._smooth_theta
                else:
                    self._prev_theta = None
                    self._prev_cx = None
                    self._prev_cy = None
                
                # FPS
                if self._frame_count % 30 == 0:
                    ticks = cv2.getTickCount()
                    self._fps = 30.0 / ((ticks - self._fps_timer) / cv2.getTickFrequency() + 1e-6)
                    self._fps_timer = ticks
                
                # Отладка
                debug_frame = self._draw_debug(frame, detection, mask)
                cv2.imshow("Debug", debug_frame)
                self._draw_controls()
                
                # Клавиши
                key = cv2.waitKey(1) & 0xFF
                
                if key == ord('q'):
                    break
                elif key == ord('s'):
                    if self.save_config():
                        print(f"  HSV: {self.config['color_lower']} -> {self.config['color_upper']}")
                        print(f"  Area: {self.config['min_area']}, Ratio: {self.config['min_triangle_ratio']:.2f}")
                        print(f"  Alpha: pos={self.config['alpha_pos']:.2f}, theta={self.config['alpha_theta']:.2f}")
                elif key == ord('r'):
                    self.reset_config()
                elif key == ord('f'):
                    self.manual_flip = not self.manual_flip
                    print(f"\n[Manual Flip: {'ON' if self.manual_flip else 'OFF'}]")
                elif key == ord('c'):
                    self.show_color_mask = not self.show_color_mask
                elif key == ord('m'):
                    modes = ["result", "contours", "mask"]
                    idx = modes.index(self.debug_mode)
                    self.debug_mode = modes[(idx + 1) % len(modes)]
                    print(f"\n[Debug mode: {self.debug_mode}]")
        
        except KeyboardInterrupt:
            pass
        finally:
            self.cap.release()
            cv2.destroyAllWindows()
            print("\nОтладка завершена")


# ==================== Точка входа ====================

if __name__ == "__main__":
    import sys
    
    camera_id = 1
    if len(sys.argv) > 1:
        camera_id = int(sys.argv[1])
    
    debugger = TrackingDebugger(camera_id=camera_id)
    debugger.run()