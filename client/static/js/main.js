(function() {
    'use strict';
    
    // ==================== Константы ====================
    const MAX_PWM = 1023;
    const WS_URL = `ws://${location.hostname}:8000/ws`;
    const MAX_PATH_POINTS = 500;
    
    // ==================== Состояние ====================
    const state = {
        connected: false,
        ws: null,
        reconnectTimer: null,
        
        voltage: 0,
        mode: 'tank',
        speed: 0.6,
        pressedKeys: new Set(),
        activeButtons: new Set(),
        pwmValues: [0, 0, 0, 0],
        
        cameraActive: false,
        cameraStarting: false,
        cameraFps: 0,
        markerVisible: false,
        robotPose: null,
        
        motionActive: false,
        motionPaused: false,
        autoTrajectory: 'custom',   // всегда custom
        autoSpeed: 0.4,
        trajectoryPoints: [],
        targetPoint: null,
        progress: 0,
        
        // Стартовая и конечная позиции для графика
        startPose: null,
        endPose: null,

        arrivalRadius: 30,
    };
    
    let pathHistory = [];
    
    // ==================== DOM-элементы ====================
    const $ = (id) => document.getElementById(id);
   
    
    const dom = {
        connectionText: $('connectionText'),
        connectionDot: $('connectionDot'),
        
        batteryVoltage: $('batteryVoltage'),
        batteryBar: $('batteryBar'),
        speedSlider: $('speedSlider'),
        speedValue: $('speedValue'),
        btnW: $('btnW'), btnA: $('btnA'), btnS: $('btnS'), btnD: $('btnD'),
        btnQ: $('btnQ'), btnE: $('btnE'), btnStop: $('btnStop'),
        pwmValues: [1,2,3,4].map(i => $('pwmValue' + i)),
        pwmBars: [1,2,3,4].map(i => $('pwmBar' + i)),
        
        btnCameraStart: $('btnCameraStart'),
        btnCameraStop: $('btnCameraStop'),
        cameraStatus: $('cameraStatus'),
        cameraInfo: $('cameraInfo'),
        cameraFps: $('cameraFps'),
        cameraPose: $('cameraPose'),
        cameraMarker: $('cameraMarker'),
        
        customInfo: $('customInfo'),
        
        autoStart: $('autoStart'),
        autoPause: $('autoPause'),
        autoStop: $('autoStop'),
        autoSpeedSlider: $('autoSpeedSlider'),
        autoSpeedValue: $('autoSpeedValue'),
        motionStatus: $('motionStatus'),
        statusIndicator: $('statusIndicator'),
        statusText: $('statusText'),
        errorInfo: $('errorInfo'),
        progressContainer: $('progressContainer'),
        progressBar: $('progressBar'),
        progressText: $('progressText'),
        
        trajectoryPlot: $('trajectoryPlot'),
        trajectoryCanvas: $('trajectoryCanvas'),
        trajectoryPlaceholder: $('trajectoryPlaceholder'),
        btnClearPath: $('btnClearPath'),
        trajX: $('trajX'), trajY: $('trajY'),
        trajTheta: $('trajTheta'), trajSpeed: $('trajSpeed'),
        btnSaveGraph: $('btnSaveGraph'),
    };
     
    
    // ==================== Вкладки ====================
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const tab = btn.dataset.tab;
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            const content = document.getElementById('tab-' + tab);
            if (content) content.classList.add('active');
        });
    });
    
    // ==================== WebSocket ====================
    function connectWebSocket() {
        if (state.ws) { state.ws.close(); state.ws = null; }
        console.log('[WS] Подключение к', WS_URL);
        const ws = new WebSocket(WS_URL);
        state.ws = ws;
        
        ws.onopen = () => {
            console.log('[WS] Соединение установлено');
            state.connected = true;
            updateConnectionUI(true);
            if (state.reconnectTimer) { clearTimeout(state.reconnectTimer); state.reconnectTimer = null; }
        };
        
        ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                handleMessage(msg);
            } catch (e) {
                console.error('[WS] Ошибка парсинга:', e);
            }
        };
        
        ws.onclose = () => {
            console.log('[WS] Соединение закрыто');
            state.connected = false;
            state.ws = null;
            updateConnectionUI(false);
            resetPWMDisplay();
            state.reconnectTimer = setTimeout(connectWebSocket, 2000);
        };
        
        ws.onerror = (err) => console.error('[WS] Ошибка:', err);
    }
    
    function sendMessage(msg) {
        if (state.ws && state.ws.readyState === WebSocket.OPEN) {
            state.ws.send(JSON.stringify(msg));
        }
    }
    
    function handleMessage(msg) {
        switch (msg.type) {
            case 'telemetry':
                state.voltage = msg.voltage || 0;
                state.connected = msg.connected || false;
                updateConnectionUI(state.connected);
                updateBatteryUI(state.voltage);
                if (msg.pwm && msg.pwm.length === 4) {
                    state.pwmValues = msg.pwm;
                    updatePWMDisplay(msg.pwm);
                }
                break;
            
            case 'camera_state':
                state.cameraStarting = (msg.state === 'starting');
                state.cameraActive = (msg.state === 'started' || msg.state === 'running');
                updateCameraUI(msg.state);
                if (msg.fps !== undefined) state.cameraFps = msg.fps;
                if (msg.pose) { state.robotPose = msg.pose; state.markerVisible = true; }
                if (msg.marker !== undefined) state.markerVisible = msg.marker;
                updateCameraInfo();
                break;
            
                case 'auto_telemetry':
                    if (msg.pose) {
                        state.robotPose = msg.pose;
                        state.markerVisible = true;
                    } else {
                        state.markerVisible = false;
                    }
                    if (msg.arrival_radius !== undefined) {
                        state.arrivalRadius = msg.arrival_radius;
                    }
                    if (msg.fps !== undefined && msg.fps > 0) {
                        state.cameraFps = msg.fps;
                        dom.cameraFps.textContent = state.cameraFps.toFixed(1);
                    }
                    if (msg.target) state.targetPoint = msg.target;
                    if (msg.progress !== undefined) {
                        state.progress = msg.progress;
                        updateProgressBar();
                    }
                    updateCameraInfo();
                    if (state.cameraActive) {
                        updateTrajectoryPlot();
                    }
                    break;
            
            case 'motion_state':
                updateMotionUI(msg.state, msg.message);
                break;
            
            case 'marker_status':
                state.markerVisible = msg.visible;
                updateCameraInfo();
                break;
            
            case 'trajectory_preview':
                state.trajectoryPoints = msg.points || [];
                state.arrivalRadius = msg.arrival_radius || 30;
                drawTrajectoryPlot();
                break;
            
            case 'error':
                console.warn('[WS] Ошибка:', msg.message);
                dom.errorInfo.textContent = msg.message;
                break;
            
            case 'pong':
            case 'status':
                break;
        }
    }
    
    // ==================== UI: Связь ====================
    function updateConnectionUI(connected) {
        if (connected) {
            dom.connectionText.textContent = 'Подключено';
            dom.connectionDot.className = 'dot connected';
            dom.btnCameraStart.disabled = false;
        } else {
            dom.connectionText.textContent = 'Нет связи';
            dom.connectionDot.className = 'dot disconnected';
            dom.btnCameraStart.disabled = true;
            dom.btnCameraStop.disabled = true;
            dom.autoStart.disabled = true;
        }
    }
    
    function updateBatteryUI(voltage) {
        if (voltage <= 0) {
            dom.batteryVoltage.textContent = '--.- V';
            dom.batteryBar.style.width = '0%';
            return;
        }
        dom.batteryVoltage.textContent = voltage.toFixed(2) + ' V';
        const pct = Math.max(0, Math.min(100, ((voltage - 6.0) / (8.4 - 6.0)) * 100));
        dom.batteryBar.style.width = pct + '%';
        dom.batteryBar.classList.remove('warning', 'danger');
        if (voltage < 6.6) dom.batteryBar.classList.add('danger');
        else if (voltage < 7.0) dom.batteryBar.classList.add('warning');
    }
    
    function updatePWMDisplay(pwmValues) {
        for (let i = 0; i < 4; i++) {
            const val = pwmValues[i] || 0;
            dom.pwmValues[i].textContent = val;
            const barEl = dom.pwmBars[i];
            const pct = Math.abs(val) / MAX_PWM * 100;
            if (val >= 0) {
                barEl.style.width = (50 + pct / 2) + '%';
                barEl.classList.remove('negative');
            } else {
                barEl.style.width = (50 - pct / 2) + '%';
                barEl.classList.add('negative');
            }
        }
    }
    
    function resetPWMDisplay() { updatePWMDisplay([0, 0, 0, 0]); }
    
    // ==================== UI: Камера ====================
    function updateCameraUI(cameraState) {
        state.cameraActive = (cameraState === 'started' || cameraState === 'running');
        state.cameraStarting = (cameraState === 'starting');

        dom.btnCameraStart.disabled = state.cameraActive || state.cameraStarting;
        dom.btnCameraStop.disabled = !state.cameraActive;
        dom.autoStart.disabled = !state.cameraActive || state.motionActive;
        dom.btnClearPath.disabled = !state.cameraActive;
        dom.btnSaveGraph.disabled = !state.cameraActive;  

        if (state.cameraStarting) {
            dom.cameraStatus.textContent = '⏳ Запускается... (ждём ~15с)';
            dom.cameraStatus.style.color = '#f39c12';
            dom.cameraInfo.style.display = 'none';
            dom.trajectoryPlaceholder.style.display = 'none';
            dom.trajectoryPlot.style.display = 'block';
        } else if (state.cameraActive) {
            dom.cameraStatus.textContent = '✅ Работает';
            dom.cameraStatus.style.color = '#27ae60';
            dom.cameraInfo.style.display = 'block';
            dom.trajectoryPlaceholder.style.display = 'none';
            dom.trajectoryPlot.style.display = 'block';
        } else {
            dom.cameraStatus.textContent = 'Не запущена';
            dom.cameraStatus.style.color = '#888';
            dom.cameraInfo.style.display = 'none';
            dom.trajectoryPlaceholder.style.display = 'block';
            dom.trajectoryPlot.style.display = 'none';
            state.robotPose = null;
            state.markerVisible = false;
        }
    }
    
    function updateCameraInfo() {
        if (!state.cameraActive) return;
        dom.cameraFps.textContent = state.cameraFps > 0 ? state.cameraFps.toFixed(1) : '--';
        if (state.robotPose) {
            dom.cameraPose.textContent = 
                `(${state.robotPose.x?.toFixed(0) || '--'}, ${state.robotPose.y?.toFixed(0) || '--'}) ` +
                `${state.robotPose.theta ? (state.robotPose.theta * 180 / Math.PI).toFixed(0) + '°' : '--'}`;
        } else {
            dom.cameraPose.textContent = 'Поиск маркера...';
        }
        dom.cameraMarker.textContent = state.markerVisible ? 'Виден' : 'Потерян';
        dom.cameraMarker.style.color = state.markerVisible ? '#27ae60' : '#c0392b';
    }
    
    // ==================== UI: Движение ====================
    function updateMotionUI(motionState, message) {
        const wasActive = state.motionActive;
        state.motionActive = (motionState === 'started' || motionState === 'running');
        state.motionPaused = (motionState === 'paused');
        
        dom.autoStart.disabled = state.motionActive || !state.cameraActive;
        dom.autoPause.disabled = !state.motionActive;
        dom.autoStop.disabled = !state.motionActive;
        
        dom.statusIndicator.className = 'status-indicator';
        dom.progressContainer.style.display = 'none';
        
        if (state.motionActive && !wasActive) {
            pathHistory = [];
            state.startPose = state.robotPose ? {...state.robotPose} : null;
            state.endPose = null;
        }
        if (!state.motionActive && wasActive) {
            state.endPose = state.robotPose ? {...state.robotPose} : null;
            drawTrajectoryPlot();
        }
        
        switch (motionState) {
            case 'started':
            case 'running':
                dom.statusIndicator.classList.add('running');
                dom.statusText.textContent = 'Выполняется...';
                dom.motionStatus.textContent = 'Едет';
                dom.motionStatus.style.color = '#27ae60';
                dom.errorInfo.textContent = '';
                dom.progressContainer.style.display = 'block';
                break;
            
            case 'paused':
                dom.statusIndicator.classList.add('paused');
                dom.statusText.textContent = 'Пауза';
                dom.motionStatus.textContent = 'Пауза';
                dom.motionStatus.style.color = '#f39c12';
                dom.progressContainer.style.display = 'block';
                break;
            
            case 'lost':
                dom.statusIndicator.classList.add('lost');
                dom.statusText.textContent = message || 'Маркер потерян!';
                dom.motionStatus.textContent = 'Потерян';
                dom.motionStatus.style.color = '#e74c3c';
                dom.progressContainer.style.display = 'block';
                break;
            
            case 'completed':
                dom.statusIndicator.classList.add('completed');
                dom.statusText.textContent = 'Завершено!';
                dom.motionStatus.textContent = 'Остановлен';
                dom.motionStatus.style.color = '#888';
                dom.errorInfo.textContent = '';
                state.motionActive = false;
                dom.autoStart.disabled = !state.cameraActive;
                dom.autoPause.disabled = true;
                dom.autoStop.disabled = true;
                state.endPose = state.robotPose ? {...state.robotPose} : null;
                drawTrajectoryPlot();
                break;
            
            case 'error':
                dom.statusIndicator.classList.add('error');
                dom.statusText.textContent = 'Ошибка';
                dom.motionStatus.textContent = 'Ошибка';
                dom.motionStatus.style.color = '#e74c3c';
                dom.errorInfo.textContent = message || '';
                state.motionActive = false;
                dom.autoStart.disabled = !state.cameraActive;
                dom.autoPause.disabled = true;
                dom.autoStop.disabled = true;
                state.endPose = state.robotPose ? {...state.robotPose} : null;
                drawTrajectoryPlot();
                break;
            
            default:
                dom.statusIndicator.classList.add('stopped');
                dom.statusText.textContent = 'Готов к запуску';
                dom.motionStatus.textContent = 'Остановлен';
                dom.motionStatus.style.color = '#888';
                dom.errorInfo.textContent = '';
                if (wasActive) {
                    state.endPose = state.robotPose ? {...state.robotPose} : null;
                    drawTrajectoryPlot();
                }
        }
    }
    
    function updateProgressBar() {
        const pct = Math.round(state.progress * 100);
        dom.progressBar.style.width = pct + '%';
        dom.progressText.textContent = pct + '%';
    }
    
    // ==================== График траектории ====================
        function drawTrajectoryPlot() {
        const canvas = dom.trajectoryCanvas;
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        const w = canvas.width, h = canvas.height;

        // Фон
        ctx.fillStyle = '#111';
        ctx.fillRect(0, 0, w, h);

        // ===== СЕТКА =====
        const zoneW = currentZone.x2 - currentZone.x1;
        const zoneH = currentZone.y2 - currentZone.y1;
        const gridStep = 50; // шаг сетки в пикселях рабочей зоны

        ctx.strokeStyle = '#1a1a1a';
        ctx.lineWidth = 0.5;

        // Вертикальные линии
        for (let gx = currentZone.x1; gx <= currentZone.x2; gx += gridStep) {
            const x = ((gx - currentZone.x1) / zoneW) * w;
            ctx.beginPath();
            ctx.moveTo(x, 0);
            ctx.lineTo(x, h);
            ctx.stroke();
        }
        // Горизонтальные линии
        for (let gy = currentZone.y1; gy <= currentZone.y2; gy += gridStep) {
            const y = ((gy - currentZone.y1) / zoneH) * h;
            ctx.beginPath();
            ctx.moveTo(0, y);
            ctx.lineTo(w, y);
            ctx.stroke();
        }

        // ===== ОСИ =====
        ctx.strokeStyle = '#444';
        ctx.lineWidth = 1.5;
        // Ось X (верхняя граница)
        ctx.beginPath();
        ctx.moveTo(0, 0);
        ctx.lineTo(w, 0);
        ctx.stroke();
        // Ось Y (левая граница)
        ctx.beginPath();
        ctx.moveTo(0, 0);
        ctx.lineTo(0, h);
        ctx.stroke();

        // Подписи осей (значения пикселей)
        ctx.fillStyle = '#888';
        ctx.font = '9px monospace';
        ctx.textAlign = 'center';
        // Подписи по X
        for (let gx = currentZone.x1; gx <= currentZone.x2; gx += gridStep) {
            const x = ((gx - currentZone.x1) / zoneW) * w;
            ctx.fillText(gx, x, 10);
        }
        ctx.textAlign = 'right';
        // Подписи по Y
        for (let gy = currentZone.y1; gy <= currentZone.y2; gy += gridStep) {
            const y = ((gy - currentZone.y1) / zoneH) * h;
            ctx.fillText(gy, 20, y + 3);
        }

        // Функция перевода координат
        function toScreen(px, py) {
            const x = ((px - currentZone.x1) / zoneW) * w;
            const y = ((py - currentZone.y1) / zoneH) * h;
            return { x, y };
        }

        // ===== ЦЕЛЕВЫЕ ТОЧКИ (кольца) =====
        if (state.trajectoryPoints.length > 0) {
            const r = state.arrivalRadius || 30;
            for (const pt of state.trajectoryPoints) {
                const p = toScreen(pt.x, pt.y);
                // Оранжевое кольцо
                ctx.beginPath();
                ctx.arc(p.x, p.y, r, 0, 2 * Math.PI);
                ctx.strokeStyle = '#FF8C00';
                ctx.lineWidth = 2;
                ctx.stroke();
                // Точка в центре
                ctx.beginPath();
                ctx.arc(p.x, p.y, 2, 0, 2 * Math.PI);
                ctx.fillStyle = '#FF8C00';
                ctx.fill();
            }
        }

        // ===== ПРОЙДЕННЫЙ ПУТЬ (зелёный) =====
        if (pathHistory.length > 1) {
            ctx.beginPath();
            ctx.strokeStyle = '#00FF00';
            ctx.lineWidth = 2.5;
            const p0 = toScreen(pathHistory[0].x, pathHistory[0].y);
            ctx.moveTo(p0.x, p0.y);
            for (let i = 1; i < pathHistory.length; i++) {
                const pt = toScreen(pathHistory[i].x, pathHistory[i].y);
                ctx.lineTo(pt.x, pt.y);
            }
            ctx.stroke();
        }

        // ===== СТАРТОВАЯ ПОЗИЦИЯ (жёлтая) =====
        if (state.startPose) {
            const s = toScreen(state.startPose.x, state.startPose.y);
            ctx.beginPath();
            ctx.arc(s.x, s.y, 6, 0, 2 * Math.PI);
            ctx.fillStyle = '#FFFF00';
            ctx.fill();
            ctx.strokeStyle = '#000';
            ctx.lineWidth = 1;
            ctx.stroke();
            if (state.startPose.theta !== undefined) {
                const arrowLen = 15;
                ctx.beginPath();
                ctx.moveTo(s.x, s.y);
                ctx.lineTo(s.x + arrowLen * Math.cos(state.startPose.theta),
                        s.y - arrowLen * Math.sin(state.startPose.theta));
                ctx.strokeStyle = '#FFFF00';
                ctx.lineWidth = 2;
                ctx.stroke();
            }
        }

        // ===== КОНЕЧНАЯ ПОЗИЦИЯ (оранжевая) =====
        if (state.endPose) {
            const e = toScreen(state.endPose.x, state.endPose.y);
            ctx.beginPath();
            ctx.arc(e.x, e.y, 6, 0, 2 * Math.PI);
            ctx.fillStyle = '#FF8C00';
            ctx.fill();
            ctx.strokeStyle = '#000';
            ctx.lineWidth = 1;
            ctx.stroke();
            if (state.endPose.theta !== undefined) {
                const arrowLen = 15;
                ctx.beginPath();
                ctx.moveTo(e.x, e.y);
                ctx.lineTo(e.x + arrowLen * Math.cos(state.endPose.theta),
                        e.y - arrowLen * Math.sin(state.endPose.theta));
                ctx.strokeStyle = '#FF8C00';
                ctx.lineWidth = 2;
                ctx.stroke();
            }
        }

        // ===== ТЕКУЩАЯ ПОЗИЦИЯ (зелёная, только при движении) =====
        if (state.motionActive && state.robotPose) {
            const r = toScreen(state.robotPose.x, state.robotPose.y);
            const theta = state.robotPose.theta || 0;
            ctx.beginPath();
            ctx.arc(r.x, r.y, 7, 0, 2 * Math.PI);
            ctx.fillStyle = '#00FF00';
            ctx.fill();
            ctx.strokeStyle = '#000';
            ctx.lineWidth = 1;
            ctx.stroke();
            const arrowLen = 18;
            ctx.beginPath();
            ctx.moveTo(r.x, r.y);
            ctx.lineTo(r.x + arrowLen * Math.cos(theta),
                    r.y - arrowLen * Math.sin(theta));
            ctx.strokeStyle = '#FFFF00';
            ctx.lineWidth = 2;
            ctx.stroke();
        }
    }

    

        
    function updateTrajectoryPlot() {
        if (!state.cameraActive) return;
        
        if (state.motionActive && state.robotPose) {
            pathHistory.push({ x: state.robotPose.x, y: state.robotPose.y });
            if (pathHistory.length > MAX_PATH_POINTS) pathHistory = pathHistory.slice(-MAX_PATH_POINTS);
            
            dom.trajX.textContent = state.robotPose.x?.toFixed(0) || '--';
            dom.trajY.textContent = state.robotPose.y?.toFixed(0) || '--';
            dom.trajTheta.textContent = state.robotPose.theta 
                ? (state.robotPose.theta * 180 / Math.PI).toFixed(0) 
                : '--';
        }
        
        drawTrajectoryPlot();
    }

    function saveGraph() {
        const canvas = dom.trajectoryCanvas;
        if (!canvas) return;

        // Создаём ссылку для скачивания
        const link = document.createElement('a');
        link.download = 'trajectory_' + new Date().toISOString().slice(0,19).replace(/:/g, '-') + '.png';
        link.href = canvas.toDataURL('image/png');
        link.click();
    }
    
    // ==================== Ручное управление ====================
    function computeTankPWM() {
        const v = Math.round(MAX_PWM * state.speed);
        let left = 0, right = 0;
        if (state.pressedKeys.has('w')) { left = v; right = v; }
        if (state.pressedKeys.has('s')) { left = -v; right = -v; }
        if (state.pressedKeys.has('a')) { left = -v; right = v; }
        if (state.pressedKeys.has('d')) { left = v; right = -v; }
        if (state.pressedKeys.has('q')) { left = -v; right = v; }
        if (state.pressedKeys.has('e')) { left = v; right = -v; }
        return [left, right, left, right];
    }
    
    function computeOmniPWM() {
        const v = Math.round(MAX_PWM * state.speed);
        let x = 0, y = 0, r = 0;
        if (state.pressedKeys.has('w')) y = v;
        if (state.pressedKeys.has('s')) y = -v;
        if (state.pressedKeys.has('a')) x = -v;
        if (state.pressedKeys.has('d')) x = v;
        if (state.pressedKeys.has('q')) r = -Math.round(v * 0.6);
        if (state.pressedKeys.has('e')) r = Math.round(v * 0.6);
        return [y + x + r, y - x - r, y - x + r, y + x - r];
    }
    
    function computePWM() {
        let result = (state.mode === 'omni') ? computeOmniPWM() : computeTankPWM();
        const maxAbs = Math.max(...result.map(Math.abs), 1);
        if (maxAbs > MAX_PWM) {
            const scale = MAX_PWM / maxAbs;
            result = result.map(v => Math.round(v * scale));
        }
        return result.map(v => Math.round(v));
    }
    
    function sendMotorCommand() {
        sendMessage({ type: 'command', pwm: computePWM() });
        updatePWMDisplay(computePWM());
    }
    
    function stopAllMotors() {
        sendMessage({ type: 'stop' });
        state.pressedKeys.clear();
        state.activeButtons.clear();
        resetPWMDisplay();
        document.querySelectorAll('.ctrl-btn.active').forEach(btn => btn.classList.remove('active'));
    }
    
    const keyMap = {
        'w':'w','W':'w',
        'a':'a','A':'a',
        's':'s','S':'s',
        'd':'d','D':'d',
        'q':'q','Q':'q',
        'e':'e','E':'e',
    };
    
    document.addEventListener('keydown', (e) => {
        if (e.target.tagName === 'INPUT') return;
        const action = keyMap[e.key];
        if (action) {
            e.preventDefault();
            if (!state.pressedKeys.has(action)) {
                state.pressedKeys.add(action);
                sendMotorCommand();
                highlightButton(action, true);
            }
        }
        if (e.key === ' ' || e.code === 'Space') { e.preventDefault(); stopAllMotors(); }
    });
    
    document.addEventListener('keyup', (e) => {
        const action = keyMap[e.key];
        if (action) {
            e.preventDefault();
            state.pressedKeys.delete(action);
            sendMotorCommand();
            highlightButton(action, false);
        }
    });
    
    function highlightButton(action, active) {
        const btnMap = { w: dom.btnW, a: dom.btnA, s: dom.btnS, d: dom.btnD, q: dom.btnQ, e: dom.btnE };
        const btn = btnMap[action];
        if (btn) { if (active) btn.classList.add('active'); else btn.classList.remove('active'); }
    }
    
    function setupTouchButton(button, action) {
        button.addEventListener('pointerdown', (e) => {
            e.preventDefault();
            button.classList.add('active');
            state.activeButtons.add(action);
            state.pressedKeys.add(action);
            sendMotorCommand();
        });
        button.addEventListener('pointerup', (e) => {
            e.preventDefault();
            button.classList.remove('active');
            state.activeButtons.delete(action);
            state.pressedKeys.delete(action);
            sendMotorCommand();
        });
        button.addEventListener('pointerleave', (e) => {
            button.classList.remove('active');
            state.activeButtons.delete(action);
            state.pressedKeys.delete(action);
            sendMotorCommand();
        });
        button.addEventListener('contextmenu', (e) => e.preventDefault());
    }
    
    setupTouchButton(dom.btnW, 'w'); setupTouchButton(dom.btnA, 'a');
    setupTouchButton(dom.btnS, 's'); setupTouchButton(dom.btnD, 'd');
    setupTouchButton(dom.btnQ, 'q'); setupTouchButton(dom.btnE, 'e');
    
    dom.btnStop.addEventListener('pointerdown', (e) => { e.preventDefault(); stopAllMotors(); });
    dom.btnStop.addEventListener('contextmenu', (e) => e.preventDefault());
    
    document.querySelectorAll('.mode-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.mode-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            state.mode = btn.dataset.mode;
            stopAllMotors();
        });
    });
    
    // ==================== Слайдеры скорости ====================
    dom.speedSlider.addEventListener('input', () => {
        const raw = parseFloat(dom.speedSlider.value);
        const inverted = 1.2 - raw;
        state.speed = inverted;
        dom.speedValue.textContent = Math.round(raw * 100) + '%';
        if (state.pressedKeys.size > 0) sendMotorCommand();
    });
    
    dom.autoSpeedSlider.addEventListener('input', () => {
        const rawAuto = parseFloat(dom.autoSpeedSlider.value);
        const invertedAuto = 1.2 - rawAuto;
        state.autoSpeed = invertedAuto;
        dom.autoSpeedValue.textContent = Math.round(rawAuto * 100) + '%';
        sendMessage({ type: 'set_speed', speed: rawAuto });
    });
    
    // ==================== Камера ====================
    dom.btnCameraStart.addEventListener('click', () => {
        state.cameraStarting = true;
        sendMessage({ type: 'camera_start' });
        updateCameraUI('starting');
    });
    
    dom.btnCameraStop.addEventListener('click', () => {
        sendMessage({ type: 'camera_stop' });
        updateCameraUI('stopped');
        pathHistory = [];
        state.startPose = null;
        state.endPose = null;
        drawTrajectoryPlot();
    });
    
    // ==================== Движение ====================
    dom.autoStart.addEventListener('click', () => {
        pathHistory = [];
        state.trajectoryPoints = [];
        state.progress = 0;
        state.startPose = state.robotPose ? {...state.robotPose} : null;
        state.endPose = null;
        sendMessage({ type: 'auto_start', trajectory: state.autoTrajectory, speed: state.autoSpeed });
        updateMotionUI('started');
    });
    
    dom.autoPause.addEventListener('click', () => sendMessage({ type: 'auto_pause' }));
    
    dom.autoStop.addEventListener('click', () => {
        state.endPose = state.robotPose ? {...state.robotPose} : null;
        sendMessage({ type: 'auto_stop' });
        updateMotionUI('stopped');
    });
    
    dom.btnClearPath.addEventListener('click', () => {
        pathHistory = [];
        state.startPose = null;
        state.endPose = null;
        state.trajectoryPoints = [];   // <-- вот это
        drawTrajectoryPlot();
        sendMessage({ type: 'clear_path' });
    });
    
    setInterval(() => { if (state.ws && state.ws.readyState === WebSocket.OPEN) sendMessage({ type: 'ping' }); }, 10000);

    // Обновление зоны и размера canvas каждые 2 секунды
    let currentZone = { x1: 0, y1: 0, x2: 640, y2: 480 }; // по умолчанию

    async function updateZone() {
        try {
            const resp = await fetch('/api/zone');
            if (resp.ok) {
                const newZone = await resp.json();
                if (newZone.x1 !== currentZone.x1 || newZone.y1 !== currentZone.y1 ||
                    newZone.x2 !== currentZone.x2 || newZone.y2 !== currentZone.y2) {
                    currentZone = newZone;
                    resizeCanvas();
                    drawTrajectoryPlot();
                }
            }
        } catch (e) {}
    }

    function resizeCanvas() {
        const canvas = dom.trajectoryCanvas;
        const zoneW = currentZone.x2 - currentZone.x1;
        const zoneH = currentZone.y2 - currentZone.y1;
        if (zoneW <= 0 || zoneH <= 0) return;
        const aspect = zoneH / zoneW;
        // Ширина canvas фиксирована (400px), высоту подгоняем под пропорции зоны
        canvas.width = 400;
        canvas.height = Math.round(400 * aspect);
    }

    setInterval(updateZone, 2000);
    updateZone(); // сразу при загрузке
    
    function init() {
        console.log('IVAN SMIT Web UI v2.3');
        connectWebSocket();
        updateConnectionUI(false);
        resetPWMDisplay();
        dom.trajectoryPlot.style.display = 'none';
        dom.trajectoryPlaceholder.style.display = 'block';
        dom.cameraInfo.style.display = 'none';
        dom.progressContainer.style.display = 'none';
        // customInfo теперь всегда виден, не скрываем
        drawTrajectoryPlot();
        dom.btnSaveGraph.addEventListener('click', saveGraph);
    }
    
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();