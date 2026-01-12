# Undo Coordinate Bug - Fixed

## The Problem

When editing cells in 3D tables, undo was affecting **cells ABOVE** the ones that were actually modified instead of the correct cells.

Example:
- User edits cell at row 6, column 4
- Press Ctrl+Z to undo
- **BUG**: Cell at row 5, column 4 (one row above) was changed instead

## Root Cause

**Coordinate Mismatch** in `editing.py` between the display layout and undo coordinate conversion.

### 3D Table Layout (from display.py)

```
Row 0: Axis labels (gray)
Row 1: X-axis values (colored)
Row 2+: Data rows (with Y-axis values in col 0)
Col 0: Y-axis values
Col 1+: Data columns
```

Data cells are placed at UI position `(data_row + 2, data_col + 1)`.

### The Bugs in editing.py

Three coordinate conversion bugs were found in `src/ui/table_viewer_helpers/editing.py`:

1. **`data_to_ui_coords()` line 217** - Data cells:
   - **Before**: `return ui_row + 1, ui_col + 1` (wrong row offset)
   - **After**: `return ui_row + 2, ui_col + 1` (correct: +2 for label row and X-axis row)

2. **`_axis_data_to_ui_coords()` line 398** - X-axis cells:
   - **Before**: `return 0, ui_col + 1` (wrong: X-axis in row 0)
   - **After**: `return 1, ui_col + 1` (correct: X-axis in row 1)

3. **`_axis_data_to_ui_coords()` line 405** - Y-axis cells:
   - **Before**: `return ui_row + 1, 0` (wrong row offset)
   - **After**: `return ui_row + 2, 0` (correct: Y-axis starts at row 2)

## The Fix

Changed three return statements in `editing.py` to match the actual UI layout from `display.py`:

```python
# Data cells (3D tables)
elif table_type == TableType.THREE_D:
    values = self.ctx.current_data['values']
    rows, cols = values.shape
    ui_row = (rows - 1 - data_row) if flipy else data_row
    ui_col = (cols - 1 - data_col) if flipx else data_col
    return ui_row + 2, ui_col + 1  # FIXED: +2 for label and X-axis rows

# X-axis cells
if axis_type == 'x_axis':
    # X axis is in row 1, columns 1+
    x_axis = self.ctx.current_data.get('x_axis')
    if x_axis is not None:
        cols = len(x_axis)
        ui_col = (cols - 1 - data_idx) if flipx else data_idx
        return 1, ui_col + 1  # FIXED: row 1, not 0

# Y-axis cells
elif axis_type == 'y_axis':
    # Y axis is in column 0, rows 2+
    y_axis = self.ctx.current_data.get('y_axis')
    if y_axis is not None:
        rows = len(y_axis)
        ui_row = (rows - 1 - data_idx) if flipy else data_idx
        return ui_row + 2, 0  # FIXED: +2, not +1
```

## Testing

✅ **All 92 tests pass** after the fix

### Manual Testing Steps

1. **Data Cell Edit + Undo**:
   - Open a 3D table
   - Edit a cell (e.g., row 6, col 4)
   - Press Ctrl+Z
   - **Expected**: Same cell reverts (row 6, col 4)
   - **Previously**: Cell above reverted (row 5, col 4) ❌

2. **Multi-Cell Edit + Undo**:
   - Select multiple cells in a column
   - Use increment (+ key)
   - Press Ctrl+Z
   - **Expected**: Same cells revert to original values
   - **Previously**: Cells one row above reverted ❌

3. **X-Axis Edit + Undo**:
   - Edit an X-axis value
   - Press Ctrl+Z
   - **Expected**: Same X-axis cell reverts

4. **Y-Axis Edit + Undo**:
   - Edit a Y-axis value
   - Press Ctrl+Z
   - **Expected**: Same Y-axis cell reverts

## Files Changed

- `src/ui/table_viewer_helpers/editing.py` (3 lines changed)

## Status

✅ **Fixed and tested**
- All undo operations now affect the correct cells
- Data cell, X-axis, and Y-axis undos all work correctly
- No tests broken by the fix

## Next Steps

With undo working correctly, we can now safely implement the modified cell borders feature.
