# NC ROM Editor

[![GitHub](https://img.shields.io/badge/GitHub-NCRomEditor-blue?logo=github)](https://github.com/cdufresne81/NCRomEditor)

An open-source ROM editor for NC Miata (MX-5) ECUs, designed to replace the discontinued EcuFlash for ROM editing tasks.

## Overview

NC ROM Editor is a desktop application that allows you to read, edit, and save ECU ROM files for NC generation Mazda MX-5 Miata vehicles. This tool focuses solely on ROM file manipulation and works in conjunction with RomDrop for actual ECU flashing.

## Installation

### Clone the Repository
```bash
git clone https://github.com/cdufresne81/NCRomEditor.git
cd NCRomEditor
```

## Quick Start

### Windows
Simply double-click `run.bat` or see [WINDOWS_SETUP.md](WINDOWS_SETUP.md) for details.

### Linux/macOS
```bash
python3 -m venv venv
source venv/bin/activate  # On Windows WSL: source venv/bin/activate
pip install -r requirements.txt
python main.py
```

### Test the Application
1. Run the application
2. File → Open ROM → Select `examples/lf9veb.bin`
3. Browse tables in the left panel
4. Click any table to view data

## Features

### ✅ Working Now
- ✓ Automatic ROM ID detection and XML definition matching
- ✓ Read NC Miata ECU ROM binary files
- ✓ Browse 511 calibration tables organized by category
- ✓ View 1D, 2D, and 3D tables with proper axis labels
- ✓ Save modified ROM files
- ✓ ROM ID verification

### 🚧 In Development
- Table editing operations (add/subtract, interpolate)
- Import/export individual tables
- ROM comparison tool
- Automatic checksum calculation
- Copy/paste table data

## Tech Stack

- **Python 3.10+**
- **PySide6** - Qt6 bindings for Python (GUI framework)
- **NumPy** - Numerical operations on table data
- **Matplotlib** - 3D visualization of maps

## Project Structure

```
nc-rom-editor/
├── src/
│   ├── core/              # ROM parsing & binary reading
│   │   ├── rom_definition.py      # Data structures
│   │   ├── definition_parser.py   # XML parser
│   │   ├── rom_reader.py          # Binary ROM reader
│   │   └── rom_detector.py        # Automatic ROM ID detection
│   ├── ui/                # Qt GUI widgets
│   │   ├── table_browser.py       # Category tree browser
│   │   └── table_viewer.py        # Table data viewer
│   └── utils/             # Helper functions
├── metadata/              # ROM definition XML files
│   └── lf9veb.xml        # NC Miata ROM definition
├── examples/              # Example ROM binary files
│   └── lf9veb.bin        # Stock NC Miata ROM
├── docs/                  # Documentation
│   └── ROM_DEFINITION_FORMAT.md
├── main.py                # Application entry point
├── run.bat                # Windows launcher
└── WINDOWS_SETUP.md       # Windows setup guide
```

## Usage

1. **Load ROM:** File → Open ROM → Select `examples/lf9veb.bin`
2. **Browse Tables:** Expand categories in the left panel (e.g., "Spark Target - Base")
3. **View Table:** Click any table to see its data with axis values
4. **Save ROM:** File → Save ROM or Save ROM As...
5. **Flash to ECU:** Use RomDrop to flash the modified ROM

## Development

### Running Tests

The project uses pytest for testing. All tests are located in the `tests/` directory.

**Run all tests:**
```bash
# Activate virtual environment first
source venv/bin/activate  # Linux/macOS
# or
venv\Scripts\activate  # Windows

# Run tests
pytest

# Run with coverage report
pytest --cov=src --cov-report=html

# Run specific test file
pytest tests/test_rom_detector.py

# Run tests matching a pattern
pytest -k "test_rom_id"
```

**Test Coverage:**
- Core modules (parser, detector, reader): 86-96%
- Overall: 70%

View detailed coverage report: `htmlcov/index.html`

### Code Quality

**Format code with black:**
```bash
black src/ tests/
```

**Lint code with flake8:**
```bash
flake8 src/ tests/
```

### CI/CD

Tests run automatically on GitHub Actions for:
- Python 3.10, 3.11, 3.12
- Ubuntu, Windows, macOS

## Development Status

**Current Version:** v0.1.0 - Alpha

**Completed:**
- ✅ Automatic ROM ID detection and XML matching
- ✅ ROM definition XML parser (511 tables loaded)
- ✅ Binary ROM reader with scaling conversions
- ✅ Table browser UI with category organization
- ✅ Table viewer for 1D, 2D, and 3D data
- ✅ ROM ID verification

**Next Priorities:**
- Table editing (modify cell values, add/subtract, interpolate)
- Checksum calculation and validation
- ROM comparison tool
- Import/export individual tables

## Contributing

Contributions welcome! This is an open-source project to preserve and improve upon the functionality of the discontinued EcuFlash tool.

## License

TBD

## Disclaimer

Modifying your ECU can damage your vehicle or violate emissions regulations. Use this software at your own risk. Always keep backups of your stock ROM.
