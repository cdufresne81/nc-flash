#!/bin/bash
# NC ROM Editor - Linux Launcher
# This script sets up and runs the ROM editor on Linux

echo "========================================"
echo "NC ROM Editor - Linux Launcher"
echo "========================================"
echo ""

# Check if Python is available
if ! command -v python3 &> /dev/null; then
    echo "ERROR: Python 3 is not installed or not in PATH"
    echo "Please install Python 3.10 or higher using your package manager"
    echo "Example: sudo apt install python3 python3-venv"
    exit 1
fi

echo "Found Python: $(python3 --version)"
echo ""

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
    if [ $? -ne 0 ]; then
        echo "ERROR: Failed to create virtual environment"
        exit 1
    fi
    echo "Virtual environment created successfully"
    echo ""
fi

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate
if [ $? -ne 0 ]; then
    echo "ERROR: Failed to activate virtual environment"
    exit 1
fi

# Check if dependencies are installed (check for PySide6)
python3 -c "import PySide6" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "Installing dependencies..."
    pip install -r requirements.txt
    if [ $? -ne 0 ]; then
        echo "ERROR: Failed to install dependencies"
        exit 1
    fi
    echo "Dependencies installed successfully"
    echo ""
fi

# Run the application
echo "Starting NC ROM Editor..."
echo ""
python3 main.py

# Capture exit code
EXIT_CODE=$?
if [ $EXIT_CODE -ne 0 ]; then
    echo ""
    echo "Application exited with error code $EXIT_CODE"
    read -p "Press Enter to continue..."
fi
