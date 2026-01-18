# Session Notes

## Next Tasks

### Housekeeping
- Directory naming standardized to plural convention (see Recent Completed Work)

### ROM Tools
- **ROM comparison tool** - Compare two ROM files (stock vs modified), highlight differences in tables and raw data
- **ROM modification tracking** - Git-like tracking for ROM edits, project organization with ROM aliases, change history viewer, export modification logs
  - Detailed planning docs: `docs/MODIFICATION_TRACKING_PLAN.md` (full technical spec) and `docs/MODIFICATION_TRACKING_SUMMARY.md` (overview)
  - Status: Not started, estimated 10 weeks part-time

### Distribution
- **Windows packaging** - Use PyInstaller to package as standalone .exe, test on clean Windows system

## Environment Notes
- Use `python3` not `python` (WSL2 environment lacks symlink)

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
