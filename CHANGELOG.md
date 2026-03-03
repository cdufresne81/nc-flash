# Changelog

All notable changes to NC Flash are documented here.

## [Unreleased]

### Changed
- **Extracted shared icon factory** — Moved QPainter toolbar icons from `main.py` (143 lines) and `table_viewer_window.py` (102 lines) into `src/ui/icons.py` with dispatch table
- **Consolidated duplicated format utilities** — Created `src/utils/formatting.py` with shared `printf_to_python_format`, `format_value`, `get_scaling_range`, `get_scaling_format` (was duplicated 3-4x across modules)
- **Unified interpolation functions** — Merged near-identical `interpolate_vertical`/`interpolate_horizontal` (~250 lines each) into shared `_interpolate_1d(direction)` with extracted helpers
- **Refactored test_runner command dispatch** — Replaced 159-line if/elif chain with dispatch table + small handler methods
- **Separated dev dependencies** — Split `requirements.txt` into runtime-only + `requirements-dev.txt` for pytest/black/flake8
- **Updated README** — Fixed Python version (3.10+ not 3.12+), removed stale "In Development" / "Next Priorities" sections, updated project structure tree to reflect all current files
- **Archived abandoned design docs** — Moved `MODIFICATION_TRACKING_PLAN.md` and `SUMMARY.md` to `docs/archive/` (described never-built SQLite design)
- **Updated ROM comparison spec** — Marked implemented "Out of Scope" items (cross-definition compare, copy-table editing)

### Fixed
- **Horizontal interpolation emit timing** — Was emitting changes per selection range instead of once after all ranges (matching vertical behavior)
- **Silent exception swallows** — Three `except: pass` blocks in `main.py` now log with `logger.debug`
- **Exception chaining** — `project_manager.create_project` now chains exceptions with `from e`
- **Test fix** — `test_get_table_font_size_default` updated to match actual default (11, not 9)

### Removed
- Dead code cleanup: 4 unused dataclasses from `version_models.py`, legacy `ScalingEditDialog`, unused `HistoryPanel`, 4 deprecated methods across `table_viewer.py`, `change_tracker.py`, `table_browser.py`

## [v2.0.0] - 2026-03-02

### Changed
- **Rebranded from "NC ROM Editor" to "NC Flash"** — App name, exe name, installer, asset filenames, QSettings keys, user data directory, MCP server name, all documentation, and GitHub URLs updated. Exe is now `NCFlash.exe`, installer outputs `NCFlash-{version}-Setup.exe`, user data moves to `%APPDATA%/NCFlash`. GitHub repo is now `cdufresne81/nc-flash`
- **Settings reorganization** — Moved Metadata Directory setting from General > Paths to Tools > RomDrop group, alongside the RomDrop executable path

## [v1.6.0] - 2026-03-01

### Added
- **Tuning log** — Every commit auto-generates a `TUNING_LOG.md` entry with version name, description, table change summary with direction indicators, and a "Results" section to fill in after testing
- **Revert to version** — Restore a previous ROM snapshot as the working file. Newer versions are soft-deleted. Available from the History viewer
- **Soft delete versions** — Remove bad snapshots by moving them to `_trash/`. Deleted versions are hidden in history (toggleable with "Show deleted" checkbox)
- **Version History toolbar button** — Clock icon in the toolbar, enabled when a project is open
- **Read-only version comparison** — Double-click a table in History or click "Compare Versions..." to open a side-by-side comparison (reuses ROM Compare window with copy buttons hidden)
- **Window geometry persistence** — History viewer and compare window remember their size, splitter position, and column widths across sessions
- **37 new tests** for project management: tuning log generation, soft delete, revert, commit flow, backward compatibility

### Changed
- **Mandatory version names** — Every commit now requires a version name (e.g., "egr_delete") and always creates a named ROM snapshot. The snapshot checkbox and optional suffix have been replaced with a single required field
- **Simplified working ROM naming** — Working file is now `{ROMID}.bin` instead of `v1_{ROMID}_working.bin`
- **Projects always enabled** — Removed `--enable-projects` feature flag. Project menu items (New Project, Commit Changes, Commit History) are always visible
- **Commit dialog redesigned** — Version name field (required, auto-sanitized), filename preview, optional description. Removed snapshot checkbox and QuickCommitDialog
- **History viewer columns** — Replaced Version + Message columns with a single Snapshot column showing the filename
- **Commit author defaults to system user** — Uses `os.getlogin()` instead of hardcoded "User"

### Fixed
- **Commit clears modified flag** — Committing no longer leaves the document marked as modified, preventing a spurious "unsaved changes" prompt on close
- **Commit message line breaks** — Multi-line commit messages now render correctly in the history details panel

### Removed
- `--enable-projects` feature flag — projects are now a core feature
- `last_suffix` and `settings` fields from Project model (dead code)
- `QuickCommitDialog` class (unused)

## [v1.5.0] - 2026-03-01

### Added
- **RomDrop setup wizard** — First-run wizard now asks for the RomDrop installation folder (not just a definitions directory). Step 1 selects the folder, Step 2 confirms derived paths for `romdrop.exe` and `metadata/` with green/red validation indicators. Both paths are editable for non-standard layouts
- **Configurable CSV export directory** — New "Export Directory" setting in Settings > General lets you choose a default folder for CSV exports (Ctrl+E). Leave empty to keep the default behavior (exports next to the ROM file)

### Changed
- **"Definitions" renamed to "Metadata"** — All UI labels, settings keys, CLI flags, and log messages now use "metadata" instead of "definitions" to match RomDrop's naming convention. Settings key changed from `paths/definitions_directory` to `paths/metadata_directory`. MCP server flag changed from `--definitions-dir` to `--metadata-dir`
- **Bundled XML files moved to examples/metadata/** — The `definitions/` directory has been restructured to `examples/metadata/` since it contains example/bundled data
- **README updated for Linux** — Installation section now documents Linux `.tar.gz` download alongside Windows
- **Project structure reorganized** — Moved build/packaging files (`build.bat`, `installer.iss`, `NCFlash.spec`, `requirements-build.txt`) into `packaging/` directory; moved `WINDOWS_SETUP.md` into `docs/`

### Fixed
- **"Modified only" filter now expands categories** — Toggling the "Modified only" checkbox in the table browser auto-expands categories with modified tables, matching search filter behavior
- **run.sh argument passthrough** — Linux/macOS launcher now passes CLI arguments (`"$@"`) to `main.py`, matching `run.bat` parity

## [v1.4.2] - 2026-03-01

### Added
- **Linux build in release pipeline** — Release workflow now builds a `NCFlash-{version}-linux-x86_64.tar.gz` package alongside the Windows installer
- **Cross-platform PyInstaller spec** — `NCFlash.spec` detects the OS and sets the icon accordingly (`.ico` on Windows, skipped on Linux)

### Changed
- **CI matrix optimized** — Reduced from 9 jobs (3 OS x 3 Python) to 4 jobs (Ubuntu 3.10+3.12, Windows 3.12, macOS 3.12). Cuts macOS billing from ~60 to ~20 minutes per run.
- **NumPy version relaxed** — Lower bound `numpy>=2.4.0` → `numpy>=2.2.0` so Python 3.10 and 3.11 can install dependencies

### Fixed
- **CI pipeline failures** — Fixed `black --check` failing on 63 unformatted files and `numpy>=2.4.0` blocking Python 3.10/3.11 installs
- **Linux CI crashes** — Install `libegl1` and set `QT_QPA_PLATFORM=offscreen` for headless PySide6 on GitHub Actions runners
- **Test port conflict** — Command server tests now use a dedicated port (18766) to avoid conflicts with a running app instance

## [v1.4.1] - 2026-03-01

### Fixed
- **MCP connection dialog** — Shows STDIO config for Claude Desktop, fixed missing `os` import

## [v1.4.0] - 2026-02-28

### Added
- **MCP server for AI assistant access** — Model Context Protocol server (`python -m src.mcp.server`) exposes 9 tools for ROM inspection and editing. Supports STDIO and SSE transports. Works with Claude Code, Claude Desktop, ChatGPT, and Gemini. LRU-cached ROM loading (4 entries).
- **AI write access to ROM tables** — New `write_table` MCP tool lets AI modify table values through the app's editing pipeline with full undo support. Changes appear in the app immediately and can be undone with Ctrl+Z.
- **Live table reading for AI** — New `read_live_table` and `list_modified_tables` MCP tools read current in-memory values (including unsaved edits) from the running app, instead of stale on-disk data.
- **Command API server** — Lightweight HTTP bridge (`src/api/command_server.py`) on port 8766 that routes MCP requests to the Qt main thread via queue + QTimer polling. Starts/stops automatically with the MCP server. No new dependencies.
- **Workspace state file for MCP auto-discovery** — App writes `workspace.json` listing open ROMs (path, xmlid, make/model/year, modified flag, active tab). MCP server reads it via new `get_workspace` tool so AI assistants can discover open ROMs without manual path entry. File is written on open/close/save and deleted on app exit.
- **MCP server toggle in app** — Start/stop the MCP server directly from the Tools menu or toolbar (broadcast antenna icon, green when running). Uses SSE transport on `http://127.0.0.1:8765/sse` so any MCP client can connect. Optional "Start MCP server on startup" setting in Settings > Tools. Server subprocess is automatically stopped on app exit.

## [v1.3.0] - 2026-02-28

### Added
- **Windows installer** — Inno Setup installer with Start Menu shortcut, optional Desktop shortcut, and uninstaller
- **PyInstaller packaging** — Standalone Windows exe build via `build.bat`, no Python required to run
- **Flash ROM to ECU** — One-click flash via RomDrop integration (`Ctrl+Shift+F`) with safety warning dialog
- **RomDrop settings** — Configurable RomDrop executable path in Settings > Tools
- **GitHub Actions release pipeline** — Automatically builds and publishes the installer on tagged releases
- **App icon** — Custom icon for the exe, taskbar, and installer

### Changed
- **Unified Open action** — Single "Open..." (`Ctrl+O`) replaces separate ROM/Project openers
- **Projects behind feature flag** — Project management UI hidden unless `--enable-projects` is passed

## [v1.2.0] - 2026-02-27

### Added
- **ROM comparison tool** — Side-by-side comparison of two ROMs (`Ctrl+Shift+D`) with change highlighting
- **Cross-definition comparison** — Compare ROMs with different ECU definitions (e.g., NC1 vs NC2)
- **Table viewer toolbar** — 12 quick-access buttons for editing, interpolation, and visualization
- **Main window toolbar** — Open, Save, Compare, Settings buttons with programmatic icons
- **Copy table between ROMs** — Copy table values from one ROM to another in compare view

### Fixed
- **Table viewer auto-sizing** — Fixed last row being clipped behind horizontal scrollbar
- **3D graph performance** — 45% faster initial render, 55% faster selection updates
- **Multi-ROM undo isolation** — Undo stacks no longer shared between ROMs with same definition

## [v1.1.0] - 2026-02-07

### Added
- **Per-table undo/redo** — Each table has its own undo stack
- **Bulk operation performance** — Single repaint for multi-cell operations
- **Min/max coloring from scaling definitions** — Instead of current data values
- **Uniform graph cell sizes** — Non-uniform axis values no longer cause thin edge cells

### Fixed
- **40 code audit findings remediated** — Security (XXE prevention), memory leaks, performance, error handling
- **Atomic file writes** — Prevents ROM corruption on crash
- **Paste uses bulk signal** — Single undo entry instead of N individual entries

## [v1.0.0] - 2026-01-16

### Added
- ROM file reading and writing for NC Miata ECUs
- Automatic ROM ID detection and XML definition matching
- 1D, 2D, and 3D table viewing with axis labels
- Cell editing with validation
- Interactive 3D surface plots and 2D line graphs
- Thermal color gradient with configurable colormaps
- Copy/paste, CSV export, clipboard support
- Interpolation (vertical, horizontal, bilinear) and smoothing
- Multi-ROM tabs with session restore
- Category-based table browser with search
