# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for NCFlash.

Build with:  pyinstaller packaging/NCFlash.spec
Output:      dist/NCFlash/NCFlash[.exe]
"""

import os
import sys

block_cipher = None

# Resolve paths relative to the repository root (one level up from this spec file)
repo_root = os.path.abspath(os.path.join(SPECPATH, '..'))

# Icon: .ico on Windows, skip on Linux (desktop icons use .desktop files)
icon_file = os.path.join(repo_root, 'assets', 'NCFlash.ico') if sys.platform == 'win32' else None

# J2534 32-bit bridge: pre-built via packaging/j2534_bridge_32.spec
bridge_dir = os.path.join(repo_root, 'dist', 'j2534_bridge_32')
if not os.path.isdir(bridge_dir):
    raise FileNotFoundError(
        f"32-bit bridge not found at {bridge_dir}. "
        "Build it first: py -3-32 -m PyInstaller packaging/j2534_bridge_32.spec --noconfirm"
    )

a = Analysis(
    [os.path.join(repo_root, 'main.py')],
    pathex=[repo_root],
    binaries=[],
    datas=[
        (os.path.join(repo_root, 'colormaps'), 'colormaps'),
        (os.path.join(repo_root, 'examples'), 'examples'),
        (bridge_dir, 'j2534_bridge_32'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'test'],
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
    name='NCFlash',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=icon_file,
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
    name='NCFlash',
)
