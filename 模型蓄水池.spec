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
    # excludes：三端 GUI 后端都用不到的东西，实测曾占 21MB（2026-10-20 拆包统计）
    # - PyQt5/PyQt6 由 pywebview 拖进来（webview/guilib.py 的可选后端）。
    #   实测后端链：Linux=[gtk, qt]、Windows=[winforms]、macOS=[cocoa, qt] ——
    #   Qt 在 Windows/macOS 根本不在链上，Linux 也是 gtk 优先；每个 import 都包在
    #   try/except ImportError 里失败返回 False，所以排除它只会让 fallback 生效，不会崩。
    # - uvloop 是 uvicorn 的可选 loop，没有它就用默认 asyncio（uvicorn 自己会 ImportError 分支）。
    #   实测 4MB。
    excludes=['PySide6', 'shiboken6', 'PyQt5', 'PyQt6', 'PyQt5_sip', 'PyQt6_sip',
              'pywebview.platforms.qt', 'uvloop',
              'numpy', 'matplotlib', 'scipy', 'pandas', 'PIL._tkinter_finder', 'tkinter',
              'unittest', 'test', 'pytest', 'setuptools', 'pkg_resources', 'xmlrpc',
              'ensurepip', 'lib2to3'],
    noarchive=False,
    optimize=0,
)
# 砍掉 PyInstaller 的 GTK hook 收进来的整套系统图标主题（hook-gi.repository.Gtk.py 里的
# collect_glib_share_files('icons')）。实测 44.27MB / 5 万多个文件，里面还混着
# ghost-downloader、motrix、heroic、baidunetdisk 等**本机其它应用**的图标 ——
# 我们自己带图标（assets/icon.ico 进窗口），GTK 运行时用的是主机 /usr/share/icons，
# 这份副本只在主机一个主题都没有时才用得上，而那不是可用的桌面环境。
# share/themes（0.46MB，GTK 样式）与 share/locale（6.85MB，GTK 弹窗翻译）刻意保留：
# 砍 locale 会让文件选择框等原生控件变英文，收益不抵行为变化。
def _keep_data(entry) -> bool:
    for part in entry:
        path = str(part).replace("\\", "/")
        if path.startswith("share/icons") or "/share/icons" in path:
            return False
    return True

_before = len(a.datas)
a.datas = [d for d in a.datas if _keep_data(d)]
if _before != len(a.datas):
    import sys as _sys
    print(f"[spec] 剔除系统图标数据 {_before - len(a.datas)} 项 "
          f"（share/icons）", file=_sys.stderr)

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
