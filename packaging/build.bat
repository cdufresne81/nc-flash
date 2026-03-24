@echo off
REM Build NCFlash as a standalone Windows executable
REM Run from the project root: packaging\build.bat
REM Output: dist\NCFlash\NCFlash.exe

echo === NCFlash Build ===

REM Change to project root (parent of packaging/)
cd /d "%~dp0\.."

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
pip install -r packaging\requirements-build.txt
if errorlevel 1 (
    echo ERROR: Failed to install build dependencies
    exit /b 1
)

REM Build 32-bit J2534 bridge (requires 32-bit Python via py launcher)
echo Building 32-bit J2534 bridge...
py -3-32 -m PyInstaller packaging\j2534_bridge_32.spec --noconfirm
if errorlevel 1 (
    echo ERROR: 32-bit bridge build failed. Ensure 32-bit Python is installed:
    echo   winget install Python.Python.3.12 --architecture x86
    exit /b 1
)

REM Run PyInstaller for main app
echo Building executable...
pyinstaller packaging\NCFlash.spec --noconfirm
if errorlevel 1 (
    echo ERROR: PyInstaller build failed
    exit /b 1
)

echo.
echo === PyInstaller Build Complete ===
echo Output: dist\NCFlash\NCFlash.exe
echo.

REM Build installer with Inno Setup (optional)
set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"

if defined ISCC (
    echo Building installer...
    "%ISCC%" packaging\installer.iss
    if errorlevel 1 (
        echo ERROR: Inno Setup build failed
        exit /b 1
    )
    echo.
    echo === Installer Build Complete ===
    dir /b Output\*.exe
) else (
    echo Skipping installer: Inno Setup 6 not found.
    echo Install from https://jrsoftware.org/isinfo.php to build the installer.
)
