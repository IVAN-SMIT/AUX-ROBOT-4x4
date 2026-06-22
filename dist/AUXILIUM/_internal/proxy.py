"""
WebSocket ↔ TCP прокси для робота BOBR 4x4.
ВЕРСИЯ 3.4 — постоянная отправка позы и FPS в HTML-клиент.
"""

import time
import math
import asyncio
import os
import socket
import threading
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn
import cv2
import numpy as np
import qrcode

from config import (
    HOST, PORT, PROXY_PORT,
    ARRIVAL_RADIUS_PX, MOTOR_REMAP, MOTOR_INVERT,
    TELEMETRY_INTERVAL, CAMERA_PARAMS
)


try:
    from zeroconf import Zeroconf, ServiceInfo
    ZEROCONF_AVAILABLE = True
except ImportError:
    ZEROCONF_AVAILABLE = False

ESP32_HOST = HOST
ESP32_PORT = PORT
WEB_PORT = PROXY_PORT

app = FastAPI(title="BOBR 4x4 Proxy v3.4")

static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir)
app.mount("/css", StaticFiles(directory=os.path.join(static_dir, "css")), name="css")
app.mount("/js", StaticFiles(directory=os.path.join(static_dir, "js")), name="js")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

local_ip = None
auto_pilot = None
odometry_instance = None
odometry_running = False
main_event_loop = None       # ссылка на asyncio event loop главного потока


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.connect((ESP32_HOST, ESP32_PORT))
        ip = s.getsockname()[0]
        s.close()
        if ip and ip != "0.0.0.0":
            return ip
    except Exception:
        pass
    return "192.168.4.2"


def generate_qr_console(url: str):
    try:
        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L,
                          box_size=1, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception as e:
        print(f"[QR] Не удалось: {e}")


def generate_qr_image(url: str) -> Optional[str]:
    try:
        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L,
                          box_size=10, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        qr_path = os.path.join(static_dir, "qr.png")
        img.save(qr_path)
        return "qr.png"
    except Exception as e:
        print(f"[QR] Не удалось сохранить: {e}")
        return None


# ==================== ESP32 TCP ====================

class ESP32Connection:
    def __init__(self, host: str = ESP32_HOST, port: int = ESP32_PORT):
        self.host = host
        self.port = port
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.connected = False
        self._lock = asyncio.Lock()

    async def connect(self) -> bool:
        try:
            print(f"[TCP] Подключение к {self.host}:{self.port}...")
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=3.0
            )
            self.connected = True
            print(f"[TCP] ✓ Подключено")
            return True
        except Exception as e:
            print(f"[TCP] ✗ Ошибка: {e}")
            self.connected = False
            return False

    async def disconnect(self):
        self.connected = False
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
        self.reader = None
        self.writer = None
        print("[TCP] Отключено")

    async def send_command(self, cmd: str) -> bool:
        if not self.connected or not self.writer:
            return False
        try:
            async with self._lock:
                message = (cmd + "\n").encode()
                self.writer.write(message)
                await self.writer.drain()
                return True
        except (ConnectionResetError, BrokenPipeError, OSError):
            print(f"[TCP] Соединение потеряно")
            self.connected = False
            return False
        except RuntimeError:
            return False
        except Exception as e:
            print(f"[TCP] Ошибка отправки: {e}")
            self.connected = False
            return False

    async def get_status(self) -> Optional[dict]:
        try:
            async with self._lock:
                if not self.connected or not self.writer:
                    return None
                try:
                    while True:
                        line = await asyncio.wait_for(self.reader.readline(), timeout=0.05)
                        if not line:
                            break
                except asyncio.TimeoutError:
                    pass

                self.writer.write(b"?\n")
                await self.writer.drain()
                response = await asyncio.wait_for(self.reader.readline(), timeout=0.5)
                response_str = response.decode().strip()
        except asyncio.TimeoutError:
            return None
        except Exception:
            return None

        try:
            result = {"voltage": 0.0, "num_motors": 4, "pwm": [0, 0, 0, 0]}
            for part in response_str.split("|"):
                if part.startswith("V:"):
                    result["voltage"] = float(part[2:])
                elif part.startswith("N:"):
                    result["num_motors"] = int(part[2:])
                elif part.startswith("PWM:"):
                    pwm_str = part[4:]
                    if pwm_str:
                        result["pwm"] = [int(x) for x in pwm_str.split(",")]
            return result
        except Exception:
            return None


esp32 = ESP32Connection()
active_ws_clients: set[WebSocket] = set()
zeroconf_instance = None

camera_active = False
marker_visible = False
camera_just_started = False


# ==================== Broadcast ====================

async def broadcast_to_all(msg: dict):
    if not active_ws_clients:
        return
    disconnected = set()
    for client in active_ws_clients:
        try:
            await client.send_json(msg)
        except Exception:
            disconnected.add(client)
    active_ws_clients.difference_update(disconnected)


# ==================== Мониторинг маркера (asyncio) ====================

async def odometry_monitor():
    global marker_visible, camera_just_started
    last_marker_state = None
    while True:
        await asyncio.sleep(0.2)
        if not odometry_running:
            last_marker_state = None
            continue
        if camera_just_started:
            camera_just_started = False
            await broadcast_to_all({"type": "camera_state", "state": "started"})
        current = marker_visible
        if current != last_marker_state:
            last_marker_state = current
            await broadcast_to_all({"type": "marker_status", "visible": current})


# ==================== Поток одометрии ====================

def run_odometry_thread():
    """Отдельный поток: камера + детекция + update_pose + отправка позы в HTML."""
    global odometry_instance, odometry_running, camera_active, marker_visible, auto_pilot, camera_just_started

    print("[Odom] 📷 Поток запущен")
    try:
        from visual_odometry import VisualOdometry
        odom = VisualOdometry(camera_id=CAMERA_PARAMS.get("source", 1), debug=True)
        odometry_instance = odom
        odometry_running = True
        camera_active = True
        camera_just_started = True

        while odometry_running:
            try:
                pose = odom.get_pose()
                marker_visible = (pose is not None)

                if pose is not None:
                    theta = math.atan2(math.sin(pose.theta), math.cos(pose.theta))

                    # Обновляем позу в планировщике
                    pilot = auto_pilot
                    if pilot is not None:
                        pilot.update_pose(pose.x, pose.y, theta)

                        if hasattr(pilot, 'trajectory_points') and pilot.trajectory_points:
                            try:
                                idx = getattr(pilot, 'current_target_idx', 0)
                                if idx < len(pilot.trajectory_points):
                                    target = pilot.trajectory_points[idx]
                                    target_angle = math.atan2(
                                        target["y"] - pose.y,
                                        target["x"] - pose.x
                                    )
                                    from visual_odometry import set_target_info
                                    set_target_info(target_angle, (target["x"], target["y"]))
                            except Exception:
                                pass

                    
                    asyncio.run_coroutine_threadsafe(
                        broadcast_to_all({
                            "type": "auto_telemetry",
                            "pose": {"x": pose.x, "y": pose.y, "theta": pose.theta},
                            "fps": odom.get_fps(),
                            "target": None,
                            "progress": None,
                            "arrival_radius": ARRIVAL_RADIUS_PX  
                        }),
                        main_event_loop
                    )

                key_action = odom.handle_keys()
                if key_action == 'quit':
                    print("[Odom] Q — выход")
                    odometry_running = False
                    break
                elif key_action == 'save_custom':
                    print("[Odom] Custom-траектория сохранена")
                elif key_action == 'clear_path':
                    print("[Odom] Путь очищен")

                time.sleep(0.03)

            except Exception as e:
                print(f"[Odom] Ошибка в цикле: {e}")
                time.sleep(0.1)

    except ImportError as e:
        print(f"[Odom] ❌ Модуль не найден: {e}")
    except Exception as e:
        print(f"[Odom] ❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()
    finally:
        odometry_running = False
        camera_active = False
        if odometry_instance:
            try:
                odometry_instance.release()
            except Exception:
                pass
        odometry_instance = None
        print("[Odom] Поток завершён")


def start_odometry():
    global odometry_running
    if odometry_running:
        print("[Odom] Уже запущена")
        return
    thread = threading.Thread(target=run_odometry_thread, daemon=True, name="OdometryThread")
    thread.start()
    print("[Odom] ✅ Поток создан")


def stop_odometry():
    global odometry_running, camera_active
    odometry_running = False
    camera_active = False
    print("[Odom] Остановка...")


# ==================== WebSocket ====================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global auto_pilot
    await websocket.accept()
    active_ws_clients.add(websocket)
    client_host = websocket.client.host if websocket.client else "unknown"
    print(f"[WS] ✓ Клиент: {client_host} (всего: {len(active_ws_clients)})")

    await websocket.send_json({
        "type": "status",
        "esp32_connected": esp32.connected,
        "camera_active": camera_active,
        "server_ip": local_ip or "192.168.4.2",
        "server_port": WEB_PORT
    })

    if not esp32.connected:
        await esp32.connect()

    if esp32.connected:
        status = await esp32.get_status()
        if status:
            await websocket.send_json({
                "type": "telemetry",
                "voltage": status["voltage"],
                "pwm": status["pwm"],
                "connected": True
            })

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "")

            if msg_type == "command":
                pwm_values = data.get("pwm", [0, 0, 0, 0])
                if len(pwm_values) != 4:
                    await websocket.send_json({"type": "error", "message": "Нужно 4 значения PWM"})
                    continue
                remapped = [0, 0, 0, 0]
                for client_idx in range(4):
                    phys_idx = MOTOR_REMAP[client_idx]
                    pwm = pwm_values[client_idx]
                    if MOTOR_INVERT[client_idx]:
                        pwm = -pwm
                    remapped[phys_idx] = pwm
                await esp32.send_command(f"m {remapped[0]} {remapped[1]} {remapped[2]} {remapped[3]}")

            elif msg_type == "stop":
                await esp32.send_command("m 0 0 0 0")
                await broadcast_to_all({
                    "type": "telemetry", "voltage": 0,
                    "pwm": [0, 0, 0, 0], "connected": esp32.connected, "stopped": True
                })

            elif msg_type == "camera_start":
                print("[Camera] Запуск камеры...")
                await websocket.send_json({"type": "camera_state", "state": "starting"})
                if not odometry_running:
                    start_odometry()
                    await asyncio.sleep(1.0)

            elif msg_type == "camera_stop":
                print("[Camera] Остановка камеры...")
                if auto_pilot is not None:
                    await auto_pilot.stop()
                    auto_pilot = None
                stop_odometry()
                await websocket.send_json({"type": "camera_state", "state": "stopped"})
                await broadcast_to_all({"type": "camera_state", "state": "stopped"})

            elif msg_type == "clear_path":
                try:
                    from visual_odometry import clear_actual_trajectory
                    clear_actual_trajectory()
                    print("[Path] График очищен")
                except ImportError:
                    pass

            elif msg_type == "auto_start":
                trajectory = data.get("trajectory", "custom")
                speed = data.get("speed", 0.4)
                print(f"[Auto] Запуск: {trajectory}, скорость: {speed}")

                if not odometry_running:
                    print("[Auto] Камера не запущена, запускаем...")
                    start_odometry()
                    await asyncio.sleep(1.0)

                try:
                    from planner import TrajectoryTracker as PilotClass
                    from visual_odometry import get_custom_trajectory, set_recording
                    set_recording(True)

                    if auto_pilot is not None:
                        await auto_pilot.stop()
                        auto_pilot = None

                    zone = None
                    custom_points = None
                    if odometry_instance is not None:
                        zone = odometry_instance.get_zone()
                        if trajectory == "custom":
                            custom_pts = get_custom_trajectory()
                            if custom_pts:
                                custom_points = custom_pts

                    auto_pilot = PilotClass(
                        esp32, trajectory=trajectory, speed=speed,
                        zone=zone, custom_points=custom_points
                    )

                    try:
                        from visual_odometry import set_desired_trajectory
                        preview = auto_pilot.get_preview()
                        set_desired_trajectory(preview)
                    except ImportError:
                        pass

                    asyncio.create_task(auto_pilot.run(broadcast_to_all))

                    await websocket.send_json({
                        "type": "motion_state", "state": "started", "trajectory": trajectory
                    })
                    preview = auto_pilot.get_preview()
                    await websocket.send_json({
                        "type": "trajectory_preview", "points": preview
                    })
                    print(f"[Auto] ✅ Запущен")

                except Exception as e:
                    print(f"[Auto] ❌ Ошибка: {e}")
                    import traceback
                    traceback.print_exc()
                    await websocket.send_json({
                        "type": "motion_state", "state": "error", "message": str(e)
                    })

            elif msg_type == "set_speed":
                new_speed = data.get("speed", 0.3)
                if auto_pilot is not None and hasattr(auto_pilot, 'set_speed'):
                    auto_pilot.set_speed(new_speed)

            elif msg_type == "auto_stop":
                if auto_pilot is not None:
                    await auto_pilot.stop()
                    auto_pilot = None
                try:
                    from visual_odometry import set_recording, save_trajectory_graph
                    set_recording(False)
                    save_trajectory_graph()
                    print("[Path] Запись остановлена, график сохранён")
                except ImportError:
                    pass
                await websocket.send_json({"type": "motion_state", "state": "stopped"})
                await broadcast_to_all({"type": "motion_state", "state": "stopped"})

            elif msg_type == "auto_pause":
                if auto_pilot is not None:
                    auto_pilot.pause()
                    state = "paused" if auto_pilot.paused else "running"
                    await websocket.send_json({"type": "motion_state", "state": state})
                    await broadcast_to_all({"type": "motion_state", "state": state})

            elif msg_type == "get_trajectories":
                await websocket.send_json({
                    "type": "trajectory_list",
                    "trajectories": ["custom"]
                })

            elif msg_type == "ping":
                await websocket.send_json({
                    "type": "pong",
                    "esp32_connected": esp32.connected,
                    "camera_active": camera_active,
                    "clients_count": len(active_ws_clients)
                })

            else:
                print(f"[WS] Неизвестный тип: {msg_type}")

    except WebSocketDisconnect:
        print(f"[WS] Клиент отключился: {client_host}")
    except Exception as e:
        print(f"[WS] Ошибка: {e}")
    finally:
        active_ws_clients.discard(websocket)


# ==================== Телеметрия ====================

async def telemetry_loop():
    while True:
        await asyncio.sleep(TELEMETRY_INTERVAL)
        if not esp32.connected:
            await esp32.connect()
            await asyncio.sleep(0.5)
            continue
        status = await esp32.get_status()
        if status is None:
            continue
        await broadcast_to_all({
            "type": "telemetry",
            "voltage": status["voltage"],
            "pwm": status["pwm"],
            "connected": esp32.connected
        })


# ==================== HTTP ====================

@app.get("/")
async def root():
    index_path = os.path.join(static_dir, "index.html")
    if not os.path.exists(index_path):
        return HTMLResponse(content="<h1>index.html не найден</h1>")
    with open(index_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/api/status")
async def api_status():
    status_data = {
        "esp32_connected": esp32.connected,
        "camera_active": camera_active,
        "odometry_running": odometry_running,
        "server_ip": local_ip or "unknown",
        "server_port": WEB_PORT,
        "ws_clients": len(active_ws_clients),
    }
    if esp32.connected:
        esp_status = await esp32.get_status()
        if esp_status:
            status_data.update({
                "voltage": esp_status["voltage"],
                "pwm": esp_status["pwm"]
            })
    return JSONResponse(content=status_data)


@app.get("/api/zone")
async def api_zone():
    from visual_odometry import zone
    return zone.copy()


@app.get("/qr")
async def get_qr():
    if not local_ip:
        return HTMLResponse(content="<h1>IP не определён</h1>")
    url = f"http://{local_ip}:{WEB_PORT}"
    qr_path = generate_qr_image(url)
    return HTMLResponse(content=f"""
    <html><head><title>BOBR 4x4 - QR</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>body{{background:#191919;color:#dcdcdc;text-align:center;padding:20px;font-family:sans-serif}}
    img{{max-width:300px;margin:20px auto;border-radius:10px}}
    .url{{background:#222;padding:10px;border-radius:5px;color:#baa98f;margin:20px}}</style>
    </head><body><h1>BOBR 4x4</h1><p>Отсканируйте QR-код</p>
    <img src="/static/{qr_path}" alt="QR"><div class="url">{url}</div></body></html>""")


# ==================== Жизненный цикл ====================

@app.on_event("startup")
async def startup():
    global local_ip, zeroconf_instance, main_event_loop
    main_event_loop = asyncio.get_running_loop()      # <-- сохраняем event loop главного потока

    local_ip = get_local_ip()
    url = f"http://{local_ip}:{WEB_PORT}"
    print("=" * 60)
    print("  BOBR 4x4 Proxy v3.4")
    print(f"  ESP32: {ESP32_HOST}:{ESP32_PORT}")
    print(f"  Сервер: {local_ip}:{WEB_PORT}")
    print(f"  URL: {url}")
    print("=" * 60)
    generate_qr_console(url)
    generate_qr_image(url)

    if ZEROCONF_AVAILABLE:
        try:
            zeroconf_instance = Zeroconf()
            info = ServiceInfo(
                "_http._tcp.local.", "BOBR 4x4._http._tcp.local.",
                addresses=[socket.inet_aton(local_ip)], port=WEB_PORT,
                properties={"path": "/"}
            )
            zeroconf_instance.register_service(info)
            print(f"  mDNS: http://bobr-4x4.local:{WEB_PORT}")
        except Exception as e:
            print(f"  mDNS: ошибка ({e})")

    await esp32.connect()
    asyncio.create_task(telemetry_loop())
    asyncio.create_task(odometry_monitor())
    print("=" * 60)


@app.on_event("shutdown")
async def shutdown():
    global odometry_running
    print("\n[Server] Завершение...")
    try:
        from visual_odometry import save_trajectory_graph
        save_trajectory_graph()
    except ImportError:
        pass
    odometry_running = False
    if auto_pilot is not None:
        try:
            await auto_pilot.stop()
        except Exception:
            pass
    if esp32.connected:
        await esp32.send_command("m 0 0 0 0")
    await esp32.disconnect()
    if zeroconf_instance:
        zeroconf_instance.close()
    print("[Server] ✓ Остановлен")


if __name__ == "__main__":
    uvicorn.run("proxy:app", host="0.0.0.0", port=WEB_PORT, reload=False, log_level="info")