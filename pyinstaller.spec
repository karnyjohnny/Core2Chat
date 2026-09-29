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
