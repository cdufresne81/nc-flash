# Code Audit — March 2026

**Scope:** Full codebase review (47 source files, 16,453 lines in `src/` + `main.py` + `tools/`)
**Previous audit:** Feb 7-8 2026 (see `CODE_AUDIT_REPORT.md`) — 50 findings, all fixed
**This audit covers:** All code added since Feb 8 2026 (MCP, command API, project management refactor, compare window enhancements, flash integration, rebrand)

### Resolved Items (this session)
- **DC1-DC5, DC7** — Dead code removed (7 items: unused dataclasses, legacy classes, deprecated methods)
- **DC6** — False positive: `_ratio_to_rgba` IS used by `_ratio_to_color`, kept
- **D1, D2, D3** — Created `src/utils/formatting.py`, consolidated `printf_to_python_format`, `format_value`, `get_scaling_range`, `get_scaling_format` from 3-4 duplicate locations
- **D4** — Unified `interpolate_vertical`/`interpolate_horizontal` into shared `_interpolate_1d(direction)`, also extracted `_apply_axis_interpolation` and `_apply_data_interpolation` helpers. Fixed bug: horizontal emit was inside sel_range loop (emitted per-range), now matches vertical pattern (emit once after all ranges).
- **E1** — All 3 silent exception swallows in `main.py` now log with `logger.debug`
- **E4** — Exception chain added (`from e`) in `project_manager.py`
- **E2** — `_load_commits` now includes `exc_info=True` for stack trace in warning log
- **S1 (partial)** — Extracted `_make_icon` (143 lines) and `_make_toolbar_icon` (102 lines) into shared `src/ui/icons.py` (253 lines) with dispatch table. `main.py` dropped from 2,606 to 2,466 lines.
- **DOC1** — README project structure tree updated with all current files
- **DOC2** — README Python version corrected from 3.12+ to 3.10+
- **DOC3** — Removed stale "In Development: Projects management" and "Next Priorities" sections
- **DOC6** — Fixed `examples/README.md` `File > Open ROM >` → `File > Open >`
- **S3** — Test runner `_execute_command` refactored from 159-line if/elif to dispatch table + handler methods
- **M4** — Split `requirements.txt` into runtime-only + `requirements-dev.txt`; CI updated
- **DOC4** — Archived `MODIFICATION_TRACKING_PLAN.md` and `SUMMARY.md` to `docs/archive/`
- **DOC5** — Updated ROM comparison spec: marked implemented "Out of Scope" items, fixed architecture table
- **S1 (MCP + API)** — Extracted 13 methods (~500 lines) from `main.py` into `src/ui/mcp_mixin.py`: MCP server lifecycle (6), command API bridge (3), API handlers (4). `main.py` now 1,970 lines (was 2,606). Also fixed latent bug: API handlers imported renamed `_printf_to_python_format` from `rom_context.py` — now imports from `src.utils.formatting`.
- **S2** — `compare_window.py` cleanup: consolidated 3 color helpers (`_get_cell_color`, `_axis_gradient_color`, inline 3D ratio code) into shared `_gradient_color`; moved `_all_nan` and `_get_axis_format` to `formatting.py`; removed redundant `_get_axis_format` method. Down from 1,413 to 1,379 lines.
- **E2** — `_load_commits` warning now includes `exc_info=True` for stack trace
- **Bonus** — Fixed pre-existing test failure: `test_get_table_font_size_default` expected 9 but default was changed to 11

---

## Summary

| Category | Findings | Priority Items |
|----------|----------|---------------|
| Structural / God-file | 3 | `main.py` decomposition |
| Code duplication | 4 | Format utilities, interpolation |
| Dead code | 7 | Unused dataclasses, deprecated methods |
| Error handling | 5 | Silent swallows, hierarchy bypasses |
| Test coverage gaps | 25+ files | Entire `src/ui/` layer |
| Documentation drift | 6 | README tree, Python version, stale specs |
| Minor / Style | 4 | Import style, hardcoded ports |

---

## Priority 1 — Structural

### S1. `main.py` is a god-file (2,606 lines, 57 methods, 17 responsibilities)

Three mixins were already extracted (`SessionMixin`, `RecentFilesMixin`, `ProjectMixin`), proving the pattern works. Remaining extraction candidates:

| Candidate | Methods | Lines | Dependency on `self` |
|-----------|---------|-------|---------------------|
| **Icon factory** (`_make_icon`) | 1 | 143 | None — pure QPainter drawing |
| **MCP server management** | 6 | ~145 | Low — just `_mcp_process` + UI refs |
| **Command API handlers** | 7 | ~310 | Medium — needs ROM documents |
| **Compare logic** | 3 | ~320 | Medium — needs tab/doc access |
| **Flash ROM** (`_on_flash_rom`) | 1 | 104 | Low — self-contained workflow |

**Recommendation:** Extract `_make_icon` to `src/ui/icons.py` first (zero risk, zero coupling). Then MCP management as `McpMixin`. The API handlers and compare logic are higher-effort.

### S2. `compare_window.py` is the largest src/ file (1,462 lines)

Contains inline format helpers, cell delegates, and table rendering that duplicate `table_viewer_helpers/` patterns. Could benefit from sharing the same helpers.

### S3. `test_runner.py` has a 159-line if/elif command dispatcher

`_execute_command` should be a dispatch dictionary mapping command names to handler methods.

---

## Priority 2 — Code Duplication

### D1. `_printf_to_python_format` duplicated in 3 files
- `src/ui/table_viewer_helpers/display.py` (line 28)
- `src/ui/compare_window.py` (line 49)
- `src/mcp/rom_context.py` (line 31, with comment "Duplicated to avoid Qt import dependency")

**Fix:** Move to `src/utils/formatting.py` (no Qt dependency needed).

### D2. `_format_value` duplicated in 3 files
- `src/ui/table_viewer_helpers/display.py` (line 580)
- `src/ui/compare_window.py` (line 71)
- `src/mcp/rom_context.py` (line 56)

**Fix:** Same — consolidate in `src/utils/formatting.py`.

### D3. `_get_scaling_range` duplicated in 4 files
- `src/ui/table_viewer_helpers/display.py` (line 593)
- `src/ui/compare_window.py` (line 97)
- `src/ui/graph_viewer.py` (line 35)
- `src/mcp/rom_context.py` (implicit)

**Fix:** Move to `src/core/rom_definition.py` as a method on `Scaling` or a standalone utility.

### D4. `interpolate_vertical()` and `interpolate_horizontal()` are near-identical (~250 lines each)

The axis direction is the only difference. A single parametric function with an axis parameter would eliminate ~250 lines.

**Fix:** Extract a shared `_interpolate_axis(direction, ...)` and have the two public functions delegate to it.

---

## Priority 3 — Dead Code

| # | Location | What | Notes |
|---|----------|------|-------|
| DC1 | `src/core/version_models.py:231-261` | `UndoableChange`, `UndoableAxisChange`, `BulkChange`, `AxisBulkChange` | Unused dataclasses — leftover from before Qt QUndoCommand refactor |
| DC2 | `src/ui/table_viewer.py:527-543` | `_get_value_format()`, `_format_value()`, `_get_cell_color()`, `_ratio_to_color()` | Marked "legacy backwards compat" — verify no external callers, then remove |
| DC3 | `src/core/change_tracker.py:370` | `get_modified_tables()` | Marked deprecated — replace callers with `get_modified_addresses_for_rom()` |
| DC4 | `src/ui/scaling_edit_dialog.py:293-344` | `ScalingEditDialog` class | Marked legacy — `TableScalingDialog` is the replacement |
| DC5 | `src/ui/history_viewer.py:377-430` | `HistoryPanel` compact widget | Unused — only `HistoryViewer` dialog is imported |
| DC6 | `src/ui/graph_viewer.py:260` | `_ratio_to_rgba()` | Appears unused in any code path |
| DC7 | `src/ui/table_browser.py:383-399` | `update_modified_tables()` | Marked deprecated — callers should use `update_modified_tables_by_address()` |

---

## Priority 4 — Error Handling

### E1. Silent `except Exception: pass` in `main.py`
- Line 907-909: `_delete_workspace_state` — completely silent. Add `logger.debug`.
- Line 961-963: `_stop_mcp_server` `terminate()` — swallowed. Add `logger.debug`.
- Line 2341-2344: `_end_bulk_update` catches `RuntimeError` with `pass` — any `RuntimeError` swallowed, not just the Qt widget-deleted one.

### E2. `_load_commits` swallows errors (project_manager.py:673)
Returns empty list on `Exception`. **Risk:** Could mask data corruption in project history. Should at minimum log a warning.

### E3. Methods that bypass the exception hierarchy
- `metadata_writer.py`: Returns `bool`/empty dict on failure instead of raising
- `rom_reader.py:315`: `verify_rom_id` returns `False` instead of raising `RomIdMismatchError`
- `editing.py:149,331`: `display_to_raw` / `_axis_display_to_raw` return `None` on error instead of raising `ScalingConversionError`

### E4. `create_project` loses original traceback (project_manager.py:139)
`except Exception as e:` wraps into `ProjectError` but doesn't chain with `from e`.

### E5. `test_runner.py` swallows exceptions without stack traces
Every method wraps in `try/except Exception as e:` with only a one-line message. No `traceback.print_exc()` or `logging.exception()`.

---

## Priority 5 — Test Coverage Gaps

### Entirely untested `src/ui/` modules (25 files)
This is expected for a Qt GUI app — the `tools/test_runner.py` framework covers UI testing via scripts. However, extractable logic (formatting, math, data transforms) within UI files could be unit-tested.

### Other untested modules
- `src/mcp/server.py` — the FastMCP wrapper (tool registration/dispatch)
- `src/utils/logging_config.py`
- `src/utils/constants.py`
- `src/core/storage_types.py`

### Fragile tests
- `test_command_server.py` — timing-dependent with hardcoded port 18766
- `test_definition_parser.py:138` — `assert len(definition.tables) > 500` hardcodes ROM-specific count

### Stats
- 243 test functions across 19 test files
- Good `src/core/` coverage (rom_reader, definition_parser, change_tracker, project_manager, undo, detector, metadata_writer)
- 4 integration test files exercise real ROM data end-to-end

---

## Priority 6 — Documentation Drift

| # | Issue | Fix |
|---|-------|-----|
| DOC1 | README project structure tree is stale — missing many files in `src/ui/`, `src/core/`, `docs/` | Update tree |
| DOC2 | README says "Python 3.12+" but CI tests 3.10/3.11 and `WINDOWS_SETUP.md` says 3.10+ | Change to "Python 3.10+" |
| DOC3 | README "Next Priorities" says "Project management" but it shipped in v1.6.0 | Update roadmap |
| DOC4 | `MODIFICATION_TRACKING_PLAN.md` and `SUMMARY.md` describe abandoned SQLite design | Archive or delete |
| DOC5 | `ROM_COMPARISON_TOOL.md` says cross-definition compare and editing are "Out of Scope" but both were implemented | Update spec |
| DOC6 | `examples/README.md` says `File > Open ROM >` but UI now uses `File > Open...` | Fix text |

---

## Priority 7 — Minor / Style

| # | Issue | Location |
|---|-------|----------|
| M1 | Import style inconsistency: mixin files use absolute imports (`from src.utils...`), other UI files use relative (`from ..core...`) | `session_mixin.py`, `recent_files_mixin.py`, `project_mixin.py` |
| M2 | Logger creation inconsistency: mixins use `get_logger(__name__)`, other files use `logging.getLogger(__name__)` | Same files |
| M3 | Hardcoded ports: command_server.py PORT=8766, server.py DEFAULT_SSE_PORT=8765 | Could conflict with other apps |
| M4 | Dev dependencies (pytest, black, flake8) mixed with runtime deps in `requirements.txt` | Should separate into `requirements-dev.txt` |

---

## Architecture Assessment (Post-Audit)

### What's good
- **Clean layer separation**: `core/` has zero imports from `ui/`, `api/`, or `mcp/`. No circular imports.
- **Mixin decomposition pattern**: Already proven with 3 mixins extracted from MainWindow.
- **Custom exception hierarchy**: Well-designed, mostly used consistently.
- **No mutable default argument bugs**: All use `None` with guards.
- **No TODO/FIXME/HACK comments**: Clean codebase.
- **Numpy-vectorized ROM I/O**: Performance-conscious where it matters.
- **Comprehensive project management**: Atomic writes, snapshots, revert, soft-delete.
- **Security**: XXE prevention, safe eval with AST whitelist, builtins empty.

### Scores (vs Feb 2026 audit)
| Dimension | Feb 2026 | Mar 2026 | Trend |
|-----------|----------|----------|-------|
| Maintainability | 8/10 | 7/10 | Down (god-file growth, duplication) |
| Reliability | 9/10 | 9/10 | Stable |
| Performance | 9/10 | 9/10 | Stable |
| Test Quality | 8/10 | 7.5/10 | Slightly down (new code less tested) |
| Security | 9/10 | 9/10 | Stable |

---

## Recommended Work Order

1. **Quick wins (< 30 min each):**
   - Remove dead code (DC1-DC7)
   - Fix silent exception swallows (E1, E2, E4)
   - Extract `_make_icon` to `src/ui/icons.py` (S1 partial)
   - Fix README Python version (DOC2)

2. **Medium effort (1-2 hours each):**
   - Consolidate duplicated format utilities (D1, D2, D3) into `src/utils/formatting.py`
   - Unify interpolation functions (D4)
   - Update README project tree (DOC1)
   - Archive abandoned design docs (DOC4)

3. **Larger refactors (half-day each):**
   - Extract MCP management mixin from `main.py` (S1)
   - Extract API handlers from `main.py` (S1)
   - Refactor `compare_window.py` to use shared helpers (S2)
   - Convert test_runner dispatcher to dictionary (S3)
