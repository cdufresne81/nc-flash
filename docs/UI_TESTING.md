# UI Testing & Screenshots

GUI testing framework for NC Flash using `tools/test_runner.py`.

## Quick Start

```bash
# Take a screenshot of a table
python tools/test_runner.py --rom examples/lf9veb.bin --table "APP to TP Desired" --screenshot my_screenshot

# Run a GUI test script
python tools/test_runner.py --script tests/gui/test_colormap.txt

# Interactive mode
python tools/test_runner.py --interactive

# List screenshots
python tools/test_runner.py --list-screenshots

# Clean up auto-generated screenshots
python tools/test_runner.py --cleanup
```

## CLI Options

| Option | Description |
|--------|-------------|
| `--rom`, `-r` | Path to ROM file to load |
| `--table`, `-t` | Name of table to open |
| `--script`, `-s` | Path to test script file |
| `--interactive`, `-i` | Start in interactive REPL mode |
| `--screenshot` | Take screenshot with this name |
| `--metadata`, `-m` | Path to metadata directory |
| `--quiet`, `-q` | Suppress non-essential output |
| `--list-screenshots` | List all screenshots in output directory |
| `--cleanup` | Delete auto-generated screenshots |
| `--cleanup-pattern` | Pattern for cleanup (e.g., `demo_*`) |
| `--cleanup-age` | Only delete files older than N hours |

## Screenshot Targets

When taking screenshots, specify a target:

| Target | Description |
|--------|-------------|
| `table` | Current table viewer window (default) |
| `graph` | Graph panel (must be visible) |
| `main` | Main application window |
| `table_browser` | Table browser sidebar |

## Test Script Format

Test scripts are plain text files with one command per line. Comments start with `#`.

**Location:** `tests/gui/*.txt`

### Script Commands

#### Application Control
| Command | Description |
|---------|-------------|
| `start` | Start the application |
| `load_rom <path>` | Load a ROM file |
| `list_tables` | Print all available tables |

#### Table Operations
| Command | Description |
|---------|-------------|
| `open_table "<name>"` | Open a table by name (use quotes) |
| `close_table` | Close current table window |
| `select <r1> <c1> [r2] [c2]` | Select cells (0-indexed, data area) |
| `select_all` | Select all data cells |

#### Data Manipulation
| Command | Description |
|---------|-------------|
| `increment` | Increment selected cells |
| `decrement` | Decrement selected cells |
| `set <value>` | Set selected cells to value |
| `multiply <factor>` | Multiply selected cells |
| `add <value>` | Add value to selected cells |
| `interpolate_v` | Vertical interpolation |
| `interpolate_h` | Horizontal interpolation |
| `interpolate_2d` | 2D bilinear interpolation |

#### Graph Operations
| Command | Description |
|---------|-------------|
| `open_graph` | Show graph panel |
| `close_graph` | Hide graph panel |
| `rotate_graph <elev> <azim>` | Set 3D view angle (elevation 0-90, azimuth 0-360) |

#### Undo/Redo
| Command | Description |
|---------|-------------|
| `undo` | Undo last action |
| `redo` | Redo last undone action |

#### Screenshots & Timing
| Command | Description |
|---------|-------------|
| `screenshot [name] [target]` | Take screenshot |
| `wait <ms>` | Wait milliseconds |
| `list_screenshots` | List all screenshots |
| `cleanup [pattern] [hours]` | Delete screenshots |

#### UI State
| Command | Description |
|---------|-------------|
| `set_level <0-5>` | Set user level filter (0=all) |
| `store_width` | Store current window width |
| `assert_width <px> [tolerance]` | Assert window width |
| `assert_width_restored [tolerance]` | Assert width matches stored |

### Example Script

```text
# Test colormap display
load_rom examples/lf9veb.bin
open_table "APP to TP Desired - 2nd Gear"
wait 300

# Screenshot table view
screenshot colormap_table table

# Open graph and screenshot
open_graph
wait 500
screenshot colormap_graph graph

close_graph
close_table
```

## Programmatic Usage

The `TestRunner` class can be imported for use in Python scripts:

```python
from tools import TestRunner

runner = TestRunner()
runner.start_app()
runner.load_rom("examples/lf9veb.bin")
runner.open_table("APP to TP Desired")
runner.select_cells(0, 0, 5, 5)
runner.screenshot("my_screenshot", "table")
```

## Output Locations

- **Screenshots:** `docs/screenshots/`
- **Test scripts:** `tests/gui/`

## Common Workflows

### Debugging a Visual Issue

1. Start interactive mode: `python tools/test_runner.py --interactive`
2. Load ROM and open the problematic table
3. Take screenshot: `screenshot before_fix table`
4. (Make code changes)
5. Restart and take another screenshot: `screenshot after_fix table`

### Creating a Reproducible Test Case

1. Create a new script in `tests/gui/test_issue_name.txt`
2. Add commands to reproduce the issue
3. Add `screenshot` commands at key points
4. Run with `--script` to verify reproducibility

### Verifying UI After Code Changes

```bash
# Run all GUI tests
for script in tests/gui/test_*.txt; do
    python tools/test_runner.py --script "$script"
done
```
