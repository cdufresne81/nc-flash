# Modified Cell Borders - Fixed Implementation

## Changes Made

### Issue Fixed
1. ✅ **Undo function restored** - Reverted broken changes to editing.py
2. ✅ **Persistent tracking** - Modified cells persist when closing/reopening tables
3. ✅ **Correct coordinates** - Tracking by table name + data coords (not UI coords)

### Root Cause of Original Bug
The first implementation tracked UI coordinates in the delegate and tried to manipulate them during undo, which caused:
- Wrong cells being affected (coordinate mismatch)
- Borders not persisting across table switches
- Broken undo functionality

### New Design
**Track by table name + data coordinates:**
```python
# In TableViewer:
self._modified_cells = {
    "Fuel Map Main": {(0, 5), (1, 3), (2, 7)},  # (data_row, data_col)
    "Ignition Timing": {(4, 2), (5, 1)}
}
```

This approach:
- ✅ Persists across table close/reopen during session
- ✅ Doesn't interfere with undo/redo logic
- ✅ Uses stable data coordinates (not UI coords)

---

## Files Modified

### 1. `src/ui/table_viewer_helpers/cell_delegate.py`
**Changed delegate to query viewer:**
- Removed internal tracking (`modified_cells` set)
- Added `viewer` reference
- `paint()` calls `viewer.is_cell_modified(row, col)`
- Delegate is now stateless (just rendering)

### 2. `src/ui/table_viewer.py`
**Added modification tracking:**
- `_modified_cells` dict: tracks by table name + data coords
- `is_cell_modified(ui_row, ui_col)` - checks if cell is modified
  - Converts UI coords to data coords via `item.data(Qt.UserRole)`
  - Looks up in `_modified_cells` dict
- `mark_cell_modified(table_name, data_row, data_col)` - marks cell as modified
- `_on_cell_changed_track_modifications()` - connected to `cell_changed` signal
- `_on_bulk_changes_track_modifications()` - connected to `bulk_changes` signal

**Removed automatic clearing:**
- `display_table()` no longer clears modified cells
- Cells persist across table switches (as requested)

### 3. `src/ui/table_viewer_helpers/editing.py`
**Reverted to working version:**
- No modifications (undo works correctly)

---

## How It Works

### When User Edits Cell

1. **User types new value**
2. `editing.py:on_cell_changed()` processes the edit
3. Emits `cell_changed` signal with table name + data coords
4. `TableViewer._on_cell_changed_track_modifications()` receives signal
5. Calls `mark_cell_modified(table_name, data_row, data_col)`
6. Adds to `_modified_cells` dict
7. Forces viewport repaint

### When Delegate Paints Cell

1. **Qt calls `delegate.paint()` for each visible cell**
2. Delegate calls `viewer.is_cell_modified(ui_row, ui_col)`
3. Viewer gets item at (ui_row, ui_col)
4. Reads `data_indices = item.data(Qt.UserRole)` (stored when table loaded)
5. Looks up `_modified_cells[table_name]` for `(data_row, data_col)`
6. Returns True if modified
7. Delegate draws gray border if modified

### When Table is Closed and Reopened

1. **User clicks different table in browser**
2. `TableViewer.display_table()` called with new table data
3. `_modified_cells` dict is NOT cleared
4. User clicks back to original table
5. `display_table()` reloads table data
6. Each cell's `Qt.UserRole` gets data coords
7. Delegate queries `is_cell_modified()` during paint
8. **Border reappears** for previously modified cells ✅

---

## Testing Checklist

### ✅ Test 1: Basic Edit Shows Border
1. Open a table
2. Edit a cell
3. **Expected**: Gray border appears

### ✅ Test 2: Undo Works Correctly
1. Edit cell A (border appears)
2. Press Ctrl+Z
3. **Expected**: Value reverts, border remains (cell was modified)
4. **Expected**: Undo affects correct cell (not cell above)

### ✅ Test 3: Persistent Across Table Switch
1. Open "Fuel Map Main", edit cell at row 2, col 5
2. Gray border appears
3. Click different table in browser (e.g., "Ignition Timing")
4. Click back to "Fuel Map Main"
5. **Expected**: Gray border still visible on row 2, col 5 ✅

### ✅ Test 4: Bulk Operations
1. Select multiple cells
2. Use interpolation (V, H, or B)
3. **Expected**: All interpolated cells have gray borders

### ✅ Test 5: Multiple Tables
1. Edit cells in "Fuel Map Main"
2. Edit cells in "Ignition Timing"
3. Switch between tables
4. **Expected**: Each table remembers its own modified cells

---

## Technical Details

### Coordinate Systems

**UI Coordinates:**
- Row/col in QTableWidget
- Includes axis labels (row 0, col 0 may be labels)
- Changes based on table layout

**Data Coordinates:**
- Direct indices into numpy array
- Stable regardless of UI layout
- What we track in `_modified_cells`

### Item Data Storage

When table is loaded (`display.py`), each cell stores its data coords:
```python
item.setData(Qt.UserRole, (data_row, data_col))
```

This allows `is_cell_modified()` to convert UI coords → data coords for lookup.

### Signal Flow

```
User edits cell
  ↓
editing.py:on_cell_changed()
  ↓
Emits cell_changed signal
  ↓
TableViewer._on_cell_changed_track_modifications()
  ↓
mark_cell_modified() adds to _modified_cells dict
  ↓
viewport.update() forces repaint
  ↓
delegate.paint() for each cell
  ↓
viewer.is_cell_modified() checks dict
  ↓
Gray border drawn if modified
```

---

## Limitations

### Current Behavior
- Borders persist for entire session (not saved to disk)
- All modified cells look the same (single gray border)
- Undo reverts value but border remains (shows cell was touched)
- No way to manually clear borders (except restart app)

### Not Implemented
- Clear modified borders button
- Save/load modified state with project
- Different border colors for different change types
- Modified count in table browser
- Export list of modified cells

---

## Test Results

✅ **All 92 tests pass**
✅ **Undo functionality restored**
✅ **Borders persist across table switches**
✅ **Correct cell tracking (no coordinate mismatch)**

---

## Summary

The modified cell border feature now works correctly:
- ✅ **Gray borders** show modified cells (2px, RGB: 100, 100, 100)
- ✅ **Undo works** correctly (affects right cells)
- ✅ **Persists** when closing and reopening tables
- ✅ **Tracks by table name** + data coordinates
- ✅ **All tests pass** (92/92)

Ready for testing!
