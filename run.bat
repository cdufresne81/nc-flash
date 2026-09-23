@echo off
REM NC Flash - Windows Launcher
REM This script sets up and runs the ROM editor on Windows

echo ========================================
echo NC Flash - Windows Launcher
echo ========================================
echo.

REM Check if Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH
    echo Please install Python 3.11 or higher from python.org
    pause
    exit /b 1
)

REM Check if virtual environment exists
if not exist "venv-windows\" (
    echo Creating Windows virtual environment...
    python -m venv venv-windows
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment
        pause
        exit /b 1
    )
    echo Virtual environment created successfully
    echo.
)

REM Activate virtual environment
echo Activating virtual environment...
call venv-windows\Scripts\activate.bat
if errorlevel 1 (
    echo ERROR: Failed to activate virtual environment
    pause
    exit /b 1
)

REM (Re)install dependencies whenever requirements.txt changed since the last
REM install (stamp copy kept inside the venv) - not only when PySide6 is missing,
REM or an existing venv never picks up new dependencies.
fc /b requirements.txt venv-windows\.requirements.stamp >nul 2>&1
if errorlevel 1 (
    echo Installing/updating dependencies...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo ERROR: Failed to install dependencies
        pause
        exit /b 1
    )
    copy /y requirements.txt venv-windows\.requirements.stamp >nul
    echo Dependencies installed successfully
    echo.
)

REM Run the application (pass any command-line args through)
echo Starting NC Flash...
echo.
python main.py %*

REM If the app exits, pause so user can see any error messages
if errorlevel 1 (
    echo.
    echo Application exited with error code %errorlevel%
    pause
)
