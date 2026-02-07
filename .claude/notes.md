# Session Notes

## Next Tasks

### ROM Tools
- **ROM comparison tool** - Compare two ROM files (stock vs modified), highlight differences in tables and raw data
- **ROM modification tracking** - Git-like tracking for ROM edits, project organization with ROM aliases, change history viewer, export modification logs
  - Detailed planning docs: `docs/MODIFICATION_TRACKING_PLAN.md` (full technical spec) and `docs/MODIFICATION_TRACKING_SUMMARY.md` (overview)
  - Status: Not started, estimated 10 weeks part-time

### Distribution
- **Windows packaging** - Use PyInstaller to package as standalone .exe, test on clean Windows system

## Environment Notes
- Use `python3` not `python` (WSL2 environment lacks symlink)

## Recent Completed Work (Feb 7, 2026) - Multi-ROM Undo Isolation Fix
- **Fixed undo stacks shared across ROMs with same definition** — When two ROMs share the same definition (same ECU type), they have identical table addresses. The undo stacks, change tracker, and table highlighting were all keyed by bare `table_address`, causing: (1) both ROMs' edits going into the same undo stack, (2) closing one ROM destroying the other's undo stacks and pending changes, (3) table highlighting showing modifications from the wrong ROM. Fix: introduced composite keys (`rom_path|table_address`) throughout the undo and change tracking systems. Files modified: `version_models.py` (added `table_key` field to CellChange/AxisChange), `table_undo_manager.py` (composite key helpers, rom_path params), `undo_commands.py` (propagate table_key through undo/redo), `change_tracker.py` (composite keys, per-ROM filtering), `main.py` (pass rom_path to all handlers, per-ROM highlight filtering), `table_viewer_window.py` (emit composite key on focus).

## Recent Completed Work (Feb 7, 2026) - Undo Wrong-ROM Fix
- **Fixed Path vs str type mismatch throughout `main.py`** — `RomReader.rom_path` is `Path`, `RomDocument.rom_path` is `str`; on Windows, forward vs backslash normalization caused `str()` comparison to fail silently. Fixed `_find_document_by_rom_path()` to use `Path()` comparison. Fixed `close_tab()` to use `rom_reader.rom_path` (Path) instead of `document.rom_path` (str) for window matching and dict cleanup.
- **Fixed test runner operations not emitting signals** — `set_value`, `multiply_selection`, `add_to_selection` called `_apply_bulk_operation` directly which doesn't emit `bulk_changes`/`axis_bulk_changes` signals. Now properly emits signals so changes are written to ROM.
- **Fixed undo/edit writing to wrong ROM** when multiple ROMs are open — all 6 `get_current_document()` call sites in edit/undo handlers now resolve the correct ROM via `_find_document_by_rom_path()` instead of using the active tab
- **Clean ROM state on tab close** — closing a ROM now: closes all its table windows, removes undo stacks, clears pending changes, and purges modified_cells/original_table_values for that ROM. Reopening a ROM starts fresh.
- **Debounced graph selection updates** — arrow key navigation no longer triggers full 3D re-render per key press (100ms debounce timer)
- **Eliminated double-draw** in graph widget — `canvas.draw_idle()` + deferred redraw only on first plot

## Recent Completed Work (Feb 7, 2026) - Audit Fix #11
- **Deduplicated GraphWidget and GraphViewer** (~700 lines → ~350 lines) — extracted `_GraphPlotMixin` with 14 shared methods (plotting, colors, axis labels, keyboard rotation/zoom). Both classes now inherit from the mixin, keeping only their unique setup logic. Also fixed minor bug: GraphViewer was not resetting `ax_3d = None` on figure clear, and removed dead `tick_positions` variable in GraphWidget._plot_3d.

## Recent Completed Work (Feb 7, 2026) - Audit Fixes
- **Atomic file writes** for `save_rom`, `_save_project_file`, `_save_commits` — write-to-temp + `os.replace()` prevents corruption on crash
- **Fixed swapxy flatten bug** in `write_table_data` — was using C order instead of F order for swapxy tables, causing silent data corruption on bulk write
- **Fixed paste to use `bulk_changes` signal** — paste now creates a single undo entry instead of N individual entries (one per cell)
- **Memory leak fixes** — added `deleteLater()` in `close_tab`, `WA_DeleteOnClose` on `TableViewerWindow` and `GraphViewer`, matplotlib figure cleanup in `closeEvent`
- **Fixed `rom_document.save()` to clear modified flag** — `set_modified(False)` was missing after successful save
- **Rewrote 3 tautological test files** — `test_axis_editing.py`, `test_interpolation.py`, `test_table_viewer_helpers.py` now import and test actual production code (ScalingConverter, _convert_expr_to_python, swapxy round-trips, atomic writes)
- **Pinned dependency versions** in `requirements.txt` with upper bounds (e.g., `PySide6>=6.10.0,<7.0.0`)
- **Code audit report** saved to `docs/CODE_AUDIT_REPORT.md` (gitignored, personal reference)

## Recent Completed Work (Feb 7, 2026) - Earlier
- Fixed undo/redo performance: ROM data writes were O(N*ROM_size) per operation due to immutable `bytes` concatenation. Changed `rom_data` to `bytearray` for O(1) in-place writes.
- Fixed CTRL+Z not working in newly opened table viewer: `set_active_stack()` failed to create the undo stack on first window focus, so the stack was never activated until the window was closed and reopened.
- Fixed bulk undo/redo performance in main.py: `_update_project_ui()` was called N+1 times during bulk undo (once per cell via `_notify_change` callback + once direct). Added `_in_bulk_undo` guard to both `_on_changes_updated` callback and removed redundant direct call in `_update_pending_from_undo`. Now called exactly once at `_end_bulk_update`.
- Fixed undo stack staying active after closing table viewer window: `closeEvent` now deactivates the undo stack, preventing undo from executing on closed tables.
- Changed min/max coloring to use scaling definition min/max instead of current data values. Applies to table viewer (values + both axes) and graph viewer. Each of the 3 scalings in a 3D table (X axis, Y axis, values) uses its own scaling range.
- Fixed non-uniform graph cell sizes: graphs now use uniform indices for mesh coordinates (all cells same size) with actual axis values as tick labels. Previously, non-uniformly spaced axis values (e.g., RPM) caused edge cells to be thinner.

## Recent Completed Work (Feb 1, 2026)
- Fixed undo/redo performance for bulk operations (matching increment/decrement speed)
  - Root cause: Bulk undo/redo called per-cell updates without batching optimizations
  - Added `begin_bulk_update()` / `end_bulk_update()` methods to TableViewer and TableDisplayHelper
  - These methods disable widget updates, block signals, disable ResizeToContents headers, and cache min/max for color calculations
  - Updated `BulkCellEditCommand` and `BulkAxisEditCommand` to call bulk callbacks before/after applying changes
  - Files modified: `display.py`, `table_viewer.py`, `table_undo_manager.py`, `undo_commands.py`, `main.py`
- Implemented per-table undo/redo using Qt's QUndoGroup pattern
  - Each table now has its own undo stack (undo in Table A only affects Table A)
  - Created `src/core/undo_commands.py` - QUndoCommand subclasses (CellEditCommand, BulkCellEditCommand, AxisEditCommand, BulkAxisEditCommand)
  - Created `src/core/table_undo_manager.py` - Manages QUndoGroup and per-table QUndoStacks
  - Refactored `src/core/change_tracker.py` - Now only handles pending changes for commit tracking
  - Updated `main.py` - Integrated TableUndoManager, QUndoGroup-based menu actions
  - Updated `table_viewer_window.py` - Shortcuts route to main window's undo group
  - Added `focus_table` command to test_runner.py for switching between open tables
  - Added `tests/test_table_undo_manager.py` - 11 unit tests for per-table undo
  - Added `tests/gui/test_per_table_undo.txt` - GUI test script

## Recent Completed Work (Jan 31, 2026)
- Fixed major performance issue with bulk cell editing - operations that changed hundreds of cells were slow due to widget repainting on every cell update
  - Wrapped all bulk operations with `setUpdatesEnabled(False)` before processing and `setUpdatesEnabled(True)` with single `viewport().update()` after
  - Fixed in: `operations.py` (apply_bulk_operation, smooth_selection), `clipboard.py` (paste_selection), `interpolation.py` (all three interpolation methods)
  - Performance improvement: from hundreds of repaints to a single repaint at the end
- Fixed undo/redo to only apply to the focused table viewer window
  - Modified `_apply_cell_change()` and `_apply_axis_change()` in main.py to check `window.isActiveWindow()` before applying changes
  - Prevents undo/redo from affecting the wrong window when multiple table viewers are open

## Recent Completed Work (Jan 17, 2026)
- Added focus/highlight selected table feature: clicking a table viewer window now highlights and scrolls to that table in the tree view
- Standardized directory naming to plural convention: `colormap/` -> `colormaps/`, `metadata/` -> `definitions/` (kept `src/` as-is per Python convention)
- Fixed logging handler MRO conflict in `log_console.py` (QObject.emit vs logging.Handler.emit)
- Attempted PyQtGraph migration but reverted - OpenGL requires desktop environment, creating two implementations wasn't worth the maintenance burden
- Documented UI testing tools in `docs/UI_TESTING.md`
- Added UI Testing section to `CLAUDE.md` with rules for screenshot/testing scenarios
- Added `rotate_graph <elev> <azim>` command to test_runner.py

## Recent Completed Work (Jan 16, 2026)
- Added Copy Table to Clipboard (Ctrl+Shift+C) and Export to CSV (Ctrl+E)
- Added Smooth Selection (S) for light neighbor-based smoothing
- Removed graph widget and "Value" label for 1D tables
- Hidden View menu for 1D tables (when not in diff mode)
- Added graph auto-refresh on data changes
- Fixed undo/redo graph refresh with debouncing (50ms timer)
