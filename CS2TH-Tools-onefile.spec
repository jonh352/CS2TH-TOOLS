# -*- mode: python ; coding: utf-8 -*-
"""Lean onefile portable build (~110MB+; already zlib-packed, hard to go under 100MB)."""

from __future__ import annotations

import os

from PyInstaller.utils.hooks import collect_all

playwright_datas, playwright_binaries, playwright_hidden = collect_all("playwright")

_EXCLUDED_QT = [
    "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DRender",
    "PySide6.QtBluetooth", "PySide6.QtCharts", "PySide6.QtConcurrent",
    "PySide6.QtDBus", "PySide6.QtDataVisualization", "PySide6.QtDesigner",
    "PySide6.QtGraphs", "PySide6.QtHelp", "PySide6.QtHttpServer",
    "PySide6.QtLocation", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtNetworkAuth", "PySide6.QtNfc", "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtPositioning", "PySide6.QtPrintSupport", "PySide6.QtQml",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickControls2",
    "PySide6.QtQuickWidgets", "PySide6.QtRemoteObjects", "PySide6.QtSensors",
    "PySide6.QtSerialBus", "PySide6.QtSerialPort", "PySide6.QtSpatialAudio",
    "PySide6.QtSql", "PySide6.QtStateMachine", "PySide6.QtTest",
    "PySide6.QtTextToSpeech", "PySide6.QtUiTools", "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebEngineWidgets", "PySide6.QtWebSockets", "PySide6.QtWebView",
    "PySide6.QtXml",
]
_EXCLUDED_MISC = [
    "tkinter", "_tkinter", "turtle", "doctest", "pydoc", "pdb",
    "matplotlib", "scipy", "pandas", "IPython", "notebook", "pytest",
]
_QT_DLL_DENY = (
    "qt63d", "qt6quick", "qt6qml", "qt6webengine", "qt6multimedia",
    "qt6charts", "qt6datavisualization", "qt6bluetooth", "qt6sensors",
    "qt6positioning", "qt6pdf", "qt6designer", "qt6test", "qt6location",
    "qt6sql", "qt6xml", "qt6remoteobjects", "qt6serial", "qt6nfc",
    "qt6help", "qt6httpserver", "qt6spatialaudio", "qt6statemachine",
    "qt6texttospeech", "qt6webview", "qt6webchannel", "qt6websockets",
    "qt6networkauth", "qt6graphs", "qt6opengl", "qt6virtualkeyboard",
    "opengl32sw",
)


def _norm(path: str) -> str:
    return path.replace("\\", "/").lower()


def _keep_binary(item) -> bool:
    name = _norm(item[0] if isinstance(item, (tuple, list)) else str(item))
    base = os.path.basename(name)
    if any(tok in base for tok in _QT_DLL_DENY):
        return False
    if "/plugins/" in name and any(
        p in name
        for p in (
            "/qmltooling/", "/geoservices/", "/multimedia/", "/sqldrivers/",
            "/sensorgestures/", "/position/", "/canbus/", "/designer/",
        )
    ):
        return False
    return True


def _keep_data(item) -> bool:
    name = _norm(item[0] if isinstance(item, (tuple, list)) else str(item))
    if "/translations/" in name or "/qml/" in name or "/metatypes/" in name:
        return False
    if name.endswith(".qmltypes"):
        return False
    if "/playwright/" in name or name.startswith("playwright/"):
        deny = (
            "/vite/", "/htmlreport/", "/traceviewer/", "/recorder/",
            "/dashboard/", "/types/", ".d.ts", "/readme", "/license",
            "/notice", "/protocol.yml",
        )
        if any(tok in name for tok in deny):
            return False
    if "/tzdata/" in name:
        allow = (
            "/tzdata/__init__", "/tzdata/zones", "/tzdata/zone",
            "/tzdata/zoneinfo/asia/shanghai", "/tzdata/zoneinfo/etc/utc",
            "/tzdata/zoneinfo/etc/gmt", "/tzdata/zoneinfo/utc",
            "zone.tab", "tzdata.zi", "iso3166.tab",
        )
        if not any(tok in name for tok in allow):
            return False
    return True


a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=playwright_binaries,
    datas=[("assets", "assets"), ("meta", "meta"), ("weapon_images", "weapon_images")]
    + playwright_datas,
    hiddenimports=list(playwright_hidden)
    + ["multiprocessing", "numpy", "qrcode", "PIL", "PySide6.QtSvg", "PySide6.QtNetwork"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=_EXCLUDED_QT + _EXCLUDED_MISC,
    noarchive=False,
    optimize=0,
)
a.binaries = [b for b in a.binaries if _keep_binary(b)]
a.datas = [d for d in a.datas if _keep_data(d)]
pyz = PYZ(a.pure, a.zipped_data)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="CS2TH-Tools",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon="assets\\logo.ico" if os.path.isfile("assets\\logo.ico") else None,
)
