# ROM Comparison Tool — Specification

## Overview

A read-only, side-by-side ROM comparison tool that lets users visually diff two ROM files table-by-table. Inspired by git diff but designed for numeric table data. Accessible from the main window menu when at least two ROMs are open.

## User Stories

1. **As a tuner**, I want to compare my tuned ROM against the stock ROM to verify exactly what changed.
2. **As a tuner**, I want to quickly navigate between all modified tables without closing/opening windows.
3. **As a tuner**, I want to visually identify changed cells at a glance using color highlighting.
4. **As a tuner**, I want to toggle between seeing all cells and seeing only changed cells for focus.

## UI Layout (Mockup 1 — Classic Split)

```
┌─────────────────────────────────────────────────────────┐
│ [Toolbar] ROM Compare  ◆ original.bin vs ◆ tuned.bin    │
│           [◄ Prev] 3/12 [Next ►]  [Toggle: Changed only]│
├──────────┬──────────────────────────────────────────────┤
│ Modified │  Original (left)     │  Modified (right)     │
│ Tables   │  ┌──────────────┐    │  ┌──────────────┐     │
│ (12)     │  │  Table data  │    │  │  Table data  │     │
│──────────│  │  with cell   │    │  │  with cell   │     │
│ Fuel 1   │  │  gradients   │    │  │  gradients   │     │
│ Fuel 2   │  │              │    │  │  changed     │     │
│►Ign Tim  │  │  changed     │    │  │  cells       │     │
│ Boost    │  │  cells       │    │  │  highlighted │     │
│ WGDC     │  │  highlighted │    │  │              │     │
│ ...      │  └──────────────┘    │  └──────────────┘     │
├──────────┴──────────────────────────────────────────────┤
│ [Status] Ignition Timing — 8 changed cells   [↑↓] [T]  │
└─────────────────────────────────────────────────────────┘
```

## Scope

### In Scope
- Compare exactly 2 ROMs (same definition/type)
- Side-by-side table display (original left, modified right)
- List of all modified tables in left sidebar with cell-change counts
- Navigation between tables (arrow keys, click, prev/next buttons)
- Orange highlight on changed cells (matching existing selection style)
- Toggle to dim unchanged cells ("changed only" mode)
- Synchronized scrolling between left and right panels
- Support for 1D, 2D, and 3D table types
- Read-only — no editing allowed
- Keyboard shortcuts: Up/Down (navigate tables), T (toggle changed only)

### Out of Scope (original spec — some items since implemented)
- ~~Editing values~~ — **Implemented:** Copy table from one ROM to another via compare window
- Graph/3D surface view
- Undo/redo
- More than 2 ROMs
- ~~Comparing ROMs with different definitions~~ — **Implemented:** Cross-definition comparison supported
- Exporting comparison results

## Architecture

### New Files

| File | Purpose |
|------|---------|
| `src/ui/compare_window.py` | `CompareWindow(QMainWindow)` — main comparison window (includes inline table panel rendering) |

### Integration Points

| Component | How |
|-----------|-----|
| **MainWindow** | New menu action: `Compare > Compare Open ROMs...` (enabled when 2+ ROMs open) |
| **RomReader** | Use `read_table_data(table)` to load both ROMs' data |
| **RomDefinition** | Iterate `tables` list to find differences |
| **TableDisplayHelper** | Reuse display logic for cell coloring, axis layout, gradient rendering |
| **ColorMap** | Reuse thermal gradient for cell backgrounds |
| **ModifiedCellDelegate** | New `CompareDelegate` subclass for changed-cell highlighting |

### No Changes To

- `TableViewer` / `TableViewerWindow` (existing viewer stays untouched)
- `RomReader` / `RomDefinition` (data layer untouched)
- `ChangeTracker` / `UndoManager` (comparison is read-only)

## Detailed Design

### 1. Entry Point — Menu Action

**Location:** `main.py`, added to menu bar.

```python
# Compare menu
compare_menu = menubar.addMenu("&Compare")
self.compare_action = compare_menu.addAction("Compare &Open ROMs...")
self.compare_action.setShortcut("Ctrl+Shift+D")
self.compare_action.triggered.connect(self._on_compare_roms)
self.compare_action.setEnabled(False)  # Enable when 2+ ROMs open
```

**ROM Selection Dialog:** When triggered, if exactly 2 ROMs are open, compare them directly (first opened = original, second = modified). If 3+ ROMs are open, show a simple dialog to pick which two to compare and which is "original".

### 2. Diff Computation

**Location:** `CompareWindow.__init__` or a helper method.

**Algorithm:**
1. Get both `RomDefinition` objects (must be same definition type)
2. Iterate all tables in the definition
3. For each table, call `rom_reader_a.read_table_data(table)` and `rom_reader_b.read_table_data(table)`
4. Compare `values` arrays using `np.array_equal()`
5. If different, add to `modified_tables` list with:
   - Table object reference
   - Data from both ROMs
   - Set of changed cell coordinates `{(row, col), ...}`
   - Count of changed cells

**Performance:** Pre-compute all diffs on window open. Tables are small (typically <100x20), so comparing all tables in a ROM takes <50ms.

### 3. CompareWindow

**Class:** `CompareWindow(QMainWindow)`

**Constructor Parameters:**
```python
def __init__(self, rom_reader_a: RomReader, rom_reader_b: RomReader,
             rom_definition: RomDefinition,
             color_a: QColor, color_b: QColor,
             name_a: str, name_b: str,
             parent=None):
```

**Layout:**
```
QMainWindow
├── Toolbar (QToolBar)
│   ├── ROM labels (name + color swatch)
│   ├── Spacer
│   ├── Prev/Next buttons (QToolButton)
│   ├── Counter label ("3 / 12")
│   └── Toggle switch ("Changed only")
├── Central Widget (QSplitter, horizontal)
│   ├── Left: Sidebar (QWidget)
│   │   ├── Header label ("Modified Tables (12)")
│   │   └── QListWidget (table names + change counts)
│   └── Right: Compare Area (QWidget)
│       ├── Panel Headers (Original | Modified)
│       └── QSplitter (horizontal, locked 50/50)
│           ├── CompareTablePanel (original)
│           └── CompareTablePanel (modified)
└── Status Bar
    ├── Table name + address + change count
    └── Keyboard hints
```

**Key Behaviors:**

- **Table navigation:** Clicking sidebar item or pressing Up/Down loads the selected table into both panels.
- **Synced scroll:** Both `CompareTablePanel` scroll areas are connected so scrolling one scrolls the other.
- **Changed-only toggle:** Applies CSS class or delegate flag to dim unchanged cells.
- **Window title:** `ROM Compare — original.bin vs tuned.bin`
- **No minimize/maximize** (matches `TableViewerWindow` pattern).

### 4. CompareTablePanel

**Class:** `CompareTablePanel(QWidget)`

**Purpose:** Displays a single read-only table with optional changed-cell highlighting.

**Layout:**
```
QWidget
├── QLabel (panel label: "Original" / "Modified" + ROM filename)
└── QTableWidget (data grid)
```

**Display Logic:**
- Reuse the same cell layout as `TableDisplayHelper`:
  - 1D: Single cell
  - 2D: Y-axis column + data column
  - 3D: Corner + X-axis row + Y-axis column + data grid
- Reuse thermal gradient coloring from `ColorMap`
- Axis cells styled with `#dcdcdc` background (matching existing)
- Grid line color: `#a0a0a0`

**Changed Cell Highlighting:**
- Uses the **same orange highlight** as the existing selection style:
  - Background: `rgba(255, 165, 0, 0.4)`
  - Border: `2px solid #FF8C00`
  - Text: bold black
- Applied via a custom `QStyledItemDelegate` (or item-level styling)
- Changed cells identified by comparing values from both ROMs

**Changed-Only Mode:**
- Unchanged cells get `opacity: 0.25` (via delegate or stylesheet)
- Axis headers dimmed to `opacity: 0.5`

**Read-Only:**
- `QTableWidget` edit triggers disabled
- No context menu for editing

### 5. Sidebar — Modified Tables List

**Widget:** `QListWidget` or custom `QWidget` with scroll area.

**Each item shows:**
- Table name (bold if currently selected)
- Badge with changed cell count (e.g., "24 cells")

**Selection:**
- Single selection mode
- Active item has blue left border + highlighted background
- Clicking loads both panels with the selected table's data

### 6. Navigation

**Toolbar Buttons:**
- Previous table: `↑` chevron icon (programmatic, matching existing icon style)
- Next table: `↓` chevron icon
- Counter label: `"3 / 12"` between buttons

**Keyboard Shortcuts:**
| Key | Action |
|-----|--------|
| `↑` / `↓` | Previous / next modified table |
| `T` | Toggle "changed only" mode |
| `Escape` | Close comparison window |

### 7. Styling

All styling must match the existing application. Key values:

**Toolbar:**
```css
QToolBar { spacing: 1px; padding: 1px 4px; border: none; }
QToolButton { padding: 3px; border: 1px solid transparent; border-radius: 3px; }
QToolButton:hover { background: rgba(128,128,128,0.15); border: 1px solid rgba(128,128,128,0.25); }
```

**Table:**
```css
QTableWidget { gridline-color: #a0a0a0; }
QTableWidget::item { padding: 0px 1px; }
```

**Toggle Switch:** Reuse `ToggleSwitch` widget from `src/ui/widgets/toggle_switch.py`.

**Cell Colors:** Use `ColorMap.ratio_to_color()` for thermal gradients (same as existing tables).

**Changed Cell Highlight:**
```css
background-color: rgba(255, 165, 0, 0.4);
border: 2px solid #FF8C00;
color: black;
font-weight: bold;
```

**ROM Color Swatches:** Small colored dots in toolbar matching the per-ROM color system.

### 8. Data Flow

```
User clicks "Compare Open ROMs..."
  │
  ├─ MainWindow._on_compare_roms()
  │   ├─ Get rom_reader_a, rom_reader_b from open tabs
  │   ├─ Verify same definition type
  │   └─ Create CompareWindow(reader_a, reader_b, ...)
  │
  └─ CompareWindow.__init__()
      ├─ _compute_diffs()
      │   ├─ For each table in definition.tables:
      │   │   ├─ data_a = reader_a.read_table_data(table)
      │   │   ├─ data_b = reader_b.read_table_data(table)
      │   │   ├─ Compare values arrays
      │   │   └─ If different → add to modified_tables
      │   └─ Sort modified_tables by category/name
      ├─ _build_ui()
      │   ├─ Create sidebar with modified table list
      │   ├─ Create left + right CompareTablePanel
      │   └─ Connect signals
      └─ _select_table(0)  # Show first modified table
          ├─ panel_a.display_table(table, data_a, changed_cells)
          └─ panel_b.display_table(table, data_b, changed_cells)
```

### 9. Error Handling

- **Different ROM definitions:** Show error dialog: "Cannot compare ROMs with different definitions."
- **No differences found:** Show info dialog: "ROMs are identical — no differences found." and don't open the window.
- **Only 1 ROM open:** Menu action stays disabled.
- **ROM read errors:** Catch exceptions from `read_table_data()`, skip table, log warning.

### 10. Window Management

- Only one `CompareWindow` can be open at a time (singleton pattern per MainWindow).
- Closing the compare window cleans up references.
- Compare window is independent of the main window's table viewer windows.
- Compare window added to `MainWindow` as a tracked reference (`self.compare_window`).

## Testing Plan

1. **Unit:** Compare algorithm with known ROM pair (stock vs modified).
2. **GUI:** Open comparison window via test runner, screenshot both panels.
3. **Navigation:** Verify Up/Down keys cycle through modified tables.
4. **Toggle:** Verify "changed only" dims unchanged cells.
5. **Scroll sync:** Verify both panels scroll together.
6. **Edge cases:** 1D table diff, empty diff (identical ROMs), large 3D tables.
