"""
Планировщик траекторий — ВЕРСИЯ 12.1 (отправка координат маркера и сегментов траектории)

Исправления:
- В auto_telemetry отправляются pose.x, pose.y (координаты маркера), а не центр вращения.
- При старте отправляется только первый сегмент траектории (маркер → первая цель).
- При достижении точки отправляется следующий сегмент.
- Пройденный путь в HTML теперь совпадает с визуальной одометрией.
"""

import asyncio
import math
import time
from config import (
    MAX_PWM, MIN_PWM, PLANNER_PARAMS, SAFETY,
    MARKER_OFFSET_PX, ARRIVAL_RADIUS_PX, SLOWDOWN_RADIUS_PX,
    TURN_SPEED, TURN_ACCURACY_DEG, KP_TURN,
    DRIVE_CORRECTION, DRIVE_CORRECTION_THRESH,
    apply_motor_mapping, normalize_angle, angle_difference
)


KP_TURN
TURN_SPEED
TURN_ACCURACY_DEG
DRIVE_CORRECTION
DRIVE_CORRECTION_THRESH 


def speed_to_pwm(frac: float) -> int:
    """
    Преобразует долю скорости [-1, 1] в PWM с учётом мёртвой зоны.
    0 -> 0, иначе знак * [MIN_PWM .. MAX_PWM].
    """
    if abs(frac) < 1e-6:
        return 0
    frac = max(-1.0, min(1.0, frac))
    magnitude = MIN_PWM + (MAX_PWM - MIN_PWM) * abs(frac)
    return int(math.copysign(magnitude, frac))


class TrajectoryTracker:
    def __init__(self, esp32, trajectory="custom", speed=0.3, zone=None, custom_points=None):
        self.esp32 = esp32
        self.trajectory_name = trajectory
        self.speed = speed
        self.running = False
        self.paused = False

        self.pose = {"x": 0.0, "y": 0.0, "theta": 0.0}
        self.pose_updated = False
        self.pose_timestamp = 0.0

        self.zone = zone or {"x1": 100, "y1": 90, "x2": 540, "y2": 390}
        self.custom_points = custom_points or []

        self.pose_timeout = SAFETY.get("pose_timeout", 0.5)
        self.dt = PLANNER_PARAMS.get("dt", 0.05)

        self._last_valid_pose = None
        self._last_valid_pose_time = 0.0
        self.trajectory_points = []
        self.current_target_idx = 0

        self._frame_count = 0
        self._pause_event = asyncio.Event()
        self._pause_event.set()

    # ---------- зона и траектория ----------
    def update_zone(self, zone: dict):
        self.zone = zone

    def _generate_trajectory(self):
        pts = []
        cx = (self.zone["x1"] + self.zone["x2"]) / 2
        cy = (self.zone["y1"] + self.zone["y2"]) / 2
        w = self.zone["x2"] - self.zone["x1"]
        h = self.zone["y2"] - self.zone["y1"]
        scale = min(w, h) * 0.35

        if self.trajectory_name == "square":
            s = scale
            for i in range(60):
                t = i / 60 * 4
                side = int(t) % 4
                u = t - int(t)
                if side == 0:
                    pts.append({"x": cx - s + u * 2*s, "y": cy - s})
                elif side == 1:
                    pts.append({"x": cx + s, "y": cy - s + u * 2*s})
                elif side == 2:
                    pts.append({"x": cx + s - u * 2*s, "y": cy + s})
                else:
                    pts.append({"x": cx - s, "y": cy + s - u * 2*s})
        elif self.trajectory_name == "polyline":
            vertices = [
                (cx - scale, cy - scale * 0.5),
                (cx + scale * 0.3, cy - scale),
                (cx + scale, cy),
                (cx - scale * 0.3, cy + scale),
                (cx - scale, cy + scale * 0.5),
            ]
            for i in range(len(vertices) - 1):
                x1, y1 = vertices[i]
                x2, y2 = vertices[i + 1]
                steps = 20
                for j in range(steps):
                    t = j / steps
                    pts.append({"x": x1 + (x2 - x1) * t, "y": y1 + (y2 - y1) * t})
            pts.append({"x": vertices[-1][0], "y": vertices[-1][1]})
        elif self.trajectory_name == "eight":
            for i in range(80):
                t = i / 80 * 2 * math.pi
                pts.append({
                    "x": cx + scale * 0.7 * math.sin(t),
                    "y": cy + scale * 0.4 * math.sin(2 * t)
                })
        elif self.trajectory_name == "custom":
            if self.custom_points:
                pts = self.custom_points.copy()
            else:
                for i in range(20):
                    pts.append({"x": cx - scale + i/20 * 2*scale, "y": cy})
        else:
            for i in range(20):
                pts.append({"x": cx - scale + i/20 * 2*scale, "y": cy})
        return pts

    # ---------- поза ----------
    def update_pose(self, x, y, theta):
        self.pose["x"] = x
        self.pose["y"] = y
        self.pose["theta"] = normalize_angle(theta)
        self.pose_updated = True
        self.pose_timestamp = time.time()
        self._last_valid_pose = {"x": x, "y": y, "theta": self.pose["theta"]}
        self._last_valid_pose_time = time.time()

    def _is_pose_fresh(self) -> bool:
        if not self.pose_updated:
            return False
        return (time.time() - self.pose_timestamp) < self.pose_timeout

    def _get_pose(self):
        if self._is_pose_fresh():
            return self.pose
        if self._last_valid_pose is not None:
            if (time.time() - self._last_valid_pose_time) < self.pose_timeout * 2:
                return self._last_valid_pose
        return self.pose

    def _get_center_of_rotation(self):
        p = self._get_pose()
        dx, dy = MARKER_OFFSET_PX
        cos_t = math.cos(p["theta"])
        sin_t = math.sin(p["theta"])
        center_x = p["x"] - (dx * cos_t - dy * sin_t)
        center_y = p["y"] - (dx * sin_t + dy * cos_t)
        return center_x, center_y, p["theta"]

    def set_speed(self, speed: float):
        self.speed = max(0.1, min(1.0, speed))

    def _is_in_zone(self):
        cx, cy, _ = self._get_center_of_rotation()
        m = 10
        return (self.zone["x1"] + m < cx < self.zone["x2"] - m and
                self.zone["y1"] + m < cy < self.zone["y2"] - m)

    async def _stop_motors(self):
        await self.esp32.send_command("m 0 0 0 0")

    async def _send_motor_command(self, left, right):
        cmd = apply_motor_mapping([left, right, left, right])
        await self.esp32.send_command(f"m {cmd[0]} {cmd[1]} {cmd[2]} {cmd[3]}")

    def _find_closest_point(self):
        if not self.trajectory_points:
            return 0
        cx, cy, _ = self._get_center_of_rotation()
        min_dist = float('inf')
        closest_idx = 0
        for i, pt in enumerate(self.trajectory_points):
            d = math.hypot(pt["x"] - cx, pt["y"] - cy)
            if d < min_dist:
                min_dist = d
                closest_idx = i
        return closest_idx

    # ===================== ГЛАВНЫЙ ЦИКЛ =====================
    async def run(self, broadcast=None):
        self.running = True
        self._frame_count = 0

        self.trajectory_points = self._generate_trajectory()

        # Передаём всю траекторию для Python-окна
        try:
            from visual_odometry import set_desired_trajectory
            set_desired_trajectory(self.trajectory_points)
        except ImportError:
            pass

        if broadcast and self.trajectory_points:
            await broadcast({
                "type": "trajectory_preview",
                "points": self.trajectory_points,
                "arrival_radius": ARRIVAL_RADIUS_PX    
            })

        wait_start = time.time()
        while not self.pose_updated and self.running:
            await asyncio.sleep(0.1)
            if time.time() - wait_start > 5.0:
                self.running = False
                if broadcast:
                    await broadcast({"type": "motion_state", "state": "error", "message": "Timeout"})
                return

        self.current_target_idx = self._find_closest_point()
        lost_frame_count = 0

        try:
            while self.running and self.current_target_idx < len(self.trajectory_points):
                await self._pause_event.wait()

                if not self._is_pose_fresh():
                    lost_frame_count += 1
                    if lost_frame_count == 1:
                        await self._stop_motors()
                    if broadcast and lost_frame_count % 10 == 1:
                        await broadcast({"type": "motion_state", "state": "lost"})
                    await asyncio.sleep(0.1)
                    continue
                else:
                    if lost_frame_count > 0:
                        # Только что восстановили маркер — шлём running
                        if broadcast:
                            await broadcast({"type": "motion_state", "state": "running"})
                    lost_frame_count = 0

                # --- координаты МАРКЕРА (для HTML) ---
                p = self._get_pose()
                marker_x = p["x"]
                marker_y = p["y"]
                robot_theta = p["theta"]

                center_x, center_y, _ = self._get_center_of_rotation()

                target = self.trajectory_points[self.current_target_idx]

                if not self._is_in_zone():
                    zone_cx = (self.zone["x1"] + self.zone["x2"]) / 2
                    zone_cy = (self.zone["y1"] + self.zone["y2"]) / 2
                    dx_target = zone_cx - center_x
                    dy_target = zone_cy - center_y
                else:
                    dx_target = target["x"] - center_x
                    dy_target = target["y"] - center_y

                dist_px = math.hypot(dx_target, dy_target)
                target_angle = math.atan2(-dy_target, dx_target)
                angle_error = angle_difference(target_angle, robot_theta)

                # ---- достижение точки ----
                if dist_px <= ARRIVAL_RADIUS_PX:
                    await self._stop_motors()
                    self.current_target_idx += 1

                    if self.current_target_idx >= len(self.trajectory_points):
                        if broadcast:
                            await broadcast({"type": "motion_state", "state": "completed"})
                        try:
                            from visual_odometry import save_trajectory_graph, set_recording
                            set_recording(False)
                            save_trajectory_graph()
                        except ImportError:
                            pass
                        break

                    # Отправляем следующий сегмент для HTML
                    if broadcast:
                        next_target = self.trajectory_points[self.current_target_idx]
                        await broadcast({
                            "type": "trajectory_preview",
                            "points": [
                                {"x": marker_x, "y": marker_y},
                                {"x": next_target["x"], "y": next_target["y"]}
                            ]
                        })

                    await asyncio.sleep(0.2)
                    continue

                # ---- ПРОПОРЦИОНАЛЬНЫЙ ПОВОРОТ НА МЕСТЕ ----
                if abs(angle_error) > math.radians(TURN_ACCURACY_DEG):
                    turn_frac = KP_TURN * angle_error / math.pi
                    turn_frac = max(-TURN_SPEED, min(TURN_SPEED, turn_frac))
                    turn_pwm = speed_to_pwm(turn_frac)
                    await self._send_motor_command(-turn_pwm, turn_pwm)
                    await asyncio.sleep(self.dt)
                    continue

                # ---- ПРЯМОЛИНЕЙНОЕ ДВИЖЕНИЕ С ТОРМОЖЕНИЕМ ----
                if dist_px < SLOWDOWN_RADIUS_PX:
                    speed_frac = self.speed * (dist_px / SLOWDOWN_RADIUS_PX)
                else:
                    speed_frac = self.speed
                base_pwm = speed_to_pwm(speed_frac)

                if abs(angle_error) > math.radians(DRIVE_CORRECTION_THRESH):
                    correction_frac = DRIVE_CORRECTION * angle_error / math.pi
                    correction_pwm = speed_to_pwm(correction_frac * self.speed)
                    left_pwm = base_pwm - correction_pwm
                    right_pwm = base_pwm + correction_pwm
                else:
                    left_pwm = right_pwm = base_pwm

                await self._send_motor_command(left_pwm, right_pwm)

                # ---- телеметрия (координаты маркера) ----
                if broadcast and self._frame_count % 5 == 0:
                    try:
                        await broadcast({
                            "type": "auto_telemetry",
                            "pose": {"x": marker_x, "y": marker_y, "theta": robot_theta},
                            "target": target,
                            "angle_error": math.degrees(angle_error),
                            "dist_px": dist_px,
                            "progress": self.current_target_idx / len(self.trajectory_points)
                        })
                    except Exception:
                        pass

                self._frame_count += 1
                await asyncio.sleep(self.dt)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            import traceback
            traceback.print_exc()
            if broadcast:
                await broadcast({"type": "motion_state", "state": "error", "message": str(e)})
        finally:
            await self._stop_motors()
            self.running = False

    async def stop(self):
        self.running = False
        await self._stop_motors()

    def pause(self):
        if self.paused:
            self._pause_event.set()
            self.paused = False
        else:
            self._pause_event.clear()
            self.paused = True

    def get_preview(self):
        if self.trajectory_points:
            return self.trajectory_points
        return self._generate_trajectory()