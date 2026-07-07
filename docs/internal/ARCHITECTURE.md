# Architecture

The one-page contract for how NC Flash is wired. The **rules** are the
enforceable summary in `CLAUDE.md` (`## Architecture Rules`); this file is the
*why* — each rule points at the real incident that motivated it so future
sessions extend the system instead of re-growing the debt.

The incident IDs below (C1, D1, F1, …) come from the 2026-07-06 senior-architect
audit. The full findings and remediation plan live in the local (untracked)
`.claude/plans/` working docs; the durable record of what was actually changed
is `CHANGELOG.md` [Unreleased] and this file's per-rule summaries.

## Layer map

```
utils  ←  core  ←  { ecu, ui, api, mcp }        main.py composes ui
```

- **utils** — leaf helpers (formatting, colormap, paths). Depend on nothing internal.
- **core** — ROM model, definitions, editing, change tracking, exceptions. Depends only on utils.
- **ecu** — transport/UDS/flash. Brick-critical. Depends on core (+ utils), never on ui.
- **ui** — PySide6 windows/widgets. The top consumer; may use everything below.
- **api / mcp** — external control surfaces (local HTTP command server, MCP subprocess). Drive core; must not reach into ui.
- **main.py** — composition root; wires ui together. Allowed to import anything.

`tests/test_architecture.py` parses every module (absolute *and* relative
imports, including lazy ones inside functions) and fails on a back-edge. It is a
ratchet: the layering is clean today (audit §A) — keep it that way.

## The rules and why they exist

**Layering — no back-imports** (test-enforced).
core/ecu/utils/api/mcp never import `src.ui`; core/utils never import `src.ecu`.
The layering below `ui` is clean today; the test exists so a convenient
`from ..ui import MainWindow` (usually for a type hint or a shortcut) can't
quietly invert it. If ecu/core needs something from ui, the dependency is
backwards — pass a callback/signal down, or move the shared piece to core.

**State has one owner** ← **C1**.
Per-ROM edit state (`modified_cells`, `original_table_values`, colors) lived on
`MainWindow` and was handed *by reference* into `TableViewer`, so two objects
mutated the same dict and "which ROM owns this edit" became ambiguous — the
root cause of most UI debt. Owner exposes methods + Qt signals; never share a
mutable dict/list across an object boundary for mutation. (Phase 3 moved this
state onto `RomDocument.edit_state` — `src/core/table_edit_state.py`.)

**No new mixins** ← **C4**.
`MainWindow` is a 5-mixin god object (65 methods) with implicit cross-mixin
attribute contracts fragile enough to need an MRO workaround in `closeEvent`.
New window-scale behavior goes in an *owned collaborator object*
(e.g. `McpServerController`), not another mixin.

**One pipeline copy** ← **D1**, **D3**.
`build_flash_package` duplicated `FlashManager`'s flash-image prep step-for-step
("Replicates … exactly") — on the one path where drift **bricks an ECU** — with
no equivalence test. Compare-window re-implements display.py's render+color
pipeline. Table rendering, flash-image prep, and the HTTP/socket read loops each
belong in exactly one module. Extend the single copy; if two callers need it,
extract a pure function they share.

**Signals carry their own context** ← **C2**.
`_get_sender_rom_context()` recovered a ROM path by walking `sender().parent()`
with a silent fallback to the active tab — a broken walk mis-attributed edits to
whichever ROM happened to be focused. Bind context (rom_path, …) into the signal
or at connect time. Never rediscover it by walking the widget tree. Keep signal
chains ≤ 2 hops.

**Styling comes from the theme module** ← **F1**.
~88 hardcoded hex literals across 13 files, no app-level theme, safe only
because the app force-pins Light. Colors/fonts/QSS live in `src/ui/theme.py`
(constants + QSS builders, e.g. `get_toolbar_stylesheet()`); widgets call it.
Not a framework, not dark mode — one source of truth. Legacy literals are held
by the shrink-only ratchet `tests/test_theme_ratchet.py` (per-file budgets that
may only go down); new colors get a named constant in `theme.py`.

**`src/ecu/` is brick-critical** ← audit §G, project rule #6.
The flash/transport path can leave a real MX-5 ECU unbootable. Behavior-
preserving refactors only; anything that changes wire behavior needs the bench
checklist in `docs/internal/WICAN_MANUAL_TEST.md` (device 192.168.1.169,
remember the `--auto-config` protocol-revert gotcha) before it lands.

## When a rule is genuinely in the way

Rules encode incidents, not dogma. If you have a case a rule doesn't fit, say so
in the change and in `.claude/notes.md` rather than quietly routing around it —
and if it's the layering rule, the test will make the decision explicit.
