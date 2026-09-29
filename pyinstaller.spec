# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build specification for Core2Chat.

Reproducible by design: every version is pinned in requirements*.txt, all data
files are listed explicitly, and hidden imports are limited to what the lazy
import strategy actually requires.

Build commands
--------------
Use the helper script, which pins the mode and prints the resulting paths::

    python build.py --mode release      # onedir, windowed  (recommended)
    python build.py --mode debug        # onedir, console + verbose logging
    python build.py --mode onefile      # single executable (slower cold start)

Equivalent raw commands::

    pyinstaller --clean --noconfirm pyinstaller.spec
    pyinstaller --clean --noconfirm --onefile --name Core2Chat ^
        --icon assets/icon.ico --add-data "assets;assets" main.py

onedir is the recommended mode for Windows 7: it starts faster than onefile
(no self-extraction step) and keeps Qt plugins on disk where they are found
reliably. The produced executable must never contain an API key - credentials
live in DPAPI-protected per-user storage at runtime (spec §27, §54).
"""

import os
import sys

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

PROJECT_ROOT = os.path.abspath(os.getcwd())
IS_WINDOWS = sys.platform == "win32"

# Assets are shipped next to the executable; gui/theme.py and utils/paths.py
# resolve them through sys._MEIPASS as well as the executable directory.
DATAS = [
    (os.path.join("assets", "style_dark.qss"), "assets"),
    (os.path.join("assets", "icon.png"), "assets"),
    (os.path.join("assets", "icon.ico"), "assets"),
    (os.path.join("assets", "logo.png"), "assets"),
    (os.path.join("assets", "icon_16.png"), "assets"),
]
DATAS = [(src, dst) for src, dst in DATAS if os.path.isfile(src)]

# Lazy imports (httpx, gui dialogs, PyQt5 modules used only on demand) must be
# discovered explicitly; PyInstaller cannot see them through static analysis of
# function-level imports in every case.
HIDDEN_IMPORTS = [
    "httpx", "httpcore", "h11", "certifi", "idna", "sniffio", "anyio",
    "sqlite3", "ctypes", "encodings.idna",
]
HIDDEN_IMPORTS += collect_submodules("PyQt5", filter=
                                      lambda name: "QtWebEngine" not in name
                                      and "QtWebKit" not in name
                                      and "Qt3D" not in name
                                      and "QtQuick" not in name
                                      and "QtQml" not in name)

# Explicitly excluded: heavyweight or forbidden stacks (spec §0, §69).
EXCLUDES = [
    "PyQt5.QtWebEngine", "PyQt5.QtWebEngineCore", "PyQt5.QtWebEngineWidgets",
    "PyQt5.QtWebKit", "PyQt5.QtWebKitWidgets", "PyQt5.Qt3DCore",
    "PyQt5.Qt3DRender", "PyQt5.QtQuick", "PyQt5.QtQuickWidgets",
    "PyQt5.QtQml", "PyQt5.QtMultimedia", "PyQt5.QtDesigner",
    "PyQt5.QtTest", "PyQt5.QtSql", "PyQt5.QtNetwork",
    "PySide2", "PySide6", "PyQt6", "tkinter", "matplotlib", "numpy",
    "pandas", "scipy", "PIL", "pytest", "pyinstaller", "setuptools",
    "pip", "notebook", "IPython", "qasync", "google.genai",
    "google.generativeai", "electron",
]

# PyQt5 binaries that Core2Chat never imports. The application uses exactly
# QtCore, QtGui and QtWidgets; everything else is dead weight in the bundle
# (measured: 52 MB of PyQt5 extensions plus their Qt libraries).
#
# Removal is verified after the build by `build.py --verify-dist`, which walks
# the dynamic dependency graph of every kept binary and fails if anything
# references a dropped library. Nothing is removed on the strength of a list
# alone.
UNWANTED_PYQT5_PREFIXES = (
    "PyQt5/QtBluetooth", "PyQt5/QtHelp", "PyQt5/QtLocation",
    "PyQt5/QtMultimedia", "PyQt5/QtNfc", "PyQt5/QtOpenGL",
    "PyQt5/QtPositioning", "PyQt5/QtPrintSupport", "PyQt5/QtQuick",
    "PyQt5/QtQml", "PyQt5/QtRemoteObjects", "PyQt5/QtSensors",
    "PyQt5/QtSerialPort", "PyQt5/QtSvg", "PyQt5/QtTextToSpeech",
    "PyQt5/QtWebChannel", "PyQt5/QtWebSockets", "PyQt5/QtXml",
    "PyQt5/QtXmlPatterns", "PyQt5/QtX11Extras", "PyQt5/Qt3D",
    "PyQt5/QtTest", "PyQt5/QtSql", "PyQt5/QtDesigner", "PyQt5/QtNetwork",
    "PyQt5/QtConcurrent", "PyQt5/_QOpenGLFunctions", "PyQt5/pylupdate",
    "PyQt5/pyrcc", "PyQt5/uic",
)
# Qt *plugin* directories that belong to the modules above. They must go
# together with the libraries, otherwise a plugin survives and links against a
# removed library (this is exactly what `build.py --verify-dist` catches).
# Kept: platforms (qwindows/qxcb), imageformats (png/jpeg - needed by QIcon),
# iconengines, styles, platformthemes, xcbglintegrations, platforminputcontexts
# (IME), accessiblebridge, tls/crypto where present.
UNWANTED_QT_PLUGIN_DIRS = (
    "geoservices", "assetimporters", "position", "sensorgestures", "sensors",
    "texttospeech", "printsupport", "multimedia", "audio", "playlistformats",
    "sqldrivers", "webview", "renderers", "renderplugin", "sceneparsers",
    "scxml", "serialbus", "serialport", "wayland-decoration-client",
    "wayland-graphics-integration-client", "wayland-shell-integration",
    "wayland-hardware-integration-client", "qmltooling", "quick",
    "generic", "bearer", "canbus", "designer", "help", "nfc", "bluetooth",
    "test", "webengine", "websockets",
)

# Individual plugin files that link against libraries we intentionally drop.
# Leaving them in produces a bundle whose dependency graph does not close -
# `build.py --verify-dist` reports exactly that. Kept on purpose:
#   platforms/qwindows + qoffscreen + qminimal  (Windows + headless tests)
#   imageformats/qico, qjpeg, qgif, qwebp       (app icon, attachments)
#   styles/qwindowsvistastyle                   (native look on Windows)
#   platformthemes, platforminputcontexts       (IME), accessiblebridge
UNWANTED_QT_PLUGIN_FILES = (
    "libqvnc.so", "libqwebgl.so", "libqsvg.so", "libqsvgicon.so",
    "libqpdf.so", "libqeglfs.so", "libqlinuxfb.so", "libqminimalegl.so",
    "libqwayland-egl.so", "libqwayland-generic.so",
    "libqwayland-xcomposite-egl.so", "libqwayland-xcomposite-glx.so",
    "libqtga.so", "libqtiff.so", "libqwbmp.so", "libqicns.so",
    # Windows equivalents of the same plugins
    "qsvg.dll", "qsvgicon.dll", "qpdf.dll", "qwebgl.dll", "qvnc.dll",
    "qtga.dll", "qtiff.dll", "qwbmp.dll", "qicns.dll",
)

# Qt libraries that exist only for the modules above.
UNWANTED_QT_LIBS = (
    "libQt5Bluetooth", "libQt5Help", "libQt5Location", "libQt5Multimedia",
    "libQt5Nfc", "libQt5OpenGL", "libQt5Positioning", "libQt5PrintSupport",
    "libQt5Quick", "libQt5Qml", "libQt5RemoteObjects", "libQt5Sensors",
    "libQt5SerialPort", "libQt5Svg", "libQt5TextToSpeech", "libQt5WebChannel",
    "libQt5WebSockets", "libQt5Xml", "libQt5XmlPatterns", "libQt5X11Extras",
    "libQt53D", "libQt5Test", "libQt5Sql", "libQt5Designer", "libQt5Network",
    "libQt5Concurrent", "libQt5PositioningQuick", "libQt5QuickControls2",
    "libQt5QuickTemplates2", "libQt5QuickWidgets", "libQt5QmlModels",
    "libQt5QmlWorkerScript", "libQt5VirtualKeyboard",
    # Windows equivalents
    "Qt5Bluetooth", "Qt5Help", "Qt5Location", "Qt5Multimedia", "Qt5Nfc",
    "Qt5OpenGL", "Qt5Positioning", "Qt5PrintSupport", "Qt5Quick", "Qt5Qml",
    "Qt5RemoteObjects", "Qt5Sensors", "Qt5SerialPort", "Qt5Svg",
    "Qt5TextToSpeech", "Qt5WebChannel", "Qt5WebSockets", "Qt5Xml",
    "Qt5XmlPatterns", "Qt5X11Extras", "Qt53D", "Qt5Test", "Qt5Sql",
    "Qt5Designer", "Qt5Network", "Qt5Concurrent",
)


def _keep_entry(name):
    """True when a binary/data entry should stay in the bundle."""
    normalized = str(name).replace("\\", "/")
    base = normalized.rsplit("/", 1)[-1]
    for prefix in UNWANTED_PYQT5_PREFIXES:
        if normalized.startswith(prefix) or ("/" + prefix) in normalized:
            return False
    for library in UNWANTED_QT_LIBS:
        if base.startswith(library):
            return False
    if base in UNWANTED_QT_PLUGIN_FILES:
        return False
    # Plugin directories: match anywhere in the path (PyQt5/Qt5/plugins/<dir>).
    parts = normalized.split("/")
    if "plugins" in parts:
        index = parts.index("plugins")
        if index + 1 < len(parts):
            plugin_dir = parts[index + 1]
            if plugin_dir in UNWANTED_QT_PLUGIN_DIRS:
                return False
    for plugin_dir in UNWANTED_QT_PLUGIN_DIRS:
        if normalized.startswith(plugin_dir + "/") or \
                ("/" + plugin_dir + "/") in normalized:
            return False
    return True


a = Analysis(
    ["main.py"],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

_before = len(a.binaries) + len(a.datas)
a.binaries = TOC([entry for entry in a.binaries if _keep_entry(entry[0])])
a.datas = TOC([entry for entry in a.datas if _keep_entry(entry[0])])
print("pyinstaller.spec: trimmed %d bundle entries (PyQt5 modules and their "
      "Qt libraries that Core2Chat never imports)"
      % (_before - len(a.binaries) - len(a.datas)))

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Core2Chat",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                       # UPX can break Qt plugins; not worth it
    console=False,                   # windowed app; set True for a debug build
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join("assets", "icon.ico")
    if os.path.isfile(os.path.join("assets", "icon.ico")) else None,
    version="version_info.txt" if os.path.isfile("version_info.txt") else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Core2Chat",
)
