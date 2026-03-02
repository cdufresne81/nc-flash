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

a = Analysis(
    [os.path.join(repo_root, 'main.py')],
    pathex=[repo_root],
    binaries=[],
    datas=[
        (os.path.join(repo_root, 'colormaps'), 'colormaps'),
        (os.path.join(repo_root, 'examples'), 'examples'),
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
