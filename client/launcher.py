"""
Запускает proxy.py, открывает страницу управления, показывает QR-код и логи.
Статус "активно" и кнопка "Открыть панель управления" появляются только
после успешного TCP-подключения к ESP32 (опрос FastAPI).
"""

import subprocess
import sys
import os
import re
import qrcode
import io
import base64
import threading
import queue
import time
import webbrowser
import urllib.request
import json
from flask import Flask, render_template_string, jsonify, request

app = Flask(__name__)

proxy_process = None
proxy_stdout = None
log_queue = queue.Queue()
logs = []
qr_base64 = None
proxy_url = None

ROBOT_WIFI_SSID = "BOBR_4x4"


def get_current_wifi():
    """Определяет текущую Wi-Fi сеть (Windows)."""
    try:
        result = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True, text=True, timeout=5,
            encoding='utf-8', errors='replace'
        )
        for line in result.stdout.split('\n'):
            if "SSID" in line and "BSSID" not in line:
                return line.split(":")[1].strip()
    except Exception:
        pass
    return None


def generate_qr_base64(url: str) -> str:
    """Генерирует QR-код и возвращает base64 для вставки в HTML."""
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L,
                      box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def check_fastapi_connected():
    """
    Проверяет, установлено ли TCP-соединение с ESP32.
    Опрашивает FastAPI на локальном порту 8000.
    Возвращает True, если esp32_connected == true.
    """
    try:
        req = urllib.request.urlopen("http://127.0.0.1:8000/api/status", timeout=0.5)
        data = json.loads(req.read().decode())
        return data.get("esp32_connected", False)
    except Exception:
        return False


def stream_proxy_output(stdout):
    """Читает stdout proxy.py и складывает в очередь."""
    global proxy_process, qr_base64, proxy_url

    try:
        for line in iter(stdout.readline, ''):
            if not line:
                break
            logs.append(line.strip())
            if len(logs) > 200:
                logs.pop(0)

            url_match = re.search(r'(http://[\d.]+:\d+)', line)
            if url_match and not proxy_url:
                proxy_url = url_match.group(1)
                qr_base64 = generate_qr_base64(proxy_url)

            log_queue.put(line.strip())
    except Exception:
        pass
    finally:
        try:
            stdout.close()
        except Exception:
            pass
        if proxy_process:
            proxy_process.wait()
            proxy_process = None
        log_queue.put("__PROXY_STOPPED__")


def is_proxy_running():
    """Проверяет, жив ли процесс proxy.py."""
    return proxy_process is not None and proxy_process.poll() is None


def is_wifi_ok():
    """Проверяет, подключены ли мы к нужной Wi-Fi сети."""
    wifi = get_current_wifi()
    return wifi == ROBOT_WIFI_SSID


@app.route('/')
def index():
    wifi = get_current_wifi()
    wifi_ok = (wifi == ROBOT_WIFI_SSID) if wifi else False

    return render_template_string(HTML_TEMPLATE,
                                  wifi=wifi or "Не подключено",
                                  wifi_ok=wifi_ok,
                                  robot_ssid=ROBOT_WIFI_SSID,
                                  proxy_running=is_proxy_running(),
                                  proxy_url=proxy_url or "",
                                  tcp_connected=check_fastapi_connected())


@app.route('/start')
def start_proxy():
    global proxy_process, proxy_stdout, qr_base64, logs, proxy_url

    if not is_wifi_ok():
        return jsonify({"status": "error", "message": "Подключитесь к Wi-Fi сети " + ROBOT_WIFI_SSID})

    if is_proxy_running():
        return jsonify({"status": "already_running", "url": proxy_url})

    logs = []
    qr_base64 = None
    proxy_url = None

    proxy_path = os.path.join(os.path.dirname(__file__), "proxy.py")

    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'

    proxy_process = subprocess.Popen(
        [sys.executable, proxy_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding='utf-8',
        errors='replace',
        env=env
    )
    proxy_stdout = proxy_process.stdout

    threading.Thread(target=stream_proxy_output, args=(proxy_stdout,), daemon=True).start()

    # Ждём появления URL (обычно 1-2 секунды)
    for _ in range(50):
        if proxy_url:
            break
        time.sleep(0.1)

    return jsonify({"status": "started", "url": proxy_url})


@app.route('/stop')
def stop_proxy():
    global proxy_process, proxy_url, proxy_stdout
    if proxy_process:
        try:
            proxy_process.terminate()
        except Exception:
            pass
        proxy_process = None
    proxy_stdout = None
    proxy_url = None
    return jsonify({"status": "stopped"})


@app.route('/status')
def proxy_status():
    return jsonify({
        "running": is_proxy_running(),
        "url": proxy_url,
        "wifi_ok": is_wifi_ok(),
        "tcp_connected": check_fastapi_connected()
    })


@app.route('/logs')
def get_logs():
    items = []
    proxy_died = False

    while not log_queue.empty():
        try:
            msg = log_queue.get_nowait()
            if msg == "__PROXY_STOPPED__":
                proxy_died = True
            else:
                items.append(msg)
        except queue.Empty:
            break

    return jsonify({
        "logs": items,
        "qr": qr_base64,
        "running": is_proxy_running(),
        "proxy_died": proxy_died,
        "url": proxy_url,
        "wifi_ok": is_wifi_ok(),
        "tcp_connected": check_fastapi_connected()
    })


@app.route('/wifi')
def wifi_status():
    wifi = get_current_wifi()
    return jsonify({
        "current": wifi or "Не подключено",
        "target": ROBOT_WIFI_SSID,
        "ok": wifi == ROBOT_WIFI_SSID
    })


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate">
    <meta http-equiv="Pragma" content="no-cache">
    <meta http-equiv="Expires" content="0">
    <title>AUX Подключение</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            background: #191919;
            color: #dcdcdc;
            font-family: 'Segoe UI', sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
        }
        .container {
            text-align: center;
            max-width: 90vw;
            width: 900px;
            padding: 40px;
        }
        h1 { font-size: 28px; margin-bottom: 8px; }
        h1 span { color: #baa98f; }
        .subtitle { color: #888; font-size: 14px; margin-bottom: 40px; }


        .btn-connect {
            width: 180px; height: 180px;
            min-width: 180px;
            border-radius: 50%;
            border: 3px solid #baa98f;
            background: #222;
            color: #baa98f;
            font-size: 22px;
            cursor: pointer;
            transition: all 0.3s;
            display: flex;
            align-items: center;
            justify-content: center;
            user-select: none;
            flex-shrink: 0;
        }
        .btn-connect:hover { background: #baa98f; color: #191919; }
        .btn-connect:disabled { opacity: 0.4; cursor: not-allowed; }
        .btn-connect.running { border-color: #27ae60; color: #27ae60; }
        .btn-connect.running:hover { background: #27ae60; color: #191919; }
        .btn-connect.dead { border-color: #e74c3c; color: #e74c3c; }
        .btn-connect.dead:hover { background: #e74c3c; color: #fff; }
        .btn-connect.starting { border-color: #f39c12; color: #f39c12; animation: pulse 1s infinite; }
        .btn-connect.blocked { border-color: #555; color: #555; cursor: not-allowed; }

        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }


        .status { font-size: 14px; margin-bottom: 8px; }
        .status-ok { color: #27ae60; }
        .status-bad { color: #e74c3c; }
        .status-pending { color: #f39c12; }

        .proxy-status { font-size: 12px; margin-bottom: 8px; }
        .proxy-status .alive { color: #27ae60; }
        .proxy-status .dead { color: #e74c3c; }
        .proxy-status .starting { color: #f39c12; }

        .wifi-hint {
            font-size: 12px;
            color: #e74c3c;
            margin-top: 8px;
            display: none;
        }
        .wifi-hint.show { display: block; }

        .link-box { margin: 8px 0; display: none; }
        .link-box.show { display: block; }
        .link-box a {
            display: inline-block;
            background: #27ae60;
            color: #fff;
            padding: 8px 18px;
            border-radius: 6px;
            text-decoration: none;
            font-size: 14px;
            transition: background 0.2s;
        }
        .link-box a:hover { background: #219a52; }

        .top-row {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 20px;
        text-align: center;
        width: 100%;
        }

        .info-col {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 10px;
        width: 100%;
        }

        .qr-box {
        display: none;
        flex-direction: column;
        align-items: center;
        margin-top: 5px;
        }

        .qr-box.show { display: block; }
        .qr-box img { border-radius: 10px; max-width: 160px; }
        .qr-box p { font-size: 11px; color: #888; margin-top: 6px; }

        .logs {
            background: #111;
            border-radius: 8px;
            padding: 12px 16px;
            margin-top: 20px;
            max-height: 150px;
            overflow-y: auto;
            text-align: left;
            font-family: 'Courier New', monospace;
            font-size: 11px;
            color: #aaa;
            display: none;
        }

        .logs.show { display: block; }
        .log-line { margin: 2px 0; word-break: break-all; white-space: pre-wrap; }
        .log-line.warn { color: #f39c12; }
        .log-line.error { color: #e74c3c; }
        .log-line.success { color: #27ae60; }
        .log-line.info { color: #aaa; }
    </style>
</head>
<body>
    <div class="container">
        <h1 style ="letter-spacing: 0.1em; font-family: Mokoto, 'Mokoto Glitch', sans-serif; font-size: 36px; font-weight: 200;text-shadow: 0 0 4px rgba(255, 255, 255, 0.8);">AUXILIUM</h1>
        <p class="subtitle">Одна база. Любая платформа </p>

        <div class="top-row">
            <button class="btn-connect {% if proxy_running %}running{% elif not wifi_ok %}blocked{% endif %}"
                    id="btnConnect"
                    onclick="toggleProxy()"
                    {% if not wifi_ok %}disabled{% endif %}>
                {% if not wifi_ok %}Нет Wi-Fi{% elif proxy_running %}Запущен{% else %}Подключиться{% endif %}
            </button>

            <div class="info-col">
                <div class="status">
                    Wi-Fi:
                    <span id="wifiStatus">
                        {% if wifi_ok %}
                            <span class="status-ok">Подключено к {{ wifi }}</span>
                        {% elif wifi and wifi != 'Не подключено' %}
                            <span class="status-bad">{{ wifi }} (нужна {{ robot_ssid }})</span>
                        {% else %}
                            <span class="status-pending">Не подключено</span>
                        {% endif %}
                    </span>
                </div>

                <div class="proxy-status" id="proxyStatus" {% if not proxy_running %}style="display: none;"{% endif %}>
                    Proxy: <span id="proxyStatusText">
                        {% if proxy_running %}<span class="starting">запуск...</span>{% endif %}
                    </span>
                </div>

                <div class="wifi-hint {% if not wifi_ok %}show{% endif %}" id="wifiHint">
                    Подключитесь к Wi-Fi сети <strong>{{ robot_ssid }}</strong> для запуска
                </div>

                <div class="link-box" id="linkBox">
                    <a href="#" target="_blank" id="controlLink">Открыть панель управления</a>
                </div>

                <div class="qr-box" id="qrBox">
                    <img id="qrImage" src="" alt="QR-код">
                    <p>Отсканируйте для подключения с телефона</p>
                </div>
            </div>
        </div>

        <div class="logs" id="logsBox">
            <div id="logsContent"></div>
        </div>
    </div>

    <script>
        let pollTimer = null;
        let statusTimer = null;
        let proxyReady = false;

        function updateWifiUI(ok, current, target) {
            const btn = document.getElementById('btnConnect');
            const wifiHint = document.getElementById('wifiHint');
            const statusEl = document.getElementById('wifiStatus');

            if (ok) {
                statusEl.innerHTML = '<span class="status-ok">Подключено к ' + current + '</span>';
                wifiHint.classList.remove('show');
                if (!btn.classList.contains('running') && !btn.classList.contains('starting')) {
                    btn.disabled = false;
                    btn.classList.remove('blocked');
                    btn.textContent = 'Подключиться';
                }
            } else if (current !== 'Не подключено') {
                statusEl.innerHTML = '<span class="status-bad">' + current + ' (нужна ' + target + ')</span>';
                wifiHint.classList.add('show');
                if (!btn.classList.contains('running')) {
                    btn.disabled = true;
                    btn.classList.add('blocked');
                    btn.textContent = 'Нет Wi-Fi';
                }
            } else {
                statusEl.innerHTML = '<span class="status-pending">Не подключено</span>';
                wifiHint.classList.add('show');
                if (!btn.classList.contains('running')) {
                    btn.disabled = true;
                    btn.classList.add('blocked');
                    btn.textContent = 'Нет Wi-Fi';
                }
            }
        }

        async function toggleProxy() {
            const btn = document.getElementById('btnConnect');

            if (btn.classList.contains('blocked')) return;

            if (btn.classList.contains('running') || btn.classList.contains('dead')) {
                btn.disabled = true;
                btn.textContent = '...';
                btn.className = 'btn-connect';
                await fetch('/stop');
                btn.textContent = 'Подключиться';
                btn.disabled = false;
                document.getElementById('proxyStatus').style.display = 'none';
                document.getElementById('linkBox').classList.remove('show');
                document.getElementById('logsContent').innerHTML = '';
                document.getElementById('logsBox').classList.remove('show');
                document.getElementById('qrBox').classList.remove('show');
                proxyReady = false;
                if (pollTimer) clearInterval(pollTimer);
                if (statusTimer) clearInterval(statusTimer);
            } else {
                btn.disabled = true;
                btn.textContent = '...';
                btn.classList.add('starting');
                document.getElementById('proxyStatus').style.display = 'block';
                document.getElementById('proxyStatusText').innerHTML = '<span class="starting">запускается...</span>';

                const resp = await fetch('/start');
                const data = await resp.json();

                if (data.status === 'started' || data.status === 'already_running') {
                    btn.className = 'btn-connect running';
                    btn.textContent = 'Запущен';
                    btn.disabled = false;
                    document.getElementById('proxyStatusText').innerHTML = '<span class="starting">ожидание подключения к ESP32...</span>';
                    document.getElementById('logsBox').classList.add('show');

                    pollTimer = setInterval(pollLogs, 300);
                    statusTimer = setInterval(checkProxyStatus, 3000);
                } else if (data.status === 'error') {
                    btn.className = 'btn-connect blocked';
                    btn.textContent = 'Нет Wi-Fi';
                    btn.disabled = true;
                    document.getElementById('proxyStatusText').innerHTML = '<span class="dead">' + data.message + '</span>';
                } else {
                    btn.className = 'btn-connect';
                    btn.textContent = 'Ошибка';
                    btn.disabled = false;
                    document.getElementById('proxyStatusText').innerHTML = '<span class="dead">ошибка запуска</span>';
                }
            }
        }

        async function checkProxyStatus() {
            try {
                const resp = await fetch('/status');
                const data = await resp.json();
                const statusEl = document.getElementById('proxyStatusText');
                const btn = document.getElementById('btnConnect');

                updateWifiUI(data.wifi_ok, '', '{{ robot_ssid }}');

                if (data.running) {
                    if (data.tcp_connected) {
                        if (!proxyReady && data.url) {
                            proxyReady = true;
                            document.getElementById('controlLink').href = data.url;
                            document.getElementById('linkBox').classList.add('show');
                        }
                        statusEl.innerHTML = '<span class="alive">активно</span>';
                    } else {
                        statusEl.innerHTML = '<span class="starting">ожидание подключения к ESP32...</span>';
                    }
                    btn.className = 'btn-connect running';
                    btn.textContent = 'Запущен';
                    btn.disabled = false;
                } else {
                    statusEl.innerHTML = '<span class="dead">остановлен</span>';
                    btn.className = 'btn-connect';
                    btn.textContent = 'Подключиться';
                    btn.disabled = data.wifi_ok ? false : true;
                    document.getElementById('linkBox').classList.remove('show');
                    proxyReady = false;
                    if (pollTimer) clearInterval(pollTimer);
                }
            } catch (e) {}
        }

        async function pollLogs() {
            try {
                const resp = await fetch('/logs');
                const data = await resp.json();

                if (data.qr) {
                    document.getElementById('qrBox').classList.add('show');
                    document.getElementById('qrImage').src = 'data:image/png;base64,' + data.qr;
                }

                // Показываем кнопку, когда TCP-подключение установлено и URL известен
                if (data.tcp_connected && data.url && !proxyReady) {
                    proxyReady = true;
                    document.getElementById('controlLink').href = data.url;
                    document.getElementById('linkBox').classList.add('show');
                }

                if (data.proxy_died) {
                    const btn = document.getElementById('btnConnect');
                    btn.className = 'btn-connect';
                    btn.textContent = 'Подключиться';
                    btn.disabled = data.wifi_ok ? false : true;
                    document.getElementById('proxyStatusText').innerHTML = '<span class="dead">упал</span>';
                    document.getElementById('linkBox').classList.remove('show');
                    proxyReady = false;
                    if (statusTimer) clearInterval(statusTimer);
                    if (pollTimer) clearInterval(pollTimer);
                    pollTimer = null;
                }

                if (data.logs && data.logs.length > 0) {
                    const content = document.getElementById('logsContent');
                    for (const line of data.logs) {
                        const div = document.createElement('div');
                        div.className = 'log-line';
                        if (line.includes('ERROR') || line.includes('Traceback')) div.classList.add('error');
                        else if (line.includes('WARNING')) div.classList.add('warn');
                        else if (line.includes('[OK]') || line.includes('INFO') || line.includes('===')) div.classList.add('success');
                        else div.classList.add('info');
                        div.textContent = line;
                        content.appendChild(div);
                    }
                    const box = document.getElementById('logsBox');
                    box.scrollTop = box.scrollHeight;
                }

                if (!data.running && pollTimer) {
                    clearInterval(pollTimer);
                    pollTimer = null;
                }
            } catch (e) {}
        }

        // Wi-Fi статус
        setInterval(async () => {
            try {
                const resp = await fetch('/wifi');
                const data = await resp.json();
                updateWifiUI(data.ok, data.current, data.target);
            } catch (e) {}
        }, 3000);

        // Инициализация
        (async function init() {
            try {
                const resp = await fetch('/status');
                const data = await resp.json();
                updateWifiUI(data.wifi_ok, '', '{{ robot_ssid }}');
                if (data.running) {
                    const btn = document.getElementById('btnConnect');
                    btn.className = 'btn-connect running';
                    btn.textContent = 'Запущен';
                    btn.disabled = false;
                    document.getElementById('proxyStatus').style.display = 'block';
                    if (data.tcp_connected) {
                        document.getElementById('proxyStatusText').innerHTML = '<span class="alive">активно</span>';
                        if (data.url) {
                            document.getElementById('controlLink').href = data.url;
                            document.getElementById('linkBox').classList.add('show');
                            proxyReady = true;
                        }
                    } else {
                        document.getElementById('proxyStatusText').innerHTML = '<span class="starting">ожидание подключения к ESP32...</span>';
                    }
                    document.getElementById('logsBox').classList.add('show');
                    pollTimer = setInterval(pollLogs, 300);
                    statusTimer = setInterval(checkProxyStatus, 3000);
                }
            } catch (e) {}
        })();
    </script>
</body>
</html>
"""

if __name__ == '__main__':
    import webbrowser
    import threading

    print("=" * 50)
    print("  AUXILIUM Launcher")
    print("  http://localhost:5000")
    print("=" * 50)

    def open_browser():
        time.sleep(0.8)
        try:
            webbrowser.open('http://localhost:5000')
        except Exception:
            pass

    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, debug=False)