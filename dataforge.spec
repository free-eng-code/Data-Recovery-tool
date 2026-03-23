# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for DataForge Recovery."""

import sys
from pathlib import Path

block_cipher = None

a = Analysis(
    ['src/main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        # Core modules
        'src',
        'src.core',
        'src.core.disk',
        'src.core.models',
        'src.core.scanner',
        'src.core.recovery',
        'src.core.carver',
        'src.core.signatures',
        'src.core.session',
        'src.core.win_scanner',
        # GUI modules
        'src.gui',
        'src.gui.main_window',
        'src.gui.disk_selector',
        'src.gui.scan_progress',
        'src.gui.tree_view',
        'src.gui.recovery_dialog',
        # Utilities
        'src.utils',
        'src.utils.admin',
        'src.utils.formatting',
        'src.utils.logging_setup',
        # Third-party
        'pytsk3',
        'win32file',
        'win32api',
        'win32con',
        'winioctlcon',
        'pywintypes',
        'ctypes',
        'ctypes.wintypes',
        'json',
        'struct',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='DataForge Recovery',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # Windowed app (no console)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True,         # Request admin on launch
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='DataForge Recovery',
)
