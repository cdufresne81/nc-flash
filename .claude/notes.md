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
