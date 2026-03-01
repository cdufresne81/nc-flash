# Changelog

All notable changes to NC ROM Editor are documented here.

## [Unreleased]

### Added
- **Configurable CSV export directory** — New "Export Directory" setting in Settings > General lets you choose a default folder for CSV exports (Ctrl+E). Leave empty to keep the default behavior (exports next to the ROM file)

### Changed
- **Projects UI hidden behind feature flag** — Projects directory setting and View menu are now only shown when `--enable-projects` is passed
- **README updated for Linux** — Installation section now documents Linux `.tar.gz` download alongside Windows
- **Project structure reorganized** — Moved build/packaging files (`build.bat`, `installer.iss`, `NCRomEditor.spec`, `requirements-build.txt`) into `packaging/` directory; moved `WINDOWS_SETUP.md` into `docs/`

### Fixed
- **"Modified only" filter now expands categories** — Toggling the "Modified only" checkbox in the table browser auto-expands categories with modified tables, matching search filter behavior
- **run.sh argument passthrough** — Linux/macOS launcher now passes CLI arguments (`"$@"`) to `main.py`, matching `run.bat` parity

## [v1.4.2] - 2026-03-01

### Added
- **Linux build in release pipeline** — Release workflow now builds a `NCRomEditor-{version}-linux-x86_64.tar.gz` package alongside the Windows installer
- **Cross-platform PyInstaller spec** — `NCRomEditor.spec` detects the OS and sets the icon accordingly (`.ico` on Windows, skipped on Linux)

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
