# -*- mode: python ; coding: utf-8 -*-
import os
import re
import sys

# 从 app.py 读取版本号，自动生成带版本号的 exe 名（如 v1.6.1-模型蓄水池）
with open('app.py', encoding='utf-8') as _f:
    _ver = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', _f.read()).group(1)
EXE_NAME = f'v{_ver}-模型蓄水池'

IS_WIN = sys.platform == "win32"

# 应用图标（scripts/make_icon.py 生成）。缺文件时退回 PyInstaller 默认图标，
# 不让打包因为图标缺失而失败。
_ICON_ICO = os.path.join('assets', 'icon.ico')
_ICON_ICNS = os.path.join('assets', 'icon.icns')
_icon_file = _ICON_ICO if IS_WIN else (_ICON_ICNS if sys.platform == "darwin" else None)
if _icon_file and not os.path.exists(_icon_file):
    _icon_file = None

# 跨平台：数据文件与隐藏导入按平台裁剪
_datas = [('templates', 'templates'), ('models_meta.json', '.')]
if IS_WIN and os.path.exists('MicrosoftEdgeWebview2Setup.exe'):
    _datas.append(('MicrosoftEdgeWebview2Setup.exe', '.'))

_hiddenimports = ['PIL', 'PIL._tkinter_finder']
if IS_WIN:
    _hiddenimports.append('pystray._win32')
elif sys.platform == "darwin":
    _hiddenimports.append('pystray._darwin')
else:
    # Linux 托盘走纯 DBus SNI（start_linux_tray），不再依赖 pystray GTK 后端
    _hiddenimports += [
        'dbus', 'dbus.bus', 'dbus.connection', 'dbus.exceptions',
        'dbus.lowlevel', 'dbus.mainloop', 'dbus.mainloop.glib',
        'dbus.service', 'dbus.utils',
        '_dbus_bindings', '_dbus_glib_bindings',
    ]

# ---- Linux GUI：把 gi 类型库(Gtk/WebKit2 等)打进包，运行时钩子设 GI_TYPELIB_PATH ----
# 依赖目标机器装有 webkit2gtk-4.1（类型库加载的是系统 .so，不打进包内）
_runtime_hooks = []
if sys.platform.startswith('linux'):
    _gir_dirs = ['/usr/lib/girepository-1.0', '/usr/lib64/girepository-1.0']
    _gir = next((d for d in _gir_dirs if os.path.isdir(d)), None)
    if _gir:  # 没有类型库目录（如 CI 的精简容器）就退回 headless 模式
        _datas.append((_gir, 'gi/girepository-1.0'))
        _runtime_hooks.append('packaging/rthook_gi_typelib.py')
        _hiddenimports += [
            'gi', 'gi.repository',
            'gi.repository.GLib', 'gi.repository.GObject', 'gi.repository.Gio',
            'gi.repository.Gdk', 'gi.repository.Gtk', 'gi.repository.WebKit2',
            'pywebview', 'pywebview.platforms.gtk',
        ]


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=_datas,
    hiddenimports=_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=_runtime_hooks,
    excludes=['PySide6', 'shiboken6', 'numpy', 'matplotlib', 'scipy', 'pandas', 'PIL._tkinter_finder', 'tkinter', 'unittest', 'test', 'pytest', 'setuptools', 'pkg_resources', 'xmlrpc', 'ensurepip', 'lib2to3'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon_file,
)

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="ModelReservoir.app",
        icon=_ICON_ICNS if os.path.exists(_ICON_ICNS) else None,
        bundle_identifier="com.modelgateway.client",
    )
