# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for NC ROM Editor.

Build with:  pyinstaller packaging/NCRomEditor.spec
Output:      dist/NCRomEditor/NCRomEditor[.exe]
"""

import os
import sys

block_cipher = None

# Icon: .ico on Windows, skip on Linux (desktop icons use .desktop files)
icon_file = 'assets/NCRomEditor.ico' if sys.platform == 'win32' else None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('examples/metadata', 'examples/metadata'),
        ('colormaps', 'colormaps'),
        ('examples', 'examples'),
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
    name='NCRomEditor',
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
    name='NCRomEditor',
)
