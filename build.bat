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
echo === Build Complete ===
echo Output: dist\NCRomEditor\NCRomEditor.exe
echo.
echo To run: dist\NCRomEditor\NCRomEditor.exe
