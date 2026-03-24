# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the J2534 32-bit bridge executable.

MUST be built with 32-bit Python so the resulting .exe can load 32-bit J2534 DLLs.

Build with:  py -3-32 -m PyInstaller packaging/j2534_bridge_32.spec
Output:      dist/j2534_bridge_32/j2534_bridge_32.exe
"""

import os

block_cipher = None

repo_root = os.path.abspath(os.path.join(SPECPATH, '..'))

a = Analysis(
    [os.path.join(repo_root, 'src', 'ecu', 'j2534_bridge.py')],
    pathex=[repo_root],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'test', 'PySide6', 'numpy', 'matplotlib'],
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
    name='j2534_bridge_32',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,  # Needs console for stdin/stdout IPC
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='j2534_bridge_32',
)
