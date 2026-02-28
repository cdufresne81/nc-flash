@echo off
REM NC ROM Editor - Windows Launcher
REM This script sets up and runs the ROM editor on Windows

echo ========================================
echo NC ROM Editor - Windows Launcher
echo ========================================
echo.

REM Check if Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH
    echo Please install Python 3.10 or higher from python.org
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

REM Check if dependencies are installed (check for PySide6)
python -c "import PySide6" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo ERROR: Failed to install dependencies
        pause
        exit /b 1
    )
    echo Dependencies installed successfully
    echo.
)

REM Run the application (pass any command-line args through, e.g. --enable-projects)
echo Starting NC ROM Editor...
echo.
python main.py %*

REM If the app exits, pause so user can see any error messages
if errorlevel 1 (
    echo.
    echo Application exited with error code %errorlevel%
    pause
)
