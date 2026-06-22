"""
Визуальная одометрия для робота
ВЕРСИЯ 5.3 — фильтрация ложных детекций вне рабочей зоны.

- Единый источник позы (стрелка всегда на носу).
- Трекбары для подстройки коэффициентов регулятора (Kv, Ka, Kd, delay).
- Гомография опциональна.
- actual_trajectory пополняется только когда recording_enabled = True.
- Детекции вне рабочей зоны игнорируются.
- Быстрый сброс сглаживания при потере маркера (3 кадра вместо 10).
"""

import cv2
import math
import numpy as np
import time
import logging
import os
from typing import Optional, Dict, Tuple, List
from dataclasses import dataclass

from config import (
    CAMERA_PARAMS, DETECTION_PARAMS,
    MARKER_OFFSET_PX, ARRIVAL_RADIUS_PX
)

try:
    from config import HOMOGRAPHY_SRC_POINTS, HOMOGRAPHY_DST_SIZE
except ImportError:
    HOMOGRAPHY_SRC_POINTS = None
    HOMOGRAPHY_DST_SIZE = None

logging.basicConfig(level=logging.INFO, format='[%(name)s] %(levelname)s: %(message)s')
logger = logging.getLogger("VisualOdometry")

GRAPHS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "graphs")
os.makedirs(GRAPHS_DIR, exist_ok=True)

# ==================== Глобальные данные ====================
desired_trajectory: List[Dict] = []
actual_trajectory: List[Dict] = []
custom_trajectory: List[Tuple[int, int]] = []
target_direction: Optional[float] = None
target_position: Optional[Tuple[float, float]] = None
debug_center: Optional[Tuple[float, float]] = None
debug_target: Optional[Tuple[float, float]] = None
debug_state: str = "IDLE"
debug_angle_error: float = 0.0
debug_distance: float = 0.0
zone = {"x1": 100, "y1": 90, "x2": 540, "y2": 390}
mouse_x, mouse_y = 0, 0

# Флаг: записывать ли путь (управляется из proxy.py)
recording_enabled: bool = False


# ==================== Внешние функции ====================
def set_desired_trajectory(points: List[Dict]):
    global desired_trajectory
    desired_trajectory = points.copy() if points else []

def set_target_info(angle: Optional[float], pos: Optional[Tuple[float, float]] = None):
    global target_direction, target_position
    target_direction = angle
    target_position = pos

def set_debug_info(center=None, target=None, state="IDLE", angle_error=0.0, distance=0.0):
    global debug_center, debug_target, debug_state, debug_angle_error, debug_distance
    debug_center = center
    debug_target = target
    debug_state = state
    debug_angle_error = angle_error
    debug_distance = distance

def get_custom_trajectory() -> List[Dict]:
    return [{"x": float(x), "y": float(y)} for x, y in custom_trajectory]

def clear_custom_trajectory():
    global custom_trajectory
    custom_trajectory = []

def set_recording(enabled: bool):
    """Включает/выключает запись пройденного пути."""
    global recording_enabled
    recording_enabled = enabled
    if not enabled:
        logger.info("Recording stopped")

def add_actual_point(x: float, y: float):
    """Добавляет точку в историю, только если запись включена."""
    global actual_trajectory
    if not recording_enabled:
        return
    actual_trajectory.append({"x": x, "y": y})
    if len(actual_trajectory) > 2000:
        actual_trajectory = actual_trajectory[-1000:]

def clear_actual_trajectory():
    global actual_trajectory
    actual_trajectory = []


# ==================== Pose ====================
@dataclass
class Pose:
    x: float = 0.0
    y: float = 0.0
    theta: float = 0.0
    timestamp: float = 0.0
    quality: float = 0.0
    area: float = 0.0


# ==================== Детектор ====================
class RobustGreenDetector:
    def __init__(self):
        self.hsv_lower = np.array(DETECTION_PARAMS.get("color_lower", [35, 80, 40]), dtype=np.uint8)
        self.hsv_upper = np.array(DETECTION_PARAMS.get("color_upper", [90, 255, 255]), dtype=np.uint8)
        self.min_area = DETECTION_PARAMS.get("min_area", 150)
        self.min_ratio = DETECTION_PARAMS.get("min_triangle_ratio", 1.15)
        ks = DETECTION_PARAMS.get("morph_kernel", 3)
        self.k_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ks, ks))
        self.k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ks + 2, ks + 2))

    def _build_mask(self, frame):
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, self.hsv_lower, self.hsv_upper)
        if cv2.countNonZero(mask) < 200:
            mask = cv2.inRange(hsv, np.array([25, 60, 40], np.uint8),
                                     np.array([95, 255, 255], np.uint8))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self.k_open, iterations=1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self.k_close, iterations=2)
        return mask

    def _find_nose(self, contour, cx, cy, pts_xy):
        hull = cv2.convexHull(contour.astype(np.int32))
        try:
            area_tri, tri = cv2.minEnclosingTriangle(hull.astype(np.float32))
            if tri is not None:
                tri = tri.reshape(-1, 2)
                if len(tri) == 3:
                    d01 = float(np.hypot(*(tri[0] - tri[1])))
                    d12 = float(np.hypot(*(tri[1] - tri[2])))
                    d20 = float(np.hypot(*(tri[2] - tri[0])))
                    sides = [(d01, 2), (d12, 0), (d20, 1)]
                    _, nose_idx = min(sides, key=lambda s: s[0])
                    nose_v = tri[nose_idx]
                    ds = sorted([d01, d12, d20])
                    elong = ds[2] / (ds[0] + 1e-9)
                    return float(nose_v[0]), float(nose_v[1]), elong
        except cv2.error:
            pass

        dx = pts_xy[:, 0] - cx
        dy = pts_xy[:, 1] - cy
        cov = np.array([[np.mean(dx*dx), np.mean(dx*dy)],
                        [np.mean(dx*dy), np.mean(dy*dy)]])
        w, v = np.linalg.eigh(cov)
        axis = v[:, int(np.argmax(w))]
        t = dx * axis[0] + dy * axis[1]
        if np.mean(t**3) < 0:
            axis = -axis
        elong = math.sqrt(max(w) / (min(w) + 1e-9))
        proj = dx * axis[0] + dy * axis[1]
        ni = int(np.argmax(proj))
        return float(pts_xy[ni, 0]), float(pts_xy[ni, 1]), elong

    def detect(self, frame: np.ndarray) -> Optional[Dict]:
        mask = self._build_mask(frame)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if not contours:
            return None

        best, best_area = None, 0.0
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < self.min_area:
                continue
            x, y, w, h = cv2.boundingRect(cnt)
            if max(w, h) / (min(w, h) + 1e-6) < self.min_ratio:
                continue
            if area > best_area:
                best_area, best = area, cnt

        if best is None:
            return None

        single = np.zeros(mask.shape, np.uint8)
        cv2.drawContours(single, [best], -1, 255, cv2.FILLED)
        ys, xs = np.where(single > 0)
        if len(xs) < 50:
            return None

        cx = float(xs.mean())
        cy = float(ys.mean())
        pts_xy = np.stack([xs.astype(np.float64), ys.astype(np.float64)], axis=1)

        nose_x, nose_y, elong = self._find_nose(best, cx, cy, pts_xy)

        theta = math.atan2(-(nose_y - cy), nose_x - cx)
        theta = (theta + np.pi) % (2 * np.pi) - np.pi

        return {
            'cx': float(cx),
            'cy': float(cy),
            'area': float(best_area),
            'theta': float(theta),
            'elong': float(elong),
            'contour': best,
            'nose': (float(nose_x), float(nose_y))
        }


# ==================== Визуальная одометрия ====================
class VisualOdometry:
    def __init__(self, camera_id: Optional[int] = None, debug: bool = False):
        if camera_id is None:
            camera_id = CAMERA_PARAMS.get("source", 1)
        self.debug = debug

        self.cap = cv2.VideoCapture(camera_id)
        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open camera #{camera_id}")

        tr = CAMERA_PARAMS.get("resolution", (640, 480))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, tr[0])
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, tr[1])

        self.H = None
        if HOMOGRAPHY_SRC_POINTS is not None and HOMOGRAPHY_DST_SIZE is not None:
            src = np.float32(HOMOGRAPHY_SRC_POINTS)
            W, Hh = HOMOGRAPHY_DST_SIZE
            dst = np.float32([[0, 0], [W, 0], [W, Hh], [0, Hh]])
            self.H = cv2.getPerspectiveTransform(src, dst)
            self.frame_width, self.frame_height = int(W), int(Hh)
            logger.info("Homography ON -> top-down %dx%d", W, Hh)
        else:
            self.frame_width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            self.frame_height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            logger.warning("Homography OFF")

        self.detector = RobustGreenDetector()

        self._smooth_cx = None
        self._smooth_cy = None
        self._smooth_dir_x = None
        self._smooth_dir_y = None
        self._alpha_pos = DETECTION_PARAMS.get("alpha_pos", 0.5)
        self._alpha_dir = DETECTION_PARAMS.get("alpha_dir", 0.5)
        self._motion_lock_px = DETECTION_PARAMS.get("motion_lock_px", 6.0)

        self._frame_count = 0
        self._lost_count = 0
        self._max_lost = 3  # быстрый сброс: 3 кадра вместо 10
        self._last_pose: Optional[Pose] = None
        self._last_raw_nose = None
        self._last_raw_center = None
        self._fps = 0.0
        self._fps_timer = time.time()

        if self.debug:
            self._setup_debug_window()

        logger.info("VisualOdometry v5.3 | %dx%d | Zone filter ON | Fast reset",
                    self.frame_width, self.frame_height)

    def _rectify(self, frame):
        if self.H is None:
            return frame
        return cv2.warpPerspective(frame, self.H, (self.frame_width, self.frame_height))

    def _setup_debug_window(self):
        cv2.namedWindow("Visual Odometry", cv2.WINDOW_NORMAL)
        cv2.resizeWindow("Visual Odometry", 900, 550)
        cv2.createTrackbar("zone_x1", "Visual Odometry", 100, self.frame_width, lambda v: None)
        cv2.createTrackbar("zone_y1", "Visual Odometry", 90, self.frame_height, lambda v: None)
        cv2.createTrackbar("zone_x2", "Visual Odometry", self.frame_width - 100, self.frame_width, lambda v: None)
        cv2.createTrackbar("zone_y2", "Visual Odometry", self.frame_height - 90, self.frame_height, lambda v: None)
        cv2.createTrackbar("TurnSpeed*100", "Visual Odometry", 40, 100, lambda v: None)       # 0..1.0
        cv2.createTrackbar("TurnAccDeg",    "Visual Odometry", 5, 20, lambda v: None)         # 1..20°
        cv2.createTrackbar("DrvCorr*100",   "Visual Odometry", 15, 50, lambda v: None)        # 0..0.5
        cv2.createTrackbar("DrvCorrThrDeg", "Visual Odometry", 15, 30, lambda v: None)   
        cv2.setMouseCallback("Visual Odometry", self._on_mouse)
        self.logo = cv2.imread('logo.png', cv2.IMREAD_UNCHANGED)
        if self.logo is not None:
            # Если логотип с альфа-каналом, приводим к обычному BGR
            if self.logo.shape[2] == 4:
                self.logo = cv2.cvtColor(self.logo, cv2.COLOR_BGRA2BGR)
            self.logo = cv2.resize(self.logo, (100, 30))   # подгоните размер
        else:
            print("[WARNING] logo.png не найден")

    def _on_mouse(self, event, x, y, flags, param):
        global custom_trajectory, mouse_x, mouse_y
        mouse_x, mouse_y = x, y
        if event == cv2.EVENT_LBUTTONDOWN:
            custom_trajectory.append((x, y))
            print(f"Точка {len(custom_trajectory)}: [{x}, {y}]")
        elif event == cv2.EVENT_RBUTTONDOWN:
            custom_trajectory = []
            print("Точки очищены")

    def _update_zone_from_trackbars(self):
        global zone
        if not self.debug:
            return
        zone["x1"] = cv2.getTrackbarPos("zone_x1", "Visual Odometry")
        zone["y1"] = cv2.getTrackbarPos("zone_y1", "Visual Odometry")
        zone["x2"] = cv2.getTrackbarPos("zone_x2", "Visual Odometry")
        zone["y2"] = cv2.getTrackbarPos("zone_y2", "Visual Odometry")
        if zone["x1"] > zone["x2"]:
            zone["x1"], zone["x2"] = zone["x2"], zone["x1"]
        if zone["y1"] > zone["y2"]:
            zone["y1"], zone["y2"] = zone["y2"], zone["y1"]

    def is_in_zone(self, x, y):
        m = 10
        return (zone["x1"] + m < x < zone["x2"] - m and
                zone["y1"] + m < y < zone["y2"] - m)

    def get_zone(self):
        return zone.copy()

    def _update_pose(self, cx_raw, cy_raw, nose_x, nose_y, area):
        raw_dx = nose_x - cx_raw
        raw_dy = nose_y - cy_raw

        if self._smooth_cx is None:
            self._smooth_cx = cx_raw
            self._smooth_cy = cy_raw
            self._smooth_dir_x = raw_dx
            self._smooth_dir_y = raw_dy
            self._last_raw_nose = (nose_x, nose_y)
            self._last_raw_center = (cx_raw, cy_raw)
        else:
            center_shift = math.hypot(cx_raw - self._last_raw_center[0],
                                      cy_raw - self._last_raw_center[1])
            if center_shift > self._motion_lock_px:
                dot = raw_dx * self._smooth_dir_x + raw_dy * self._smooth_dir_y
                if dot < 0:
                    raw_dx = -raw_dx
                    raw_dy = -raw_dy

            self._smooth_cx += self._alpha_pos * (cx_raw - self._smooth_cx)
            self._smooth_cy += self._alpha_pos * (cy_raw - self._smooth_cy)
            self._smooth_dir_x += self._alpha_dir * (raw_dx - self._smooth_dir_x)
            self._smooth_dir_y += self._alpha_dir * (raw_dy - self._smooth_dir_y)

            self._last_raw_nose = (nose_x, nose_y)
            self._last_raw_center = (cx_raw, cy_raw)

        theta = math.atan2(-self._smooth_dir_y, self._smooth_dir_x)
        theta = (theta + np.pi) % (2 * np.pi) - np.pi

        draw_nose_x = self._smooth_cx + self._smooth_dir_x
        draw_nose_y = self._smooth_cy + self._smooth_dir_y

        return theta, draw_nose_x, draw_nose_y

    def get_pose(self) -> Optional[Pose]:
        ret, frame = self.cap.read()
        if not ret:
            return None

        frame = self._rectify(frame)
        self._frame_count += 1
        self._update_zone_from_trackbars()

        try:
            import planner
            planner.TURN_SPEED = cv2.getTrackbarPos("TurnSpeed*100", "Visual Odometry") / 100.0
            planner.TURN_ACCURACY_DEG = float(cv2.getTrackbarPos("TurnAccDeg", "Visual Odometry"))
            planner.DRIVE_CORRECTION = cv2.getTrackbarPos("DrvCorr*100", "Visual Odometry") / 100.0
            planner.DRIVE_CORRECTION_THRESH = float(cv2.getTrackbarPos("DrvCorrThrDeg", "Visual Odometry"))
        except Exception:
            pass

        det = self.detector.detect(frame)

        # Игнорируем детекции вне рабочей зоны
        if det is None or not self.is_in_zone(det['cx'], det['cy']):
            self._lost_count += 1
            if self.debug:
                self._draw_debug(frame, None)
            if self._lost_count >= self._max_lost:
                self._smooth_cx = None
                self._smooth_cy = None
                self._smooth_dir_x = None
                self._smooth_dir_y = None
                self._last_pose = None
            return None

        self._lost_count = 0
        cx_r, cy_r = det['cx'], det['cy']
        nose_x, nose_y = det['nose']
        area = det['area']

        theta, draw_nose_x, draw_nose_y = self._update_pose(
            cx_r, cy_r, nose_x, nose_y, area
        )

        pose = Pose(
            x=float(self._smooth_cx),
            y=float(self._smooth_cy),
            theta=float(theta),
            timestamp=time.time(),
            quality=min(1.0, area / 3000),
            area=area
        )
        self._last_pose = pose
        add_actual_point(pose.x, pose.y)

        if self.debug:
            self._draw_debug(frame, {
                'cx': self._smooth_cx,
                'cy': self._smooth_cy,
                'theta': theta,
                'area': area,
                'contour': det.get('contour'),
                'nose': (draw_nose_x, draw_nose_y)
            })

        if self._frame_count % 30 == 0:
            self._fps = 30.0 / (time.time() - self._fps_timer + 1e-6)
            self._fps_timer = time.time()

        return pose

    def _draw_trajectory_overlay(self, frame):
        overlay = frame.copy()
        if len(desired_trajectory) > 1:
            for i in range(len(desired_trajectory) - 1):
                p1 = (int(desired_trajectory[i]["x"]), int(desired_trajectory[i]["y"]))
                p2 = (int(desired_trajectory[i + 1]["x"]), int(desired_trajectory[i + 1]["y"]))
                cv2.line(overlay, p1, p2, (0, 215, 255), 2)
        if len(actual_trajectory) > 1:
            pts = np.array([(int(p["x"]), int(p["y"])) for p in actual_trajectory], np.int32)
            cv2.polylines(overlay, [pts], False, (0, 255, 100), 2)
        if len(custom_trajectory) > 1:
            for i in range(len(custom_trajectory) - 1):
                cv2.line(overlay, custom_trajectory[i], custom_trajectory[i + 1], (255, 150, 0), 2)
        for pt in custom_trajectory:
            r = max(3, ARRIVAL_RADIUS_PX // 4)
            cv2.circle(overlay, pt, r, (0, 165, 255), 2)
            cv2.circle(overlay, pt, 2, (0, 165, 255), -1)
        return cv2.addWeighted(overlay, 0.5, frame, 0.5, 0)

    def _draw_debug(self, frame, result):
        global mouse_x, mouse_y, recording_enabled
        if not self.debug:
            return

        debug = self._draw_trajectory_overlay(frame)
        h, w = debug.shape[:2]

        cv2.rectangle(debug, (zone["x1"], zone["y1"]), (zone["x2"], zone["y2"]), (0, 255, 0), 2)

        status_color = (0, 255, 0) if result else (0, 0, 255)
        homography_status = "TOPDOWN" if self.H is not None else "RAW"
        rec_status = "REC" if recording_enabled else "PAUSED"
        try:
            import planner
            kv = planner.KV
            ka = planner.KA
        except Exception:
            kv, ka = 0.5, 0.8
        cv2.putText(debug, f"FPS:{self._fps:.1f} | {'OK' if result else 'LOST:'+str(self._lost_count)} | {homography_status} | {rec_status} | Kv={kv:.2f} Ka={ka:.2f}",
                    (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.4, status_color, 1)

        if result:
            cx = int(result['cx'])
            cy = int(result['cy'])
            theta = result.get('theta', 0.0)
            nose = result.get('nose')

            if result.get('contour') is not None:
                cv2.drawContours(debug, [result['contour']], -1, (0, 255, 0), 2)

            cv2.circle(debug, (cx, cy), 7, (0, 255, 0), -1)

            if nose is not None:
                nx, ny = int(nose[0]), int(nose[1])
                cv2.arrowedLine(debug, (cx, cy), (nx, ny), (0, 255, 0), 2, tipLength=0.3)
                cv2.circle(debug, (nx, ny), 8, (0, 0, 255), -1)
                cv2.putText(debug, "NOSE", (nx + 10, ny),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)

            cv2.putText(debug, f"th={np.rad2deg(theta):.0f}deg area={result['area']:.0f}",
                       (cx + 12, cy - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)

            dx_m, dy_m = MARKER_OFFSET_PX
            ct, st = math.cos(theta), math.sin(theta)
            rcx = cx - (dx_m * ct - dy_m * st)
            rcy = cy - (dx_m * st + dy_m * ct)
            cv2.circle(debug, (int(rcx), int(rcy)), 6, (255, 150, 0), -1)
            cv2.putText(debug, "CENTER", (int(rcx) + 10, int(rcy) - 5),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 150, 0), 1)
            cv2.line(debug, (cx, cy), (int(rcx), int(rcy)), (255, 150, 0), 1, cv2.LINE_AA)

        if debug_target is not None:
            tx, ty = int(debug_target[0]), int(debug_target[1])
            # Полупрозрачный заполненный круг
            overlay = debug.copy()
            cv2.circle(overlay, (tx, ty), ARRIVAL_RADIUS_PX, (0, 255, 0), -1)
            cv2.addWeighted(overlay, 0.35, debug, 0.65, 0, debug)
            # Контур
            cv2.circle(debug, (tx, ty), ARRIVAL_RADIUS_PX, (0, 255, 0), 2)

        if debug_center is not None and debug_target is not None:
            dcx, dcy = int(debug_center[0]), int(debug_center[1])
            tx, ty = int(debug_target[0]), int(debug_target[1])
            cv2.line(debug, (dcx, dcy), (tx, ty), (255, 150, 0), 1, cv2.LINE_AA)
            mid_x = (dcx + tx) // 2
            mid_y = (dcy + ty) // 2
            cv2.putText(debug, f"{debug_distance:.0f}px", (mid_x, mid_y),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 200, 100), 1)

        cv2.putText(debug, f"STATE:{debug_state} err:{debug_angle_error:.1f}deg dist:{debug_distance:.0f}px",
                    (10, h - 30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
        cv2.putText(debug, f"C=clear S=save R=reset Q=quit | mouse:({mouse_x},{mouse_y})",
                    (10, h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (150, 150, 150), 1)
        cv2.imshow("Visual Odometry", debug)

    def handle_keys(self) -> Optional[str]:
        if not self.debug:
            return None
        key = cv2.waitKey(1) & 0xFF
        if key == ord('q'): return 'quit'
        if key == ord('c'): clear_actual_trajectory(); logger.info("Path cleared"); return 'clear_path'
        if key == ord('s') and custom_trajectory: logger.info(f"Custom saved: {len(custom_trajectory)} pts"); return 'save_custom'
        if key == ord('r'):
            cv2.setTrackbarPos("zone_x1", "Visual Odometry", 100)
            cv2.setTrackbarPos("zone_y1", "Visual Odometry", 90)
            cv2.setTrackbarPos("zone_x2", "Visual Odometry", self.frame_width - 100)
            cv2.setTrackbarPos("zone_y2", "Visual Odometry", self.frame_height - 90)
            logger.info("Zone reset"); return 'reset_zone'
        return None

    def is_robot_visible(self):
        return self._last_pose is not None and self._lost_count < self._max_lost

    def get_last_pose(self):
        return None if self._lost_count >= self._max_lost else self._last_pose

    def get_fps(self):
        return self._fps

    def reset(self):
        self._smooth_cx = None
        self._smooth_cy = None
        self._smooth_dir_x = None
        self._smooth_dir_y = None
        self._last_pose = None
        self._frame_count = 0
        self._lost_count = 0
        global actual_trajectory
        actual_trajectory = []

    def release(self):
        self.cap.release()
        if self.debug:
            try:
                cv2.destroyWindow("Visual Odometry")
            except cv2.error:
                pass


def test_odometry():
    print("=" * 60)
    print("  BOBR 4x4 Visual Odometry v5.3")
    print("  Zone filter ON | Fast reset (3 frames)")
    print("  C=clear  S=save  R=reset  Q=quit")
    print("  Mouse: L-click=point  R-click=clear")
    print("=" * 60)

    odom = VisualOdometry(debug=True)
    try:
        while True:
            pose = odom.get_pose()
            if odom.handle_keys() == 'quit':
                break
            if pose:
                print(f"\r[OK] x={pose.x:.0f} y={pose.y:.0f} th={np.rad2deg(pose.theta):.0f}deg   ", end="")
            else:
                print(f"\r[--] searching ({odom._lost_count})                             ", end="")
    except KeyboardInterrupt:
        pass
    finally:
        odom.release()
        print("\nDone")


if __name__ == "__main__":
    test_odometry()