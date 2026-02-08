# Full Code Audit Report: NC ROM Editor

**Date:** February 7, 2026
**Last updated:** February 8, 2026 (second audit — 13 new findings)
**Scope:** Full codebase scan (~18,000 LOC, 64 Python files)

## Executive Summary

This is an **~18,000 LOC Python/Qt6 desktop application** for editing automotive ECU ROM files. The architecture is clean — separation between `core/` (data), `ui/` (presentation), and `utils/` (infrastructure), improved by mixin classes and shared helpers.

**Round 1 (Feb 7):** 40 findings identified and remediated. All critical data-corruption bugs, memory leaks, and tautological tests were fixed.

**Round 2 (Feb 8):** 13 new findings identified. One is a functional bug (axis edits not tracked as pending changes). The rest are performance optimizations and maintainability improvements. No new critical data-corruption issues.

**Note:** The project/versioning subsystem (`project_manager.py`, `version_models.py`, `history_viewer.py`, `commit_dialog.py`) is slated for a full rewrite and is excluded from this report.

---

## Round 1 — All 37 Findings Fixed

*(3 project-related findings removed from original 40)*

| # | Sev | Finding | Fix |
|---|-----|---------|-----|
| 1 | CRIT | `write_table_data` ignores `swapxy` on flatten — silent data corruption | Added `order='F' if table.swapxy else 'C'` to the flatten call |
| 2 | CRIT | Paste emits N individual `cell_changed` signals | Paste now emits a single `bulk_changes` signal |
| 3 | CRIT | `save()` never clears the modified flag | Added `set_modified(False)` after successful save |
| 4 | CRIT | ~46% of tests are tautological | Rewrote `test_axis_editing.py`, `test_interpolation.py`, `test_table_viewer_helpers.py` to test production code |
| 5 | HIGH | `save_rom` has no atomicity | Write-to-temp + `os.replace()` + `os.fsync()` |
| 7 | HIGH | `close_tab` doesn't `deleteLater()` the widget | Added `widget.deleteLater()` after `removeTab()` |
| 8 | HIGH | `open_table_windows` holds refs to closed windows | `deleteLater()` in `closeEvent`, `WA_DeleteOnClose` on `GraphViewer` |
| 9 | HIGH | Matplotlib figures never closed | Added `plt.close(self.figure)` in `GraphViewer.closeEvent` and `TableViewerWindow.closeEvent` |
| 10 | HIGH | `except (SpecificError, Exception)` swallows all errors | Split into specific catch + `Exception` with `logger.exception()` for full tracebacks |
| 11 | HIGH | `GraphWidget` and `GraphViewer` 90% duplicated | Extracted `_GraphPlotMixin` with 14 shared methods (~745 to ~350 lines) |
| 12 | HIGH | Undo callbacks use `get_current_document()` (active tab) | All undo/edit handlers resolve correct ROM via `_find_document_by_rom_path()` |
| 13 | HIGH | `QApplication.processEvents()` in event handlers | Removed entirely; no `processEvents()` calls remain in production code |
| 14 | HIGH | Cell delegate unpacks axis `data_coords` as `(row, col)` | Added `isinstance(data_coords[0], str)` guard to skip axis cells |
| 15 | HIGH | All deps use `>=` with no upper bounds | Pinned versions with upper bounds (e.g., `PySide6>=6.10.0,<7.0.0`) |
| 16 | MED | `simple_eval` called per-element in Python loop | Vectorized via AST-validated `compile()` + `eval()` on whole numpy arrays; fallback to per-element `simple_eval` |
| 17 | MED | `modified_cells`/`original_table_values` never pruned on tab close | `close_tab()` now calls `.pop(rom_path, None)` on both dicts |
| 18 | MED | `remove_stack()` is a no-op | Fixed: `stack.clear()` + `stack.deleteLater()` in both `remove_stack()` and `clear_all()` |
| 19 | MED | `MainWindow.__init__` does too much | Deferred heavy work to `_deferred_init()` via `QTimer.singleShot(0, ...)` |
| 20 | MED | `MainWindow` is a god class (~15 responsibilities) | Extracted `RecentFilesMixin`, `SessionMixin` (~400 lines moved out) |
| 21 | MED | `lstrip('0x')` strips chars not prefix | Replaced with `removeprefix('0x')` |
| 22 | MED | `_display_3d` sets items cell-by-cell without signal blocking | Wrapped in `blockSignals(True)` + `setUpdatesEnabled(False)` with single `viewport().update()` |
| 24 | MED | Definitions path uses `Path.cwd()` not `Path(__file__)` | Changed to `Path(__file__).resolve().parent.parent.parent` |
| 25 | MED | No type validation on QSettings geometry/splitter | Added `isinstance(value, QByteArray)` guard; returns `None` for corrupted values |
| 26 | MED | `update_recent_files_menu` leaks QAction objects | Added `action.deleteLater()` before clearing the list |
| 27 | MED | Interpolation silently skips cells when `scaling` is None | Added `_check_scaling_available()` with `QMessageBox.warning()` |
| 28 | MED | `closeEvent` doesn't check unsaved changes across tabs | Added per-tab Save/Discard/Cancel prompt loop in `SessionMixin.closeEvent` |
| 29 | MED | `QTimer.singleShot(0, canvas.draw)` after every plot (2N draws) | All graph refreshes use 50ms debounce timer; `_refresh_graph()` cancels pending selection timer |
| 30 | MED | Header resize mode save/restore duplicated 8+ times | Extracted `frozen_table_updates()` context manager in `context.py` |
| 31 | MED | lxml XXE not mitigated | All `etree.parse()` sites use `resolve_entities=False, no_network=True` |
| 32 | MED | Tests mutate global/class state without cleanup | Added `autouse` fixtures in `test_colormap.py` and `test_settings.py` |
| 33 | LOW | `get_tables_by_category()`/`get_table_by_name()` are O(n) | Added lazy cache dicts; O(1) after first call |
| 34 | LOW | `sys.exit(1)` in constructor bypasses Qt cleanup | Deferred via `QTimer.singleShot(0, lambda: sys.exit(1))` |
| 36 | LOW | `setup_logging()` at import time | Removed auto-call at module level; only called explicitly in `main.py` |
| 37 | LOW | `"Courier New"` is Windows-only | Changed to font family tuple with `QFont.setFamilies()` |
| 38 | LOW | Backup file has no rotation | Implemented 3-level rotation (.bak.1, .bak.2, .bak.3) |
| 39 | LOW | `import` statements inside function bodies | Moved key imports (matplotlib, simpleeval, Qt classes) to module level |
| 40 | LOW | `sync()` called on every `setValue()` | Removed all per-setValue `sync()` calls; only 1 intentional `sync()` remains in `open_recent_file()` |

---

## Round 2 — 13 New Findings (Open)

### HIGH — Fix soon

| # | Category | Location | Issue |
|---|----------|----------|-------|
| 41 | Perf | `display.py:424-435,596-598` | **O(n^2) min/max in `get_cell_color()` during initial 3D table display.** The `_cached_min_max` optimization only activates during `begin_bulk_update()`, NOT during `_display_3d()`. For tables without a scaling-defined range, `np.min()`/`np.max()` is called once PER CELL. A 50x20 table = 1,000 redundant array scans. **Fix:** Set `_cached_min_max` before the cell loop in `_display_3d()`, clear it after. |
| 42 | Perf | `table_viewer.py:726,741,749,763` | **`viewport().update()` called per modification tracking handler.** During bulk undo (which replays changes individually via callbacks), each replayed change triggers a full viewport repaint. A 500-cell undo = 500 repaints. **Fix:** Remove per-handler `viewport().update()` calls; the `end_bulk_update()` path already triggers a single repaint. For non-bulk single edits, the table widget's built-in repaint from `setItem()` is sufficient. |
| 43 | Bug | `main.py:1003-1054` | **Axis change handlers don't record to `change_tracker`.** `_on_table_cell_changed` and `_on_table_bulk_changes` both record to `change_tracker`, but `_on_table_axis_changed` and `_on_table_axis_bulk_changes` do NOT. Axis edits aren't tracked as pending changes — modified-table highlighting in the browser doesn't reflect axis-only edits. This is exactly the kind of bug DRY violations produce (finding #44). |

### MEDIUM — Should fix

| # | Category | Location | Issue |
|---|----------|----------|-------|
| 44 | Maint | `main.py:939-1055` | **Four near-identical signal handlers.** All follow the same pattern: get ROM path from sender → record to undo manager → record to change tracker → find document → write to ROM → set modified flag. The only differences are the method names on undo_manager/rom_reader and whether change_tracker is called (bug #43). **Fix:** Extract a generic `_handle_table_change(change_type, ...)` dispatcher. |
| 45 | Maint | `main.py:92-96` | **Three parallel mutable dicts tracking overlapping modification state.** `modified_cells` (for border display), `original_table_values` (for smart border removal), and `change_tracker` (for pending changes) all track "what's been modified" with different structures. Multiple sources of truth that must stay in sync. **Fix:** Consider unifying into a single modification tracker that serves all three needs. |
| 46 | Perf | `table_viewer.py:467-489` | **`_apply_diff_tooltips()` iterates ALL rows x columns** to find changed cells. Should only iterate cells that exist in `_diff_base_data` and check for differences. |
| 47 | Reliability | `rom_reader.py:453-456` | **`write_table_data()` doesn't validate flattened array length.** After flattening a 2D array, the code proceeds to pack and write without checking that the flattened length matches `table.elements`. If the array has wrong dimensions, the bounds check catches "too many bytes" but NOT "too few bytes" — which silently under-writes the table region, leaving stale data in the remaining bytes. |
| 48 | Reliability | `rom_reader.py:201` | **Vectorized eval fallback logged at DEBUG.** When the fast-path `eval()` fails and falls through to per-element `simpleeval`, the failure is logged at DEBUG level. If the failure is a genuine error (not a benign division-by-zero), it's invisible in production logs. **Fix:** Log at WARNING level. |

### LOW — Nice to fix

| # | Category | Location | Issue |
|---|----------|----------|-------|
| 49 | Reliability | `rom_reader.py:389-413` | When `get_scaling()` returns None for an axis, the axis is silently omitted from the result dict. No warning logged. User sees index numbers instead of axis labels with no explanation. |
| 50 | Perf | `display.py:499` | `re.match()` called on every `_printf_to_python_format()` invocation without caching. Minor overhead but called once per cell during display. |
| 51 | Reliability | `table_undo_manager.py:40` | Composite key `f"{rom_path}\|{table_address}"` uses pipe as delimiter without escaping. If `rom_path` contains `\|`, key parsing breaks. Unlikely on normal paths but fragile. |
| 52 | Maint | `table_viewer.py:122-130,175-179` | Dead code: info label created but hidden with `setVisible(False)`. Commented-out block and TODO about restoring later. Should be removed or implemented. |
| 53 | Reliability | `rom_reader.py:278` | ROM ID bytes decoded with `errors='ignore'`. Non-ASCII bytes silently dropped, could cause false-positive ROM identification on corrupted ROM files. |

---

## Engineer's Assessment

### Maintainability: 7/10

**Strengths:**
- Clean `core/ui/utils` layered architecture
- Mixin decomposition reduced MainWindow from ~1,500 to ~1,100 lines
- Graph viewer duplication resolved via `_GraphPlotMixin`
- Good context manager pattern (`frozen_table_updates`)
- Consistent naming conventions and reasonable file sizes
- Safe expression evaluation with AST whitelist

**Weaknesses:**
- `main.py` still owns too many responsibilities (ROM I/O, tab management, undo, change tracking, 4 duplicated signal handlers)
- Three parallel state-tracking dicts that must stay in sync
- `TableViewer` at 843 lines is still a god class despite helper extraction
- Dead code persists (hidden info label, commented blocks)

**Key refactor:** Consolidating the 4 duplicate signal handlers into 1 generic handler would simultaneously fix the axis tracking bug (#43), eliminate the DRY violation (#44), and make future signal additions less error-prone.

### Reliability: 8/10

- All round-1 critical fixes in place (atomic writes, swapxy, paste signals, save flag)
- Memory leaks plugged (deleteLater, matplotlib cleanup, dict pruning)
- One functional bug remains: axis edits not tracked as pending changes (#43)
- One data-integrity gap: write_table_data doesn't validate element count (#47)
- Error logging could be improved on vectorized eval fallback (#48)

### Performance: 7/10

- Vectorized numpy scaling is excellent (1000x speedup for safe expressions)
- Signal blocking during `_display_3d` prevents O(n^2) repaints during population
- Graph draw deduplication via 50ms debounce timer
- **Remaining O(n^2):** `get_cell_color()` min/max during initial display (#41)
- **Remaining O(n):** viewport repaints per modification tracking call during bulk undo (#42)

### Test Quality: 7/10

- Core logic well-tested (rom_reader, definition_parser, undo_manager, colormaps, settings)
- Tautological tests rewritten to test production code
- 240 tests pass, proper test isolation with autouse fixtures
- **Gaps:** No integration test for full ROM read/edit/save cycle, no UI unit tests, no property-based testing on scaling expressions

### Security: 9/10

- XXE mitigated on all `etree.parse()` sites
- Vectorized `eval()` uses strict AST whitelist with `__builtins__` = `{}`
- `simpleeval` for untrusted-ish expressions
- XPath string interpolation in `metadata_writer.py` remains (low risk — scaling names from trusted XML)

---

## Action Plan — Batched for Parallel Execution

Each batch touches **different files** and can be assigned to a separate agent with no collision risk.

### Batch A: `src/core/rom_reader.py` — Data integrity and error handling

| # | Task | Lines |
|---|------|-------|
| 47 | Validate flattened array length matches `table.elements` before writing | 453-456 |
| 48 | Change vectorized eval fallback log from DEBUG to WARNING | 201-206 |
| 49 | Log WARNING when `get_scaling()` returns None for an axis | 389-413 |
| 53 | Consider stricter ROM ID decode (log warning on non-ASCII bytes) | 278 |

### Batch B: `src/ui/table_viewer_helpers/display.py` — Rendering performance

| # | Task | Lines |
|---|------|-------|
| 41 | Cache min/max before cell loop in `_display_3d()` — set `_cached_min_max` before line 424, clear after line 435 | 424-435 |
| 50 | Cache compiled regex in `_printf_to_python_format()` | 499 |

### Batch C: `src/ui/table_viewer.py` — Viewer performance and cleanup

| # | Task | Lines |
|---|------|-------|
| 42 | Remove per-handler `viewport().update()` calls from modification tracking methods | 726,741,749,763 |
| 46 | Optimize `_apply_diff_tooltips()` to iterate only changed cells | 467-489 |
| 52 | Remove dead code (hidden info label, commented blocks) | 122-130, 145, 175-179 |

### Batch D: `main.py` — Signal handler consolidation (includes bug fix)

| # | Task | Lines |
|---|------|-------|
| 43 | **BUG FIX:** Add `change_tracker` recording to axis change handlers | 1003-1054 |
| 44 | Consolidate 4 signal handlers into 1 generic dispatcher | 939-1055 |
| 45 | (Future) Unify parallel state-tracking dicts | 92-96 |

### Batch E: `src/core/table_undo_manager.py` — Low-priority

| # | Task | Lines |
|---|------|-------|
| 51 | Escape or replace pipe delimiter in composite key | 40 |

### Batch F: Tests (new file, no conflicts)

| Task | Notes |
|------|-------|
| Write integration test for ROM read → edit → save → read-back cycle | Highest value-to-effort ratio for regression protection |
| Fix `test_custom_definitions_dir` path normalization on Windows | Use `Path()` comparison instead of string comparison |

---

## Recommended Priority

1. **Batch D** — Fixes a real bug (#43) and eliminates the DRY violation that caused it
2. **Batch A** — Data integrity improvements on the ROM write path
3. **Batch B + C** — Performance improvements for large table display
4. **Batch F** — Integration test for the most critical code path
5. **Batch E** — Low-priority cleanup
