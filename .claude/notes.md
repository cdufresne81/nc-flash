# Session Notes

## Next Tasks

### Performance & UI
- **Replace matplotlib with PyQtGraph** in `graph_viewer.py` for better 3D performance
  - PyQtGraph uses OpenGL for hardware acceleration
  - Supports in-place data updates without recreating plots
  - Key issues to solve: full figure recreation, nested Python loops for colors, matplotlib 3D limitations
- **Focus/highlight selected table** - When clicking a table viewer (e.g., "Load Scaling"), highlight and focus that table in the tree (gray background like manual selection)

### Housekeeping
- **Standardize directory naming** - Choose singular or plural convention and apply consistently. Current state:
  - Plural: `docs/`, `examples/`, `projects/`, `tests/`, `tools/`
  - Singular: `colormap/`, `metadata/`, `src/`

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
