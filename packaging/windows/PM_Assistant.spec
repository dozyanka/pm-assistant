# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

ROOT = Path.cwd()
webview_datas, webview_binaries, webview_hiddenimports = collect_all("webview")

datas = [
    (str(ROOT / "pm_app" / "static"), "pm_app/static"),
    (str(ROOT / "samples"), "samples"),
    (str(ROOT / "examples"), "examples"),
]

a = Analysis(
    [str(ROOT / "desktop_app.py")],
    pathex=[str(ROOT)],
    binaries=webview_binaries,
    datas=datas + webview_datas,
    hiddenimports=webview_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PM Assistant",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PM Assistant",
)
