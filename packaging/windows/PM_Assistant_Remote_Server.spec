# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

ROOT = Path.cwd()

datas = [
    (str(ROOT / "pm_app" / "static"), "pm_app/static"),
    (str(ROOT / "examples"), "examples"),
]

a = Analysis(
    [str(ROOT / "remote_server.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
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
    name="PM Assistant Remote Server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PM Assistant Remote Server",
)
