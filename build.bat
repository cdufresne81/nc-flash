@echo off
REM Build NC ROM Editor as a standalone Windows executable
REM Output: dist\NCRomEditor\NCRomEditor.exe

echo === NC ROM Editor Build ===

REM Activate virtual environment
if exist venv-windows\Scripts\activate.bat (
    call venv-windows\Scripts\activate.bat
) else (
    echo ERROR: venv-windows not found. Create it first with:
    echo   python -m venv venv-windows
    echo   venv-windows\Scripts\activate.bat
    echo   pip install -r requirements.txt
    exit /b 1
)

REM Install build dependencies
echo Installing build dependencies...
pip install -r requirements-build.txt
if errorlevel 1 (
    echo ERROR: Failed to install build dependencies
    exit /b 1
)

REM Run PyInstaller
echo Building executable...
pyinstaller NCRomEditor.spec --noconfirm
if errorlevel 1 (
    echo ERROR: PyInstaller build failed
    exit /b 1
)

echo.
echo === PyInstaller Build Complete ===
echo Output: dist\NCRomEditor\NCRomEditor.exe
echo.

REM Build installer with Inno Setup (optional)
set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"

if defined ISCC (
    echo Building installer...
    "%ISCC%" installer.iss
    if errorlevel 1 (
        echo ERROR: Inno Setup build failed
        exit /b 1
    )
    echo.
    echo === Installer Build Complete ===
    echo Installer: Output\NCRomEditor-1.3.0-Setup.exe
) else (
    echo Skipping installer: Inno Setup 6 not found.
    echo Install from https://jrsoftware.org/isinfo.php to build the installer.
)
