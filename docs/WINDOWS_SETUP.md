# Windows Setup Guide

## Quick Start

### 1. Prerequisites
- **Python 3.10+** installed from [python.org](https://www.python.org/downloads/)
- Make sure Python is added to PATH during installation

### 2. Run the Application

Simply double-click `run.bat` in the project folder or run from Command Prompt:

```cmd
cd NCRomEditor
run.bat
```

**First run:** The script will:
- Create a Windows virtual environment (`venv-windows/`)
- Install all required packages (PySide6, NumPy, etc.)
- Launch the application

**Subsequent runs:** The script will:
- Activate the existing virtual environment
- Launch the application immediately

## Manual Setup (Alternative)

If you prefer manual control:

### 1. Create Virtual Environment
```cmd
cd NCRomEditor
python -m venv venv-windows
```

### 2. Activate Virtual Environment
```cmd
venv-windows\Scripts\activate.bat
```

### 3. Install Dependencies
```cmd
pip install -r requirements.txt
```

### 4. Run Application
```cmd
python main.py
```

## Testing the Application

1. Launch the application using `run.bat`
2. Click **File → Open ROM...**
3. Navigate to `examples/lf9veb.bin` and open it
4. Browse tables in the left panel (expand categories)
5. Click any table to view its data

## Troubleshooting

### "Python is not recognized"
- Python is not installed or not in PATH
- Install Python from python.org
- During installation, check "Add Python to PATH"

### "Failed to create virtual environment"
- Run Command Prompt as Administrator
- Make sure you have write permissions in the project folder

### GUI doesn't appear
- Check if antivirus is blocking Python
- Try running from Command Prompt to see error messages

### Import errors
- Delete `venv-windows/` folder
- Run `run.bat` again to reinstall dependencies

## Development

For development work:
1. Open project in your IDE (VS Code, PyCharm, etc.)
2. Set Python interpreter to `venv-windows\Scripts\python.exe`
3. Run/debug from Windows for GUI testing
