# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


ROOT = Path(SPECPATH).resolve()
SOURCE = ROOT / "src" / "app.py"
ASSETS = ROOT / "assets"
VERSION_FILE = ROOT / "version_info.txt"
ICON_FILE = ASSETS / "app.ico"


a = Analysis(
    [str(SOURCE)],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[
        (str(ASSETS / "app.png"), "."),
        (str(ASSETS / "app-48.png"), "."),
        (str(ASSETS / "app.ico"), "."),
        (str(ASSETS / "checkbox-checked.png"), "."),
        (str(ASSETS / "checkbox-unchecked.png"), "."),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name="数据自动备份助手",
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
    version=str(VERSION_FILE),
    icon=[str(ICON_FILE)],
)
