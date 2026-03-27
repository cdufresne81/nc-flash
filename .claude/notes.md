# Session Notes

## Next Tasks
- CI secret `SECURE_REPO_PAT` is configured and matches workflows. No graceful fallback if missing (CI hard-fails), but this is acceptable.
- `examples/metadata/LFDJEA.xml` is untracked — may need committing
- **Review romdrop.crc fallback** — `src/ecu/rom_utils.py:169` silently skips CRC verification if `romdrop.crc` is missing. Patching still proceeds without validation. Need to decide: should patching be blocked without the CRC database, or is a warning sufficient?

## Recent Completed Work (Mar 27, 2026) - House Cleaning
- **CHANGELOG restructured** — Unreleased section was a mess (contained v2.1.0 through v2.3.0 items). Split into proper `[v2.3.0]`, `[v2.2.0]`, `[v2.1.1]`, `[v2.1.0]` sections with correct dates. Unreleased now only has current house-cleaning work
- **GitHub v2.3.0 release notes updated** — Were using stale Unreleased dump; now match the proper v2.3.0 changelog section
- **README overhaul** — Version v2.0.0 → v2.3.0, rewrote ECU Flashing for native J2534 (removed RomDrop references), added missing feature sections (Project Management, cross-definition compare, toolbars, setup wizard), corrected test coverage and CI versions
- **Docs reorganized** — Moved internal docs to `docs/internal/`, removed 19 obsolete files (~6 MB): code audits, mockups, error screenshots, EcuFlash examples, archived design docs. Updated CLAUDE.md and README paths
- **Deleted `run-dev.bat`** — Vestigial, identical to `run.bat` since `--enable-projects` removed
- **Removed `examples/LF5AEG*`** — 3 tracked ROM/patch files removed from git
- **Removed `Thinking-pad.md` from git** — Added to `.gitignore`, kept local file
- **Branch `fix/house-cleaning`** created from `origin/master` with all changes staged

## Recent Completed Work (Mar 26, 2026) - Build Fix
- **J2534 bridge frozen-app fix** — PyInstaller builds failed to load 32-bit DLL because frozen ctypes raises a different OSError than native bitness mismatch. Bridge fallback now detects both.

## ECU Module Status (feature/ecu-flash-module branch)
- **Read ROM**: Working end-to-end. Threading verified safe (explicit `Qt.QueuedConnection` on all worker signals).
- **Flash ROM**: Working. CheckFlashCounter resolved — moved from `_authenticate()` to flash-only path (`flash_manager.py:525-527`), matching romdrop binary analysis.
- **Security algorithm**: Working (3-byte seed + "MazdA" → 8-byte LFSR)
- **32-bit bridge**: Working. Auto-builds on first dev use via `packaging/build_bridge.py`
- **_secure module**: Private repo only (nc-flash-secure). CI pulls via `secrets.SECURE_REPO_PAT` (not `SECURE_MODULE_PAT` as previously noted)

## Recent Completed Work (Mar 24, 2026) - ECU Programming Window
- **ECU Programming window** — Dedicated window replacing scattered ECU menu items. Auto-connects, status cards (battery/engine/ECU), one-click dynamic flash, inline progress, auto-save ROM reads as `{ROM_ID}_{timestamp}.bin`
- **OBD-II PID reading** — Battery voltage (PID 0x42) and engine RPM (PID 0x0C) confirmed working on NC2 ECU. Voltage is soft warning (12V threshold), engine running is hard block
- **Checksum 67x faster** — struct.unpack batch decode replaces Python for-loop. Bounds checking added for invalid table entries
- **Safety audit** — Fixed _ecu_busy stuck True, abort signal accumulation, missing __init__ attrs, _owns_connection reset, subprocess error handling, closeEvent thread cleanup
- **Per-session logs** — `./logs/YYYY-MM-DD_HHMMSS.log` per app launch
- **UDS log direction prefixes** — `ECU >>` / `Tool >>` on protocol messages
- **DTC log deduplication** — "Read 15 DTCs (7 unique)"
- **Window geometry persistence** — Saves/restores position and size
- **Tester Present demoted to DEBUG**

## Recent Completed Work (Mar 24, 2026) - ECU Flash Module Hardening
- **Security algorithm fix** — Seed-to-key was wrong: ECU sends 3-byte seed, must append 5-byte challenge constant "MazdA" to form 8-byte LFSR input. Found by tracing romdrop.exe binary at 0x0040587C. Verified against 2 known pairs from romdrop logs.
- **32-bit bridge exe** — Built j2534_bridge.py as standalone 32-bit PyInstaller exe. Updated NCFlash.spec to bundle it, build.bat to build it, release.yml for CI. j2534.py looks for exe first, falls back to py -3-32.
- **ECU Info cleanup** — VIN strips non-printable bytes, ROM ID strips 2-byte echo prefix, DTCs deduplicated. Added P0F01, U3F01-U3F04, U3F21, U3FC1 to DTC table.
- **CheckFlashCounter moved to flash-only** — Was in _authenticate(), bricked ECU when called during Read ROM. Binary analysis confirmed it's only in flash path (0x00404C72), never in read path.
- **_secure module purged from public repo** — git filter-branch rewrote all 232 commits. .gitignore updated. Private repo nc-flash-secure updated with corrected algorithm.
- **Thread safety fixes** — Qt.QueuedConnection on all worker→UI signals (was missing in flash path). Abort flag changed from bool to threading.Event.
- **Error handling** — J2534Error now propagates instead of being masked as UDSTimeoutError. Bridge timeout overhead reduced from +5s to +2s.
- **Removed redundant "Clear DTCs" menu item** — Read DTCs already offers clear.
- **Bridge log levels** — Demoted bridge startup messages from INFO to DEBUG.
- **Abort during read** — Enabled abort button during READING state (safe — no write transaction).

## Recent Completed Work (Mar 23, 2026) - Interleaved 3D Tables
- **Interleaved 3D table support** — Added `TableLayout` enum (`CONTIGUOUS`/`INTERLEAVED`), `layout` attribute parsing in definition parser, and interleaved read/write/cell-edit/axis-edit in `RomReader`. 256 lines of tests in `test_interleaved_tables.py`. Enables TCM ROM support where Y-axis values are interleaved with data rows.

## Recent Completed Work (Mar 26, 2026) - README Audit & Cleanup
- **Rebased feature/ecu-flash-module onto master** — Picked up v2.1.0 changelog and merge commit
- **README audit and update** — Fixed version (v2.0.0 → v2.3.0), corrected test coverage stats, fixed CI Python versions (3.10/3.12), added `mcp_mixin.py` to project structure tree
- **Deleted `run-dev.bat`** — Vestigial launcher identical to `run.bat` since `--enable-projects` flag was removed
- **CHANGELOG updated** — Added README changes and run-dev.bat removal to Unreleased section

## Recent Completed Work (Mar 5, 2026) - Pipeline Fixes
- **Black formatting** — Ran black on 21 unformatted files (was failing CI lint)
- **Release pytest fix** — Changed `requirements.txt` to `requirements-dev.txt` in release.yml (pytest was missing)
- **workflow_dispatch** — Added manual trigger to both CI and release workflows
- **CLAUDE.md** — Added `black` to quality gates checklist

## Recent Completed Work (Mar 3, 2026) - Code Audit & Cleanup
- **Full codebase audit** — Read all 47 source files (16,453 lines), all 19 test files (243 tests), all docs/configs. Wrote detailed audit to `docs/CODE_AUDIT_2026_03.md`.
- **Dead code removal** — Removed 7 items: 4 unused dataclasses from `version_models.py`, 4 legacy methods from `table_viewer.py`, deprecated `get_modified_tables()` from `change_tracker.py`, legacy `ScalingEditDialog` from `scaling_edit_dialog.py`, unused `HistoryPanel` from `history_viewer.py`, deprecated `update_modified_tables()` from `table_browser.py`.
- **Duplication consolidation** — Created `src/utils/formatting.py` with shared `printf_to_python_format`, `format_value`, `get_scaling_range`, `get_scaling_format`. Eliminated triple-duplication across `display.py`, `compare_window.py`, `rom_context.py`.
- **Interpolation dedup** — Unified near-identical `interpolate_vertical`/`interpolate_horizontal` (~250 lines each) into shared `_interpolate_1d(direction)` + extracted `_apply_axis_interpolation` and `_apply_data_interpolation` helpers. Fixed bug where horizontal emit was per-range instead of once-after-all-ranges.
- **Error handling fixes** — 3 silent `except: pass` in `main.py` now log with `logger.debug`; exception chain added in `project_manager.py`.
- **Test fix** — Fixed pre-existing `test_get_table_font_size_default` (expected 9 but default was changed to 11).
- **Test runner dispatch refactor** — Replaced 159-line if/elif chain in `_execute_command` with dispatch table + small handler methods.
- **Dependency split** — `requirements.txt` now runtime-only; dev tools in `requirements-dev.txt`. CI updated to use `requirements-dev.txt`.
- **Doc cleanup** — Archived abandoned `MODIFICATION_TRACKING_PLAN/SUMMARY.md` to `docs/archive/`. Updated ROM comparison spec to reflect implemented features.
- **MCP mixin extraction** — Moved 13 MCP/API methods (~500 lines) from `main.py` into `src/ui/mcp_mixin.py`. `main.py` is now 1,970 lines (was 2,606). Also fixed latent import bug where API handlers referenced renamed `_printf_to_python_format`.

## Recent Completed Work (Mar 2, 2026) - Rebrand: NC ROM Editor → NC Flash
- **Full project rename** — Renamed all references from "NC ROM Editor" / "NCRomEditor" to "NC Flash" across the entire codebase. Display name is "NC Flash", exe/filenames use "NCFlash" (no space), GitHub repo is `cdufresne81/nc-flash`. Updated: app name, exe name, asset files, installer, build scripts, CI workflow, MCP server, QSettings keys, user data directory, launcher scripts, setup wizard, documentation, tests, and CHANGELOG.

## Recent Completed Work (Mar 1, 2026) - Project Management Refactor: Tuning Log with Mandatory Snapshots
- **Tuning log auto-generation** — Every commit appends a markdown section to `TUNING_LOG.md` with version name, description, table change summary (count + direction: ↑/↓/→/~ with avg %), based-on reference, ROM filename, and a "Results" placeholder. Header written on project creation with vehicle/ECU/checksum info.
- **Mandatory version snapshots** — Removed optional snapshot checkbox from commit flow. Every commit always creates `v{N}_{ROMID}_{name}.bin`. Version name is required and auto-sanitized (lowercase, spaces→underscores, strip special chars).
- **Soft delete versions** — `soft_delete_version()` moves snapshot to `_trash/`, marks `deleted=True` in commits.json. Cannot delete v0.
- **Revert to version** — `revert_to_version()` loads snapshot bytes, overwrites working ROM, soft-deletes all newer versions. Appends revert entry to tuning log.
- **Simplified working ROM naming** — Changed from `v1_{ROMID}_working.bin` to `{ROMID}.bin`. Old projects still work (backward compat via `project.json.working_rom` field).
- **Removed `--enable-projects` feature flag** — Projects are now always enabled. Removed `projects_enabled` from main.py, session_mixin.py, recent_files_mixin.py, settings_dialog.py, run-dev.bat. All project menu items always visible.
- **Commit dialog redesigned** — Single required version name field with auto-sanitization and real-time filename preview. Optional message field. Removed snapshot checkbox, suffix field, and `QuickCommitDialog`.
- **History viewer enhancements** — Added "Revert to this version" and "Delete this version" buttons. Deleted commits hidden by default with "Show deleted" toggle. Deleted items shown with strikethrough + gray when toggled on.
- **Version model cleanup** — Added `deleted: bool` to Commit dataclass. Removed `last_suffix` and `settings` from Project dataclass.
- **37 new tests** — Full coverage for: create_project (working ROM naming, tuning log, v0), commit_changes (snapshot, tuning log, direction, sequential versions), soft_delete (trash, marks, persistence, guards), revert (overwrite, cascade delete, log, v0, monotonic), backward compat, commit dialog sanitization.
- **History viewer polish** — Snapshot filename as primary column (removed Version+Message columns), "Show deleted" toggle, read-only CompareWindow for version diffs (single instance, parented to history dialog so it appears on top), git-log style toolbar icon for version history (enabled when project is open).
- **Default author to system user** — `Commit.create()` uses `os.getlogin()` instead of hardcoded "User".
- **Window geometry persistence** — History viewer and compare window save/restore size, splitter position, and column widths via QSettings. History viewer uses `done()` override (not `closeEvent`) since `accept()` doesn't trigger `closeEvent` on modal dialogs.
- **Commit clears modified flag** — `document.set_modified(False)` called after successful commit so the close prompt doesn't ask about already-committed changes.
- **Commit message preserves line breaks** — Newlines in commit messages rendered as `<br>` in the HTML details view.

## Recent Completed Work (Mar 1, 2026) - RomDrop Setup Wizard & Definitions → Metadata Rename
- **RomDrop setup wizard** — Rewrote `setup_wizard.py` from single-page definitions directory picker to two-step QWizard: Step 1 selects RomDrop installation folder (validates romdrop.exe + metadata/ presence), Step 2 confirms derived paths with green/red status indicators and editable fields. Saves both `romdrop_executable_path` and `metadata_directory` on completion.
- **Renamed "definitions" → "metadata" across codebase** — Renamed `get_definitions_directory()`/`set_definitions_directory()` → `get_metadata_directory()`/`set_metadata_directory()` in settings.py. Updated QSettings key from `paths/definitions_directory` to `paths/metadata_directory`. Default path changed from `definitions/` to `examples/metadata/`. Updated all callers: main.py, session_mixin.py, settings_dialog.py, project_wizard.py, rom_detector.py, rom_context.py, server.py. MCP CLI flag renamed `--definitions-dir` → `--metadata-dir`.
- **Restructured project directories** — Moved `definitions/lf9veb.xml` to `examples/metadata/lf9veb.xml`. Deleted `definitions/` directory. Updated packaging spec, test fixtures, README project tree.

## Recent Completed Work (Mar 1, 2026) - Configurable CSV Export Directory
- **Configurable export directory** — Added "Export Directory" setting (Settings > General) with browse button. CSV exports (Ctrl+E) default to `%APPDATA%/NCFlash/exports` (or platform equivalent). Configurable to any folder.
- **Projects UI hidden behind feature flag** — Projects directory setting in Settings > General and the View menu (which only contained "Commit History") are now hidden unless `--enable-projects` is passed

## Recent Completed Work (Mar 1, 2026) - Table Browser & run.sh Fixes
- **"Modified only" filter auto-expands categories** — Table browser now expands category folders when "Modified only" filter is active, matching search filter behavior
- **run.sh CLI argument passthrough** — Added `"$@"` to `python3 main.py` call in `run.sh` to match `run.bat`'s `%*`

## Recent Completed Work (Mar 1, 2026) - README & Project Cleanup
- **README Linux install docs** — Added Linux `.tar.gz` download/extract instructions alongside Windows in Installation section
- **Project structure reorganization** — Moved build files (`build.bat`, `installer.iss`, `NCFlash.spec`, `requirements-build.txt`) to `packaging/` directory; moved `WINDOWS_SETUP.md` to `docs/`; updated all references in CI, build scripts, and README
- **WINDOWS_SETUP.md cleanup** — Fixed hardcoded paths, removed WSL-specific dev notes
- **Junk file cleanup** — Deleted `nul` (Windows artifact) and `testsguitemp_screenshot.txt`; added `nul` to `.gitignore`

## Recent Completed Work (Mar 1, 2026) - Linux Release Build
- **Linux build in release pipeline** — Added `build-linux` job to `release.yml` (ubuntu-22.04, PyInstaller → tar.gz). Release job now collects artifacts from both Windows and Linux builds. Cross-platform `NCFlash.spec` (conditional icon). Tests use dedicated port 18766 to avoid conflicts with running app.

## Recent Completed Work (Mar 1, 2026) - CI Pipeline Fix
- **Fixed CI pipeline** — Relaxed `numpy>=2.4.0` → `numpy>=2.2.0` (Python 3.10/3.11 support), ran `black` on 63 files, optimized CI matrix from 9→4 jobs (Ubuntu 3.10+3.12, Windows 3.12, macOS 3.12). Lint job updated to Python 3.12, codecov trigger updated to match.

## Recent Completed Work (Feb 28, 2026) - Live App Bridge & AI Write
- **Command API server** — `src/api/command_server.py` runs an HTTP server on daemon thread (127.0.0.1:8766) that bridges MCP requests to Qt main thread via `queue.Queue` + `QTimer` (50ms poll). Handles POST to `/api/read-table`, `/api/modified`, `/api/edit-table`. Auto-starts/stops with MCP server. No new dependencies.
- **AI write access to ROM tables** — `write_table` MCP tool sends cell edits through the full app pipeline: undo tracking (`table_undo_manager.record_bulk_cell_changes`), change tracking (`change_tracker.record_pending_bulk_changes`), ROM write (`rom_reader.write_cell_value`), modified flag, cell border highlighting, and table viewer refresh. Values are in display units; conversion to raw via `ScalingConverter.from_display()`.
- **Live table reading** — `read_live_table` MCP tool reads from the app's in-memory `RomReader` (includes unsaved edits) instead of disk. `list_modified_tables` queries `change_tracker._pending` for modified table names and change counts.
- **`_handle_api_request()` dispatcher in main.py** — Routes API requests to `_api_list_modified`, `_api_read_table`, `_api_edit_table`. The `_api_edit_table` handler follows the same pattern as `apply_compare_copy` (undo, change tracker, ROM write, border tracking, viewer refresh).
- **`_post_to_app()` in rom_context.py** — MCP server reads `command_api_url` from `workspace.json`, POSTs to it with `urllib.request`. 10s timeout. Graceful errors for missing workspace, connection refused, timeout.
- **12 new tests** in `tests/test_command_server.py` — 7 for CommandServer HTTP mechanics (start/stop, POST routing, 404/405/400 errors, callback exception), 5 for RomContext live bridge (no workspace, connection refused, delegation tests).

## Recent Completed Work (Feb 28, 2026) - Workspace State File & MCP Toggle
- **Workspace state file for MCP auto-discovery** — App writes `workspace.json` to project root listing open ROMs (path, file_name, xmlid, make/model/year, is_modified, active_rom). Written on ROM open, close, save, and project open. Deleted on app exit. MCP server reads it via new `get_workspace()` tool in `rom_context.py` and `server.py`. Graceful fallback when file missing or corrupt. 3 new tests in `test_mcp_server.py`. File gitignored.
- **MCP server toggle in app** — Tools menu checkable action + toolbar button (broadcast antenna icon, green when on) to start/stop MCP server subprocess. Uses **SSE transport** on `http://127.0.0.1:8765/sse` so any MCP client can connect. "Start MCP server on startup" checkbox in Settings > Tools. Server auto-stopped on app exit via `_handle_close()`. Methods: `_start_mcp_server()`, `_stop_mcp_server()`, `_toggle_mcp_server()`, `_update_mcp_ui()`, `_is_mcp_running()`.
- **MCP server SSE transport** — `server.py` refactored to support `--transport stdio|sse` and `--port` flags. `_create_mcp()` factory builds the FastMCP instance with all tools. STDIO remains default for CLI clients (`.mcp.json`); SSE used when app starts the server. Fixed latent bug: `description=` kwarg replaced with `instructions=` (correct FastMCP 1.26 param).

## Recent Completed Work (Feb 28, 2026) - MCP Server
- **MCP server for AI assistant access** — Read-only Model Context Protocol server (`src/mcp/`) with 5 tools: `get_rom_info`, `list_tables`, `read_table`, `compare_tables`, `get_table_statistics`. STDIO transport, LRU-cached ROM loading (4 entries), no Qt dependency. Works with Claude Code (`.mcp.json`), Claude Desktop, ChatGPT, Gemini. 34 unit tests in `tests/test_mcp_server.py`. Added `mcp>=1.0.0,<2.0.0` to requirements.txt.

## Recent Completed Work (Feb 28, 2026) - Flash ROM via RomDrop
- **Flash ROM to ECU via RomDrop** — Added "Flash ROM to ECU..." action (Ctrl+Shift+F) in Tools menu and toolbar (lightning-bolt icon). Shows safety warning dialog before flashing (engine off, battery healthy, don't interrupt). Auto-saves unsaved changes ("Save and Flash" vs "Flash"). Launches `subprocess.Popen([romdrop.exe, rom_file], cwd=romdrop_dir)` with resolved absolute path. RomDrop executable path configurable in Settings → Tools tab.
- **README disclaimer** — Added prominent vibe-coded / use-at-your-own-risk notice at the top of README.md.

## Recent Completed Work (Feb 28, 2026) - Windows Packaging
- **PyInstaller packaging support** — Added `src/utils/paths.py` with `get_app_root()` that resolves `sys._MEIPASS` when frozen or `Path(__file__)` tree when running from source. Replaced all 4 `Path(__file__).parent.parent.parent` references in `settings.py` with `get_app_root()`. Created `NCFlash.spec` (one-dir, windowed, bundles definitions/colormaps/examples, excludes tkinter/test/unittest), `build.bat` (activates venv, installs pyinstaller, runs build), and `requirements-build.txt` (pyinstaller>=6.0,<7.0).

## Recent Completed Work (Feb 28, 2026) - Unified Open Action
- **Unified "Open" action** — Replaced separate "Open Project..." (folder picker) and "Open ROM..." (file picker) menu items with a single "Open..." (Ctrl+O) that shows a file picker. If the selected ROM's parent directory is a project folder (`project.json` present), opens as project via `open_project_path()`; otherwise opens as standalone ROM. Toolbar button updated to match. Removed `open_project()` from `ProjectMixin`.
- **`--enable-projects` feature flag** — All project UI (New Project, Commit Changes, Commit History, project auto-detection in Open/Save/session restore/recent files) is hidden unless `--enable-projects` is passed on the command line. `run.bat` passes args through (`%*`); `run-dev.bat` launches with the flag enabled.

## Recent Completed Work (Feb 27, 2026) - Project Management UI Fixes
- **ROM comparison NaN filter** — `_compute_diffs()` now skips tables where both sides (or a one-sided table) have all-NaN values, preventing unpatched ROM tables from cluttering the comparison sidebar.
- **Session restore for projects** — Session save uses `document.project_path` to detect project tabs; stores `project:<path>` entries; restore calls `open_project_path()` to reopen with full project context (`[P]` prefix, history, etc.).
- **Project tab color swatch** — `open_project_path()` calls `_assign_rom_color()` and `_create_tab_color_button()` so project tabs get the same color swatch as standalone ROM tabs.
- **Flat project structure** — Projects no longer create `history/` subfolder. `commits.json` and all snapshots live at project root. Backward compat: `_load_commits()` and `get_snapshot_path()` fall back to `history/` paths for old projects.
- **v0/v1 project layout** — `create_project()` creates `v0_{romid}_original.bin` (pristine, never modified) and `v1_{romid}_working.bin` (editable copy). No more `original.bin` or `modified.bin`.
- **New project gets [P] prefix** — `new_project()` now calls `open_project_path()` instead of `_open_rom_file()`, so newly created projects get the `[P]` tab prefix, color swatch, and recent files entry.
- **Projects in recent files** — `open_project_path()` adds `project:<path>` to recent files. `RecentFilesMixin` displays them as `[P] folder_name` and opens via `open_project_path()`.
- **RomDocument.project_path** — New attribute tracks project association per-document (used by session save and recent files).
- **Fixed closeEvent MRO bug** — `SessionMixin.closeEvent` was shadowed by `QWidget.closeEvent` (C++ slot) in Python's MRO, meaning session data was *never* saved on app close. Renamed to `_handle_close()` with an explicit `MainWindow.closeEvent` override that delegates to it. Added MRO regression tests.
- **Legacy session/recent data handling** — `_restore_session()` and `open_recent_file()` detect ROM files inside project folders (parent has `project.json`) and open them as projects. Covers stale QSettings data from before project-aware code.

## Environment Notes
- Use `python` not `python3` (Windows environment)

## Recent Completed Work (Feb 24, 2026) - Compare Window Fixes
- **Cell border highlighting after compare copy** — `apply_compare_copy()` now updates `self.modified_cells[rom_path]` for both cell and axis changes, so `ModifiedCellDelegate` draws borders on copied cells. Also refreshes open table viewer windows via bulk `update_cell_value()` calls.
- **Copy buttons moved between panels** — Copy table buttons (→| and |←) moved from the toolbar to a narrow centered column between the two table panels, vertically centered. Fixed-width 32px column in the QSplitter, non-collapsible.

## Recent Completed Work (Feb 24, 2026) - Compare Window Enhancements
- **Panel labels show ROM filenames** — Replaced generic "Original"/"Modified" labels above compare panels with actual ROM filenames.
- **Copy table between ROMs** — Two toolbar buttons (→| and |←) to copy a table's values from one ROM to the other. Routes through `MainWindow.apply_compare_copy()` which uses the full edit pipeline: undo support, change tracker, modified indicator (*) on tab, pink table highlighting in browser, cell-level modification tracking. Confirmation dialog before copy. Disabled for one-sided tables and shape mismatches.
- **Main window toolbar** — Added 4 quick-access buttons: Open ROM, Save, Compare, Settings. Programmatic QPainter icons (high-DPI aware).
- **Tools menu** — Replaced single-item "Compare" menu with "Tools" menu.

## Recent Completed Work (Feb 23, 2026) - Cross-Definition ROM Comparison
- **Enabled cross-definition ROM comparison** — Removed the xmlid gate that blocked comparing ROMs with different definitions (e.g., NC1 vs NC2). `CompareWindow` now accepts two separate `RomDefinition` objects (one per ROM) and uses name-based table matching. Features: one-sided tables (A-only/B-only) shown with one panel populated and the other cleared, shape mismatches display each panel at native shape with all cells highlighted, sidebar labels include ◀/▶/≠ indicators, status bar shows context-appropriate messages. Each side uses its own definition for scaling, formatting, axis ranges, and flip flags — no cross-contamination. Window title shows both xmlids when definitions differ.

## Recent Completed Work (Feb 23, 2026) - ROM Comparison Tool
- **Added ROM comparison tool** — New `CompareWindow` (`src/ui/compare_window.py`) provides side-by-side table comparison between two open ROMs. Features: category tree sidebar listing modified tables with change counts, synchronized scrolling between original and modified panels, changed cells highlighted with gray border (matching `ModifiedCellDelegate` pattern), "Changed only" toggle that dims unchanged cells, keyboard navigation (Up/Down to switch tables, T to toggle, Esc to close). Window supports maximize. Compact "Original"/"Modified" labels above each table panel. Accessible via Compare > Compare Open ROMs (Ctrl+Shift+D). Supports 1D, 2D, and 3D table types with proper axis display, flip handling, and thermal gradient coloring. Spec at `docs/specs/ROM_COMPARISON_TOOL.md`, HTML mockups at `docs/mockups/`.

## Recent Completed Work (Feb 22, 2026) - Table Viewer Toolbar
- **Added action toolbar to table viewer window** — 12 quick-access buttons below the menu bar with programmatic QPainter icons (high-DPI aware). Grouped by function: File (clipboard, export CSV), Basic edits (increment, decrement), Value ops (add to data, multiply, set value), Interpolation (vertical, horizontal, 2D, smooth), View (graph toggle). Edit actions auto-disabled in diff mode. Graph toggle button syncs checked state with View menu. Toolbar height accounted for in auto-sizing.

## Recent Completed Work (Feb 10, 2026) - Table Viewer Auto-Size Fix
- **Fixed table viewer window not showing all rows for 3D tables** — `_auto_size_window()` rewritten to use `header.length()` API instead of manual row/column iteration. Added one-row-height safety padding to prevent the last row from being clipped behind the horizontal scrollbar (the scrollbar `sizeHint()` underreports actual size on themed/high-DPI systems). Also subtracts 40px from available geometry for OS window frame. Verified on 1D, 2D, 3D, and large 3D tables.

## Recent Completed Work (Feb 10, 2026) - Graph Performance Optimization
- **Removed constrained_layout from GraphWidget Figure** — `layout='constrained'` was broken for 3D axes (warning: "axes sizes collapsed to zero"), adding ~200ms overhead per draw for a failing constraint solver. Removed in favor of default layout. Canvas.draw() dropped from ~380ms to ~220ms.
- **Vectorized `_calculate_colors()` and `_calculate_colors_1d()`** — Replaced per-cell Python loop (O(rows*cols) function calls through 3 layers of indirection) with numpy array operations: batch ratio-to-index mapping + LUT lookup. Color computation dropped from ~6-30ms to ~1ms.
- **In-place facecolor update for selection changes** — Added `_update_3d_facecolors()` that calls `set_facecolors()` on the existing Poly3DCollection instead of removing and recreating the surface. Selection update pre-draw cost dropped from ~60-100ms to ~1-2ms.
- **Net result (29x25 table, 725 cells):** Initial graph open 764ms→419ms (**45% faster**), selection update 275-342ms→142-157ms (**55% faster**).

## Recent Completed Work (Feb 7, 2026) - Post-Remediation Re-Audit
- **Comprehensive re-audit of all 40 findings** — Systematically verified every fix by reading source files. All 40 fixes confirmed in place. No regressions introduced by the remediation work. Updated `docs/CODE_AUDIT_REPORT.md` with: all items moved to DONE table, updated scores (Maintainability 9/10, Reliability 9/10, Test Quality 7/10, Performance 8/10, Security 9/10), Post-Remediation Notes section assessing new code (mixins, context manager, vectorized scaling, deferred init), and updated priority list for future improvements.
- **Test suite results:** 240 passed, 1 skipped, 1 failed (platform-specific path normalization in `test_custom_definitions_dir` — Windows backslash vs forward slash, not a production bug).

## Recent Completed Work (Feb 7, 2026) - Audit Fixes #19, #20
- **Deferred heavy init work to `_deferred_init()` (#19)** — `MainWindow.__init__` was synchronously doing file I/O, modal dialogs, XML parsing, ROM detection, and session restore, blocking startup. Moved `check_definitions_directory()` + `show_setup_wizard()`, `RomDetector` initialization, `log_startup_message()`, and `_restore_session()` to `_deferred_init()` called via `QTimer.singleShot(0, ...)`. Set `self.rom_detector = None` initially; it's already handled as None by `_open_rom_file()`.
- **Extracted 3 mixin classes from MainWindow (#20)** — Reduced `main.py` from ~1,513 lines / 44 methods to ~1,104 lines / 33 methods. Created: `RecentFilesMixin` (3 methods: `update_recent_files_menu`, `open_recent_file`, `clear_recent_files`) in `src/ui/recent_files_mixin.py`; `ProjectMixin` (5 methods: `new_project`, `open_project`, `commit_changes`, `show_history`, `_on_view_table_diff`) in `src/ui/project_mixin.py`; `SessionMixin` (5 methods: `_restore_session`, `closeEvent`, `show_settings`, `on_settings_changed`, `show_about`) in `src/ui/session_mixin.py`. MainWindow now inherits from `QMainWindow, RecentFilesMixin, ProjectMixin, SessionMixin`.

## Recent Completed Work (Feb 7, 2026) - Audit Fix #30
- **Extracted header resize mode save/restore into `frozen_table_updates` context manager (#30)** — The pattern of saving per-section header resize modes, setting them to Fixed for bulk operations, and restoring them in a finally block was duplicated 8 times across `operations.py` (2), `clipboard.py` (1), `interpolation.py` (3), and `display.py` (2 in `begin/end_bulk_update`). Created `frozen_table_updates()` context manager and `save_header_resize_modes()`, `set_headers_fixed()`, `restore_header_resize_modes()` helper functions in `context.py`. Replaced all 6 inline try/finally patterns with `with frozen_table_updates(...)`, and refactored `begin_bulk_update`/`end_bulk_update` in `display.py` to use the shared helpers. Removed now-unused `QHeaderView` imports from `operations.py`, `clipboard.py`, and `interpolation.py`.

## Recent Completed Work (Feb 7, 2026) - Audit Fix #39
- **Moved function-level imports to module level (#39)** — Moved `import matplotlib.pyplot as plt` in `graph_viewer.py`, `from pathlib import Path` in `version_models.py`, `QPainter/QFontMetrics/QSize` in `table_viewer.py`, `ToggleSwitch` in `table_viewer.py`, `QSize` in `toggle_switch.py`, `from simpleeval import simple_eval` in `editing.py`, and `AddValueDialog/MultiplyDialog/SetValueDialog` in `operations.py` from inside function bodies to module-level imports. Left `from .settings import get_settings` lazy in `colormap.py` (test mock compatibility).

## Recent Completed Work (Feb 7, 2026) - Audit Fix #29
- **Eliminated redundant graph draw calls on cell/axis edits (#29)** — When editing a cell or axis, `_on_cell_changed`/`_on_bulk_changes`/`_on_axis_changed`/`_on_axis_bulk_changes` in `table_viewer_window.py` called `_refresh_graph()` directly AND the selection-change signal also fired a second debounced draw. Changed all four handlers to use `_schedule_graph_refresh()` (50ms debounce) instead of direct calls, and added `self._selection_timer.stop()` inside `_refresh_graph()` so that when the data-refresh fires, it cancels any pending selection-only timer. Result: one draw per user action instead of two.

## Recent Completed Work (Feb 7, 2026) - Audit Fix #18
- **Fixed `clear_all()` not freeing undo stacks from QUndoGroup (#18)** — `remove_stack()` was already fixed in a prior session (composite keys + `deleteLater()`), but `clear_all()` had the same leak: it called `stack.clear()` without `stack.deleteLater()`, leaving QUndoStack objects registered in the QUndoGroup. Added `self._undo_group.setActiveStack(None)` and `stack.deleteLater()` to match `remove_stack()` cleanup behavior.

## Recent Completed Work (Feb 7, 2026) - Audit Fix #32
- **Fixed test state leaking across tests (#32)** — `tests/test_colormap.py` mutated `ColorMap._builtin_gradient` and `colormap_module._current_colormap` without cleanup; `tests/test_settings.py` mutated `settings_module._settings` without cleanup. Added `@pytest.fixture(autouse=True)` fixtures in both files that save original values before each test and restore them in teardown, preventing order-dependent failures.

## Recent Completed Work (Feb 7, 2026) - Audit Fixes #26, #34
- **Fixed QAction memory leak in recent files menu (#26)** — `update_recent_files_menu()` in `main.py` called `removeAction()` but never deleted the old QAction objects, leaking them (and their lambda connections) on every menu rebuild. Added `action.deleteLater()` before clearing the list.
- **Fixed `sys.exit(1)` in MainWindow constructor bypassing Qt cleanup (#34)** — When the user cancels the setup wizard, `sys.exit(1)` was called directly inside `__init__`, bypassing Qt's cleanup sequence. Replaced with `QTimer.singleShot(0, lambda: sys.exit(1))` plus `return` to defer the exit to the event loop, allowing Qt to finish construction and clean up properly.
- **Audit finding #17 already fixed** — `modified_cells` and `original_table_values` are already cleaned in `close_tab()` (lines 461-463) via `.pop(rom_path, None)` calls added in an earlier fix.

## Recent Completed Work (Feb 7, 2026) - Audit Fixes #22, #27
- **Fixed `_display_3d` per-cell signal storm (#22)** — `_display_3d` in `display.py` calls `setItem()` cell-by-cell, each firing internal model signals causing the view to repaint per cell. Wrapped the entire cell-population block in `blockSignals(True)` / `setUpdatesEnabled(False)` with a single `viewport().update()` at the end for one batched repaint.
- **Added user warning when interpolation skips cells due to missing scaling (#27)** — All three interpolation methods (`interpolate_vertical`, `interpolate_horizontal`, `interpolate_2d`) in `interpolation.py` now check upfront whether the table's scaling is defined and resolvable. If not, a `QMessageBox.warning()` informs the user that interpolation was skipped and why. Previously, the operation silently did nothing.

## Recent Completed Work (Feb 7, 2026) - Audit Fix #16
- **Vectorized scaling expressions (#16)** — `ScalingConverter` in `rom_reader.py` now pre-compiles scaling expressions (via `ast` validation + `compile()`) into numpy-compatible code objects at init time. Array evaluation uses a single `eval()` call on the whole numpy array instead of per-element `simple_eval` loops. Falls back to per-element `simple_eval` for expressions that fail AST safety checks or vectorized eval. Added `_is_safe_numpy_expr()` (AST whitelist: only arithmetic ops + `x` variable) and `_compile_numpy_expr()` helpers.

## Recent Completed Work (Feb 7, 2026) - Audit Fix #31
- **Mitigated lxml XXE (XML External Entity) injection** — All `etree.parse()` calls across the codebase now use a secure parser with `resolve_entities=False` and `no_network=True`. Fixed in `definition_parser.py`, `rom_detector.py`, and `metadata_writer.py` (two call sites). Prevents crafted XML definition files from reading local files or making network requests via entity expansion.

## Recent Completed Work (Feb 7, 2026) - Audit Fix #23
- **Fixed O(N) `get_commit()` calls per keystroke in history filter** — `_filter_commits` in `history_viewer.py` was calling `self.project_manager.get_commit(commit_id)` for every tree item on every keystroke. The commit object was already stored in the item at `Qt.UserRole + 1` (set in `_add_commit_item`). Changed to `item.data(0, Qt.UserRole + 1)` to use the stored data directly, eliminating the redundant lookups.

## Recent Completed Work (Feb 7, 2026) - Audit Fixes #33, #35, #37
- **Cached O(n) table lookups (#33)** — `get_tables_by_category()` and `get_table_by_name()` in `rom_definition.py` now build lazy lookup dicts on first access and return cached results on subsequent calls. Cache fields use `field(init=False, repr=False, compare=False)` to stay invisible to dataclass construction and equality.
- **Full UUID for commit IDs (#35)** — `Commit.create()` in `version_models.py` now uses `uuid.uuid4().hex` (32 hex chars) instead of `str(uuid.uuid4())[:12]` (12 chars). The full hex string provides proper collision resistance.
- **Cross-platform monospace font (#37)** — Renamed `LOG_CONSOLE_FONT_FAMILY` to `LOG_CONSOLE_FONT_FAMILIES` in `constants.py`, changed from a single `"Courier New"` string to a tuple of fallbacks `("Consolas", "Courier New", "DejaVu Sans Mono", "Monospace")`. Updated `log_console.py` to use `QFont.setFamilies()` for proper cross-platform font resolution.

## Recent Completed Work (Feb 7, 2026) - Audit Fixes #36, #38
- **Fixed `setup_logging()` at import time (#36)** — Removed the module-level `setup_logging()` auto-call at the bottom of `logging_config.py`. The entry point in `main.py` already calls `setup_logging()` explicitly, so the import-time call was clobbering any pre-existing logging configuration.
- **Implemented backup file rotation (#38)** — `metadata_writer.py` now keeps the last 3 backups using `.bak.1`, `.bak.2`, `.bak.3` naming (`.bak.1` = most recent). Before creating a new backup, existing ones are rotated (delete `.bak.3`, rename `.bak.2`->`.bak.3`, `.bak.1`->`.bak.2`, current->`.bak.1`). Updated test to match new naming and added `test_backup_rotation` test.

## Recent Completed Work (Feb 7, 2026) - Audit Fix #28
- **Fixed `closeEvent` not checking for unsaved changes across tabs** — When the user closed the main window (X button), it bypassed the per-tab unsaved-change prompts and silently discarded all work. Modified `closeEvent` in `main.py` to iterate through all open tabs, check `is_modified()` on each, and prompt Save/Discard/Cancel. If the user cancels on any tab, the close is aborted via `event.ignore()`. Session save (`_save_session`) still runs if the user proceeds with closing.

## Recent Completed Work (Feb 7, 2026) - Audit Fix #21
- **Fixed `lstrip('0x')` bug in `table_browser.py`** — `lstrip('0x')` strips individual characters (`0` and `x`), not the literal prefix `"0x"`. For example, `"0x0080".lstrip('0x')` returns `"80"` instead of `"0080"`. Replaced both instances (lines 423 and 435 in `select_table_by_address`) with `removeprefix('0x')` which correctly removes only the exact prefix string.

## Recent Completed Work (Feb 7, 2026) - Audit Fix #10
- **Fixed exception handling swallowing programming bugs** — Refactored all 12 `except Exception` blocks in `main.py` to separate expected errors (`RomEditorError` hierarchy) from unexpected exceptions. Expected errors (e.g., `RomWriteError`, `RomFileError`, `DetectionError`) get clean user-facing messages via `logger.error()`. Unexpected exceptions now use `logger.exception()` for full traceback logging plus a generic "Unexpected error" user message that includes the exception type. Added `RomEditorError`, `RomWriteError`, and `ProjectError` to imports.

## Recent Completed Work (Feb 7, 2026) - Multi-ROM Undo Isolation Fix
- **Fixed undo stacks shared across ROMs with same definition** — When two ROMs share the same definition (same ECU type), they have identical table addresses. The undo stacks, change tracker, and table highlighting were all keyed by bare `table_address`, causing: (1) both ROMs' edits going into the same undo stack, (2) closing one ROM destroying the other's undo stacks and pending changes, (3) table highlighting showing modifications from the wrong ROM. Fix: introduced composite keys (`rom_path|table_address`) throughout the undo and change tracking systems. Files modified: `version_models.py` (added `table_key` field to CellChange/AxisChange), `table_undo_manager.py` (composite key helpers, rom_path params), `undo_commands.py` (propagate table_key through undo/redo), `change_tracker.py` (composite keys, per-ROM filtering), `main.py` (pass rom_path to all handlers, per-ROM highlight filtering), `table_viewer_window.py` (emit composite key on focus).

## Recent Completed Work (Feb 7, 2026) - Undo Wrong-ROM Fix
- **Fixed Path vs str type mismatch throughout `main.py`** — `RomReader.rom_path` is `Path`, `RomDocument.rom_path` is `str`; on Windows, forward vs backslash normalization caused `str()` comparison to fail silently. Fixed `_find_document_by_rom_path()` to use `Path()` comparison. Fixed `close_tab()` to use `rom_reader.rom_path` (Path) instead of `document.rom_path` (str) for window matching and dict cleanup.
- **Fixed test runner operations not emitting signals** — `set_value`, `multiply_selection`, `add_to_selection` called `_apply_bulk_operation` directly which doesn't emit `bulk_changes`/`axis_bulk_changes` signals. Now properly emits signals so changes are written to ROM.
- **Fixed undo/edit writing to wrong ROM** when multiple ROMs are open — all 6 `get_current_document()` call sites in edit/undo handlers now resolve the correct ROM via `_find_document_by_rom_path()` instead of using the active tab
- **Clean ROM state on tab close** — closing a ROM now: closes all its table windows, removes undo stacks, clears pending changes, and purges modified_cells/original_table_values for that ROM. Reopening a ROM starts fresh.
- **Debounced graph selection updates** — arrow key navigation no longer triggers full 3D re-render per key press (100ms debounce timer)
- **Eliminated double-draw** in graph widget — `canvas.draw_idle()` + deferred redraw only on first plot

## Recent Completed Work (Feb 7, 2026) - 3D Graph Zoom Fix
- **Fixed 3D graph zoom-out on cell edits and selection changes** — `_refresh_graph()` was calling `set_data()` on every cell change, which did `figure.clear()` → full replot → `constrained_layout` recalculation → visible zoom-out. Changed to `update_selection()` which routes through `_update_3d_surface()` — replaces the surface collection on the existing axes without clearing the figure. Also added `_update_3d_surface` fast path for `update_data` and `update_selection` in GraphWidget. Axis limits saved/restored to prevent auto-rescale.

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

## Recent Completed Work (Jan 18, 2026)
- Fixed blank space under table cells: set QTableWidget size policy to prevent vertical expansion beyond content
- Fixed Windows-only blank space issue: added post-resize correction for high-DPI displays (detects/removes viewport blank space)

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
