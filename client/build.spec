# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

# Определяем корневую папку (где лежит этот spec)
ROOT = Path.cwd()

# Файлы, которые нужно скопировать в дистрибутив
ADDITIONAL_DATA = [
    # статические файлы веб-интерфейса
    ('static', 'static'),
    # дополнительные Python-модули, которые не импортируются явно, но нужны
    ('proxy.py', '.'),
    ('planner.py', '.'),
    ('visual_odometry.py', '.'),
    ('config.py', '.'),
    # папка с графиками (будет создана при первом сохранении, но можно и сейчас)
    ('graphs', 'graphs'),
    # шрифты, если они используются в HTML (необязательно)
    # ('fonts', 'fonts'),
]

a = Analysis(
    ['launcher.py'],          # точка входа
    pathex=[],
    binaries=[],
    datas=ADDITIONAL_DATA,
    hiddenimports=[
        'cv2',
        'numpy',
        'fastapi',
        'uvicorn',
        'flask',
        'qrcode',
        'PIL',
        'zeroconf',           # если нужен mDNS
        'visual_odometry',    # явно указываем скрытые импорты
        'planner',
        'config',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AUXILIUM_Launcher',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,            # консольное приложение (показывать терминал)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='logo.png',             
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='AUXILIUM',
)